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


class TestApplyLinkLocaleHardening:
    """The zh-CN Blackstone leak was one shape of the same bug. These cover
    the other shapes that actually occur in the live feeds."""

    def test_underscore_locale_segment_is_rewritten(self):
        assert (
            career.english_url("https://x.wd5.myworkdayjobs.com/zh_CN/site/job/A_1")
            == "https://x.wd5.myworkdayjobs.com/en_US/site/job/A_1"
        )

    def test_rewrite_preserves_the_separator_style(self):
        """en_US for underscore tenants, en-US for hyphen ones -- ATSes are
        picky about which form they accept."""
        assert "/en_US/" in career.english_url("https://h/fr_CA/s/job/1")
        assert "/en-US/" in career.english_url("https://h/fr-CA/s/job/1")

    def test_lang_query_parameter_is_rewritten(self):
        assert (
            career.english_url("https://careers.acme.com/job/1?lang=zh-cn")
            == "https://careers.acme.com/job/1?lang=en-us"
        )

    def test_locale_and_language_parameters_are_rewritten(self):
        assert "locale=en_US" in career.english_url("https://h/j?locale=ja_JP")
        assert "language=en" in career.english_url("https://h/j?language=de")

    def test_query_rewrite_keeps_other_parameters_intact(self):
        out = career.english_url("https://h/j?gh_jid=8384705002&lang=fr-ca&src=x")
        assert "gh_jid=8384705002" in out
        assert "src=x" in out
        assert "lang=en-ca" not in out
        assert "lang=en-us" in out

    def test_english_query_values_are_untouched(self):
        for url in (
            "https://www.moveworks.com/us/en/careers?gh_jid=8384705002&lang=en-us",
            "https://h/j?locale=en_US",
        ):
            assert career.english_url(url) == url

    def test_two_letter_tenant_slugs_are_never_treated_as_locales(self):
        """ey=Ernst & Young, ls=Living Spaces, ac=Arrow, au=American
        University. Rewriting these would break 450+ working links."""
        for url in (
            "https://careers.ey.com/ey/job/New-York-Data-Architecture_1",
            "https://livingspaces.wd5.myworkdayjobs.com/ls/job/La-Mirada/FE-Dev-1",
            "https://arrow.wd1.myworkdayjobs.com/ac/job/Purchase-NY/Analyst_R242574",
            "https://american.wd1.myworkdayjobs.com/au/job/DC/Research-Assistant",
            "https://job-boards.greenhouse.io/cc/jobs/5170312008",
        ):
            assert career.english_url(url) == url


class TestForeignLocaleDetector:
    """Normalisation only rewrites shapes we can prove are locales. Anything
    else foreign-looking must surface in CloudWatch, not in Discord."""

    def test_flags_a_locale_that_normalisation_deliberately_skips(self):
        assert (
            career.foreign_locale("https://jobs.example.com/careers/zh-CN/job/1")
            == "zh-CN"
        )

    def test_clean_urls_are_not_flagged(self):
        for url in (
            "https://boards.greenhouse.io/acme/jobs/123",
            "https://careers.ey.com/ey/job/New-York_1",
            "https://x.wd1.myworkdayjobs.com/en-US/site/job/A_1",
        ):
            assert career.foreign_locale(url) is None

    def test_a_normalised_url_is_no_longer_flagged(self):
        bad = "https://blackstone.wd1.myworkdayjobs.com/zh-CN/site/job/A_1"
        assert career.foreign_locale(bad) == "zh-CN"
        assert career.foreign_locale(career.english_url(bad)) is None

    def test_audit_reports_rows_whose_links_are_still_foreign(self):
        chosen = {
            "Internships": [
                listing(1, company="Clean", url="https://boards.greenhouse.io/a/1"),
                listing(2, company="Odd", url="https://h/careers/zh-CN/job/1"),
            ],
            "New Grad": [listing(3, company="AlsoOdd", url="https://h/x/ja_JP/j/2")],
        }
        assert career.audit_links(chosen) == [
            ("Odd", "zh-CN", "https://h/careers/zh-CN/job/1"),
            ("AlsoOdd", "ja_JP", "https://h/x/ja_JP/j/2"),
        ]

    def test_audit_is_silent_when_every_link_is_english(self):
        chosen = {"Internships": [listing(1)], "New Grad": [listing(2)]}
        assert career.audit_links(chosen) == []

    def test_audit_sees_links_as_rendered_not_as_fetched(self):
        """A link english_url can fix must not be reported as a problem."""
        chosen = {"Internships": [listing(1, url="https://h/zh-CN/site/job/1")]}
        assert career.audit_links(chosen) == []


