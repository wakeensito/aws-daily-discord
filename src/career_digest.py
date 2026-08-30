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
import re
import time
import urllib.parse
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
# Cloud and security are the club's whole identity and the feed's rarest rows.
# A 14-day window holds 13 such internships out of 579 and 6 new-grad roles out
# of 609; production runs label 2-5 IT/CYBER listings against 20-32 CS and 8-21
# AI ones. Selection is newest-first, so two cloud rows lose all 8 slots to
# fresher SWE postings roughly three days in four. LABEL_CAPS caps HW from
# above for the opposite reason; this is the same control pointed the other way.
# Per the classifier prompt, IT covers cloud engineering, DevOps, SRE and
# platform work, and CYBER covers security -- together they ARE the bucket.
PRIORITY_LABELS = frozenset({"CYBER", "IT"})
LABEL_FLOOR = 2  # reserved seats per section; a floor, never a quota
# A reserved seat is the one place a wrong label does real damage: it turns a
# coin-flip classification into a guaranteed slot. Live runs seated "Notability
# - Backend Engineer" as IT and "Cogent Security - Forward Deployed Agent
# Engineer" as CYBER -- the second is the prompt's own first rule broken, the
# employer's name read as the job. So a seat needs the TITLE to corroborate the
# label. This is an AND with Nova, never a replacement: the model still rejects
# the physical "Security Installer" and "Data Center Technician" this pattern
# would happily match, and the pattern still rejects the product-software rows
# the model mislabels. Each covers the other's failure mode. Uncorroborated
# rows are not dropped -- they just compete on rank like everything else.
CLOUD_TITLE = re.compile(
    r"\bcloud\b|\bdev ?ops\b|\bdev ?sec ?ops\b|\bsre\b|site reliability|"
    r"platform engineer|infrastructur|\binfra\b|kubernetes|virtuali[sz]ation|"
    r"observability|\bnetwork(ing|s)?\b|sys ?admin|systems? administrat|"
    r"database administrat|help ?desk|\bit support\b|endpoint|"
    r"\bsecurity\b|\bcyber|\binfosec\b|\bappsec\b|penetration test|"
    r"\bpen ?test|threat|incident response|malware|forensic|"
    r"identity and access|\biam\b|\bgrc\b|vulnerabilit|\bsoc analyst\b",
    re.IGNORECASE,
)


def _is_cloud_security(listing):
    """True when Nova's label AND the title both say cloud or security.

    Only the title is read -- never the company name, which is what put a
    security firm's agent-engineering role in a reserved seat.
    """
    return bool(
        listing.get("_label") in PRIORITY_LABELS
        and CLOUD_TITLE.search(str(listing.get("title", "")))
    )

MESSAGE_CAP = 1900  # Discord rejects at 2000
# Both runs are daily, so the headers contrast on GEOGRAPHY, not frequency —
# "Daily" vs "Florida" wrongly implied the Florida drop wasn't daily. Same
# briefcase on the national run: it's the channel's mark, and swapping it
# would make the two posts look like different bots.
HEADERS = {
    "usa": "💼 **National Career Drops**",
    "florida": "🌴 **Florida Career Drops**",
}
MAX_MESSAGES = 2
SEEN_TTL_DAYS = 180  # a season's listing is long dead by then

