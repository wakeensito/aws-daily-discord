# Setup runbook

One-time bootstrap, in order. Everything is CLI; nothing is clicked into
existence except the Discord webhook (Discord has no API-first path for
that) and Bedrock model access (account-level toggle).

## 0. Prerequisites

- AWS CLI + SAM CLI, credentials for the target account (`AWS_PROFILE=iamadmin`)
- Bedrock model access enabled for **Amazon Nova Micro** (console → Bedrock →
  Model access, us-east-1) — one-time account toggle
- The GitHub OIDC provider exists in the account (create once if missing):
  ```bash
  aws iam create-open-id-connect-provider \
    --url https://token.actions.githubusercontent.com \
    --client-id-list sts.amazonaws.com
  ```

## 1. Discord inputs

1. **Webhook**: #daily-cloud channel → Edit Channel → Integrations →
   Webhooks → New Webhook → Copy URL. (The webhook is bound to the channel —
   the bot never needs a channel ID.)
2. **Role ID** for the @Student Builder ping: Server Settings → Roles →
   right-click **Student Builder** → Copy ID. (Optional — without it the
   post ships un-pinged.)

Neither value is ever committed. The webhook URL is a NoEcho CloudFormation
parameter; the role ID is a plain parameter (a Discord snowflake, not
sensitive, but kept out of tracked files anyway for consistency).

## 2. First deploy (laptop)

```bash
AWS_PROFILE=iamadmin sam build
AWS_PROFILE=iamadmin sam deploy --guided
#   DiscordWebhookUrl    -> paste the webhook URL
#   StudentBuilderRoleId -> paste the role ID (or leave empty)
```

CloudFormation remembers both parameters (previous-values), so every later
deploy — including CI — runs plain `sam deploy` without knowing them.

Smoke test:

```bash
AWS_PROFILE=iamadmin aws lambda invoke \
  --function-name daily-discord-post --payload '{}' out.json && cat out.json
```

A post should land in #daily-cloud and a rotation row in the
`daily-discord-topics` table. Invoke twice — the second post picks a
different topic.

## 3. Wire CI/CD (the OIDC deploy role)

```bash
AWS_PROFILE=iamadmin aws cloudformation deploy \
  --template-file infra/cicd-role.yaml \
  --stack-name daily-discord-cicd \
  --capabilities CAPABILITY_NAMED_IAM

AWS_PROFILE=iamadmin aws cloudformation describe-stacks \
  --stack-name daily-discord-cicd \
  --query 'Stacks[0].Outputs[?OutputKey==`DeployRoleArn`].OutputValue' --output text
# -> gh variable set AWS_DEPLOY_ROLE_ARN --body "<that arn>"

gh variable set DEPLOY_ENABLED --body true
gh workflow run deploy.yml        # dispatch once to verify the OIDC path
```

After this, every merge to `main` redeploys automatically (the deploy
workflow runs `sam build && sam deploy`; an unchanged stack is a no-op).

## 4. Verify the schedule

```bash
aws scheduler get-schedule --name daily-discord-schedule
```

Expect `ENABLED`, `cron(0 12 * * ? *)`, timezone `America/New_York` — noon
Miami every day, unaffected by DST. Then confirm the next day's post lands
unattended.

## Career digest (second bot)

Ships inert: until `CareerWebhookUrl` is supplied, the career Lambda logs
"skipping" and does nothing, so deploys never block on it. To activate:

1. #career channel → Edit Channel → Integrations → Webhooks → New Webhook →
   Copy URL (webhooks are channel-bound; no app ID or bot token involved).
2. One deploy: `sam deploy --parameter-overrides CareerWebhookUrl=<url>`
   (CloudFormation remembers it afterward, like the fun-fact webhook).
3. Smoke: `aws lambda invoke --function-name daily-discord-career
   --payload '{}' out.json` → digest lands in #career; invoke again →
   no post (everything's been marked seen).
4. Optional ping: redeploy with `CareerRoleId=<role id>` (off by default —
   a daily digest shouldn't ping).

## Changing things

- **Post time**: `ScheduleExpression` / `ScheduleTimezone` parameters
  (deploy with `--parameter-overrides`, or edit the defaults via PR).
- **Topics / exam tags**: edit `src/topics.json` via PR — merge redeploys.
- **Webhook rotation**: delete the webhook in Discord, create a new one,
  run one deploy with `--parameter-overrides DiscordWebhookUrl=<new>`.
- **Model**: `BedrockModelId` parameter; the IAM policy in `template.yaml`
  is scoped to Nova Micro and must be updated in the same PR.

## Troubleshooting

- **AccessDeniedException from Bedrock** → model access not enabled for
  Nova Micro in this account/region, or the IAM resource ARNs don't match
  the model — see the policy comment in `template.yaml`.
- **Post missing** → `aws logs tail /aws/lambda/daily-discord-post --since 1d`.
- **Webhook 401/404** → webhook was deleted in Discord; rotate per above.
- **Ping not notifying** → `StudentBuilderRoleId` empty or wrong; the
  webhook payload only allows that one role mention by design.
