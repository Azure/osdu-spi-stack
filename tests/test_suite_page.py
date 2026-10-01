# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""The pages: what a review's rows add up to, one service's page, and the scoreboard."""

import copy
import re

import pytest

from spi.suite_page import (
    EQUAL,
    NEITHER,
    PAGE_SCHEMA,
    UNREAD,
    embedded,
    latest,
    needed,
    read_pages,
    render,
    render_scoreboard,
    score,
)

NOTE = "revisit this later -- Istio is changing the response code"
RUN = {
    "service": "partition",
    "commit": "8e056d4a61429bdd4d4de963ef313e5bf7ca53e6",
    "environment": "dks 0.22.0",
    "generated": "2026-09-28 21:39 UTC",
    "cli": "0.22.0",
}


def _suite(name: str, *tests: tuple[str, str], passed: bool = True) -> dict:
    cases = [{"name": test, "seconds": 1.0, "status": status} for test, status in tests]
    for case in cases:
        if case["status"] == "empty":
            case.update(note=NOTE, declared_in="TestList")
    totals: dict = {key: 0 for key in ("passed", "empty", "failed", "error", "skipped")}
    for case in cases:
        totals[case["status"]] += 1
    return {
        "name": name,
        "passed": passed,
        "verdict": f"{'pass' if passed else 'FAIL'}: {len(cases)} tests, 0 skipped",
        "provenance": "paired",
        "image": "ghcr.io/acme/partition-acceptance@sha256:abc",
        "totals": {**totals, "tests": len(cases), "seconds": float(len(cases))},
        "classes": [{"name": "org.example.TestList", "seconds": 1.0, "tests": cases}],
    }


def _row(name: str, documented: bool = True, **cells: tuple) -> dict:
    operation, _, behavior = name.partition(" :: ")
    suites = {
        suite: {"grade": grade, "tests": [f"TestList.{test}" for test in tests]}
        for suite, (grade, *tests) in cells.items()
    }
    return {
        "id": name,
        "operation": operation,
        "behavior": behavior,
        "documented": documented,
        "suites": suites,
    }


ROWS = [
    _row("GET /partitions :: 200", acceptance=(2, "lists"), integration=(2, "should_list")),
    _row("GET /partitions/{id} :: 200", acceptance=(3, "reads"), integration=(1, "should_read")),
    _row("PATCH /partitions/{id} :: 204", documented=False, integration=(2, "should_patch")),
    _row("GET /partitions :: 401", integration=(0, "should_401", "should_pretend")),
    _row("GET /partitions :: 403"),
]


def _scored(facts: dict) -> dict:
    scored = score(facts)
    assert scored is not None
    return scored


def _facts(review: bool = True, service: str = "partition", generated: str = "") -> dict:
    facts = {
        "schema": PAGE_SCHEMA,
        "run": {**RUN, "service": service, "generated": generated or RUN["generated"]},
        "suites": [
            _suite("acceptance", ("lists", "passed"), ("reads", "passed")),
            _suite(
                "integration",
                ("should_list", "passed"),
                ("should_read", "passed"),
                ("should_patch", "passed"),
                ("should_401", "empty"),
                ("should_pretend", "passed"),
                ("should_blank", "empty"),
            ),
        ],
    }
    if review:
        facts["contract"] = {"source": "https://gw/api/partition/v1/api-docs", "rows": []}
        facts["review"] = {
            "reviewer": "copilot",
            "model": "gpt-5.4",
            "effort": "medium",
            "summary": "Proves reads; proves nothing about callers without a token.",
            "determination": {
                "suites": ["acceptance", "integration"],
                "reason": "Integration guards the writes.",
            },
            "rows": copy.deepcopy(ROWS),
            "findings": [
                {
                    "title": "Auth tests are empty",
                    "severity": "high",
                    "suite": "integration",
                    "detail": "The override drops the assertion.",
                    "tests": ["TestList.should_401"],
                }
            ],
            "gaps": ["No test sends a missing token"],
        }
    return facts


