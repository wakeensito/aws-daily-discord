# Career Digest — Relevance Filtering for Club Majors

**Date:** 2026-08-28
**Status:** Design approved, implementation not started
**Component:** `src/career_digest.py` (career digest Lambda, stack `daily-discord`)

## Problem

The #career digest ranks listings by AWS-affinity then recency, with no notion of what
members actually study. The AWS Student Builder group at FIU is made up of
**Cybersecurity, IT, Computer Science, and AI** majors, and the feed does not serve them:

- Of 3,202 active new-grad listings, **827 are Hardware and 205 are Quant** — roughly a
  third of the feed is unactionable for this membership.
- Real digests have carried Panel Technicians, Residential Installation Technicians,
  Electrical Assemblers, and SONAR Hull Arrays Engineers.

The feed offers no field that solves this. `category` has five usable values and is dirty
(`Software` 998 vs `Software Engineering` 13; `AI/ML/Data` 1050 vs
`Data Science, AI & Machine Learning` 1). `sponsorship` is dead — 3,192 of 3,202 say
`"Other"`. `degrees` is empty on 700. **There is no job description in the feed at all**;
the only field describing the work is the free-text `title`.

A regex over titles was tried and rejected: "infrastructure" matched Data Center
Technicians, "security" matched Electronic Security Installers, and tightening the
patterns dropped yield to ~1 listing/day.

## Approach

Use Amazon Nova Micro as a **relevance gate** on the listing title: is this
Cybersecurity, IT, Computer Science, or AI work? Drop everything that is not. Rank what
survives exactly as today.

**This is deliberately not a router.** An earlier draft allocated slots per major
(2 Cyber / 2 IT / 2 CS / 2 AI). That was rejected: the majors overlap heavily in practice
— CS majors take security roles, IT majors take SWE roles — so per-major quotas would
bench a strong listing because its bucket was full while another sat empty. What members
need is *good postings across the combined set*, not proportional representation.

Dropping the routing also removes the hardest classification problem. The CS-vs-IT
boundary is genuinely fuzzy, and it stops mattering once nothing depends on the
distinction.

Nothing about the message format, schedules, caps, dedup semantics, or ping behavior
changes.

### Flow

```
fetch feed
  -> is_eligible()                 (unchanged: active, visible, undergrad, USA, 14 days)
  -> filter_unseen()               (unchanged)
  -> rank_key()                    (unchanged: AWS-first, then newest)
  -> take top CLASSIFY_LIMIT       (new: only ~120 candidates needed for 16 slots)
  -> classify_titles()             (new: Nova Micro, batches of BATCH_SIZE)
  -> drop NONE                     (new — the entire behavior change)
  -> _cap_per_company()            (unchanged, COMPANY_CAP = 1)
  -> SECTION_CAPS slice            (unchanged, 8/8 with redistribution)
  -> build_messages()              (unchanged: one message per section)
  -> mark_seen(all unseen)         (unchanged — see Invariants)
```

The only new step that affects output is `drop NONE`. Everything downstream is the
existing pipeline.

### Labels

The model returns one of `CYBER` / `IT` / `CS` / `AI` / `NONE`. Only the `NONE` versus
not-`NONE` distinction affects the digest. The four positive labels are retained purely
for **logging** — a per-run breakdown makes it visible if, say, security roles have
vanished from the feed for a week. They do not influence ranking, ordering, or slots.

Keeping four labels rather than a yes/no also measurably helps the model reason about the
boundary, since it must name what kind of work it sees rather than make a vague relevance
judgment.

### Failure handling

Any Bedrock error, throttle, malformed response, or missing label for a batch:
**post nothing, mark nothing, log it.** The run is abandoned intact and the next run
(9 AM / 3 PM) picks up every listing untouched. A Bedrock outage costs one silent drop.

This matches the existing instinct in `filter_unseen`, which treats a seen-table read
failure as "everything is seen" rather than risk spamming the channel.

### Model

- `us.amazon.nova-micro-v1:0` via the Converse API — same model, region, and IAM pattern
  as the fun-fact Lambda.