class TestCloudSecurityFloor:
    """Cloud and security roles are the club's identity and the feed's rarest
    rows: production runs label 2-5 IT/CYBER listings against 20-32 CS and
    8-21 AI ones. Selection is newest-first, so without a reserved seat they
    lose all 8 slots to fresher SWE postings roughly three days in four."""

    def _pool(self, section, count, label=None, start=0):
        rows = []
        for i in range(count):
            row = listing(start + i, company=f"Co{start + i}")
            if label:
                row["_label"] = label
            rows.append(row)
        return {section: rows, "New Grad" if section == "Internships" else "Internships": []}

    def test_cloud_role_survives_a_flood_of_newer_swe_rows(self):
        """The bug: an IT row posted yesterday is buried by 30 CS rows posted
        today, and never reaches the digest."""
        cloud = listing(999, company="Datadog", posted_ago_days=9,
                        title="Cloud Infrastructure Engineer Intern")
        cloud["_label"] = "IT"
        swe = [listing(i, company=f"Co{i}", posted_ago_days=1) for i in range(30)]
        for row in swe:
            row["_label"] = "CS"
        chosen, _, _ = career.plan_digest(
            {"Internships": [*swe, cloud], "New Grad": []}
        )
        assert cloud in chosen["Internships"]

    def test_security_role_survives_the_same_flood(self):
        cyber = listing(999, company="Verkada", posted_ago_days=9,
                        title="Security Engineer Intern")
        cyber["_label"] = "CYBER"
        swe = [listing(i, company=f"Co{i}", posted_ago_days=1) for i in range(30)]
        for row in swe:
            row["_label"] = "CS"
        chosen, _, _ = career.plan_digest(
            {"Internships": [*swe, cyber], "New Grad": []}
        )
        assert cyber in chosen["Internships"]

    def test_reserves_at_most_the_floor(self):
        """A floor, never a takeover. An IT-heavy day must not push every
        other label out of the digest."""
        cloud = []
        for i in range(8):
            row = listing(900 + i, company=f"Cloud{i}", posted_ago_days=9,
                          title="DevOps Engineer Intern")
            row["_label"] = "IT"
            cloud.append(row)
        swe = [listing(i, company=f"Co{i}", posted_ago_days=1) for i in range(30)]
        for row in swe:
            row["_label"] = "CS"
        chosen, _, _ = career.plan_digest(
            {"Internships": [*swe, *cloud], "New Grad": []}
        )
        promoted = [x for x in chosen["Internships"] if x["_label"] == "IT"]
        assert len(promoted) == career.LABEL_FLOOR

    def test_promotion_keeps_rank_order_among_promoted_rows(self):
        """Two reserved seats go to the two BEST cloud rows, not any two."""
        newer = listing(901, company="Newer", posted_ago_days=2,
                        title="Cloud Engineer")
        older = listing(902, company="Older", posted_ago_days=20,
                        title="Site Reliability Engineer")
        oldest = listing(903, company="Oldest", posted_ago_days=30,
                         title="Platform Engineer")
        for row in (newer, older, oldest):
            row["_label"] = "IT"
        swe = [listing(i, company=f"Co{i}", posted_ago_days=1) for i in range(30)]
        for row in swe:
            row["_label"] = "CS"
        chosen, _, _ = career.plan_digest(
            {"Internships": [*swe, oldest, older, newer], "New Grad": []}
        )
        promoted = [x for x in chosen["Internships"] if x["_label"] == "IT"]
        assert promoted == [newer, older]

    def test_promotion_only_displaces_the_last_rows(self):
        """Reserving seats must cost the rows nearest the cut, never the top
        of the digest — an AWS-starred row still leads."""
        aws = listing(500, company="Amazon Web Services", posted_ago_days=1)
        aws["_label"] = "CS"
        cloud = listing(999, company="Datadog", posted_ago_days=9)
        cloud["_label"] = "IT"
        swe = [listing(i, company=f"Co{i}", posted_ago_days=1) for i in range(30)]
        for row in swe:
            row["_label"] = "CS"
        chosen, _, _ = career.plan_digest(
            {"Internships": [aws, *swe, cloud], "New Grad": []}
        )
        assert chosen["Internships"][0] is aws

    def test_unlabelled_pools_are_untouched(self):
        """Florida dry runs and older fixtures carry no _label; selection must
        behave exactly as before rather than raising."""
        unseen = {
            "Internships": [listing(i, company=f"Co{i}") for i in range(30)],
            "New Grad": [],
        }
        chosen, _, _ = career.plan_digest(unseen)
        assert len(chosen["Internships"]) == career.TOTAL_CAP

    def test_floor_does_not_invent_rows(self):
        """No cloud rows in the pool -> the digest is unchanged, not short."""
        swe = [listing(i, company=f"Co{i}") for i in range(30)]
        for row in swe:
            row["_label"] = "CS"
        chosen, _, _ = career.plan_digest({"Internships": swe, "New Grad": []})
        assert len(chosen["Internships"]) == career.TOTAL_CAP

    def test_company_cap_still_wins_over_the_floor(self):
        """One employer must not take both reserved seats with two cloud reqs."""
        rows = []
        for i in range(4):
            row = listing(900 + i, company="Deloitte", posted_ago_days=9,
                          title="Cyber Software Engineering Analyst")
            row["_label"] = "CYBER"
            rows.append(row)
        swe = [listing(i, company=f"Co{i}", posted_ago_days=1) for i in range(30)]
        for row in swe:
            row["_label"] = "CS"
        chosen, _, _ = career.plan_digest(
            {"Internships": [*swe, *rows], "New Grad": []}
        )
        deloitte = [x for x in chosen["Internships"] if x["company_name"] == "Deloitte"]
        assert len(deloitte) == career.COMPANY_CAP