class TestScore:
    def test_a_row_goes_to_the_suite_that_proves_most_about_it(self):
        scored = _scored(_facts())

        assert scored["split"] == {
            EQUAL: 1,
            "acceptance": 1,
            "integration": 1,
            UNREAD: 0,
            NEITHER: 2,
        }
        assert (scored["rows"], scored["documented"]) == (5, 4)
        assert scored["categories"] == {
            "GET /partitions :: 200": EQUAL,
            "GET /partitions/{id} :: 200": "acceptance",
            "PATCH /partitions/{id} :: 204": "integration",
            "GET /partitions :: 401": NEITHER,
            "GET /partitions :: 403": NEITHER,
        }
        assert scored["grades"] == {
            "acceptance": {0: 0, 1: 0, 2: 1, 3: 1},
            "integration": {0: 1, 1: 1, 2: 2, 3: 0},
        }

    def test_hollow_counts_empty_bodies_and_tests_graded_as_proving_nothing_once(self):
        facts = _facts()
        # Graded as proving nothing on one row, and as proving something on another.
        facts["review"]["rows"].append(
            _row("GET /info :: 200", integration=(0, "should_list", "should_401"))
        )

        # should_401 and should_blank are empty; should_pretend is graded 0 and nothing more.
        assert _scored(facts)["hollow"] == {"acceptance": 0, "integration": 3}

    def test_a_test_that_failed_is_not_a_pass_that_proves_nothing(self):
        facts = _facts()
        facts["suites"][1]["classes"][0]["tests"][4]["status"] = "failed"

        # should_pretend is graded 0 on its only row, and it failed.
        assert _scored(facts)["hollow"] == {"acceptance": 0, "integration": 2}

    def test_one_suite_alone_holds_its_rows_or_leaves_them_untested(self):
        facts = _facts()
        facts["suites"] = facts["suites"][:1]

        assert _scored(facts)["split"] == {EQUAL: 0, "acceptance": 2, UNREAD: 0, NEITHER: 3}

    def test_tests_that_could_not_be_read_are_neither_proof_nor_hollow(self):
        facts = _facts()
        facts["review"]["rows"] = [
            _row("GET /info :: 200", acceptance=(None, "reads"), integration=(2, "should_list")),
            _row("GET /info/ :: 200", acceptance=(None, "lists")),
            _row(
                "GET /partitions :: 401", acceptance=(None, "lists"), integration=(0, "should_401")
            ),
        ]

        scored = _scored(facts)

        assert scored["split"] == {
            EQUAL: 0,
            "acceptance": 0,
            "integration": 1,
            UNREAD: 2,
            NEITHER: 0,
        }
        assert scored["unread"] == {"acceptance": 3, "integration": 0}
        assert scored["hollow"] == {"acceptance": 0, "integration": 2}
        assert "S? unread" in render(facts) and "could not be read 2" in render(facts)

    def test_without_a_review_there_is_nothing_to_score(self):
        assert score(_facts(review=False)) is None

    def test_a_review_that_placed_no_test_is_not_scored_and_says_so(self):
        facts = _facts()
        facts["review"]["rows"] = [_row("GET /partitions :: 200"), _row("GET /partitions :: 401")]
        facts["review"]["unrecognized"] = 53

        page = render(facts)

        assert score(facts) is None
        assert "No test could be placed on the contract" in page and "53 tests" in page
        assert "Auth tests are empty" in page
        for absent in ("Contract matrix", "Who protects the contract", "· 100%"):
            assert absent not in page, absent


