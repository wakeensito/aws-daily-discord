# Daily Discord bots (AWS Student Builder)

Two scheduled bots in one SAM stack.

## Bot 2: Career digest (#career)

Every day at **9 AM Miami**, a digest of internships and new-grad roles
posted since the last run — real listings from the community-maintained
[SimplifyJobs repos](https://github.com/SimplifyJobs/Summer2026-Internships)
(the continuation of the Pitt CSC list; public JSON, no account or API key,
apply links go straight to employer application systems). One message,
~12 newest listings in two sections, "…and X more" footer.

- **Open feed, AWS highlight**: no company filtering (member outcomes over
  brand purity) — Amazon/AWS roles get ⭐ and sort first.
- **Undergrad-friendly**: listings flagged advanced-degree-only (🎓 in the
  source repo) are dropped.
- **Strictly new**: a seen-listings table (180-day TTL) dedups every run;
  everything unseen is marked, shown or not — tomorrow is only new drops.
  No new listings → no post (silence over noise).
- Season note: the internships source URL is per-season
  (`Summer2026-Internships`) — bump it in `src/career_digest.py` when the
  next season's repo opens.
- Preview locally: `python local_run.py --career` (no post, no writes).

## Bot 1: Daily Cloud Fun Fact (#daily-cloud)

Automates the **#daily-cloud** channel for the AWS Student Builder Discord:
every day at noon (America/New_York, DST-proof) it picks an AWS topic
not yet posted this cycle, has Bedrock write a short fun-fact post, and delivers
it through a Discord webhook — including **which certification exams the
topic can appear on** (AIF-C01, CLF-C02, SAA-C03).

```
EventBridge Scheduler ──▶ Lambda (python3.13, arm64)
                             │  1. pick topic  (DynamoDB full-cycle rotation)
                             │  2. generate    (Bedrock Converse · Nova Micro)
                             │  3. assemble    (Discord markdown, in code)
                             └▶ 4. post        (webhook + @Student Builder ping)
```

Everything is one SAM stack (`template.yaml`) — table, function, schedule,
IAM. Estimated cost: well under $1/month (Nova Micro is the cheapest
Bedrock text model; one invocation a day).

## Design rules

- **Full-cycle rotation.** Every topic posts exactly once before any topic
  repeats — a complete ~97-day syllabus at one post per day. When the list
  is exhausted the Lambda clears the rotation table and starts a fresh
  cycle. (Used-topic rows persist for the whole cycle; no TTL on purpose.)

- **Exam tags are data, not generation.** `src/topics.json` maps every topic
  to its exams, derived from the official exam guides' in-scope lists. The
  model never writes the "Exam Tip (AIF & CCP)" label — code does. (SAA tags
  currently derive from the well-known published SAA-C03 scope; re-derive
  when the SAA guide file is added to the source material.)
- **The model fills fields; code owns the skeleton.** Nova Micro returns
  strict JSON (`definition`, three Q&As, tip keywords). The Discord
  markdown — `> ` blockquotes, `**__underlined-bold__**`, the ☁️/🔶/📌
  emoji, the role ping — is assembled deterministically in
  `assemble_message()`, so the post always renders correctly.
- **Q1 is always "What is it used for?"** (or a defining variant); Q2/Q3 are
  model-chosen for relevance to the topic.
- **Length is capped three times**: prompt targets (~200/250 chars per
  field), hard per-field caps with sentence-boundary truncation, and a
  1900-char whole-message guard (Discord rejects at 2000).
- **No account IDs or ARNs in the repo — ever.** Templates build ARNs from
  pseudo-parameters; the deploy role ARN lives in a GitHub Actions variable;
  the webhook URL is a NoEcho parameter CloudFormation remembers.

## Local preview (no Discord, no deploy)

```bash
pip install -r requirements.txt
AWS_PROFILE=<your-profile> python local_run.py            # random topic
AWS_PROFILE=<your-profile> python local_run.py "S3"       # force a topic
```

Prints the exact post that would be sent. Needs Bedrock model access for
Nova Micro (us-east-1). Offline tests: `pytest -q`.

## Deploy

See `SETUP.md` for the full runbook (one-time bootstrap + CI/CD). Short
version:

```bash
sam build && sam deploy --guided     # first time: supplies the webhook URL
sam build && sam deploy              # ever after (CI does this on merge)
```

## Repo scaffolding

CI (`ci-ok` required check: ruff, pytest, gitleaks, trivy), branch
protection, the project board, and the gated OIDC deploy workflow come from
the `/launch-repo` DevOps skill — deploys assume a role created by
`infra/cicd-role.yaml`; no static AWS keys exist anywhere.