class TestReservedSeatCorroboration:
    """A reserved seat is the one place a wrong label does real damage: it
    converts a coin-flip classification into a guaranteed slot. Live runs put
    'Notability - Backend Engineer' (IT) and 'Cogent Security - Forward
    Deployed Agent Engineer' (CYBER) in reserved seats -- both violations of
    rules already in the prompt. The title must corroborate the label before a
    row can claim a seat; uncorroborated rows still compete normally on rank."""

    def _flood(self):
        rows = [listing(i, company=f"Co{i}", posted_ago_days=1) for i in range(30)]
        for row in rows:
            row["_label"] = "CS"
        return rows

    def _priority(self, title, company="Somewhere", label="IT", days=9):
        row = listing(999, company=company, posted_ago_days=days, title=title)
        row["_label"] = label
        return row

    def test_backend_engineer_labelled_it_gets_no_reserved_seat(self):
        row = self._priority("Backend Engineer", company="Notability")
        chosen, _, _ = career.plan_digest(
            {"Internships": [*self._flood(), row], "New Grad": []}
        )
        assert row not in chosen["Internships"]

    def test_employer_name_alone_does_not_corroborate(self):
        """The classifier's own first rule: never classify by employer."""
        row = self._priority(
            "Forward Deployed Agent Engineer - Early Career",
            company="Cogent Security",
            label="CYBER",
        )
        chosen, _, _ = career.plan_digest(
            {"Internships": [*self._flood(), row], "New Grad": []}
        )
        assert row not in chosen["Internships"]

    @pytest.mark.parametrize(
        "title",
        [
            "DevOps Engineer",
            "Cloud Engineer - Platform",
            "Site Reliability Engineer",
            "Software Engineer Intern - Infrastructure Engineering",
            "Systems Administrator",
            "Network Engineer",
        ],
    )
    def test_real_cloud_titles_take_the_seat(self, title):
        row = self._priority(title)
        chosen, _, _ = career.plan_digest(
            {"Internships": [*self._flood(), row], "New Grad": []}
        )
        assert row in chosen["Internships"]

    @pytest.mark.parametrize(
        "title",
        [
            "Security Engineer",
            "Cybersecurity Analyst Intern",
            "Information Security Intern",
            "Application Security Engineer",
            "Identity and Access Management Analyst",
            "Threat Detection Engineer",
        ],
    )
    def test_real_security_titles_take_the_seat(self, title):
        row = self._priority(title, label="CYBER")
        chosen, _, _ = career.plan_digest(
            {"Internships": [*self._flood(), row], "New Grad": []}
        )
        assert row in chosen["Internships"]

    def test_uncorroborated_row_does_not_consume_the_floor(self):
        """A mislabelled row sitting in the natural top 8 must not spend a
        reserved seat -- otherwise it blocks the genuine cloud role behind it."""
        impostor = listing(500, company="Notability", posted_ago_days=1,
                           title="Backend Engineer")
        impostor["_label"] = "IT"
        real = self._priority("Cloud Security Engineer", company="Datadog",
                              label="CYBER", days=9)
        chosen, _, _ = career.plan_digest(
            {"Internships": [impostor, *self._flood(), real], "New Grad": []}
        )
        assert real in chosen["Internships"]

    def test_title_alone_is_not_enough_without_the_label(self):
        """Corroboration is an AND. Nova rejecting a row still governs --
        a physical 'Security Installer' is NONE and never reaches selection,
        but a CS-labelled row must not seize a seat on a keyword either."""
        row = self._priority("Software Engineer - Cloud Infrastructure",
                             label="CS")
        chosen, _, _ = career.plan_digest(
            {"Internships": [*self._flood(), row], "New Grad": []}
        )
        assert row not in chosen["Internships"]
