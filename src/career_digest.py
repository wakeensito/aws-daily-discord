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
  - ONE message per section (Internships, New Grad) so a full block of one
    can never starve the other; plain header on the first, no counts, no
    outbound links, single role ping on the last.
  - A run consumes only what it POSTS plus what the relevance gate rejected.
    Relevant listings that didn't fit stay unseen as BACKLOG — new listings
    outrank them (rank_key is newest-first), so they only surface on a quiet
    day. This replaced mark-everything, which burned ~1,150 listings daily.
  - Nothing to show -> no post at all (silence beats "nothing today" noise).

DRY_RUN=1 prints the digest and skips both the webhook and the seen-table
writes (local preview: `python local_run.py --career`).
"""

import collections
import json
import os
import pathlib
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
# One message per section (2026-08-28): the earlier single-message rule let
# Internships — rendered first — consume the whole budget and silently drop
# every New Grad row. Caps bound SELECTION evenly (AWS-starred first, then
# newest); each section then renders into its own message and fits what it
# holds (~8 rows; apply URLs run 100-180 chars against Discord's 2000-char
# cap). The tail drops silently and is marked seen, so nothing repeats.
SECTION_CAPS = {"Internships": 8, "New Grad": 8}
TOTAL_CAP = 16
# One row per company per section (2026-08-28): Disney once took 4 of the
# slots with near-identical Spring 2027 postings, and E2 Optics lists the
# same technician req in seven cities. With ~600 eligible listings a day
# there is always another employer, so breadth beats depth. Suppressed rows
# are still MARKED SEEN — capping selection must never shrink the mark set,
# or those listings return on every run forever.
COMPANY_CAP = 1
MESSAGE_CAP = 1900  # Discord rejects at 2000
MAX_MESSAGES = 2
SEEN_TTL_DAYS = 180  # a season's listing is long dead by then

# --- Relevance gate (2026-08-28) -------------------------------------------
# The feed carries NO job description; `title` is the only field describing
# the work, and a regex over it was rejected ("infrastructure" matched Data
# Center Technicians, "security" matched alarm installers). Nova Micro reads
# the title instead and answers one of LABELS. Only NONE-vs-not changes what
# is posted; the positive labels exist so the logs show what the feed held.
LABELS = frozenset({"CYBER", "IT", "CS", "AI", "PM", "SOLNS", "NONE"})
BATCH_SIZE = 40  # 40 titles measured at ~690 in / ~190 out tokens, ~1s
CLASSIFY_LIMIT = 80  # per section; 8 slots survive company-capping and NONE
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.amazon.nova-micro-v1:0")
PROMPT_PATH = pathlib.Path(__file__).with_name("prompts") / "classify_titles.txt"


class ClassificationError(Exception):
    """Raised for any classification failure. Callers must abandon the run:
    posting a partially-classified digest would show exactly the noise the
    gate exists to remove, and marking it seen would consume those listings
    forever."""


def _load_prompt():
    return PROMPT_PATH.read_text(encoding="utf-8")


def _converse(text):
    """The only Bedrock touchpoint — tests replace this, so the offline suite
    needs no network, no credentials, and no mocking library."""
    client = boto3.client("bedrock-runtime")
    response = client.converse(
        modelId=BEDROCK_MODEL_ID,
        system=[{"text": _load_prompt()}],
        messages=[{"role": "user", "content": [{"text": text}]}],
        inferenceConfig={"maxTokens": 1200, "temperature": 0},
    )
    return response["output"]["message"]["content"][0]["text"]


def _parse_labels(reply, expected):
    """Pull `N:LABEL` lines out of a reply, tolerating prose and blank lines
    that small models pad with. Every position must be present and valid —
    a partial answer is a failure, never a silent gap."""
    found = {}
    for line in reply.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        number, _, label = line.partition(":")
        number, label = number.strip().rstrip("."), label.strip().upper()
        if not number.isdigit():
            continue
        if label not in LABELS:
            raise ClassificationError(f"unknown label {label!r} for item {number}")
        found[int(number)] = label
    missing = [i for i in range(1, expected + 1) if i not in found]
    if missing:
        raise ClassificationError(f"missing labels for items {missing}")
    return [found[i] for i in range(1, expected + 1)]


def classify_titles(listings):
    """One label per listing, index-aligned. Raises ClassificationError on any
    Bedrock error or malformed reply."""
    labels = []
    for start in range(0, len(listings), BATCH_SIZE):
        batch = listings[start : start + BATCH_SIZE]
        text = "\n".join(
            f"{i}. {x.get('company_name', '?')} | {x.get('title', '?')}"
            for i, x in enumerate(batch, 1)
        )
        try:
            reply = _converse(text)
        except ClassificationError:
            raise
        except Exception as e:  # every failure is abandon-the-run
            raise ClassificationError(f"Bedrock call failed: {e}") from e
        labels += _parse_labels(reply, len(batch))
    return labels


def filter_relevant(candidates):
    """Classify the top CLASSIFY_LIMIT candidates and drop the NONE ones.

    Returns (kept, rejected_ids). rejected_ids holds ONLY the NONE-labelled
    listings — they are permanently unwanted, and marking them stops us paying
    to re-classify the same Panel Technician every run.

    Everything else stays unmarked on purpose. A relevant listing that merely
    did not fit today's 8 slots is BACKLOG, not waste: it stays unseen and can
    fill a slot on a quieter day. Marking it here is what used to burn ~1,150
    listings a day."""
    if not candidates:
        return [], []
    head = candidates[:CLASSIFY_LIMIT]
    labels = classify_titles(head)
    counts = collections.Counter(labels)
    print(f"  classified {len(head)}: {dict(counts)}")
    kept, rejected = [], []
    for listing, label in zip(head, labels, strict=True):
        (rejected.append(listing["id"]) if label == "NONE" else kept.append(listing))
    return kept, rejected


def ids_to_mark(chosen, rejected_by_section):
    """What a successful run consumes: the rows actually POSTED, plus the
    NONE rejections. Deliberately excludes relevant-but-unshown listings —
    those are the backlog that keeps a quiet day from posting nothing."""
    marks = {x["id"] for picks in chosen.values() for x in picks}
    marks |= {i for ids in rejected_by_section.values() for i in ids}
    return sorted(marks)


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


def _cap_per_company(pool):
    """Keep at most COMPANY_CAP listings per company, preserving rank order.

    Each kept listing is annotated with `_suppressed`: how many of that
    company's other eligible listings this row stands in for. The digest
    renders it as "(+N more)" so a reader can tell that Disney has 25 open
    roles today and go look, rather than assuming the one row is all there is.
    """
    totals = collections.Counter(
        str(x.get("company_name", "")).strip().lower() for x in pool
    )
    counts = {}
    kept = []
    for listing in pool:
        name = str(listing.get("company_name", "")).strip().lower()
        if counts.get(name, 0) >= COMPANY_CAP:
            continue
        counts[name] = counts.get(name, 0) + 1
        listing["_suppressed"] = totals[name] - counts[name]
        kept.append(listing)
    return kept


def plan_digest(unseen_by_section):
    """Pure selection logic (unit-tested): pick what to show per section,
    respecting per-section caps but redistributing spare capacity up to the
    total cap. Returns (chosen_by_section, ids_to_mark, more_count) — note
    ids_to_mark is EVERY unseen id, shown or not."""
    pools = {
        section: sorted(unseen_by_section.get(section, []), key=rank_key)
        for section, _, _ in SOURCES
    }
    # Selection pools are company-capped; `pools` stays whole for marking.
    showable = {s: _cap_per_company(pool) for s, pool in pools.items()}
    chosen = {s: pool[: SECTION_CAPS[s]] for s, pool in showable.items()}
    # Second pass: hand any unused capacity (either section running short)
    # to whichever section still has supply, up to the total cap.
    remaining = TOTAL_CAP - sum(len(v) for v in chosen.values())
    for section, pool in showable.items():
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
    row = f"{star}**{company}** — {title} — {where} — [apply]({url})"
    others = listing.get("_suppressed") or 0
    if others > 0:
        row += f"  *(+{others} more)*"
    return row


def build_messages(chosen):
    """Render the digest as ONE MESSAGE PER SECTION (up to MAX_MESSAGES),
    each under MESSAGE_CAP. Sections used to flow into shared chunks, which
    let a full Internships block eat the whole char budget and silently drop
    the entire New Grad section — headline bug, since those listings were
    still marked seen and never came back. Header rides the first message
    only (no counts, no outbound links — user calls); the role ping rides
    the LAST message only, so a drop is one notification. Rows that don't
    fit their own section's message drop silently; they're marked seen
    either way, so nothing repeats tomorrow."""
    messages = []
    for section, _, _repo in SOURCES:
        picks = chosen.get(section, [])
        if not picks or len(messages) >= MAX_MESSAGES:
            continue
        lines = ["💼 **Daily Career Drops**", ""] if not messages else []
        lines.append(f"**{section}**")
        block = "\n".join(lines)
        rows = 0
        for listing in picks:
            row = format_line(listing)
            if len(block) + 1 + len(row) > MESSAGE_CAP:
                break
            block += "\n" + row
            rows += 1
        if rows:  # never emit a header with nothing under it
            messages.append(block)

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
    rejected_by_section = {}
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
        unseen = [x for x in candidates if x["id"] in unseen_ids]
        print(f"{section}: {len(candidates)} eligible, {len(unseen_ids)} unseen")
        # Classify AFTER dedup (never spend tokens on rows that can't be
        # shown) and rank first, so CLASSIFY_LIMIT keeps the best candidates.
        unseen.sort(key=rank_key)
        try:
            relevant, rejected = filter_relevant(unseen)
        except ClassificationError as e:
            # Abandon the whole run untouched: the next schedule picks up
            # every listing, nothing is posted, nothing is consumed.
            print(f"Classification failed — skipping run entirely: {e}")
            return {"statusCode": 200, "skipped": "classification failed"}
        unseen_by_section[section] = relevant
        rejected_by_section[section] = rejected
        print(f"{section}: {len(relevant)} relevant after the gate")
    if not fetched_any:
        raise RuntimeError("Every listings source failed")

    chosen, _candidate_ids, _more = plan_digest(unseen_by_section)
    # Consume only what we POST plus what the gate rejected. Relevant listings
    # that didn't fit stay unseen as backlog for a quieter run.
    marks = ids_to_mark(chosen, rejected_by_section)
    if not any(chosen.values()):
        print("Nothing to show — skipping this digest.")
        if marks:
            mark_seen(marks)  # still burn the NONE rejections
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
    mark_seen(marks)
    shown = sum(len(v) for v in chosen.values())
    return {
        "statusCode": 200,
        "posted": shown,
        "messages": len(messages),
        "marked": len(marks),
    }
