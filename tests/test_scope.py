"""Scope filtering: the 9 AM run is nationwide, the 3 PM run is Florida only."""

import os
import sys

os.environ.setdefault("SEEN_TABLE_NAME", "test-seen")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import career_digest as career


def listing(i, locations):
    return {"id": f"id-{i}", "company_name": "Acme", "title": "SWE",
            "locations": locations, "url": "https://x/1"}


class TestIsFlorida:
    def test_matches_florida_cities(self):
        assert career.is_florida(listing(1, ["Miami, FL"]))
        assert career.is_florida(listing(2, ["Lake Buena Vista, FL"]))
        assert career.is_florida(listing(3, ["Florida"]))
        assert career.is_florida(listing(4, ["Austin, TX", "Tampa, FL"]))

    def test_rejects_non_florida(self):
        assert not career.is_florida(listing(5, ["Seattle, WA"]))
        assert not career.is_florida(listing(6, []))

    def test_remote_is_not_florida(self):
        """'Remote' is US-eligible but not local — the Florida digest exists
        for roles members can take without leaving Miami."""
        assert not career.is_florida(listing(7, ["Remote"]))
        assert not career.is_florida(listing(8, ["Remote in USA"]))

    def test_does_not_match_other_states_containing_fl(self):
        assert not career.is_florida(listing(9, ["Flagstaff, AZ"]))
        assert not career.is_florida(listing(10, ["Fleming Island, NY"]))


class TestScopedHeaders:
    def test_usa_and_florida_headers_differ(self, monkeypatch):
        monkeypatch.setattr(career, "CAREER_ROLE_ID", "")
        picks = {"Internships": [listing(1, ["Miami, FL"])], "New Grad": []}
        assert career.build_messages(picks, "usa")[0].startswith(
            "\U0001f4bc **Daily Career Drops**"
        )
        assert career.build_messages(picks, "florida")[0].startswith(
            "\U0001f334 **Florida Career Drops**"
        )

    def test_unknown_scope_falls_back_to_usa_header(self, monkeypatch):
        monkeypatch.setattr(career, "CAREER_ROLE_ID", "")
        picks = {"Internships": [listing(1, ["Miami, FL"])], "New Grad": []}
        msg = career.build_messages(picks, "mars")[0]
        assert msg.startswith("\U0001f4bc **Daily Career Drops**")


class TestFloridaLocationDisplay:
    def test_florida_row_shows_the_florida_location(self):
        """A listing can be multi-city. In the Florida digest the row must show
        the Florida site, not locations[0] — 'Honolulu, HI' in a Florida digest
        reads as a broken bot."""
        x = listing(1, ["Honolulu, HI", "McLean, VA", "Tampa, FL"])
        career.lead_with_florida(x)
        assert career.format_line(x).split(" — ")[2] == "Tampa, FL"

    def test_leaves_other_locations_intact(self):
        x = listing(2, ["Honolulu, HI", "Tampa, FL"])
        career.lead_with_florida(x)
        assert set(x["locations"]) == {"Honolulu, HI", "Tampa, FL"}

    def test_no_florida_location_is_left_alone(self):
        x = listing(3, ["Seattle, WA", "Austin, TX"])
        career.lead_with_florida(x)
        assert x["locations"][0] == "Seattle, WA"


class TestScopeParsing:
    def test_non_dict_event_does_not_crash(self):
        """EventBridge sends a dict, but a manual invoke can send anything.
        A malformed event must fall back to usa, never take the digest down."""
        for event in (None, [], "florida", 42):
            assert career.scope_from(event) == "usa"

    def test_unknown_scope_falls_back(self):
        assert career.scope_from({"scope": "mars"}) == "usa"
        assert career.scope_from({"scope": None}) == "usa"
        assert career.scope_from({}) == "usa"

    def test_valid_scopes_pass_through(self):
        assert career.scope_from({"scope": "florida"}) == "florida"
        assert career.scope_from({"scope": "usa"}) == "usa"

    def test_scope_is_case_insensitive(self):
        assert career.scope_from({"scope": "Florida"}) == "florida"


class TestMiamiPriority:
    def test_miami_outranks_newer_listings_elsewhere_in_florida(self):
        """A Miami school: locality beats recency inside the Florida digest.
        The 3 PM run on 2026-08-28 posted zero Miami rows because Blackstone
        and BCG were 7-8 days old and lost to fresher Jacksonville listings."""
        miami_old = {**listing(1, ["Miami, FL"]), "date_posted": 1000}
        tampa_new = {**listing(2, ["Tampa, FL"]), "date_posted": 9999}
        ordered = sorted([tampa_new, miami_old], key=career.florida_rank_key)
        assert ordered[0]["id"] == "id-1"

    def test_broward_sits_between_miami_and_the_rest(self):
        miami = {**listing(1, ["Miami, FL"]), "date_posted": 1}
        broward = {**listing(2, ["Fort Lauderdale, FL"]), "date_posted": 1}
        orlando = {**listing(3, ["Orlando, FL"]), "date_posted": 9999}
        ordered = sorted([orlando, broward, miami], key=career.florida_rank_key)
        assert [x["id"] for x in ordered] == ["id-1", "id-2", "id-3"]

    def test_recency_still_breaks_ties_within_a_tier(self):
        older = {**listing(1, ["Miami, FL"]), "date_posted": 100}
        newer = {**listing(2, ["Doral, FL"]), "date_posted": 900}
        ordered = sorted([older, newer], key=career.florida_rank_key)
        assert ordered[0]["id"] == "id-2"

    def test_nationwide_ranking_is_untouched(self):
        miami = {**listing(1, ["Miami, FL"]), "date_posted": 100}
        seattle = {**listing(2, ["Seattle, WA"]), "date_posted": 900}
        ordered = sorted([miami, seattle], key=career.rank_key)
        assert ordered[0]["id"] == "id-2", "USA run still ranks by recency"


class TestLocalityDisplay:
    def test_shows_the_city_it_ranked_on(self):
        x = listing(1, ["Tampa, FL", "Orlando, FL", "Miami, FL"])
        career.lead_with_florida(x)
        assert x["locations"][0] == "Miami, FL"

    def test_falls_back_to_any_florida_city(self):
        x = listing(2, ["Seattle, WA", "Orlando, FL"])
        career.lead_with_florida(x)
        assert x["locations"][0] == "Orlando, FL"


class TestPlanDigestRespectsKey:
    def test_florida_key_puts_miami_at_the_top_of_the_block(self):
        """plan_digest re-sorts internally; without honouring the key it threw
        the Miami-first ordering away and Miami landed mid-list."""
        pool = [
            {**listing(1, ["Tampa, FL"]), "date_posted": 9999, "company_name": "A"},
            {**listing(2, ["Miami, FL"]), "date_posted": 100, "company_name": "B"},
            {**listing(3, ["Orlando, FL"]), "date_posted": 5000, "company_name": "C"},
        ]
        chosen, _, _ = career.plan_digest(
            {"Internships": pool, "New Grad": []}, career.florida_rank_key
        )
        assert chosen["Internships"][0]["id"] == "id-2"

    def test_default_key_is_unchanged(self):
        pool = [
            {**listing(1, ["Tampa, FL"]), "date_posted": 9999, "company_name": "A"},
            {**listing(2, ["Miami, FL"]), "date_posted": 100, "company_name": "B"},
        ]
        chosen, _, _ = career.plan_digest({"Internships": pool, "New Grad": []})
        assert chosen["Internships"][0]["id"] == "id-1", "USA run: newest first"
