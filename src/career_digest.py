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
  - One message, ~12 listings across two sections, footer counts the rest.
  - Everything unseen gets marked seen each run, posted or not: tomorrow is
    strictly new drops; the tail lives behind the footer links.
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
SECTION_CAPS = {"Internships": 8, "New Grad": 4}
TOTAL_CAP = 12
MESSAGE_CAP = 1900  # Discord rejects at 2000
SEEN_TTL_DAYS = 180  # a season's listing is long dead by then


def fetch_listings(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def is_eligible(listing, now):
    """Live, visible, undergrad-friendly, and recent enough to matter."""
    if not listing.get("active") or not listing.get("is_visible"):
        return False
    degrees = listing.get("degrees") or []
    if degrees and "Bachelor's" not in degrees:
        return False  # Simplify's advanced-degree flag
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


def build_digest(chosen, more_count):
    total_shown = sum(len(v) for v in chosen.values())
    lines = [f"💼 **Daily Career Drops** — {total_shown + more_count} new today", ""]
    for section, _, repo_url in SOURCES:
        picks = chosen.get(section, [])
        if not picks:
            continue
        lines.append(f"**{section}**")
        lines += [format_line(x) for x in picks]
        lines.append("")
    footer_links = " · ".join(f"<{repo}>" for _, _, repo in SOURCES)
    if more_count > 0:
        lines.append(f"…and {more_count} more today → {footer_links}")
    else:
        lines.append(f"Full lists: {footer_links}")
    if CAREER_ROLE_ID:
        lines += ["", f"<@&{CAREER_ROLE_ID}>"]
    return "\n".join(lines)


def shrink_to_cap(chosen, more_count):
    """Drop lines from the largest section until the message fits."""
    message = build_digest(chosen, more_count)
    while len(message) > MESSAGE_CAP:
        largest = max(chosen, key=lambda s: len(chosen[s]))
        if not chosen[largest]:
            break
        chosen[largest] = chosen[largest][:-1]
        more_count += 1
        message = build_digest(chosen, more_count)
    return message


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

    chosen, ids_to_mark, more_count = plan_digest(unseen_by_section)
    if not ids_to_mark:
        print("No new listings — skipping today's digest.")
        return {"statusCode": 200, "posted": 0}

    message = shrink_to_cap(chosen, more_count)
    print(f"Digest ({len(message)} chars):\n{message}")

    if DRY_RUN:
        print("DRY_RUN=1 — not posting, not marking seen.")
        return {"statusCode": 200, "dryRun": True}

    post_to_discord(CAREER_WEBHOOK_URL, message, CAREER_ROLE_ID)
    mark_seen(ids_to_mark)
    shown = sum(len(v) for v in chosen.values())
    return {"statusCode": 200, "posted": shown, "marked": len(ids_to_mark)}
