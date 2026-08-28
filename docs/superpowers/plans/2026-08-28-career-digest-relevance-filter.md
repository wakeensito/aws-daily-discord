# Career Digest Relevance Filter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Drop listings that aren't Cybersecurity / IT / CS / AI / technical-PM /
solutions-and-consulting work, using Amazon Nova Micro as a relevance gate on the title.

**Architecture:** After the existing eligibility and dedup filters, take the top
`CLASSIFY_LIMIT` ranked candidates per section, send their titles to Nova Micro in
batches, drop everything labeled `NONE`, and feed survivors into the existing selection
pipeline unchanged. Any classification failure abandons the run without posting or marking.

**Tech Stack:** Python 3.13, boto3 (`bedrock-runtime` Converse), AWS SAM, pytest.

**Spec:** `docs/superpowers/specs/2026-08-28-career-digest-major-routing-design.md`

## Global Constraints

- Account: personal `iamadmin` / us-east-1. Never Everé.
- No AWS account IDs or ARNs in the repo — CloudFormation pseudo-parameters only.
- Model `us.amazon.nova-micro-v1:0` via Converse, `temperature=0`.
- Offline tests make no network and no AWS calls. Bedrock is mocked.
- `mark_seen` receives every unseen listing, including `NONE`-dropped ones.
- A failed classification posts nothing and marks nothing.
- Message format, schedules, `SECTION_CAPS` (8/8), `COMPANY_CAP` (1), ping behavior:
  unchanged.
- ruff 0.16.5 passes; the existing 37 tests keep passing.

## Labels

`CYBER` `IT` `CS` `AI` `PM` `SOLNS` `NONE`. Only `NONE`-vs-not affects output; the six
positive labels exist for logging. `SOLNS` is a separate label rather than folded into
`PM` because the model refuses to call a Solutions Engineer a product manager — measured:
folding them in dropped AWS Solutions Engineer and Deloitte Technology Consulting entirely,
splitting them out fixed both.

---

### Task 1: Prompt file and classifier

**Files:**
- Create: `src/prompts/classify_titles.txt` (done — v6, scored 31/32)
- Modify: `src/career_digest.py`
- Test: `tests/test_classify.py`

**Interfaces:**
- Produces: `LABELS: frozenset[str]`, `ClassificationError(Exception)`,
  `_converse(text: str) -> str` (the only boto3 touchpoint, mocked in tests),
  `classify_titles(listings: list[dict]) -> list[str]` — one label per listing,
  index-aligned, raising `ClassificationError` on any failure.

- [ ] Write failing tests: one label per listing; missing line raises; unknown label
      raises; Bedrock exception raises.
- [ ] Run, verify failure (`no attribute 'classify_titles'`).
- [ ] Implement batching, `N:LABEL` parsing, completeness validation.
- [ ] Run tests, verify pass, ruff clean.
- [ ] Commit.

### Task 2: Drop NONE in the pipeline

**Files:** `src/career_digest.py` (`lambda_handler`), `tests/test_classify.py`

**Interfaces:**
- Consumes: `classify_titles`.
- Produces: `CLASSIFY_LIMIT: int`,
  `filter_relevant(candidates: list[dict]) -> tuple[list[dict], list[str]]` returning
  (kept listings, ids to mark — which is ALL input ids).

- [ ] Failing test: `NONE`-labelled listings are dropped from display but every id is
      still returned for marking.
- [ ] Run, verify failure.
- [ ] Implement; call per section between dedup and `plan_digest`. On
      `ClassificationError`, return without posting or marking.
- [ ] Run tests, verify pass.
- [ ] Commit.

### Task 3: IAM, model parameter, observability

**Files:** `template.yaml`

- [ ] Add `BedrockInvokeNovaMicro` statement to `CareerDigestFunction` (both ARN shapes,
      pseudo-params) and `BEDROCK_MODEL_ID: !Ref BedrockModelId` to its environment.
- [ ] `sam validate --lint`.
- [ ] Log per run: label histogram, dropped count, batch count.
- [ ] Commit.

### Task 4: Prompt regression fixture

**Files:** `tests/fixtures/prompt_cases.json`, `score_prompt.py`

- [ ] Check in the 32 hand-labelled adversarial cases with expected relevant/`NONE`.
- [ ] `score_prompt.py` scores the live prompt. NOT in CI — costs money, needs creds.
- [ ] Commit.

### Task 5: End-to-end verification

- [ ] `local_run.py --career` with real credentials: no technician/assembler/quant rows,
      PM and SOLNS roles survive.
- [ ] PR, CI green, merge, Deploy succeeds, Lambda `LastModified` matches.

## Known limitation

`Accenture — Technology Analyst - Cloud Practice` scores `NONE` (over-drop). Generic
"Technology Analyst" titles without a clearer qualifier are the weak spot. Logged drop
counts are the signal for whether this class is common enough to chase.