class TestPage:
    def test_the_page_carries_the_facts_it_was_drawn_from(self):
        facts = _facts()

        page = render(facts)

        assert embedded(page) == facts
        for shown in (
            "Each suite guards what the others cannot",
            "Integration guards the writes.",
            "gpt-5.4, medium effort",
            "takes no part in any verdict",
            "2 · 40%",
            "PATCH /partitions/{id}",
            "Auth tests are empty",
            "No test sends a missing token",
            'id="suite-acceptance"',
            'id="suite-integration"',
            "2 of 6 passing tests have an empty body",
            NOTE,
        ):
            assert shown in page, shown

    def test_suites_that_ran_at_different_commits_are_each_named(self):
        facts = _facts(review=False)
        for suite in facts["suites"]:
            suite["commit"] = RUN["commit"]

        assert "different commits" not in render(facts)

        facts["suites"][1]["commit"] = "2" * 40
        page = render(facts)

        for shown in (
            "suites ran at different commits",
            f"at <code>{RUN['commit'][:12]}</code>",
            "at <code>222222222222</code>",
        ):
            assert shown in page, shown

    def test_a_suite_past_the_palette_still_has_a_color(self):
        names = ["acceptance", "integration", "load", "soak", "smoke", "chaos", "upgrade"]
        facts = _facts()
        facts["suites"] = [_suite(name, ("runs", "passed")) for name in names]
        facts["review"]["findings"] = []
        facts["review"]["rows"] = [
            _row(f"GET /{name} :: 200", True, **{name: (2, "runs")}) for name in names
        ]

        page = render(facts)

        swatch = r'<i class="(s\d+)" style="background:var\(--(\w+)\)"></i>(\w+) stronger'
        swatches = re.findall(swatch, page)
        assert [name for _, _, name in swatches] == names
        for kind, color, name in swatches:
            assert color != "none", name
            assert f".bar .{kind}{{background:var(--{color})}}" in page, name

    def test_a_finding_names_a_test_whose_class_is_cited_in_full(self):
        facts = _facts()
        twin = copy.deepcopy(facts["suites"][1]["classes"][0])
        twin["name"] = "org.other.TestList"
        facts["suites"][1]["classes"].append(twin)
        facts["review"]["findings"][0]["tests"] = ["org.other.TestList.should_401"]

        page = render(facts)

        assert '<td class="test">should_401</td>' in page
        assert '<span class="chip low">org.other.TestList</span>' in page

    def test_a_page_without_a_review_shows_the_runs_and_judges_nothing(self):
        page = render(_facts(review=False))

        assert 'id="suite-integration"' in page and "2 of 6 passing tests" in page
        for absent in ("Contract matrix", "Findings", "Who protects the contract", "Judged by"):
            assert absent not in page, absent

    def test_text_from_a_run_or_a_reviewer_cannot_end_the_page_or_its_facts(self):
        hostile = "</script><script>alert(1)</script>"
        facts = _facts()
        facts["suites"][0]["verdict"] = hostile
        facts["suites"][0]["classes"][0]["tests"][0].update(
            status="failed", message=hostile, type="T", detail=hostile, output=hostile
        )
        facts["suites"][0]["totals"].update(passed=1, failed=1)
        facts["review"]["summary"] = hostile
        facts["review"]["determination"]["reason"] = hostile
        facts["review"]["findings"][0].update(title=hostile, detail=hostile)
        facts["review"]["rows"].append(_row(f"GET /x :: {hostile}", documented=False))
        facts["review"]["gaps"] = [hostile]

        page = render(facts)

        assert hostile not in page
        assert page.count("<script") == 1
        assert embedded(page) == facts

    @pytest.mark.parametrize(
        "page",
        [
            "<html>no facts</html>",
            '<script type="application/json" id="spi-test-facts">{not json</script>',
            '<script type="application/json" id="spi-test-facts">{"schema": 1, "suites": []}</script>',
            '<script type="application/json" id="spi-test-facts">{"schema": 2}</script>',
            '<script type="application/json" id="spi-test-facts">{"schema": 2, "suites": []}</script>',
            '<script type="application/json" id="spi-test-facts">'
            '{"schema": 2, "suites": [], "run": "dks"}</script>',
            '<script type="application/json" id="spi-test-facts">[2]</script>',
        ],
    )
    def test_a_page_this_cli_did_not_write_holds_no_facts(self, page):
        assert embedded(page) is None


