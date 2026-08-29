"""Offline tests for the career digest — no AWS, no network. They pin the
selection logic (caps, redistribution, AWS-first ranking), the dedup
semantics (everything unseen gets marked, shown or not), the undergrad
filter, and the Discord length guard."""

import os
import sys
import time
from typing import ClassVar

import pytest

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
            "Internships": [listing(i, company=f"Co{i}") for i in range(30)],
            "New Grad": [listing(100 + i, company=f"Ng{i}") for i in range(10)],
        }
        chosen, to_mark, more = career.plan_digest(unseen)
        assert len(chosen["Internships"]) == 8
        assert len(chosen["New Grad"]) == 8
        assert len(to_mark) == 40
        assert more == 40 - career.TOTAL_CAP

    def test_spare_capacity_flows_both_directions(self):
        # New Grad short -> Internships takes the slack, and vice versa.
        chosen, _, _ = career.plan_digest(
            {
                "Internships": [listing(i, company=f"Co{i}") for i in range(30)],
                "New Grad": [],
            }
        )
        assert len(chosen["Internships"]) == career.TOTAL_CAP
        chosen, _, _ = career.plan_digest(
            {
                "Internships": [],
                "New Grad": [listing(i, company=f"Ng{i}") for i in range(30)],
            }
        )
        assert len(chosen["New Grad"]) == career.TOTAL_CAP

    def test_marks_everything_unseen_even_unshown(self):
        unseen = {
            "Internships": [listing(i, company=f"Co{i}") for i in range(15)],
            "New Grad": [],
        }
        _, to_mark, _ = career.plan_digest(unseen)
        assert set(to_mark) == {f"id-{i}" for i in range(15)}


    @pytest.mark.parametrize("section", ["Internships", "New Grad"])
    def test_at_most_one_listing_per_company(self, section):
        """Disney flooded a real digest with 4 near-identical rows; one company
        must never take more than COMPANY_CAP slots. Both sections are capped
        independently — a regression in either is a regression."""
        flooded = [listing(i, company="Disney") for i in range(6)]
        others = [listing(100 + i, company=f"Co{i}") for i in range(6)]
        unseen = {"Internships": [], "New Grad": []}
        unseen[section] = flooded + others
        chosen, _, _ = career.plan_digest(unseen)
        companies = [x["company_name"] for x in chosen[section]]
        assert companies.count("Disney") == career.COMPANY_CAP
        assert len(companies) == len(set(companies)), "one row per company"

    def test_company_cap_does_not_shrink_what_gets_marked_seen(self):
        """Rows suppressed by the company cap must still be marked seen, or
        they return on every future run forever."""
        unseen = {
            "Internships": [listing(i, company="Disney") for i in range(6)],
            "New Grad": [],
        }
        _, to_mark, _ = career.plan_digest(unseen)
        assert set(to_mark) == {f"id-{i}" for i in range(6)}


class TestDigestMessage:
    CHOSEN: ClassVar[dict] = {
        "Internships": [listing(1, company="Amazon"), listing(2, company="Stripe")],
        "New Grad": [listing(3, company="Datadog")],
    }

    def test_skeleton_and_star(self, monkeypatch):
        monkeypatch.setattr(career, "CAREER_ROLE_ID", "")
        msgs = career.build_messages(dict(self.CHOSEN))
        assert len(msgs) == 2, "one message per populated section"
        msg = "\n".join(msgs)
        assert msgs[0].startswith("💼 **National Career Drops**\n")
        assert "new today" not in msg, "no counts in the header"
        assert "**Internships**" in msgs[0] and "**New Grad**" in msgs[1]
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
        assert msgs[0].startswith("💼 **National Career Drops**")
        assert "Career Drops" not in "".join(msgs[1:]), "header once"


    def test_new_grad_section_survives_a_full_internship_block(self, monkeypatch):
        """Regression: sections were flowed into one shared message, so a full
        Internships block consumed the whole char budget and the entire New Grad
        section — header and rows — was silently dropped and marked seen."""
        monkeypatch.setattr(career, "CAREER_ROLE_ID", "")
        big = {
            "Internships": [
                listing(i, company="C" * 25, title="T" * 60, url="https://x/" + "u" * 110)
                for i in range(8)
            ],
            "New Grad": [
                listing(100 + i, company="D" * 25, title="G" * 60, url="https://y/" + "v" * 110)
                for i in range(8)
            ],
        }
        msgs = career.build_messages(big)
        joined = "\n".join(msgs)
        assert "**Internships**" in joined, "internships block missing"
        assert "**New Grad**" in joined, "new grad block starved by internships"
        assert all(len(m) <= career.MESSAGE_CAP for m in msgs)

    def test_empty_section_produces_no_orphan_header(self, monkeypatch):
        monkeypatch.setattr(career, "CAREER_ROLE_ID", "")
        msgs = career.build_messages(
            {"Internships": [listing(1)], "New Grad": []}
        )
        joined = "\n".join(msgs)
        assert "**Internships**" in joined
        assert "**New Grad**" not in joined


    def test_section_with_no_room_for_any_row_is_omitted(self, monkeypatch):
        """A row too long to fit must not leave a header-only message behind."""
        monkeypatch.setattr(career, "CAREER_ROLE_ID", "")
        monkeypatch.setattr(career, "MESSAGE_CAP", 60)
        msgs = career.build_messages(
            {
                "Internships": [listing(1, url="https://x/" + "u" * 300)],
                "New Grad": [listing(2, url="https://y/" + "v" * 300)],
            }
        )
        for m in msgs:
            assert "[apply](" in m, f"header-only message with no listings: {m!r}"


