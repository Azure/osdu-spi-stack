# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""The suite review: what the reviewer may read, how it runs, and what is kept of its answer."""

import json
import subprocess
from pathlib import Path

import pytest

from spi import suite_review
from spi.suite_review import (
    REVIEWERS,
    ReviewUnavailable,
    add_suite,
    choose_reviewer,
    known_tests,
    parse_review,
    review_suites,
)


def _suite(*tests: tuple[str, str]) -> dict:
    cases = [{"name": name, "seconds": 1.0, "status": status} for name, status in tests]
    return {"totals": {}, "classes": [{"name": "org.example.api.TestList", "tests": cases}]}


ACCEPTANCE = _suite(("reads_list", "passed"))
INTEGRATION = _suite(("should_return200", "passed"), ("should_return401", "empty"))
INTEGRATION["classes"][0]["tests"] += [
    {"name": "inherited", "seconds": 1.0, "status": "passed", "outside": "TestBase"},
    {"name": "skipped_here", "seconds": 0.0, "status": "skipped"},
    {"name": "fails_here", "seconds": 1.0, "status": "failed"},
]
INTEGRATION["classes"].append(
    {
        "name": "org.example.api.TokenTest",
        "tests": [{"name": "should_x", "seconds": 1.0, "status": "passed"}],
    }
)
SUITES = {"acceptance": ACCEPTANCE, "integration": INTEGRATION}
RAN = {name: known_tests(facts) for name, facts in SUITES.items()}
LIST_200 = "GET /partitions :: 200"
LIST_401 = "GET /partitions :: 401"
CONTRACT = [
    {"id": LIST_200, "operation": "GET /partitions", "behavior": "200"},
    {"id": LIST_401, "operation": "GET /partitions", "behavior": "401"},
]


def _answer(**fields) -> str:
    body = {"summary": "Proves the list answers.", "rows": [], "findings": [], "gaps": []}
    body.update(fields)
    return json.dumps(body)


def _finding(**fields) -> dict:
    finding = {"title": "Status only", "severity": "high", "suite": "integration", "detail": "d"}
    return {**finding, "tests": ["TestList.should_return200"], **fields}


def _row(name: str, **suites) -> dict:
    return {"id": name, "suites": suites}


def _cell(grade, *tests: str) -> dict:
    return {"grade": grade, "tests": [f"TestList.{test}" for test in tests]}


def _parse(answer: str, secrets=()) -> dict:
    return parse_review(answer, RAN, CONTRACT, secrets)


class TestBundle:
    def test_the_reviewer_is_given_sources_and_facts_and_never_the_reports(self, tmp_path):
        suite = tmp_path / "suite"
        for relative in (
            "module/src/test/java/p/TestList.java",
            "module/src/test/resources/list.feature",
            "module/target/surefire-reports/TEST-p.TestList.xml",
            "module/target/classes/p/Generated.java",
            "module/pom.xml",
        ):
            (suite / relative).parent.mkdir(parents=True, exist_ok=True)
            (suite / relative).write_text("Bearer raw-token")

        add_suite(tmp_path / "bundle", "integration", suite, INTEGRATION)

        bundle = tmp_path / "bundle"
        files = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()}
        assert files == {
            "suites/integration/facts.json",
            "suites/integration/src/module/src/test/java/p/TestList.java",
            "suites/integration/src/module/src/test/resources/list.feature",
        }
        assert json.loads((bundle / "suites/integration/facts.json").read_text()) == INTEGRATION


