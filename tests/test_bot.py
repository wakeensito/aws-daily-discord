"""Offline tests — no AWS calls. They pin the parts that must never drift:
the exam tags are valid data, the Discord markdown skeleton is exact
(`> ` quotes, `**__bold__**`, blockquoted exam tip), rotation avoids
repeats, and the length guards hold."""

import json
import os
import sys

import pytest

os.environ.setdefault("TOPICS_TABLE_NAME", "test-table")
os.environ.setdefault("DISCORD_WEBHOOK_URL", "http://localhost/unused")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import lambda_function as bot  # noqa: E402

VALID_EXAMS = {"AIF", "CCP", "SAA"}


def load_topics():
    with open(os.path.join(os.path.dirname(__file__), "..", "src", "topics.json")) as f:
        return json.load(f)


class TestTopicsData:
    def test_every_entry_is_well_formed(self):
        topics = load_topics()
        assert len(topics) >= 50
        names = [t["topic"] for t in topics]
        assert len(names) == len(set(names)), "duplicate topics"
        for t in topics:
            assert t["topic"].strip()
            assert t["category"].strip()
            assert t["exams"], f"{t['topic']} has no exam tags"
            assert set(t["exams"]) <= VALID_EXAMS, f"{t['topic']} bad tags"

    def test_exam_label_order_is_fixed(self):
        assert bot.exam_label(["SAA", "AIF", "CCP"]) == "AIF & CCP & SAA"
        assert bot.exam_label(["SAA"]) == "SAA"


class TestRotation:
    def test_pick_avoids_used_topics(self):
        topics = [{"topic": n, "exams": ["CCP"], "category": "x"} for n in "abcd"]
        picked = bot.pick_topic(topics, used={"a", "b", "c"})
        assert picked["topic"] == "d"

    def test_pick_resets_when_all_used(self):
        topics = [{"topic": n, "exams": ["CCP"], "category": "x"} for n in "ab"]
        picked = bot.pick_topic(topics, used={"a", "b"})
        assert picked["topic"] in {"a", "b"}


class TestModelParsing:
    GOOD = {
        "definition": "is a thing.",
        "qas": [
            {"q": "What is it used for?", "a": "Stuff."},
            {"q": "Q2?", "a": "A2."},
            {"q": "Q3?", "a": "A3."},
        ],
        "tip_keywords": ["kw one", "kw two", "kw three"],
    }

    def test_parses_clean_json(self):
        out = bot.parse_model_json(json.dumps(self.GOOD))
        assert out["definition"] == "is a thing."
        assert len(out["qas"]) == 3

    def test_parses_fenced_json(self):
        fenced = "```json\n" + json.dumps(self.GOOD) + "\n```"
        assert bot.parse_model_json(fenced)["qas"][0][0] == "What is it used for?"

    def test_rejects_wrong_qa_count(self):
        bad = dict(self.GOOD, qas=self.GOOD["qas"][:2])
        with pytest.raises(ValueError):
            bot.parse_model_json(bad and json.dumps(bad))

    def test_caps_overlong_answers(self):
        bad = json.loads(json.dumps(self.GOOD))
        bad["qas"][1]["a"] = "word " * 200
        out = bot.parse_model_json(json.dumps(bad))
        assert len(out["qas"][1][1]) <= bot.ANSWER_CAP + 1


class TestAssembly:
    ENTRY = {"topic": "Amazon S3", "exams": ["AIF", "CCP", "SAA"], "category": "s"}
    CONTENT = {
        "definition": "is AWS's object storage service.",
        "qas": [
            ("What is it used for?", "Storing objects."),
            ("How is it billed?", "Per GB."),
            ("Common mistake?", "Public buckets."),
        ],
        "tip_keywords": ["object storage", "durability", "buckets"],
    }

    def test_exact_skeleton(self, monkeypatch):
        monkeypatch.setattr(bot, "ROLE_ID", "")
        msg = bot.assemble_message(self.ENTRY, self.CONTENT)
        assert msg.startswith("☁️ Daily Cloud Fun Fact: Amazon S3")
        assert "🔶 **__Amazon S3__** is AWS's object storage service." in msg
        assert "Q: What is it used for?\n> A: Storing objects." in msg
        assert "> 📌 Exam Tip (AIF & CCP & SAA): Keywords like" in msg
        assert '"object storage", "durability", or "buckets" = Amazon S3.' in msg
        assert "<@&" not in msg, "no ping line when role unset"

    def test_role_ping_when_configured(self, monkeypatch):
        monkeypatch.setattr(bot, "ROLE_ID", "123456789012345678")
        msg = bot.assemble_message(self.ENTRY, self.CONTENT)
        assert msg.endswith("<@&123456789012345678>")

    def test_message_cap_holds(self, monkeypatch):
        monkeypatch.setattr(bot, "ROLE_ID", "")
        huge = {
            "definition": "is " + "very " * 50 + "big.",
            "qas": [(f"Q{i}?", "A. " * 110) for i in range(3)],
            "tip_keywords": ["k1", "k2", "k3"],
        }
        msg = bot.assemble_message(self.ENTRY, huge)
        assert len(msg) <= bot.MESSAGE_CAP


class TestClip:
    def test_short_text_untouched(self):
        assert bot.clip("hello", 10) == "hello"

    def test_prefers_sentence_boundary(self):
        text = "First sentence is long enough. Second sentence overflows badly."
        out = bot.clip(text, 40)
        assert out == "First sentence is long enough."
