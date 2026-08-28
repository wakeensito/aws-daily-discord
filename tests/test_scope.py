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