# --- Relevance gate (2026-08-28) -------------------------------------------
# The feed carries NO job description; `title` is the only field describing
# the work, and a regex over it was rejected ("infrastructure" matched Data
# Center Technicians, "security" matched alarm installers). Nova Micro reads
# the title instead and answers one of LABELS. Only NONE-vs-not changes what
# is posted; the positive labels exist so the logs show what the feed held.
LABELS = frozenset({"CYBER", "IT", "CS", "AI", "PM", "SOLNS", "HW", "NONE"})
# Hardware/computer engineering is in scope — the club has computer-engineering
# members and a lot of hardware work is cloud-adjacent (AWS IoT, Greengrass,
# edge). But Hardware is the LARGEST category in the new-grad feed (204 of 601
# in a 14-day window), so uncapped it would take 3 of every 8 rows. Balance,
# not exclusion.
LABEL_CAPS = {"HW": 2}
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
            # Observed live: the model sometimes hedges ("CS/AI"). One hedged
            # line must not abandon a whole digest — take the first valid label
            # it names, and only give up if it named none.
            label = next(
                (p for p in re.split(r"[^A-Z]+", label) if p in LABELS), None
            )
            if label is None:
                raise ClassificationError(f"unknown label for item {number}")
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

    Returns (kept, rejected_ids). rejected_ids is ALWAYS EMPTY: classification
    is not deterministic — the same 44-case fixture scores 41-44 across runs at
    temperature 0 — so a NONE verdict is a coin flip we must not act on
    permanently. A listing dropped today can be kept tomorrow.

    Nothing here consumes anything. Only a row that actually gets POSTED is
    marked seen. The cost is re-classifying previously-rejected listings on
    later runs, which is a few tokens; the alternative is destroying a good
    listing because the model wavered once."""
    if not candidates:
        return [], []
    head = candidates[:CLASSIFY_LIMIT]
    labels = classify_titles(head)
    counts = collections.Counter(labels)
    print(f"  classified {len(head)}: {dict(counts)}")
    kept, used = [], collections.Counter()
    for listing, label in zip(head, labels, strict=True):
        if label == "NONE":
            continue
        if used[label] >= LABEL_CAPS.get(label, len(head)):
            # Over its share for this run. NOT rejected — it stays unseen and
            # can lead a quieter digest tomorrow.
            continue
        used[label] += 1
        listing["_label"] = label
        kept.append(listing)
    return kept, []


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


FL_CITY = re.compile(r",\s*FL\b|\bflorida\b", re.IGNORECASE)


def is_florida(listing):
    """True when any listed location is in Florida. Deliberately strict —
    'Remote' does not count, because the point of the Florida digest is roles
    members can take without leaving Miami."""
    return any(FL_CITY.search(str(x)) for x in (listing.get("locations") or []))


# Miami-Dade first, then the rest of the tri-county area (commutable), then
# the rest of the state. We are a Miami school: a local role a week old beats
# a Jacksonville role posted this morning.
MIAMI_DADE = re.compile(
    r"\b(miami|miami beach|miami gardens|miami lakes|coral gables|doral|hialeah|"
    r"brickell|aventura|kendall|homestead|cutler bay|pinecrest|sweetwater|"
    r"north miami|coral way)\b",
    re.IGNORECASE,
)
BROWARD_PALM = re.compile(
    r"\b(fort lauderdale|ft\.? lauderdale|boca raton|west palm beach|palm beach|"
    r"weston|sunrise|plantation|pompano|deerfield|davie|coral springs|"
    r"boynton|delray|jupiter|hollywood)\b",
    re.IGNORECASE,
)


def locality_tier(listing):
    """0 = Miami-Dade, 1 = Broward/Palm Beach, 2 = rest of Florida.

    Only Florida locations are considered: several tier names exist in other
    states ("Hollywood, CA", "Jupiter, NC"), and a multi-city listing that
    happens to include one would otherwise be ranked as if it were local."""
    places = [
        str(x) for x in (listing.get("locations") or []) if FL_CITY.search(str(x))
    ]
    if any(MIAMI_DADE.search(x) for x in places):
        return 0
    if any(BROWARD_PALM.search(x) for x in places):
        return 1
    return 2


def florida_rank_key(listing):
    """Locality beats recency in the Florida digest — the whole point of a
    local digest is roles members can take without moving."""
    return (locality_tier(listing), *rank_key(listing))


def lead_with_florida(listing):
    """Reorder a listing's locations so a Florida site renders first.

    Listings are often multi-city, and format_line shows locations[0]. Without
    this the Florida digest can render 'Honolulu, HI' for a role that matched
    on its Tampa site, which reads as a broken bot."""
    locations = listing.get("locations") or []
    # Prefer the city the listing actually ranked on: Miami-Dade, then
    # Broward/Palm Beach, then any Florida site. Otherwise a Miami-ranked role
    # can render "Tampa, FL" and look like it was sorted wrong.
    for pattern in (MIAMI_DADE, BROWARD_PALM, FL_CITY):
        for i, place in enumerate(locations):
            if FL_CITY.search(str(place)) and pattern.search(str(place)):
                listing["locations"] = [place, *locations[:i], *locations[i + 1 :]]
                return listing
    return listing


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


def _select_with_floor(pool, limit):
    """Take `limit` rows, reserving up to LABEL_FLOOR seats for cloud/security.

    A seat goes only to a row whose label AND title agree (_is_cloud_security);
    an uncorroborated row still competes on rank like any other.

    The reserved seats are the LAST ones, so promotion costs only the rows
    nearest the cut. The top of the digest is never touched -- the AWS-starred
    row still leads the national run, and the Miami-Dade row still leads the
    Florida one, which is the whole point of each ranking.

    Runs after _cap_per_company, so one employer with four open security reqs
    still takes only COMPANY_CAP of the reserved seats. Promotes rows that are
    already in the pool and never invents or drops any: a pool carrying no
    `_label` (Florida dry runs, unit fixtures) selects exactly as before.
    """
    chosen = pool[:limit]
    waiting = [x for x in pool[limit:] if _is_cloud_security(x)]
    have = sum(1 for x in chosen if _is_cloud_security(x))
    need = min(LABEL_FLOOR - have, len(waiting))
    if need <= 0:
        return chosen
    # Evict the weakest rows that are not themselves cloud/security.
    evictable = [
        i for i, x in enumerate(chosen) if not _is_cloud_security(x)
    ]
    for i in reversed(evictable[-need:]):
        chosen.pop(i)
    return chosen + waiting[:need]


def plan_digest(unseen_by_section, key=None):
    """Pure selection logic (unit-tested): pick what to show per section,
    respecting per-section caps but redistributing spare capacity up to the
    total cap. Returns (chosen_by_section, ids_to_mark, more_count) — note
    ids_to_mark is EVERY unseen id, shown or not."""
    key = key or rank_key
    pools = {
        section: sorted(unseen_by_section.get(section, []), key=key)
        for section, _, _ in SOURCES
    }
    # Selection pools are company-capped; `pools` stays whole for marking.
    showable = {s: _cap_per_company(pool) for s, pool in pools.items()}
    limits = {s: min(len(pool), SECTION_CAPS[s]) for s, pool in showable.items()}
    # Second pass: hand any unused capacity (either section running short)
    # to whichever section still has supply, up to the total cap.
    remaining = TOTAL_CAP - sum(limits.values())
    for section, pool in showable.items():
        if remaining <= 0:
            break
        extra = min(remaining, len(pool) - limits[section])
        limits[section] += extra
        remaining -= extra
    # Sizes settled, fill each section -- reserving its cloud/security seats.
    chosen = {s: _select_with_floor(pool, limits[s]) for s, pool in showable.items()}
    all_unseen = [x for pool in pools.values() for x in pool]
    shown = sum(len(v) for v in chosen.values())
    return chosen, [x["id"] for x in all_unseen], len(all_unseen) - shown


LOCALE_SEGMENT = re.compile(
    r"^(https?://[^/]+)/([a-zA-Z]{2})([-_])([a-zA-Z]{2})(?=[/?#]|$)"
)
LOCALE_QUERY = re.compile(
    r"\b(lang|locale|language)=([a-zA-Z]{2})(?:([-_])([a-zA-Z]{2}))?\b",
    re.IGNORECASE,
)
LOCALE_VALUE = re.compile(r"([a-zA-Z]{2})[-_]([a-zA-Z]{2})")


def _english(separator, region):
    """Build the English equivalent of a locale token, keeping the source's
    separator and capitalisation -- ATSes reject the wrong form."""
    if not separator:
        return "en"
    return f"en{separator}{'US' if region.isupper() else 'us'}"


def english_url(url):
    """Force an ATS apply link to its English locale.

    Simplify scrapes some postings with the scraper's own locale baked in, so
    the apply page opens in a foreign language -- a Blackstone Miami role
    rendered entirely in Simplified Chinese.

    Only unambiguous locale tokens are rewritten: an ``xx-YY``/``xx_YY``
    leading path segment, or a lang/locale/language query value. A bare
    two-letter segment is left alone because in this feed those are tenant
    slugs, not locales (ey = Ernst & Young, ls = Living Spaces, au = American
    University), and rewriting them would break 450+ working links.
    """
    url = url or ""

    match = LOCALE_SEGMENT.match(url)
    if match and match.group(2).lower() != "en":
        host, _, separator, region = match.groups()
        url = f"{host}/{_english(separator, region)}{url[match.end():]}"

    def swap(found):
        key, language, separator, region = found.groups()
        if language.lower() == "en":
            return found.group(0)
        return f"{key}={_english(separator, region or '')}"

    return LOCALE_QUERY.sub(swap, url)


def foreign_locale(url):
    """Return a non-English locale token still present in ``url``, if any.

    english_url only rewrites shapes it can prove are locales. This catches
    the rest -- a locale buried deeper in a path, say -- so a novel shape
    surfaces in CloudWatch instead of in front of a club member.
    """
    parts = urllib.parse.urlsplit(url or "")
    values = [segment for segment in parts.path.split("/") if segment]
    values += [value for _, value in urllib.parse.parse_qsl(parts.query)]
    for value in values:
        token = LOCALE_VALUE.fullmatch(value)
        if token and token.group(1).lower() != "en":
            return value
    return None


def audit_links(chosen):
    """Rows whose apply link is still foreign after normalisation.

    Reported, never dropped -- a mangled-looking link is more often a tenant
    slug we should leave alone than a real locale, and silently binning real
    jobs is the worse failure.
    """
    flagged = []
    for rows in chosen.values():
        for row in rows:
            url = english_url(row.get("url", ""))
            token = foreign_locale(url)
            if token:
                flagged.append((str(row.get("company_name", "?")), token, url))
    return flagged


def format_line(listing):
    star = "⭐ " if is_aws(listing) else "• "
    company = str(listing.get("company_name", "?")).strip()[:40]
    title = str(listing.get("title", "?")).strip()[:70]
    locations = listing.get("locations") or []
    where = str(locations[0]).strip()[:30] if locations else "—"
    url = english_url(listing.get("url", ""))
    row = f"{star}**{company}** — {title} — {where} — [apply]({url})"
    others = listing.get("_suppressed") or 0
    if others > 0:
        row += f"  *(+{others} more)*"
    return row


def build_messages(chosen, scope="usa"):
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
        lines = [HEADERS.get(scope, HEADERS["usa"]), ""] if not messages else []
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


SCOPES = {"usa", "florida"}


def scope_from(event):
    """Read the run scope off the EventBridge payload. Anything unexpected —
    a non-dict event, a missing key, an unknown value — falls back to `usa`
    rather than taking the digest down over a malformed input."""
    scope = event.get("scope") if isinstance(event, dict) else None
    scope = str(scope).strip().lower() if scope else "usa"
    if scope not in SCOPES:
        print(f"Unknown scope {scope!r} — falling back to usa")
        return "usa"
    return scope


def lambda_handler(event, context):
    if not CAREER_WEBHOOK_URL and not DRY_RUN:
        # Deploys stay green before the #career webhook parameter exists.
        print("CAREER_WEBHOOK_URL unset — career digest inert, skipping.")
        return {"statusCode": 200, "skipped": "no webhook configured"}

    # EventBridge passes {"scope": "..."} per schedule: the morning run is
    # nationwide, the afternoon run is Florida-only (we are a Miami school, and
    # local roles need no relocation). Unknown/missing = usa, the safe default.
    scope = scope_from(event)
    print(f"scope: {scope}")

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
        if scope == "florida":
            candidates = [lead_with_florida(x) for x in candidates if is_florida(x)]
        ids = [x["id"] for x in candidates]
        unseen_ids = ids if DRY_RUN else filter_unseen(ids)
        unseen = [x for x in candidates if x["id"] in unseen_ids]
        print(f"{section}: {len(candidates)} eligible, {len(unseen_ids)} unseen")
        # Classify AFTER dedup (never spend tokens on rows that can't be
        # shown) and rank first, so CLASSIFY_LIMIT keeps the best candidates.
        unseen.sort(key=florida_rank_key if scope == "florida" else rank_key)
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

    chosen, _candidate_ids, _more = plan_digest(
        unseen_by_section, florida_rank_key if scope == "florida" else rank_key
    )
    # Consume only what we POST plus what the gate rejected. Relevant listings
    # that didn't fit stay unseen as backlog for a quieter run.
    marks = ids_to_mark(chosen, rejected_by_section)
    if not any(chosen.values()):
        print("Nothing to show — skipping this digest.")
        if marks:
            mark_seen(marks)  # still burn the NONE rejections
        return {"statusCode": 200, "posted": 0}

    for company, token, url in audit_links(chosen):
        print(f"WARNING foreign apply link ({token}) — {company}: {url}")

    messages = build_messages(chosen, scope)
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
