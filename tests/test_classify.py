"""Offline tests for title classification — Bedrock is mocked, no network, no AWS.

The gate drops listings a Cyber/IT/CS/AI/PM/Solutions undergrad would not apply to.
Every failure path must abandon the run rather than post a partially-classified digest.
"""

import os
import sys

import pytest

os.environ.setdefault("SEEN_TABLE_NAME", "test-seen")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import career_digest as career


def listing(i, company="Acme", title=None):
    return {
        "id": f"id-{i}",
        "company_name": company,
        "title": title or f"Software Engineer {i}",
        "locations": ["Miami, FL"],
        "url": f"https://jobs.example.com/{i}",
    }


class TestClassifyTitles:
    def test_returns_one_label_per_listing(self, monkeypatch):
        monkeypatch.setattr(career, "_converse", lambda text: "1:CS\n2:NONE")
        assert career.classify_titles([listing(1), listing(2)]) == ["CS", "NONE"]

    def test_accepts_every_defined_label(self, monkeypatch):
        labels = ["CYBER", "IT", "CS", "AI", "PM", "SOLNS", "NONE"]
        body = "\n".join(f"{i}:{l}" for i, l in enumerate(labels, 1))
        monkeypatch.setattr(career, "_converse", lambda text: body)
        got = career.classify_titles([listing(i) for i in range(len(labels))])
        assert got == labels

    def test_batches_larger_inputs(self, monkeypatch):
        monkeypatch.setattr(career, "BATCH_SIZE", 2)
        calls = []

        def fake(text):
            calls.append(text)
            n = len(text.strip().splitlines())
            return "\n".join(f"{i}:CS" for i in range(1, n + 1))

        monkeypatch.setattr(career, "_converse", fake)
        got = career.classify_titles([listing(i) for i in range(5)])
        assert got == ["CS"] * 5
        assert len(calls) == 3, "5 listings at BATCH_SIZE=2 is three calls"

    def test_raises_when_a_line_is_missing(self, monkeypatch):
        monkeypatch.setattr(career, "_converse", lambda text: "1:CS")
        with pytest.raises(career.ClassificationError):
            career.classify_titles([listing(1), listing(2)])

    def test_takes_the_first_label_when_the_model_hedges(self, monkeypatch):
        """Observed live: the model answered 'CS/AI'. A single hedged line must
        not abandon the whole digest — take the first valid label it names."""
        monkeypatch.setattr(career, "_converse", lambda text: "1:CS/AI\n2:NONE")
        assert career.classify_titles([listing(1), listing(2)]) == ["CS", "NONE"]

    def test_hedge_that_names_no_valid_label_still_raises(self, monkeypatch):
        monkeypatch.setattr(career, "_converse", lambda text: "1:WIZARD/GOBLIN")
        with pytest.raises(career.ClassificationError):
            career.classify_titles([listing(1)])

    def test_raises_on_unknown_label(self, monkeypatch):
        monkeypatch.setattr(career, "_converse", lambda text: "1:CS\n2:WIZARD")
        with pytest.raises(career.ClassificationError):
            career.classify_titles([listing(1), listing(2)])

    def test_raises_when_bedrock_errors(self, monkeypatch):
        def boom(text):
            raise RuntimeError("throttled")

        monkeypatch.setattr(career, "_converse", boom)
        with pytest.raises(career.ClassificationError):
            career.classify_titles([listing(1)])

    def test_tolerates_prose_and_blank_lines(self, monkeypatch):
        """Small models pad output; parsing must survive it rather than fail closed."""
        monkeypatch.setattr(
            career, "_converse", lambda text: "Here you go:\n\n1:CS\n\n2:AI\n"
        )
        assert career.classify_titles([listing(1), listing(2)]) == ["CS", "AI"]


class TestFilterRelevant:
    def test_returns_only_rejected_ids_for_marking(self, monkeypatch):
        """Only NONE-labelled listings are burned. A relevant listing that
        simply didn't fit stays unseen so it can fill a slot on a later run."""
        monkeypatch.setattr(career, "classify_titles", lambda xs: ["CS", "NONE"])
        kept, rejected = career.filter_relevant([listing(1), listing(2)])
        assert [x["id"] for x in kept] == ["id-1"]
        assert rejected == ["id-2"], "relevant-but-unshown must NOT be marked"

    def test_empty_input_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(career, "classify_titles", lambda xs: [])
        assert career.filter_relevant([]) == ([], [])

    def test_classifies_at_most_classify_limit(self, monkeypatch):
        monkeypatch.setattr(career, "CLASSIFY_LIMIT", 3)
        seen = []

        def fake(xs):
            seen.append(len(xs))
            return ["CS"] * len(xs)

        monkeypatch.setattr(career, "classify_titles", fake)
        kept, rejected = career.filter_relevant([listing(i) for i in range(10)])
        assert seen == [3], "only the top CLASSIFY_LIMIT are sent to the model"
        assert len(kept) == 3
        assert rejected == [], "unclassified listings stay unseen as backlog"


class TestBacklog:
    def test_marks_shown_and_rejected_only(self):
        """The whole point: a relevant listing squeezed out by the caps is NOT
        consumed, so tomorrow it can fill a slot instead of being lost."""
        chosen = {"Internships": [listing(1)], "New Grad": [listing(2)]}
        rejected = {"Internships": ["id-9"], "New Grad": []}
        assert career.ids_to_mark(chosen, rejected) == ["id-1", "id-2", "id-9"]

    def test_a_relevant_listing_that_did_not_fit_is_never_marked(self):
        chosen = {"Internships": [listing(1)], "New Grad": []}
        marked = career.ids_to_mark(chosen, {"Internships": [], "New Grad": []})
        assert "id-2" not in marked, "backlog listing must survive to a later run"
        assert marked == ["id-1"]