class TestRows:
    def test_every_contract_row_comes_back_and_added_rows_follow(self):
        added = "PATCH /partitions/{id} :: value read back"
        answer = _answer(
            rows=[
                _row(added, integration=_cell(3, "should_return200")),
                _row(
                    LIST_200,
                    acceptance=_cell(2, "reads_list"),
                    integration=_cell("1", "should_return200"),
                ),
            ]
        )

        rows = _parse(answer)["rows"]

        assert [(row["id"], row["documented"]) for row in rows] == [
            (LIST_200, True),
            (LIST_401, True),
            (added, False),
        ]
        assert rows[0]["suites"] == {
            "acceptance": {"grade": 2, "tests": ["TestList.reads_list"]},
            "integration": {"grade": 1, "tests": ["TestList.should_return200"]},
        }
        assert rows[1]["suites"] == {}
        assert (rows[2]["operation"], rows[2]["behavior"]) == (
            "PATCH /partitions/{id}",
            "value read back",
        )

    @pytest.mark.parametrize(
        ("cell", "kept"),
        [
            (_cell(3, "should_return401"), {"grade": 0, "tests": ["TestList.should_return401"]}),
            (
                _cell(2, "should_return401", "should_return200"),
                {"grade": 2, "tests": ["TestList.should_return401", "TestList.should_return200"]},
            ),
            (_cell(9, "should_return200"), {"grade": 3, "tests": ["TestList.should_return200"]}),
            (_cell(-1, "should_return200"), {"grade": 0, "tests": ["TestList.should_return200"]}),
            (
                _cell("strong", "should_return200"),
                {"grade": 0, "tests": ["TestList.should_return200"]},
            ),
            (_cell(0, "inherited"), {"grade": None, "tests": ["TestList.inherited"]}),
            (
                _cell(2, "inherited", "should_return401"),
                {"grade": None, "tests": ["TestList.inherited", "TestList.should_return401"]},
            ),
            (
                _cell(None, "should_return200"),
                {"grade": None, "tests": ["TestList.should_return200"]},
            ),
            (
                _cell(2, "inherited", "should_return200"),
                {"grade": 2, "tests": ["TestList.inherited", "TestList.should_return200"]},
            ),
            (_cell(3, "skipped_here"), None),
            (
                _cell(2, "skipped_here", "fails_here"),
                {"grade": 2, "tests": ["TestList.fails_here"]},
            ),
            (
                {"grade": float("inf"), "tests": ["TestList.should_return200"]},
                {"grade": 0, "tests": ["TestList.should_return200"]},
            ),
            (_cell(2, "invented"), None),
            (_cell(2, "reads_list"), None),
            ({"grade": 2}, None),
            ("S2", None),
        ],
        ids=[
            "only-empty-tests",
            "empty-beside-a-real-test",
            "above-the-scale",
            "below-the-scale",
            "not-a-number",
            "only-a-body-outside-the-suite",
            "outside-and-empty",
            "reviewer-could-not-read",
            "outside-beside-a-test-that-was-read",
            "only-a-test-that-was-skipped",
            "a-failed-test-still-asserts",
            "a-grade-past-any-number",
            "test-that-did-not-run",
            "test-of-another-suite",
            "no-tests",
            "not-a-cell",
        ],
    )
    def test_a_grade_stands_only_on_tests_that_ran_and_can_prove_it(self, cell, kept):
        rows = _parse(_answer(rows=[_row(LIST_401, integration=cell)]))["rows"]

        assert rows[1]["suites"] == ({"integration": kept} if kept else {})

    @pytest.mark.parametrize(
        "cited",
        [
            "TestList.should_return200",
            "org.example.api.TestList.should_return200",
            "TestList#should_return200",
            "TestList::should_return200",
            "TestList.should_return200()",
        ],
    )
    def test_a_test_is_known_by_the_ways_a_reviewer_writes_it(self, cited):
        answer = _answer(
            rows=[_row(LIST_200, integration={"grade": 2, "tests": [cited, cited]})],
            findings=[_finding(tests=[cited])],
        )

        review = _parse(answer)

        assert review["rows"][0]["suites"]["integration"]["tests"] == ["TestList.should_return200"]
        assert review["findings"][0]["tests"] == ["TestList.should_return200"]
        assert review["unrecognized"] == 0

    def test_a_name_that_reads_as_a_credentials_is_still_the_tests_name(self):
        row = "GET /partitions :: rejects token: expired401"
        answer = _answer(
            rows=[
                _row(row, integration={"grade": 1, "tests": ["TokenTest::should_x"]}),
            ],
            findings=[_finding(tests=["TokenTest::should_x"])],
        )

        review = _parse(answer, ["minted-bearer-value"])

        assert review["rows"][-1]["id"] == row
        assert review["rows"][-1]["suites"]["integration"]["tests"] == ["TokenTest.should_x"]
        assert review["findings"][0]["tests"] == ["TokenTest.should_x"]
        assert review["unrecognized"] == 0

    def test_tests_that_did_not_run_are_counted(self):
        answer = _answer(
            rows=[_row(LIST_200, integration=_cell(2, "invented", "should_return200"))],
            findings=[_finding(tests=["Other.should_return200", "should_return200"])],
        )

        assert _parse(answer)["unrecognized"] == 3

    def test_rows_the_answer_cannot_stand_behind_are_dropped(self):
        answer = _answer(
            rows=[
                _row("the list endpoint", integration=_cell(2, "should_return200")),
                _row("GET /invented :: 200", integration=_cell(2, "never_ran")),
                _row(LIST_200, unknown_suite=_cell(2, "should_return200")),
                _row(LIST_200, integration=_cell(2, "should_return200")),
                "not a row",
            ]
        )

        rows = _parse(answer)["rows"]

        assert [row["id"] for row in rows] == [LIST_200, LIST_401]
        assert rows[0]["suites"] == {}


