#!/usr/bin/env python3
"""Score src/prompts/classify_titles.txt against the hand-labelled fixture.

NOT part of CI: it calls Bedrock, so it costs money and needs credentials.
It exists so prompt edits get re-scored instead of argued about.

    python score_prompt.py

Only the relevant-vs-NONE distinction is scored. Which positive label the
model picks (CS vs IT vs SOLNS) does not change what the digest posts, and
the CS/IT boundary is genuinely fuzzy — chasing it is wasted effort.
"""

import json
import os
import pathlib
import sys

os.environ.setdefault("SEEN_TABLE_NAME", "unused")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, str(pathlib.Path(__file__).parent / "src"))

import career_digest as career

cases = json.loads(
    (pathlib.Path(__file__).parent / "tests/fixtures/prompt_cases.json").read_text()
)
listings = [{"company_name": c["company"], "title": c["title"]} for c in cases]
labels = career.classify_titles(listings)

ok = 0
for case, label in zip(cases, labels, strict=True):
    binary = "NONE" if label == "NONE" else "REL"
    hit = binary == case["expect"]
    ok += hit
    mark = "ok  " if hit else "MISS"
    print(f"  {mark} want={case['expect']:4} got={label:6}| {case['company'][:18]:20}| {case['title'][:44]}")
    if not hit:
        print(f"       why it matters: {case['why']}")
print(f"\n{ok}/{len(cases)} on relevant-vs-NONE")