@pytest.mark.parametrize(
    ("suites", "ran", "heading"),
    [
        (None, ["acceptance", "integration"], ""),
        ([], ["acceptance", "integration"], "No suite protects this service"),
        (["acceptance"], ["acceptance"], "acceptance is the one suite that ran"),
        (
            ["integration"],
            ["acceptance", "integration"],
            "integration protects this service better",
        ),
        (
            ["acceptance", "integration"],
            ["acceptance", "integration"],
            "Each suite guards what the others cannot",
        ),
        (
            ["acceptance", "load"],
            ["acceptance", "integration", "load"],
            "acceptance and load are needed here",
        ),
        (
            ["acceptance", "integration", "load"],
            ["acceptance", "integration", "load", "smoke"],
            "acceptance, integration and load are needed here",
        ),
    ],
)
def test_the_judgment_names_as_many_suites_as_the_service_needs(suites, ran, heading):
    assert needed({"suites": suites}, ran) == heading


class TestScoreboard:
    def test_each_service_is_scored_from_its_newest_reviewed_page(self):
        reviewed = _facts(generated="2026-09-28 21:00 UTC")
        later = _facts(review=False, generated="2026-09-28 22:00 UTC")
        legal = _facts(review=False, service="legal")
        pages = [("old.html", reviewed), ("new.html", later), ("legal.html", legal)]

        assert latest(pages) == [("legal.html", legal), ("old.html", reviewed)]
        assert latest(pages[1:]) == [("legal.html", legal), ("new.html", later)]
        assert latest([("a.html", reviewed), ("b.html", _facts())])[0][0] == "b.html"

    def test_services_stand_side_by_side_and_add_up(self):
        storage = _facts(service="storage")
        storage["suites"] = storage["suites"][:1]
        storage["review"]["determination"]["suites"] = ["acceptance"]
        legal = _facts(review=False, service="legal")
        pages = [("p.html", _facts()), ("s.html", storage), ("l.html", legal)]

        page = render_scoreboard(pages, RUN)

        for shown in (
            'href="p.html"',
            'href="s.html"',
            'href="l.html"',
            "not reviewed",
            "not run",
            '<span class="chip ai">acceptance + integration</span>',
            '<span class="chip ai">acceptance</span>',
            "All 3",
            # 5 rows each: equal 1+0, acceptance 1+2, integration 1+0, neither 2+3
            "<td class=n>10</td><td class=n>1</td><td class=n>3</td><td class=n>1</td>"
            "<td class=n>5</td>",
            "5 · 50%",
        ):
            assert shown in page, shown
        assert embedded(page) is None

    def test_a_review_that_placed_no_test_is_not_called_unreviewed(self):
        unplaced = _facts()
        for row in unplaced["review"]["rows"]:
            row["suites"] = {}

        # An older page with a score does not stand in for the newer review.
        page = render_scoreboard([("old.html", _facts()), ("p.html", unplaced)], RUN)

        assert "not scored" in page and 'href="old.html"' not in page
        assert "not reviewed" not in page and "--review" not in page

    def test_a_row_from_another_environment_is_named_as_such(self):
        legal = _facts(review=False, service="legal")
        legal["run"]["environment"] = "shared 0.21.0"

        alone = render_scoreboard([("l.html", legal)], RUN)
        both = render_scoreboard([("p.html", _facts()), ("l.html", legal)], RUN)

        assert '<p class="where">shared 0.21.0 &middot; ' in alone
        for shown in (
            '<p class="where">several environments &middot; ',
            "<td>shared 0.21.0 &middot; ",
            "<td>dks 0.22.0 &middot; ",
        ):
            assert shown in both, shown

    def test_pages_are_read_back_from_the_files_this_cli_wrote(self, tmp_path):
        facts = _facts()
        (tmp_path / "spi-test-partition.html").write_text(render(facts))
        (tmp_path / "spi-test-scoreboard.html").write_text(render_scoreboard([], RUN))
        (tmp_path / "spi-test-other.html").write_text("<html>someone else's</html>")
        (tmp_path / "spi-test-bytes.html").write_bytes(b"\xff\xfe")

        paths = sorted(tmp_path.iterdir()) + [tmp_path / "missing.html"]

        assert read_pages(paths) == [("spi-test-partition.html", facts)]
