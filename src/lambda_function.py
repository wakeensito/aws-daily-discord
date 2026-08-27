"""Daily Cloud Fun Fact bot.

EventBridge Scheduler -> this Lambda -> Bedrock (Nova Micro via the Converse
API) -> Discord webhook. A DynamoDB table tracks which topics ran in the last
30 days so the rotation never repeats early.

Split of responsibilities, on purpose:
  - The MODEL writes only the content fields (definition, three Q&As, exam
    keywords) and returns strict JSON.
  - The CODE owns everything trustworthy: which exams a topic appears on
    (topics.json, derived from the official exam guides — never generated),
    the Discord markdown skeleton (`> ` quotes, `**__bold-underline__**`),
    length caps, and the @Student Builder ping.

DRY_RUN=1 prints the assembled post instead of sending it and skips the
rotation write — the local preview path (see local_run.py).
"""

import json
import os
import random
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

dynamodb = boto3.resource("dynamodb")
bedrock = boto3.client("bedrock-runtime")

TABLE_NAME = os.environ.get("TOPICS_TABLE_NAME", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.amazon.nova-micro-v1:0")
ROLE_ID = os.environ.get("STUDENT_BUILDER_ROLE_ID", "")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

ROTATION_DAYS = 30

# Per-field caps: prompt asks for less; these are the hard ceilings before
# sentence-boundary truncation. The whole message is guarded at 1900 chars
# (Discord rejects at 2000) so a send can never 400 on length.
DEFINITION_CAP = 300
ANSWER_CAP = 350
MESSAGE_CAP = 1900

# Exam tag -> display order. Tags come from topics.json, never the model.
EXAM_ORDER = ["AIF", "CCP", "SAA"]


def load_topics():
    """topics.json: [{"topic": str, "exams": [tag...], "category": str}]."""
    path = os.path.join(os.path.dirname(__file__), "topics.json")
    with open(path) as f:
        return json.load(f)


def get_used_topics(days=ROTATION_DAYS):
    """Topic names posted in the last N days. Best-effort: an unreadable
    table must not kill the daily post — worst case a topic repeats early."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        table = dynamodb.Table(TABLE_NAME)
        response = table.scan(
            FilterExpression="used_date > :cutoff",
            ExpressionAttributeValues={":cutoff": cutoff},
        )
        return {item["topic"] for item in response.get("Items", [])}
    except ClientError as e:
        print(f"Rotation table read failed (continuing): {e}")
        return set()


def pick_topic(all_topics, used):
    fresh = [t for t in all_topics if t["topic"] not in used]
    return random.choice(fresh if fresh else all_topics)


def record_topic_usage(topic):
    now = datetime.now(timezone.utc)
    try:
        dynamodb.Table(TABLE_NAME).put_item(
            Item={
                "topic": topic,
                "used_date": now.isoformat(),
                # TTL hygiene: the row deletes itself once it can no longer
                # matter to the rotation window.
                "ttl": int(now.timestamp()) + (ROTATION_DAYS + 5) * 86400,
            }
        )
    except ClientError as e:
        print(f"Rotation table write failed (post already sent): {e}")


PROMPT_TEMPLATE = """\
You write the daily "Cloud Fun Fact" post for an AWS student Discord. The
audience is students studying for AWS certifications (Cloud Practitioner,
AI Practitioner, Solutions Architect Associate).

Topic: {topic}
Category: {category}

Return STRICT JSON only — no markdown fences, no commentary — exactly:
{{"definition": "...", "qas": [{{"q": "...", "a": "..."}},
{{"q": "...", "a": "..."}}, {{"q": "...", "a": "..."}}],
"tip_keywords": ["...", "...", "..."]}}

Rules:
- definition: ONE sentence, at most 200 characters, plain English. It
  completes the sentence "{topic} ..." so it must START with "is" (for a
  service: "is AWS's ... that ..."). Do not repeat the topic name inside it.
- qas: exactly 3 pairs. Q1 must be "What is it used for?" or a close variant
  that defines what it is. For Q2 and Q3, choose the two MOST USEFUL angles
  for this specific topic — for example: how it differs from a commonly
  confused service, how pricing/cost behaves, how AI/ML teams use it, a
  mistake students make, or when NOT to use it. Answers at most 250
  characters each, beginner-friendly, technically accurate.
- tip_keywords: 3 short phrases that signal this topic in an exam question
  (the kind of wording that appears in the question stem).
- No emojis, no hashtags, no links anywhere.

Example of the level and tone (topic was "AWS Cost Explorer"):
{{"definition": "is AWS's visual cost analysis tool that lets you see \
exactly where your money is going across all your AWS services.",
"qas": [{{"q": "What is it used for?", "a": "Visualizing and breaking down \
your AWS spending over time, by service, account, region, or tag, so you \
can spot waste and optimize costs."}},
{{"q": "How is it different from AWS Budgets?", "a": "Budgets alerts you \
before you overspend. Cost Explorer shows you where you already spent money \
and helps you understand why."}},
{{"q": "How does it help AI/ML teams?", "a": "Easily see how much SageMaker \
training jobs, Bedrock API calls, or S3 data storage are actually costing \
you, then make smarter decisions about your pipeline."}}],
"tip_keywords": ["visualize AWS spending", "cost breakdown by service", \
"analyze cloud costs over time"]}}
"""


def parse_model_json(text):
    """Parse + validate the model's JSON. Raises ValueError on any problem
    so the caller's single retry can kick in."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").removeprefix("json")
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    data = json.loads(cleaned[start : end + 1])

    definition = str(data.get("definition", "")).strip()
    qas = data.get("qas", [])
    keywords = [str(k).strip() for k in data.get("tip_keywords", []) if str(k).strip()]
    if not definition:
        raise ValueError("missing definition")
    if not isinstance(qas, list) or len(qas) != 3:
        raise ValueError("qas must have exactly 3 items")
    pairs = []
    for qa in qas:
        q, a = str(qa.get("q", "")).strip(), str(qa.get("a", "")).strip()
        if not q or not a:
            raise ValueError("empty q or a")
        pairs.append((q, clip(a, ANSWER_CAP)))
    if len(keywords) < 2:
        raise ValueError("need at least 2 tip keywords")
    return {
        "definition": clip(definition, DEFINITION_CAP),
        "qas": pairs,
        "tip_keywords": keywords[:3],
    }


def generate_content(entry):
    """One Converse call, one retry on malformed output."""
    prompt = PROMPT_TEMPLATE.format(topic=entry["topic"], category=entry["category"])
    last_error = None
    for _ in range(2):
        response = bedrock.converse(
            modelId=BEDROCK_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 700, "temperature": 0.7},
        )
        text = response["output"]["message"]["content"][0]["text"]
        try:
            return parse_model_json(text)
        except (ValueError, json.JSONDecodeError) as e:
            last_error = e
            print(f"Model output rejected, retrying once: {e}")
    raise RuntimeError(f"Model never produced valid content: {last_error}")