- `temperature=0`.
- Batches of 40 titles, formatted `N. Company | Title`, response parsed as `N:LABEL` lines.
- Measured: 40 titles = 691 input / 193 output tokens, ~1.0s. At ~120 candidates per run
  and two runs a day, added cost is well under $0.01/month.
- The career Lambda needs a new IAM statement for `bedrock:InvokeModel`, mirroring
  `BedrockInvokeNovaMicro` on the fun-fact function.

## Invariants that must not break

1. **`mark_seen` still receives every unseen listing**, including ones dropped as `NONE`
   and ones suppressed by the company cap. Marking only what is shown makes suppressed
   listings return on every run forever. Already guarded by
   `test_company_cap_does_not_shrink_what_gets_marked_seen`; the same guard must cover
   `NONE`-dropped listings.
2. **Classification never runs before dedup.** Classifying seen listings burns tokens on
   rows that can never be shown.
3. **A failed run marks nothing.** Partial marking would consume listings that were never
   displayed — the exact bug that lost a day of New Grad listings on 2026-08-28.
4. **Message format is untouched.** One message per section, header on the first, ping on
   the last, no counts, no outbound links, `SUPPRESS_EMBEDS` on.

## Classification quality

Measured on 24 hand-labeled adversarial cases, scored on the only distinction that
matters — relevant versus `NONE`: **20/24**.

The first prompt draft failed on five cases that are now fixed: System-on-Chip read as
Security Operations Center; classifying by employer industry rather than the job itself
(a hedge fund's Software Developer is still software); Quantitative Researchers read as
CS; and DevOps drifting away from IT.

The 4 remaining misses all sit on the `NONE` boundary:

| Listing | Expected | Got | Cost |
|---|---|---|---|
| Goldman Sachs — Engineering New Analyst | relevant | `NONE` | silent deletion |
| Noblis — Identity Intelligence Analyst | `NONE` | `CYBER` | noise in the digest |
| Microsoft — Data Center Technician, Cloud Ops | `NONE` | `IT` | noise (defensible either way) |
| Susquehanna — Trading Systems Engineer | `NONE` | `CS` | noise (defensible either way) |

Three of the four are over-inclusion, which is the cheap direction: a slightly-off listing
appears and a reader skips it. The expensive direction is dropping something good, and
only one case did that.

**Open question for implementation:** the prompt says *"If you cannot tell what the work
is, answer NONE."* That biases toward silent deletion. Recommend starting as written and
revisiting after a week of logged drop counts, since over-dropping is the failure mode a
reader cannot see.

## Observability

`NONE` is a deletion, so it must be visible:

- Log per run: candidates classified, count per label, count dropped as `NONE`, batches
  failed, tokens used.
- A sudden rise in the `NONE` share is the signal that the prompt is over-dropping.

## Testing

- Classification is **mocked** in unit tests — the offline suite makes no network or AWS
  calls, matching the existing 37 tests.
- Pure functions tested directly: `NONE` dropping, interaction with the company cap and
  section caps, and the mark-seen invariant.
- Failure paths tested: Bedrock exception, malformed response, missing or extra line
  numbers, labels outside the enum — each must abandon the run without posting or marking.
- A live smoke script (like `local_run.py --career`) prints real labels for eyeballing.
- The 24-case adversarial set is checked in as a prompt-regression fixture, scored on
  relevant-vs-`NONE`, so prompt edits can be re-scored rather than argued about.

## Explicitly out of scope

- Fetching job descriptions. ~58% of the feed lands on Workday/Oracle, which are
  JS-rendered and rate-limited. Titles only.
- Per-major slots, channels, or role pings.
- Resume-specific scoring. This serves a 150-member club, not one person.
- Changing schedules, caps, ping cadence, or message format.

## The prompt

Lives at `src/prompts/classify_titles.txt`, loaded at import so it can be edited and
re-scored without touching logic. Its rules exist to counter specific observed failures:
the acronym section exists because of System-on-Chip; the "classify the work, not the
employer" rule because of Point72 and Goldman Sachs; the explicit Quant exclusion because
Quantitative Researcher reads as technical; the physical-security exclusion because
alarm installers matched on "security".
