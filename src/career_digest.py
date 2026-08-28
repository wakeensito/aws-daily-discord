"""Daily career digest for #career.

EventBridge Scheduler (9 AM America/New_York) -> this Lambda -> SimplifyJobs
community listings -> Discord webhook. No AI involved: the digest is real
internship and new-grad postings, deduplicated so every morning shows only
what's NEW since the last run.

Design rules (docs/plan 2026-08-27):
  - Open feed, no company filtering — but AWS-flavored listings get a star
    and sort first (club identity as a highlight, never a gate).
  - Undergrad-friendly: listings whose `degrees` requires an advanced degree
    (no "Bachelor's") are dropped — that's Simplify's per-listing flag.
  - ONE message, as many rows as fit (~9-11) from the best ~18 candidates;
    plain header, no counts, no outbound links, single role ping.
  - Everything unseen gets marked seen each run, posted or not: tomorrow is
    strictly new drops.
  - Zero unseen -> no post at all (silence beats "nothing today" noise).

DRY_RUN=1 prints the digest and skips both the webhook and the seen-table
writes (local preview: `python local_run.py --career`).
"""

import json
import os
import time
import urllib.request

import boto3
from botocore.exceptions import ClientError

from discord_client import USER_AGENT, post_to_discord

dynamodb = boto3.resource("dynamodb")

SEEN_TABLE_NAME = os.environ.get("SEEN_TABLE_NAME", "")
CAREER_WEBHOOK_URL = os.environ.get("CAREER_WEBHOOK_URL", "")
CAREER_ROLE_ID = os.environ.get("CAREER_ROLE_ID", "")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

# Season-specific repo: bump the internships URL when Simplify opens the
# next season's repo (New-Grad-Positions is evergreen).
SOURCES = [
    (
        "Internships",
        (
            "https://raw.githubusercontent.com/SimplifyJobs/"
            "Summer2026-Internships/dev/.github/scripts/listings.json"
        ),
        "https://github.com/SimplifyJobs/Summer2026-Internships",
    ),
    (
        "New Grad",
        (
            "https://raw.githubusercontent.com/SimplifyJobs/"
            "New-Grad-Positions/dev/.github/scripts/listings.json"
        ),
        "https://github.com/SimplifyJobs/New-Grad-Positions",
    ),
]

WINDOW_DAYS = 14  # only consider recent postings; older unseen ones age out
# One message, always (user call: a couple of rows short beats a second
# message). The caps below bound SELECTION — ranking picks the best ~18
# candidates (AWS-starred first, then newest) and the chunker fits what a
# single message holds (typically 9-11 rows; apply URLs are 100-180 chars
# each against Discord's 2000-char cap). The tail drops silently and is
# marked seen, so nothing repeats.
SECTION_CAPS = {"Internships": 12, "New Grad": 6}
TOTAL_CAP = 18
MESSAGE_CAP = 1900  # Discord rejects at 2000
MAX_MESSAGES = 1
SEEN_TTL_DAYS = 180  # a season's listing is long dead by then


def fetch_listings(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR",
}
US_SHORTHANDS = {"nyc", "sf", "bay area", "remote", "remote in usa", "usa"}


def is_us_location(loc):
    """Best-effort US detector for Simplify's freeform location strings
    ('Seattle, WA', 'Vancouver, BC, Canada', 'Remote in USA'). Bare
    'Remote' counts as US — the data uses 'Remote in <country>' when it
    means elsewhere."""
    text = str(loc).strip()
    lowered = text.lower()
    if "usa" in lowered or "united states" in lowered:
        return True
    if lowered in US_SHORTHANDS:
        return True
    tail = text.rsplit(",", 1)[-1].strip().upper()
    return tail in US_STATES


def is_eligible(listing, now):
    """Live, visible, undergrad-friendly, US-based, recent enough."""
    if not listing.get("active") or not listing.get("is_visible"):
        return False
    degrees = listing.get("degrees") or []
    if degrees and "Bachelor's" not in degrees:
        return False  # Simplify's advanced-degree flag
    locations = listing.get("locations") or []
    # USA-only (user call): drop only when every location is identifiably
    # non-US; no locations at all = unknown, keep.
    if locations and not any(is_us_location(x) for x in locations):
        return False
    return listing.get("date_posted", 0) > now - WINDOW_DAYS * 86400


def is_aws(listing):
    name = str(listing.get("company_name", "")).lower()
    return name.startswith(("amazon", "aws"))


def rank_key(listing):
    """AWS-flavored first, then newest."""
    return (0 if is_aws(listing) else 1, -listing.get("date_posted", 0))


def plan_digest(unseen_by_section):
    """Pure selection logic (unit-tested): pick what to show per section,
    respecting per-section caps but redistributing spare capacity up to the
    total cap. Returns (chosen_by_section, ids_to_mark, more_count) — note
    ids_to_mark is EVERY unseen id, shown or not."""
    pools = {
        section: sorted(unseen_by_section.get(section, []), key=rank_key)
        for section, _, _ in SOURCES
    }
    chosen = {s: pool[: SECTION_CAPS[s]] for s, pool in pools.items()}
    # Second pass: hand any unused capacity (either section running short)
    # to whichever section still has supply, up to the total cap.
    remaining = TOTAL_CAP - sum(len(v) for v in chosen.values())
    for section, pool in pools.items():
        if remaining <= 0:
            break
        extra = pool[len(chosen[section]) : len(chosen[section]) + remaining]
        chosen[section] += extra
        remaining -= len(extra)
    all_unseen = [x for pool in pools.values() for x in pool]
    shown = sum(len(v) for v in chosen.values())
    return chosen, [x["id"] for x in all_unseen], len(all_unseen) - shown


