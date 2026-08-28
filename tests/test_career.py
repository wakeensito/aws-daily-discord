"""Offline tests for the career digest — no AWS, no network. They pin the
selection logic (caps, redistribution, AWS-first ranking), the dedup
semantics (everything unseen gets marked, shown or not), the undergrad
filter, and the Discord length guard."""

import os
import sys
import time
from typing import ClassVar

os.environ.setdefault("SEEN_TABLE_NAME", "test-seen")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import career_digest as career

NOW = time.time()


def listing(i, company="Acme", posted_ago_days=1, degrees=None, **kw):
    base = {
        "id": f"id-{i}",
        "company_name": company,
        "title": f"SWE Intern {i}",
        "locations": ["Miami, FL"],
        "url": f"https://jobs.example.com/{i}",
        "active": True,
        "is_visible": True,
        "date_posted": NOW - posted_ago_days * 86400,
        "degrees": degrees or [],
    }
    base.update(kw)
    return base


class TestEligibility:
    def test_active_recent_bachelors_passes(self):
        assert career.is_eligible(listing(1, degrees=["Bachelor's"]), NOW)
        assert career.is_eligible(listing(2), NOW)  # empty degrees = open

    def test_advanced_degree_only_is_dropped(self):
        assert not career.is_eligible(listing(3, degrees=["Master's", "PhD"]), NOW)

    def test_inactive_hidden_or_stale_dropped(self):
        assert not career.is_eligible(listing(4, active=False), NOW)
        assert not career.is_eligible(listing(5, is_visible=False), NOW)
        assert not career.is_eligible(listing(6, posted_ago_days=30), NOW)

    def test_usa_only(self):
        drop = listing(7, locations=["Vancouver, BC, Canada"])
        assert not career.is_eligible(drop, NOW)
        assert not career.is_eligible(listing(8, locations=["London, UK"]), NOW)
        keep_multi = listing(9, locations=["Toronto, ON, Canada", "Austin, TX"])
        assert career.is_eligible(keep_multi, NOW)
        assert career.is_eligible(listing(10, locations=["Remote in USA"]), NOW)
        assert career.is_eligible(listing(11, locations=["Remote"]), NOW)
        assert career.is_eligible(listing(12, locations=["NYC"]), NOW)
        assert career.is_eligible(listing(13, locations=[]), NOW)  # unknown: keep


class TestRanking:
    def test_aws_flavored_sorts_first_then_newest(self):
        pool = [
            listing(1, company="Stripe", posted_ago_days=0.1),
            listing(2, company="Amazon Web Services", posted_ago_days=5),
            listing(3, company="Google", posted_ago_days=2),
        ]
        ordered = sorted(pool, key=career.rank_key)
        assert [x["company_name"] for x in ordered] == [
            "Amazon Web Services",
            "Stripe",
            "Google",
        ]


class TestPlanDigest:
    def test_caps_and_leftover_counting(self):
        unseen = {
            "Internships": [listing(i) for i in range(30)],
            "New Grad": [listing(100 + i) for i in range(10)],
        }
        chosen, to_mark, more = career.plan_digest(unseen)
        assert len(chosen["Internships"]) == 12
        assert len(chosen["New Grad"]) == 6
        assert len(to_mark) == 40
        assert more == 40 - career.TOTAL_CAP

    def test_spare_capacity_flows_both_directions(self):
        # New Grad short -> Internships takes the slack, and vice versa.
        chosen, _, _ = career.plan_digest(
            {"Internships": [listing(i) for i in range(30)], "New Grad": []}
        )
        assert len(chosen["Internships"]) == career.TOTAL_CAP
        chosen, _, _ = career.plan_digest(
            {"Internships": [], "New Grad": [listing(i) for i in range(30)]}
        )
        assert len(chosen["New Grad"]) == career.TOTAL_CAP

    def test_marks_everything_unseen_even_unshown(self):
        unseen = {"Internships": [listing(i) for i in range(15)], "New Grad": []}
        _, to_mark, _ = career.plan_digest(unseen)
        assert set(to_mark) == {f"id-{i}" for i in range(15)}


class TestDigestMessage:
    CHOSEN: ClassVar[dict] = {
        "Internships": [listing(1, company="Amazon"), listing(2, company="Stripe")],
        "New Grad": [listing(3, company="Datadog")],
    }

    def test_skeleton_and_star(self, monkeypatch):
        monkeypatch.setattr(career, "CAREER_ROLE_ID", "")
        msgs = career.build_messages(dict(self.CHOSEN))
        assert len(msgs) == 1
        msg = msgs[0]
        assert msg.startswith("💼 **Daily Career Drops**\n")
        assert "new today" not in msg, "no counts in the header"
        assert "**Internships**" in msg and "**New Grad**" in msg
        assert "⭐ **Amazon**" in msg and "• **Stripe**" in msg
        assert "[apply](https://jobs.example.com/1)" in msg
        # Deliberately no outbound footer — the digest keeps members here.
        assert "github.com/SimplifyJobs" not in msg
        assert "more today" not in msg
        assert "<@&" not in msg, "no ping by default"

    def test_role_ping_only_on_last_chunk(self, monkeypatch):
        monkeypatch.setattr(career, "CAREER_ROLE_ID", "987654321098765432")
        big = {
            "Internships": [
                listing(i, title="T" * 70, url="https://x/" + "u" * 150)
                for i in range(12)
            ],
            "New Grad": [
                listing(100 + i, title="T" * 70, url="https://x/" + "u" * 150)
                for i in range(6)
            ],
        }
        msgs = career.build_messages(big)
        assert len(msgs) == career.MAX_MESSAGES
        assert msgs[-1].endswith("<@&987654321098765432>")
        assert "<@&" not in "".join(msgs[:-1]), "ping only once"

    def test_chunks_stay_under_cap_and_header_once(self, monkeypatch):
        monkeypatch.setattr(career, "CAREER_ROLE_ID", "")
        big = {
            "Internships": [
                listing(i, company="C" * 40, title="T" * 70, url="https://x/" + "u" * 150)
                for i in range(12)
            ],
            "New Grad": [
                listing(100 + i, company="C" * 40, title="T" * 70) for i in range(6)
            ],
        }
        msgs = career.build_messages(big)
        assert 1 <= len(msgs) <= career.MAX_MESSAGES
        assert all(len(m) <= career.MESSAGE_CAP for m in msgs)
        assert msgs[0].startswith("💼 **Daily Career Drops**")
        assert "Daily Career Drops" not in "".join(msgs[1:]), "header once"


class TestEmbedSuppression:
    def test_career_payload_suppresses_link_previews(self):
        import discord_client as dc

        payload = dc.build_payload("msg", role_id="1", suppress_embeds=True)
        assert payload["flags"] == dc.SUPPRESS_EMBEDS
        assert payload["allowed_mentions"]["roles"] == ["1"]

    def test_fun_fact_payload_unchanged(self):
        import discord_client as dc

        payload = dc.build_payload("msg")
        assert "flags" not in payload