def clip(text, limit):
    """Truncate at a sentence boundary if possible, else at a word."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for boundary in (". ", "! ", "? "):
        idx = cut.rfind(boundary)
        if idx > limit // 2:
            return cut[: idx + 1].rstrip()
    return cut[: cut.rfind(" ")].rstrip() + "…"


def exam_label(exams):
    """Deterministic 'AIF & CCP & SAA' label in fixed order — from data."""
    ordered = [tag for tag in EXAM_ORDER if tag in exams]
    return " & ".join(ordered)


def assemble_message(entry, content):
    """The Discord markdown skeleton, byte-for-byte the format the club uses:
    `> ` blockquotes on answers AND the exam tip, `**__Topic__**` for the
    underlined-bold hook. Unicode emoji (not :shortcodes:) because webhooks
    don't reliably render shortcodes."""
    topic = entry["topic"]
    kws = content["tip_keywords"]
    kw_text = ", ".join(f'"{k}"' for k in kws[:-1]) + f', or "{kws[-1]}"'
    lines = [
        f"☁️ Daily Cloud Fun Fact: {topic}",
        "",
        f"🔶 **__{topic}__** {content['definition']}",
        "",
    ]
    for q, a in content["qas"]:
        lines += [f"Q: {q}", f"> A: {a}", ""]
    lines.append(
        f"> 📌 Exam Tip ({exam_label(entry['exams'])}): "
        f"Keywords like {kw_text} = {topic}."
    )
    if ROLE_ID:
        lines += ["", f"<@&{ROLE_ID}>"]
    message = "\n".join(lines)
    if len(message) > MESSAGE_CAP:
        message = clip(message, MESSAGE_CAP)
    return message


def post_to_discord(message):
    payload = {
        "content": message,
        # Nothing pings unless explicitly allowed; the role ping is the one
        # exception when configured.
        "allowed_mentions": {"parse": [], "roles": [ROLE_ID] if ROLE_ID else []},
    }
    request = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status not in (200, 204):
            raise RuntimeError(f"Discord webhook failed: {response.status}")


def lambda_handler(event, context):
    topics = load_topics()
    entry = pick_topic(topics, get_used_topics())
    print(f"Selected topic: {entry['topic']} ({exam_label(entry['exams'])})")

    content = generate_content(entry)
    message = assemble_message(entry, content)
    print(f"Assembled post ({len(message)} chars):\n{message}")

    if DRY_RUN:
        print("DRY_RUN=1 — not posting, not recording rotation.")
        return {"statusCode": 200, "dryRun": True, "topic": entry["topic"]}

    post_to_discord(message)
    record_topic_usage(entry["topic"])
    return {"statusCode": 200, "topic": entry["topic"]}