def format_line(listing):
    star = "⭐ " if is_aws(listing) else "• "
    company = str(listing.get("company_name", "?")).strip()[:40]
    title = str(listing.get("title", "?")).strip()[:70]
    locations = listing.get("locations") or []
    where = str(locations[0]).strip()[:30] if locations else "—"
    url = listing.get("url", "")
    return f"{star}**{company}** — {title} — {where} — [apply]({url})"


def build_messages(chosen):
    """Render the digest as 1..MAX_MESSAGES Discord messages, each under
    MESSAGE_CAP. Plain header on the first chunk only (no counts, no
    outbound links — user calls); sections flow across chunks; the role
    ping rides only the LAST chunk (single notification). Rows that don't
    fit within MAX_MESSAGES are silently dropped — they're marked seen
    either way, so nothing repeats tomorrow."""
    lines = ["💼 **Daily Career Drops**", ""]
    for section, _, _repo in SOURCES:
        picks = chosen.get(section, [])
        if not picks:
            continue
        lines.append(f"**{section}**")
        lines += [format_line(x) for x in picks]
        lines.append("")

    messages, current = [], []
    for line in lines:
        if current and len("\n".join([*current, line])) > MESSAGE_CAP:
            messages.append("\n".join(current).rstrip())
            current = [] if len(messages) >= MAX_MESSAGES else [line]
            if not current:
                break
        else:
            current.append(line)
    if current and len(messages) < MAX_MESSAGES:
        messages.append("\n".join(current).rstrip())

    if CAREER_ROLE_ID and messages:
        # MESSAGE_CAP leaves 100 chars of headroom under Discord's real
        # 2000 limit; the ping is ~27, so appending is always safe.
        messages[-1] += f"\n\n<@&{CAREER_ROLE_ID}>"
    return messages


def filter_unseen(ids):
    """Which of these listing ids are NOT in the seen table. Chunked
    batch-get with an UnprocessedKeys retry loop; on table failure, treat
    everything as seen — never risk re-spamming the channel."""
    if not ids:
        return set()
    seen = set()
    try:
        client = dynamodb.meta.client
        for i in range(0, len(ids), 100):
            keys = [{"id": x} for x in ids[i : i + 100]]
            while keys:
                resp = client.batch_get_item(
                    RequestItems={
                        SEEN_TABLE_NAME: {
                            "Keys": keys,
                            "ProjectionExpression": "id",
                        }
                    }
                )
                seen.update(
                    item["id"] for item in resp["Responses"].get(SEEN_TABLE_NAME, [])
                )
                keys = resp.get("UnprocessedKeys", {}).get(SEEN_TABLE_NAME, {}).get(
                    "Keys", []
                )
    except ClientError as e:
        print(f"Seen-table read failed — treating all as seen: {e}")
        return set()
    return set(ids) - seen


def mark_seen(ids):
    now = int(time.time())
    try:
        table = dynamodb.Table(SEEN_TABLE_NAME)
        with table.batch_writer() as batch:
            for listing_id in ids:
                batch.put_item(
                    Item={
                        "id": listing_id,
                        "seen_at": now,
                        "ttl": now + SEEN_TTL_DAYS * 86400,
                    }
                )
    except ClientError as e:
        # Post already went out; worst case some listings repeat tomorrow.
        print(f"Seen-table write failed (continuing): {e}")


def lambda_handler(event, context):
    if not CAREER_WEBHOOK_URL and not DRY_RUN:
        # Deploys stay green before the #career webhook parameter exists.
        print("CAREER_WEBHOOK_URL unset — career digest inert, skipping.")
        return {"statusCode": 200, "skipped": "no webhook configured"}

    now = time.time()
    unseen_by_section = {}
    fetched_any = False
    for section, url, _ in SOURCES:
        try:
            listings = fetch_listings(url)
            fetched_any = True
        except Exception as e:  # noqa: BLE001 — one dead source must not kill the other
            print(f"Fetch failed for {section} (skipping section): {e}")
            continue
        candidates = [x for x in listings if is_eligible(x, now)]
        ids = [x["id"] for x in candidates]
        unseen_ids = ids if DRY_RUN else filter_unseen(ids)
        unseen_by_section[section] = [x for x in candidates if x["id"] in unseen_ids]
        print(f"{section}: {len(candidates)} eligible, {len(unseen_ids)} unseen")
    if not fetched_any:
        raise RuntimeError("Every listings source failed")

    chosen, ids_to_mark, _more = plan_digest(unseen_by_section)
    if not ids_to_mark:
        print("No new listings — skipping today's digest.")
        return {"statusCode": 200, "posted": 0}

    messages = build_messages(chosen)
    for i, message in enumerate(messages, 1):
        print(f"Digest chunk {i}/{len(messages)} ({len(message)} chars):\n{message}")

    if DRY_RUN:
        print("DRY_RUN=1 — not posting, not marking seen.")
        return {"statusCode": 200, "dryRun": True}

    for i, message in enumerate(messages, 1):
        # The ping lives inside the last chunk's content; passing the role
        # id allows the mention to actually notify on that chunk only.
        role = CAREER_ROLE_ID if i == len(messages) else ""
        post_to_discord(CAREER_WEBHOOK_URL, message, role, suppress_embeds=True)
    mark_seen(ids_to_mark)
    shown = sum(len(v) for v in chosen.values())
    return {
        "statusCode": 200,
        "posted": shown,
        "messages": len(messages),
        "marked": len(ids_to_mark),
    }
