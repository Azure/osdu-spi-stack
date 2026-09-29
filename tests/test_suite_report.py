# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""The suite report: facts from a run's reports and sources, and the file a page goes to."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

from spi.suite_report import (
    Declaration,
    Sources,
    collect,
    named_tests,
    report_folder,
    saved_pages,
    show,
    write,
)

PACKAGE = "org.example.api"
NOTE = "revisit this later -- Istio is changing the response code"

PARENT = """
package org.example.api;

import org.junit.Test;

public abstract class ListApiTest extends org.example.util.TestBase {
    @Test
    public void should_return200() throws Exception {
        assertEquals(200, run().getCode());
    }

    @Test
    public void should_return401_when_noAccessToken() throws Exception {
        assertEquals(401, run("").getCode());
    }
}
"""

CHILD = f"""
package org.example.api;

public class TestList extends ListApiTest {{
    @Test
    @Override
    public void should_return401_when_noAccessToken() throws Exception {{
        // {NOTE}
    }}

    @Test
    public void should_fail() {{ fail("no"); }}
}}
"""


def _java(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


def _reports(root: Path, body: str, folder: str = "module/target/surefire-reports") -> None:
    path = root / folder / "TEST-suite.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _case(name: str, inner: str = "", classname: str = f"{PACKAGE}.TestList") -> str:
    return f'<testcase name="{name}" classname="{classname}" time="1.5">{inner}</testcase>'


@pytest.fixture
def suite(tmp_path):
    root = tmp_path / "suite"
    _java(root, "core/src/main/java/org/example/api/ListApiTest.java", PARENT)
    _java(root, "module/src/test/java/org/example/api/TestList.java", CHILD)
    return root


def _declared(root: Path, classname: str, case: str) -> tuple[str, bool]:
    found = Sources(root).declaration(classname, case)
    assert found is not None
    return found.declared_in, found.empty


def _statuses(facts: dict) -> dict:
    return {test["name"]: test["status"] for entry in facts["classes"] for test in entry["tests"]}


class TestCollect:
    def test_a_pass_with_no_statement_is_empty_and_every_other_test_keeps_its_status(self, suite):
        _reports(
            suite,
            "<testsuite>"
            + _case("should_return200")
            + _case("should_return401_when_noAccessToken")
            + _case(
                "should_fail",
                '<failure message="no" type="java.lang.AssertionError">at X</failure>',
            )
            + _case("broke", '<error message="timed out" type="java.io.IOException"/>')
            + _case("later", '<skipped message="not here"/>')
            + "</testsuite>",
        )

        facts = collect(suite)

        assert _statuses(facts) == {
            "should_return200": "passed",
            "should_return401_when_noAccessToken": "empty",
            "should_fail": "failed",
            "broke": "error",
            "later": "skipped",
        }
        assert facts["totals"] == {
            "tests": 5,
            "passed": 1,
            "empty": 1,
            "failed": 1,
            "error": 1,
            "skipped": 1,
            "seconds": 7.5,
        }
        [empty] = [t for t in facts["classes"][0]["tests"] if t["status"] == "empty"]
        assert (empty["note"], empty["declared_in"]) == (NOTE, "TestList")

    def test_only_this_runs_surefire_and_failsafe_reports_are_read(self, suite):
        _reports(suite, "<testsuite>" + _case("should_return200") + "</testsuite>")
        _reports(suite, "<testsuite>" + _case("kept") + "</testsuite>", "m/target/failsafe-reports")
        _reports(suite, "<testsuite>" + _case("stray") + "</testsuite>", "m/old-reports")
        _reports(suite, "<testsuite><testcase", "m/target/surefire-reports")

        assert set(_statuses(collect(suite))) == {"should_return200", "kept"}

    def test_what_a_failure_carried_is_kept_without_the_runs_credentials(self, suite):
        jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzcGkifQ.c2lnbmF0dXJl"
        cut = "cut-by-the-line-width-secret"
        output = "\n".join(
            [
                *(f"line {n}" for n in range(200)),
                "GET https://gw.example/api/partition/v1/partitions",
                "Authorization: Bearer minted-bearer-value",
                f"other caller {jwt}",
                "client_secret=hunter2hunter2",
                "credential: hunter3hunter3",
                "from env: kv-secret-value",
                "after a short wait",
                "x" * 290 + cut,
            ]
        )
        _reports(
            suite,
            "<testsuite>"
            + _case(
                "[1] caller=minted-bearer-value",
                '<failure message="sent minted-bearer-value" type="Refused: kv-secret-value">'
                f"at Client.send({jwt})\n{'y' * 290}{cut}</failure>"
                f"<system-out>{output}</system-out>",
                classname="p.Tkv-secret-value",
            )
            + "</testsuite>",
        )

        facts = collect(suite, secrets=["minted-bearer-value", "kv-secret-value", cut, "short"])

        kept = json.dumps(facts)
        for secret in (
            "minted-bearer-value",
            jwt,
            "hunter2hunter2",
            "hunter3hunter3",
            "kv-secret-value",
            cut[:8],
        ):
            assert secret not in kept, secret
        assert "after a short wait" in kept
        [test] = facts["classes"][0]["tests"]
        assert "https://gw.example/api/partition/v1/partitions" in test["output"]
        assert "line 0" not in test["output"] and "line 199" in test["output"]
        assert "at Client.send(" in test["detail"]

    def test_a_comment_in_the_source_is_redacted_like_the_reports_text(self, suite):
        source = (
            "package org.example.api;\npublic class TestNote {\n"
            " @Test public void check() {\n  // client_secret=hunter2hunter2 kv-secret-value\n }\n}"
        )
        _java(suite, "module/src/test/java/org/example/api/TestNote.java", source)
        body = _case("check", classname=f"{PACKAGE}.TestNote")
        _reports(suite, f"<testsuite>{body}</testsuite>")

        [test] = collect(suite, secrets=["kv-secret-value"])["classes"][0]["tests"]

        assert test["status"] == "empty"
        assert test["note"] == "client_secret=[redacted] [redacted]"

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
    def test_a_link_in_the_suite_is_not_the_suites_own(self, suite, tmp_path):
        host = tmp_path / "host"
        (host / "target" / "surefire-reports").mkdir(parents=True)
        (host / "TestLeak.java").write_text(
            "package org.example.api;\nclass TestLeak { void check() { /* host */ } }"
        )
        (host / "target/surefire-reports/TEST-host.xml").write_text(
            "<testsuite>" + _case("from_host") + "</testsuite>"
        )
        try:
            (suite / "module/src/test/java/org/example/api/TestLeak.java").symlink_to(
                host / "TestLeak.java"
            )
            (suite / "linked").symlink_to(host, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks need a privilege this run lacks")
        body = _case("check", classname=f"{PACKAGE}.TestLeak")
        _reports(suite, f"<testsuite>{body}</testsuite>")

        facts = collect(suite)

        assert _statuses(facts) == {"check": "passed"}
        assert Sources(suite).declaration(f"{PACKAGE}.TestLeak", "check") is None


class TestSources:
    @pytest.mark.parametrize(
        ("body", "empty"),
        [
            ("// later\n /* and\n * later */", True),
            ("", True),
            ("run();", False),
            ('String s = "}";', False),
            ("// not yet\n assertTrue(true);", False),
        ],
    )
    def test_a_body_is_empty_only_when_it_holds_no_statement(self, tmp_path, body, empty):
        source = f"package p;\nclass T {{\n @Test\n public void check() {{ {body} }}\n}}"
        _java(tmp_path, "src/test/java/p/T.java", source)

        assert _declared(tmp_path, "p.T", "check") == ("T", empty)

    def test_braces_in_literals_comments_and_annotations_neither_end_nor_extend_a_body(
        self, tmp_path
    ):
        source = (
            "package p;\nclass T {\n"
            ' private final Map<String, String> made = new HashMap<>() {{ put("a", "b"); }};\n'
            ' String block = """\n  { \\""" {\n  """;\n'
            " void helper() { log(\"{\"); char c = '{'; /* { */ // {\n }\n"
            ' @ParameterizedTest\n @CsvSource(value = {"a;1", "b;2"}, delimiter = \';\')\n'
            " void check(String name, int count) { }\n"
            " @Test void real() { run(); }\n}"
        )
        _java(tmp_path, "src/test/java/p/T.java", source)

        assert _declared(tmp_path, "p.T", "check(String, int)[1]") == ("T", True)
        assert _declared(tmp_path, "p.T", "real") == ("T", False)

    @pytest.mark.parametrize(
        ("child", "reported", "why"),
        [
            (
                "class Child extends Base { void create(String id) { } }",
                "p.Child",
                "an empty helper shares the name with other parameters",
            ),
            (
                "class Child extends Base {\n"
                " @ParameterizedTest @CsvSource(value = {\"a\"}, delimiter = ';')\n"
                " void create() { run(); }\n}",
                "p.Child",
                "the override sits under an annotation holding braces",
            ),
            (
                "class Child { void create() { }\n @Nested class Inner extends Base { }\n}",
                "p.Child$Inner",
                "the enclosing class holds an empty method of the name",
            ),
            (
                "class Child extends Base {\n"
                " static class Helper extends Other { void create() { } }\n}",
                "p.Child",
                "a nested helper declares the name and another parent",
            ),
            (
                "class Child<T extends Other> extends Base { }",
                "p.Child",
                "a type parameter has a bound",
            ),
        ],
    )
    def test_a_test_that_asserts_is_never_read_as_empty(self, tmp_path, child, reported, why):
        _java(tmp_path, "src/p/Base.java", "package p;\nclass Base { void create() { run(); } }")
        _java(tmp_path, "src/p/Other.java", "package p;\nclass Other { void create() { } }")
        _java(tmp_path, "src/p/Child.java", f"package p;\n{child}")

        found = Sources(tmp_path).declaration(reported, "create")

        assert found is not None and not found.empty, why

    @pytest.mark.parametrize(
        ("classname", "case", "why"),
        [
            ("p.T", "check(int)", "two declarations take one parameter"),
            ("p.T", "reads a partition back", "a display name is not the method it starts with"),
            ("p.T", "absent", "no class declares it and the class has no parent"),
            ("p.Missing", "check", "the class is not in the suite"),
            ("q.Only", "check", "the one file of that name is another package's class"),
            ("r.Twin", "check", "two modules declare the class"),
            ("p.Heir", "check", "two classes could be the parent"),
        ],
    )
    def test_what_the_sources_cannot_settle_is_left_unjudged(self, tmp_path, classname, case, why):
        _java(
            tmp_path,
            "src/test/java/p/T.java",
            "package p;\nclass T {\n void check(int n) { }\n"
            " void check(String s) { run(); }\n void reads() { }\n}",
        )
        _java(tmp_path, "src/test/java/p/Only.java", "package p;\nclass Only { void check() { } }")
        _java(
            tmp_path,
            "src/test/java/p/Heir.java",
            "package p;\nimport r.*;\nclass Heir extends Twin { }",
        )
        _java(
            tmp_path, "a/src/test/java/r/Twin.java", "package r;\nclass Twin { void check() { } }"
        )
        _java(
            tmp_path, "b/src/test/java/r/Twin.java", "package r;\nclass Twin { void check() { } }"
        )

        assert Sources(tmp_path).declaration(classname, case) is None, why

    @pytest.mark.parametrize(
        ("heading", "outside"),
        [
            ("import org.lib.TestBase;\nclass T extends TestBase { }", "TestBase"),
            ("class T extends org.lib.TestBase { }", "TestBase"),
            ("import org.lib.*;\nclass T extends Absent { }", "Absent"),
        ],
    )
    def test_a_parent_the_suite_does_not_hold_is_named_not_guessed(
        self, tmp_path, heading, outside
    ):
        # A class of the same name elsewhere in the suite is not the parent.
        _java(tmp_path, "src/b/TestBase.java", "package b;\nclass TestBase { void check() { } }")
        _java(tmp_path, "src/p/T.java", f"package p;\n{heading}")

        assert Sources(tmp_path).declaration("p.T", "check") == Declaration("", False, "", outside)

    def test_a_test_inherited_from_outside_the_suite_names_the_parent_that_holds_it(self, suite):
        _reports(
            suite,
            "<testsuite>" + _case("inherited") + _case("should_return200") + "</testsuite>",
        )

        tests = {test["name"]: test for test in collect(suite)["classes"][0]["tests"]}

        assert tests["inherited"] == {
            "name": "inherited",
            "seconds": 1.5,
            "status": "passed",
            "outside": "TestBase",
        }
        assert "outside" not in tests["should_return200"]

    @pytest.mark.parametrize(
        ("heading", "declared_in"),
        [
            ("import s.Base;\nclass T extends Base { }", "Base"),
            ("import s.*;\nclass T extends Base { }", "Base"),
            ("class T extends s.Base { }", "Base"),
            ("class T extends Near { }\nclass Near { void check() { run(); } }", "Near"),
        ],
    )
    def test_a_parent_is_the_class_the_child_can_see(self, tmp_path, heading, declared_in):
        _java(tmp_path, "a/src/r/Base.java", "package r;\nclass Base { void check() { } }")
        _java(tmp_path, "b/src/s/Base.java", "package s;\nclass Base { void check() { run(); } }")
        _java(tmp_path, "c/src/p/T.java", f"package p;\n{heading}")

        assert _declared(tmp_path, "p.T", "check") == (declared_in, False)

    def test_a_nested_class_is_read_from_its_own_body(self, tmp_path):
        source = (
            "package p;\nclass Outer {\n @Test void check() { run(); }\n"
            " @Nested class Inner {\n  @Test void check() { }\n }\n}"
        )
        _java(tmp_path, "src/test/java/p/Outer.java", source)

        assert _declared(tmp_path, "p.Outer$Inner", "check") == ("Inner", True)
        assert _declared(tmp_path, "p.Outer", "check") == ("Outer", False)

    def test_compiled_output_is_not_read_as_source(self, tmp_path):
        _java(tmp_path, "m/target/generated/p/T.java", "package p;\nclass T { void check() { } }")

        assert Sources(tmp_path).declaration("p.T", "check") is None


def test_a_test_is_named_by_its_class_in_full_where_two_share_the_simple_name():
    facts = {
        "classes": [
            {"name": "p.T", "tests": [{"name": "t", "status": "empty"}]},
            {"name": "q.T", "tests": [{"name": "t", "status": "passed"}]},
            {"name": "q.Other", "tests": [{"name": "t", "status": "passed"}]},
        ]
    }

    assert [(name, test["status"]) for name, test in named_tests(facts)] == [
        ("p.T.t", "empty"),
        ("q.T.t", "passed"),
        ("Other.t", "passed"),
    ]


@pytest.fixture
def temp(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


class TestFile:
    def test_a_report_replaces_the_last_one_and_week_old_reports_go(self, temp):
        now = time.time()
        folder = report_folder()
        first = write("one", folder, "partition", "integration", now=now)
        for name, age_days in (
            ("spi-test-legal-acceptance.html", 8),
            ("spi-test-file-acceptance.html", 6),
            ("notes.txt", 30),
        ):
            (folder / name).write_text("x")
            then = now - age_days * 86400
            os.utime(folder / name, (then, then))

        second = write("two", folder, "partition", "integration", now=now)

        assert second == first and second.read_text() == "two"
        assert folder.parent == temp
        assert {path.name for path in folder.iterdir()} == {
            "spi-test-partition-integration.html",
            "spi-test-file-acceptance.html",
            "notes.txt",
        }

    def test_a_suite_name_cannot_leave_the_report_folder(self, temp):
        path = write("page", report_folder(), "partition", "../../elsewhere")

        assert path.parent.parent == temp and path.read_text() == "page"

    def test_text_utf8_cannot_encode_does_not_cost_the_page(self, temp):
        path = write("lone \ud83d surrogate", report_folder(), "partition", "acceptance")

        assert path.read_text(encoding="utf-8") == "lone ? surrogate"

    def test_saved_pages_are_the_folders_reports_oldest_first(self, temp):
        folder = report_folder()
        assert saved_pages(folder) == [] and saved_pages(temp / "absent") == []
        now = time.time()
        newer = write("b", folder, "partition", "acceptance+integration", now=now)
        older = write("a", folder, "legal", "acceptance", now=now)
        os.utime(older, (now - 60, now - 60))
        (folder / "notes.txt").write_text("x")

        assert saved_pages(folder) == [older, newer]

    @pytest.mark.skipif(not hasattr(os, "getuid"), reason="no owner or mode to compare on Windows")
    def test_the_folder_is_this_users_alone(self, temp, monkeypatch):
        (temp / "spi-reports").mkdir(mode=0o755)
        (temp / "spi-reports").chmod(0o755)

        shared = report_folder()
        assert shared == temp / "spi-reports" and shared.stat().st_mode & 0o777 == 0o700

        shared.chmod(0o770)
        apart = report_folder()
        assert apart != shared and apart.parent == temp
        assert shared.stat().st_mode & 0o777 == 0o770
        shared.chmod(0o700)

        monkeypatch.setattr(os, "getuid", lambda: shared.stat().st_uid + 1)
        fresh = report_folder()

        assert fresh != shared and fresh.parent == temp and fresh.is_dir()

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
    def test_a_folder_that_is_a_link_is_left_alone(self, temp):
        (temp / "elsewhere").mkdir()
        try:
            (temp / "spi-reports").symlink_to(temp / "elsewhere", target_is_directory=True)
        except OSError:
            pytest.skip("symlinks need a privilege this run lacks")

        fresh = report_folder()

        assert fresh.parent == temp and fresh.name.startswith("spi-reports-")
        assert list((temp / "elsewhere").iterdir()) == []

    @pytest.mark.parametrize(
        ("ci", "tty", "opened"),
        [("", True, True), ("true", True, False), ("", False, False)],
    )
    def test_the_page_opens_only_for_someone_at_a_terminal(
        self, monkeypatch, tmp_path, ci, tty, opened
    ):
        import webbrowser

        seen = []
        monkeypatch.setattr(webbrowser, "open", lambda url: seen.append(url) or True)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: tty, raising=False)
        monkeypatch.setenv("CI", ci)
        page = tmp_path / "report.html"
        page.write_text("page")

        assert show(page) is opened
        assert seen == ([page.as_uri()] if opened else [])
