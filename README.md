# Daily Cloud Fun Fact bot

Automates the **#daily-cloud** channel for the AWS Student Builder Discord:
every day at noon (America/New_York, DST-proof) it picks an AWS topic that
hasn't run in 30 days, has Bedrock write a short fun-fact post, and delivers
it through a Discord webhook — including **which certification exams the
topic can appear on** (AIF-C01, CLF-C02, SAA-C03).

```
EventBridge Scheduler ──▶ Lambda (python3.12, arm64)
                             │  1. pick topic  (DynamoDB 30-day rotation, TTL)
                             │  2. generate    (Bedrock Converse · Nova Micro)
                             │  3. assemble    (Discord markdown, in code)
                             └▶ 4. post        (webhook + @Student Builder ping)
```

Everything is one SAM stack (`template.yaml`) — table, function, schedule,
IAM. Estimated cost: well under $1/month (Nova Micro is the cheapest
Bedrock text model; one invocation a day).

## Design rules

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