class TestAnswer:
    def test_an_answer_wrapped_in_prose_is_read(self):
        answer = f"Here is the review.\n```json\n{_answer(gaps=['no token case'])}\n```\nDone."

        review = _parse(answer)

        assert review["summary"] == "Proves the list answers."
        assert review["gaps"] == ["no token case"]

    @pytest.mark.parametrize(
        ("judged", "better"),
        [
            ({"better": "integration", "reason": "guards writes"}, "integration"),
            ({"better": "both", "reason": "each guards its own"}, "both"),
            ({"better": "the newer one", "reason": "r"}, ""),
            ("integration", ""),
        ],
    )
    def test_the_better_suite_is_one_that_ran(self, judged, better):
        review = _parse(_answer(determination=judged))

        assert review["determination"]["better"] == better

    def test_findings_keep_only_tests_their_suite_ran_and_the_most_severe_lead(self):
        answer = _answer(
            findings=[
                _finding(title="minor", severity="LOW"),
                _finding(
                    title="odd",
                    severity="critical",
                    tests=["TestList.invented", "TestList.reads_list", "TestList.should_return401"],
                ),
                _finding(
                    title="worst",
                    severity="high",
                    suite="acceptance",
                    tests=["TestList.reads_list"],
                ),
                {"severity": "high", "detail": "no title"},
                "not a finding",
            ]
        )

        findings = _parse(answer)["findings"]

        assert [(f["title"], f["severity"], f["suite"]) for f in findings] == [
            ("worst", "high", "acceptance"),
            ("minor", "low", "integration"),
            ("odd", "low", "integration"),
        ]
        assert findings[2]["tests"] == ["TestList.should_return401"]

    def test_the_runs_credentials_are_kept_out_of_the_answer(self):
        answer = _answer(
            summary="Sent minted-bearer-value once.",
            determination={"better": "both", "reason": "token minted-bearer-value"},
            gaps=["Authorization: Bearer abcdefgh12345678 is accepted"],
            findings=[_finding(detail="uses minted-bearer-value")],
            rows=[
                _row(
                    "GET /x :: sends minted-bearer-value", integration=_cell(1, "should_return200")
                )
            ],
        )

        kept = json.dumps(_parse(answer, ["minted-bearer-value"]))

        assert "minted-bearer-value" not in kept and "abcdefgh12345678" not in kept

    def test_an_overlong_detail_ends_on_a_whole_sentence(self):
        sentence = "The test asserts the status code only. "

        review = _parse(_answer(findings=[_finding(detail=sentence * 60)]))

        kept = review["findings"][0]["detail"]
        assert len(kept) <= suite_review.TEXT_LIMIT
        assert kept.endswith("only.") and len(kept) % len(sentence) == len(sentence) - 1

    @pytest.mark.parametrize(
        "answer",
        [
            "I could not read the files.",
            "{not json}",
            "[1, 2]",
            json.dumps({"summary": "", "findings": [], "rows": []}),
            '{"rows": ' + "[" * 100_000 + "]" * 100_000 + "}",
        ],
    )
    def test_an_answer_that_says_nothing_is_no_review(self, answer):
        with pytest.raises(ReviewUnavailable):
            _parse(answer)


