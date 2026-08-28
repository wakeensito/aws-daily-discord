"""Shared Discord webhook client — the ONE posting implementation both
bots use (fun fact + career digest), so hard-won behavior lives in exactly
one place: the real User-Agent (Discord/Cloudflare 403s Python-urllib's
default), the allowed_mentions discipline (nothing pings unless explicitly
allowed), and sentence-boundary clipping."""

import json
import urllib.request

USER_AGENT = "DailyCloudFunFactBot/1.0 (+github.com/wakeensito/aws-daily-discord)"


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


SUPPRESS_EMBEDS = 1 << 2  # Discord message flag: no auto link-preview cards


def build_payload(message, role_id="", suppress_embeds=False):
    payload = {
        "content": message,
        # Nothing pings unless explicitly allowed; the role ping is the one
        # exception when configured.
        "allowed_mentions": {"parse": [], "roles": [role_id] if role_id else []},
    }
    if suppress_embeds:
        # Link-heavy digests would otherwise sprout a preview card per URL.
        payload["flags"] = SUPPRESS_EMBEDS
    return payload


def post_to_discord(webhook_url, message, role_id="", suppress_embeds=False):
    payload = build_payload(message, role_id, suppress_embeds)
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status not in (200, 204):
            raise RuntimeError(f"Discord webhook failed: {response.status}")