class TestOtherPostingsCount:
    def test_row_shows_count_of_a_companys_suppressed_postings(self):
        row = career.format_line(listing(1, company="Disney", _suppressed=25))
        assert row.endswith("*(+25 more)*")
        assert "**Disney**" in row

    def test_row_unchanged_when_company_has_only_one_posting(self):
        assert "more)*" not in career.format_line(listing(1, company="Disney"))
        assert "more)*" not in career.format_line(
            listing(2, company="Disney", _suppressed=0)
        )

    def test_plan_digest_annotates_how_many_were_suppressed(self):
        unseen = {
            "Internships": [listing(i, company="Disney") for i in range(6)]
            + [listing(100, company="Solo")],
            "New Grad": [],
        }
        chosen, _, _ = career.plan_digest(unseen)
        by_company = {x["company_name"]: x for x in chosen["Internships"]}
        assert by_company["Disney"]["_suppressed"] == 5
        assert by_company["Solo"]["_suppressed"] == 0

    def test_suffix_counts_against_the_message_cap(self, monkeypatch):
        """The suffix must go through the same fit check as the row itself."""
        monkeypatch.setattr(career, "CAREER_ROLE_ID", "")
        picks = [listing(i, company=f"Co{i}", _suppressed=99) for i in range(8)]
        msgs = career.build_messages({"Internships": picks, "New Grad": []})
        assert all(len(m) <= career.MESSAGE_CAP for m in msgs)


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


class TestApplyLinkLocale:
    """Simplify sometimes captures an ATS URL with the scraper's own locale
    baked into the path, so the apply page renders in a foreign language.
    A Blackstone Miami role opened entirely in Simplified Chinese."""

    def test_chinese_locale_is_rewritten_to_english(self):
        assert career.english_url(
            "https://blackstone.wd1.myworkdayjobs.com/zh-CN/Blackstone_Campus_Careers/job/Miami/X_45021"
        ) == (
            "https://blackstone.wd1.myworkdayjobs.com/en-US/Blackstone_Campus_Careers/job/Miami/X_45021"
        )

    def test_french_canadian_locale_is_rewritten(self):
        assert (
            career.english_url("https://rtx.wd1.myworkdayjobs.com/fr-CA/rec_ext/job/A_1")
            == "https://rtx.wd1.myworkdayjobs.com/en-US/rec_ext/job/A_1"
        )

    def test_english_locales_are_left_alone(self):
        for url in (
            "https://x.wd1.myworkdayjobs.com/en-US/site/job/A_1",
            "https://x.wd1.myworkdayjobs.com/en-CA/site/job/A_1",
        ):
            assert career.english_url(url) == url

    def test_urls_without_a_locale_segment_are_untouched(self):
        for url in (
            "https://boards.greenhouse.io/acme/jobs/123",
            "https://blackstone.wd1.myworkdayjobs.com/bx_external_site/job/Miami/X_1",
            "",
        ):
            assert career.english_url(url) == url

    def test_locale_is_only_matched_as_the_first_path_segment(self):
        url = "https://jobs.example.com/careers/zh-CN/job/1"
        assert career.english_url(url) == url

    def test_rendered_row_carries_the_english_link(self):
        row = career.format_line(
            listing(1, url="https://x.wd1.myworkdayjobs.com/zh-CN/site/job/A_1")
        )
        assert "/en-US/" in row
        assert "zh-CN" not in row