class TestReviewer:
    @pytest.fixture
    def installed(self, monkeypatch):
        present = {"copilot", "claude"}
        monkeypatch.setattr(
            suite_review.shutil, "which", lambda name: name if name in present else None
        )
        for variable in (
            suite_review.REVIEWER_ENV,
            suite_review.MODEL_ENV,
            suite_review.EFFORT_ENV,
        ):
            monkeypatch.delenv(variable, raising=False)
        return present

    @pytest.fixture
    def answered(self, installed, monkeypatch):
        seen = {}

        def run_command(cmd, **kwargs):
            bundle = Path(kwargs["cwd"])
            files = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()}
            home = Path(kwargs["env"]["COPILOT_HOME"])
            seen.update(cmd=cmd, files=files, kwargs=kwargs, home=home)
            seen["home_held"] = list(home.iterdir())
            answer = _answer(
                summary="Sent minted-bearer-value once.",
                rows=[_row(LIST_200, integration=_cell(2, "should_return200"))],
            )
            return subprocess.CompletedProcess(cmd, 0, answer, "")

        monkeypatch.setattr(suite_review, "run_command", run_command)
        return seen

    def test_the_named_reviewer_wins_over_the_first_installed(self, installed, monkeypatch):
        assert choose_reviewer() == "copilot"

        monkeypatch.setenv(suite_review.REVIEWER_ENV, "Claude")
        assert choose_reviewer() == "claude"

        installed.discard("copilot")
        monkeypatch.delenv(suite_review.REVIEWER_ENV)
        assert choose_reviewer() == "claude"

    @pytest.mark.parametrize(
        ("named", "present"),
        [("gemini", {"copilot"}), ("claude", {"copilot"}), ("", set())],
    )
    def test_a_reviewer_that_cannot_run_is_no_review(self, installed, monkeypatch, named, present):
        installed.intersection_update(present)
        if named:
            monkeypatch.setenv(suite_review.REVIEWER_ENV, named)

        with pytest.raises(ReviewUnavailable):
            choose_reviewer()

    def test_a_reviewer_is_given_three_tools_that_read_and_nothing_beside_the_bundle(self):
        copilot = REVIEWERS["copilot"][1]("m", "medium")
        denied = {copilot[i + 1] for i, arg in enumerate(copilot) if arg == "--deny-tool"}
        given = copilot.index("--available-tools")
        assert denied == {"shell", "write", "url"}
        assert copilot[given + 1 : given + 4] == ["view", "grep", "glob"]
        assert copilot[given + 4].startswith("--")
        for flag in ("--disable-builtin-mcps", "--no-custom-instructions", "--disallow-temp-dir"):
            assert flag in copilot, flag

        claude = REVIEWERS["claude"][1]("m", "medium")
        assert claude[claude.index("--tools") + 1] == "Read,Glob,Grep"
        assert claude[claude.index("--allowed-tools") + 1] == "Read,Glob,Grep"
        assert claude[claude.index("--permission-prompts") + 1] == "none"
        for flag in ("--restricted", "--safe-mode", "--strict-mcp-config"):
            assert flag in claude, flag

    def test_the_reviewer_reads_the_bundle_as_opus_at_medium_effort(
        self, answered, monkeypatch, tmp_path
    ):
        add_suite(tmp_path, "integration", tmp_path / "missing", INTEGRATION)

        monkeypatch.setenv("KEPT_FOR_THE_REVIEWER", "its own sign-in")
        contract = {"source": "https://gw", "rows": CONTRACT}

        review = review_suites(tmp_path, SUITES, contract, ["minted-bearer-value"])

        command = answered["cmd"]
        home = answered["home"]
        assert answered["home_held"] == [] and not home.exists()
        assert tmp_path not in home.parents and home not in tmp_path.parents
        assert answered["kwargs"]["env"]["KEPT_FOR_THE_REVIEWER"] == "its own sign-in"
        assert review["summary"] == "Sent [redacted] once."
        assert command[0] == "copilot"
        assert command[command.index("--model") + 1] == "claude-opus-5.5"
        assert command[command.index("--reasoning-effort") + 1] == "medium"
        assert answered["kwargs"]["cwd"] == str(tmp_path)
        assert answered["kwargs"]["check"] is False and answered["kwargs"]["timeout"] > 0
        assert answered["files"] == {
            "REVIEW.md",
            "contract.json",
            "suites/integration/facts.json",
        }
        assert (review["reviewer"], review["model"], review["effort"]) == (
            "copilot",
            "claude-opus-5.5",
            "medium",
        )
        assert review["rows"][0]["suites"]["integration"]["grade"] == 2

    def test_the_model_and_effort_can_be_named(self, answered, monkeypatch, tmp_path):
        monkeypatch.setenv(suite_review.REVIEWER_ENV, "claude")
        monkeypatch.setenv(suite_review.MODEL_ENV, "claude-sonnet-5")
        monkeypatch.setenv(suite_review.EFFORT_ENV, "high")

        review = review_suites(tmp_path, SUITES)

        command = answered["cmd"]
        assert command[command.index("--model") + 1] == "claude-sonnet-5"
        assert command[command.index("--effort") + 1] == "high"
        assert "contract.json" not in answered["files"]
        assert (review["model"], review["effort"]) == ("claude-sonnet-5", "high")

    @pytest.mark.parametrize(
        ("variable", "value"),
        [(suite_review.MODEL_ENV, "--allow-all"), (suite_review.EFFORT_ENV, "high --yolo")],
    )
    def test_a_setting_that_reads_as_an_option_is_refused(
        self, answered, monkeypatch, tmp_path, variable, value
    ):
        monkeypatch.setenv(variable, value)

        with pytest.raises(ReviewUnavailable):
            review_suites(tmp_path, SUITES)
        assert answered == {}

    def test_a_reviewer_that_fails_says_why(self, installed, monkeypatch, tmp_path):
        monkeypatch.setattr(
            suite_review,
            "run_command",
            lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, "", "warming\nnot signed in"),
        )

        with pytest.raises(ReviewUnavailable, match="copilot exited 1: not signed in"):
            review_suites(tmp_path, SUITES)
