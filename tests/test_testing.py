# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""spi test: pairing, the commit's own machinery, guards, and verdicts."""

import base64
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from spi import cli, pins, testing
from spi.images import ImageNotFoundError
from spi.pins import PinError, ServicePin, VerifyError, encode_pins
from spi.testing import SuiteNotRun

SHA = "8e056d4a61429bdd4d4de963ef313e5bf7ca53e6"
PAIR = "sha256:" + "a" * 64

# Stand-ins for the fork's resolver and verdict script. Each carries its
# behavior inline, since paired mode fetches only the three machinery files
# and the resolver sees only an allowlisted environment.
FAKE_RESOLVER = """
import argparse, os
parser = argparse.ArgumentParser()
for flag in ("--mode", "--suite", "--descriptor", "--facts", "--env-file", "--report"):
    parser.add_argument(flag)
args = parser.parse_args()
report = {
    "report_schema": behavior.get("schema", 1),
    "service": behavior.get("service", "partition"),
    "contract": {"test_dir": "suite", "maven_arguments": ["verify"], "timeout_minutes": 5},
    "seen_env": sorted(os.environ),
    "suite": args.suite,
    "error": None,
}
if behavior.get("exit"):
    said = os.environ.get(behavior.get("quote", ""), "")
    report["error"] = {"code": "WHY", "detail": f"because {said}".strip()}
    pathlib.Path(args.report).write_text(json.dumps(report))
    sys.exit(behavior["exit"])
pathlib.Path(args.env_file).write_text("HOST=https://gw\\nTOKEN=a=b\\n")
pathlib.Path(args.report).write_text(json.dumps(report))
"""
FAKE_VERDICT = """
pathlib.Path(behavior["record"]).write_text(json.dumps(sys.argv[1:]))
print(behavior.get("verdict_line", "pass: 1 tests, 0 skipped"))
sys.exit(behavior.get("verdict", 0))
"""


def _machinery(root: Path, **behavior) -> Path:
    behavior.setdefault("record", str(root / "verdict-args.json"))
    prelude = f"import json, pathlib, sys\nbehavior = json.loads({json.dumps(behavior)!r})\n"
    for relative, body in (
        (testing.DESCRIPTOR_PATH, "service: partition\n"),
        (testing.RESOLVER_PATH, prelude + FAKE_RESOLVER),
        (testing.VERDICT_PATH, prelude + FAKE_VERDICT),
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    (root / "suite").mkdir(exist_ok=True)
    return root


def _checkout(tmp_path: Path, **behavior) -> Path:
    root = _machinery(tmp_path / "partition", **behavior)
    (root / testing.SETTINGS_PATH).parent.mkdir()
    (root / testing.SETTINGS_PATH).write_text("<settings/>")
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fork"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True)
    return root


def _head(checkout: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()


def _lock(*, pin: ServicePin | None = None, source="Acme/partition", tag=None, pair=PAIR):
    data = {
        "PARTITION_IMAGE_REPOSITORY": "ghcr.io/acme/partition",
        "PARTITION_IMAGE_TAG": f"sha-{SHA[:12]}" if tag is None else tag,
        "PARTITION_IMAGE_DIGEST": "sha256:service",
    }
    if pair:
        data["PARTITION_ACCEPTANCE_DIGEST"] = pair
    annotations = {pins.TRUSTED_REPOS_ANNOTATION: json.dumps({"partition": "acme/partition"})}
    if source:
        annotations[pins.CANONICAL_SOURCES_ANNOTATION] = json.dumps({"partition": source})
    if pin:
        annotations[pins.PINS_ANNOTATION] = encode_pins({"partition": pin})
    return {"metadata": {"annotations": annotations}, "data": data}


def _pin(**overrides) -> ServicePin:
    fields: dict = dict(
        mr="",
        branch="",
        repository="ghcr.io/acme/partition",
        tag="",
        canonical_repository="ghcr.io/acme/partition",
        canonical_tag=f"sha-{SHA[:12]}",
        canonical_created_at="",
        canonical_digest="sha256:service",
        applied_at="2026-09-27T00:00:00Z",
        digest="sha256:" + "b" * 64,
        origin="github",
        ephemeral=True,
        run_id="4321",
        source_repo="Acme/partition",
        source_sha="b" * 40,
    )
    fields.update(overrides)
    return ServicePin(**fields)


@pytest.fixture
def cluster(monkeypatch, tmp_path):
    """Everything past the process boundary: lock, cluster, GitHub, GHCR, docker, and mvn."""

    state: dict = {
        "locks": [_lock()],
        "revisions": ["7"],
        "commands": [],
        "github": [],
        "run_exit": 0,
    }
    commit_tree = _machinery(tmp_path / "at-commit")

    def read_lock(required=True):
        locks = state["locks"]
        return locks.pop(0) if len(locks) > 1 else locks[0]

    def revision(service):
        revisions = state["revisions"]
        return revisions.pop(0) if len(revisions) > 1 else revisions[0]

    def github_file(repository, path, ref):
        state["github"].append((repository, path, ref))
        return (commit_tree / path).read_bytes()

    def run_command(cmd, **kwargs):
        state["commands"].append((cmd, kwargs))
        if cmd[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(cmd, state["run_exit"])
        if cmd[0] == "mvn":
            reports = Path(kwargs["cwd"]) / "target" / "surefire-reports"
            reports.mkdir(parents=True, exist_ok=True)
            (reports / "TEST-fresh.xml").write_text('<testsuite tests="2" skipped="1"/>')
            return subprocess.CompletedProcess(cmd, state["run_exit"])
        return subprocess.CompletedProcess(cmd, 0)

    real_run_process = testing.run_process

    def run_process(cmd, **kwargs):
        if cmd[0] != "docker":
            return real_run_process(cmd, **kwargs)
        state["commands"].append((cmd, kwargs))
        if cmd[1] == "cp":
            reports = Path(cmd[3]) / "target" / "surefire-reports"
            reports.mkdir(parents=True)
            (reports / "TEST-a.xml").write_text('<testsuite tests="11" skipped="0"/>')
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(testing, "require_deployable", lambda: None)
    monkeypatch.setattr(testing, "read_lock", read_lock)
    monkeypatch.setattr(testing, "verify_service_image", lambda service, ref: None)
    monkeypatch.setattr(testing, "_deployment_revision", revision)
    monkeypatch.setattr(testing, "collect_facts", lambda: {"base_url": "https://gw"})
    monkeypatch.setattr(
        testing, "mint_bearers", lambda: {"RESOLVER_TOKEN": "t", "RESOLVER_MEMBER_TOKEN": "m"}
    )
    monkeypatch.setattr(testing, "resolve_ghcr_manifest", lambda repository, digest: None)
    monkeypatch.setattr(testing, "github_get", lambda path: {"sha": SHA})
    monkeypatch.setattr(testing, "github_file", github_file)
    monkeypatch.setattr(testing, "_require_tool", lambda name: None)
    monkeypatch.setattr(testing, "run_command", run_command)
    monkeypatch.setattr(testing, "run_process", run_process)
    state["commit_tree"] = commit_tree
    return state


def _commands(state, program):
    return [(cmd, kwargs) for cmd, kwargs in state["commands"] if cmd[:2] == program]


class TestPairing:
    @pytest.mark.parametrize(
        ("lock", "reason"),
        [
            (_lock(pin=_pin(ephemeral=False, run_id="")), "runs a pinned image"),
            (_lock(source=""), "follows community images"),
            (_lock(source="Other/partition"), "but acme/partition is trusted"),
            (_lock(tag="c" * 40), "is not Acme/partition's canonical yet"),
            (_lock(pair=""), "has no recorded acceptance image"),
        ],
    )
    def test_only_a_trusted_fork_canonical_with_a_recorded_pair_pairs(self, lock, reason):
        target, why = testing.paired_target(lock, "partition")

        assert target is None
        assert reason in why

    def test_the_pair_is_the_recorded_digest_beside_the_lock_repository(self):
        target, _ = testing.paired_target(_lock(), "partition")

        assert target is not None
        assert target.acceptance_image == f"ghcr.io/acme/partition-acceptance@{PAIR}"
        assert (target.source_repo, target.commit) == ("Acme/partition", SHA[:12])


class TestPairedRun:
    def test_the_commits_own_machinery_runs_the_recorded_image(self, cluster):
        result = testing.run_suite("partition")

        assert {path for _, path, ref in cluster["github"] if ref == SHA} == set(testing.MACHINERY)
        [(run, kwargs)] = _commands(cluster, ["docker", "run"])
        image = f"ghcr.io/acme/partition-acceptance@{PAIR}"
        assert run[run.index("--platform") + 1] == "linux/amd64"
        assert run[run.index("-e") + 1] == "SUITE_DIR=suite"
        assert run[run.index(image) + 1 :] == ["verify"]
        assert kwargs["timeout"] == 300
        [(rm, _)] = _commands(cluster, ["docker", "rm"])
        assert rm[-1] == run[run.index("--name") + 1]
        assert result.passed and result.exit_code == 0
        assert (result.mode, result.commit, result.image) == ("paired", SHA, image)
        assert result.tests["tests"] == 11

    def test_a_partial_report_copy_gives_no_verdict(self, cluster, monkeypatch):
        fake = testing.run_process

        def run_process(cmd, **kwargs):
            if cmd[:2] == ["docker", "cp"]:
                # A passing report arrived; the one carrying the failure did not.
                fake(cmd, **kwargs)
                return subprocess.CompletedProcess(cmd, 1, "", "unexpected EOF")
            return fake(cmd, **kwargs)

        monkeypatch.setattr(testing, "run_process", run_process)

        with pytest.raises(SuiteNotRun, match="unexpected EOF") as exc:
            testing.run_suite("partition")

        assert (exc.value.code, exc.value.exit_code) == ("reports_unavailable", 1)
        assert _commands(cluster, ["docker", "rm"])
        assert not (cluster["commit_tree"] / "verdict-args.json").exists()

    @pytest.mark.parametrize(
        ("stderr", "refused"),
        [("Error response from daemon: No such container: x", False), ("daemon hung up", True)],
    )
    def test_a_container_that_could_not_be_removed_is_reported(
        self, cluster, monkeypatch, stderr, refused
    ):
        fake = testing.run_process

        def run_process(cmd, **kwargs):
            if cmd[:2] == ["docker", "rm"]:
                return subprocess.CompletedProcess(cmd, 1, "", stderr)
            return fake(cmd, **kwargs)

        monkeypatch.setattr(testing, "run_process", run_process)

        if not refused:
            assert testing.run_suite("partition").passed
            return
        with pytest.raises(SuiteNotRun, match="docker rm -f spi-test-") as exc:
            testing.run_suite("partition")
        assert (exc.value.code, exc.value.exit_code) == ("cleanup_failed", 1)

    def test_each_run_owns_a_container_name_no_earlier_run_can_hold(self, cluster):
        testing.run_suite("partition")
        testing.run_suite("partition")

        names = [cmd[cmd.index("--name") + 1] for cmd, _ in _commands(cluster, ["docker", "run"])]
        assert len(set(names)) == 2

    def test_the_resolver_sees_only_path_the_bearers_and_overrides(self, cluster, monkeypatch):
        monkeypatch.setenv("STRAY_EXPORT", "stale")
        seen: dict = {}
        real = testing.resolve_suite

        def spy(service, root, suite, facts, work, bearers, overrides):
            contract, env_file = real(service, root, suite, facts, work, bearers, overrides)
            seen.update(json.loads((work / "report.json").read_text()))
            return contract, env_file

        monkeypatch.setattr(testing, "resolve_suite", spy)

        testing.run_suite("partition", overrides={"HOST": "https://override"})

        assert "STRAY_EXPORT" not in seen["seen_env"]
        assert {"PATH", "RESOLVER_TOKEN", "RESOLVER_MEMBER_TOKEN", "HOST"} <= set(seen["seen_env"])

    def test_a_branch_named_like_the_commit_is_refused(self, cluster, monkeypatch):
        monkeypatch.setattr(testing, "github_get", lambda path: {"sha": "f" * 40})

        with pytest.raises(SuiteNotRun) as exc:
            testing.run_suite("partition")

        assert (exc.value.code, exc.value.exit_code) == ("commit_mismatch", 1)
        assert cluster["github"] == []

    def test_a_pruned_pair_is_reported(self, cluster, monkeypatch):
        def gone(repository, digest):
            raise ImageNotFoundError("manifest missing")

        monkeypatch.setattr(testing, "resolve_ghcr_manifest", gone)

        with pytest.raises(SuiteNotRun) as exc:
            testing.run_suite("partition")

        assert (exc.value.code, exc.value.exit_code) == ("pair_pruned", 1)

    def test_no_pair_names_the_checkout_mode(self, cluster):
        cluster["locks"] = [_lock(source="")]

        with pytest.raises(SuiteNotRun, match="--source") as exc:
            testing.run_suite("partition")

        assert (exc.value.code, exc.value.exit_code) == ("unpaired", 1)

    def test_the_commits_verdict_decides_and_hears_the_suite_exit(self, cluster):
        _machinery(cluster["commit_tree"], verdict=1, verdict_line="FAIL: timed out after 3 tests")
        cluster["run_exit"] = 124

        result = testing.run_suite("partition")

        verdict_args = json.loads((cluster["commit_tree"] / "verdict-args.json").read_text())
        assert verdict_args[:2] == ["--exit-code", "124"]
        assert (result.passed, result.exit_code) == (False, 3)
        assert result.verdict == "FAIL: timed out after 3 tests"


class TestGuards:
    def test_a_borrowed_service_is_refused_before_any_fetch(self, cluster):
        cluster["locks"] = [_lock(pin=_pin())]

        with pytest.raises(SuiteNotRun, match="run 4321") as exc:
            testing.run_suite("partition")

        assert (exc.value.code, exc.value.exit_code) == ("service_borrowed", 2)
        assert cluster["github"] == [] and cluster["commands"] == []

    def test_an_environment_that_is_not_deployable_is_refused(self, cluster, monkeypatch):
        def not_ready():
            raise PinError("Environment is in maintenance")

        monkeypatch.setattr(testing, "require_deployable", not_ready)

        with pytest.raises(SuiteNotRun) as exc:
            testing.run_suite("partition")

        assert (exc.value.code, exc.value.exit_code) == ("not_deployable", 2)

    def test_a_pin_landing_during_the_suite_discards_its_result(self, cluster):
        cluster["locks"] = [_lock(), _lock(), _lock(pin=_pin())]

        with pytest.raises(SuiteNotRun, match="is discarded") as exc:
            testing.run_suite("partition")

        assert (exc.value.code, exc.value.exit_code) == ("target_changed", 2)
        assert _commands(cluster, ["docker", "run"])

    def test_a_borrow_and_restore_during_the_suite_discards_its_result(self, cluster):
        # The restore puts the lock entry back; only the rollout revision moved.
        cluster["revisions"] = ["7", "9"]

        with pytest.raises(SuiteNotRun, match="revision 9 during the run") as exc:
            testing.run_suite("partition")

        assert (exc.value.code, exc.value.exit_code) == ("target_changed", 2)

    def test_a_pod_off_the_lock_digest_after_the_suite_discards_its_result(
        self, cluster, monkeypatch
    ):
        calls = []

        def verify(service, ref):
            calls.append(ref)
            if len(calls) > 1:
                raise VerifyError("pod_mismatch", "no running pod carries it")

        monkeypatch.setattr(testing, "verify_service_image", verify)

        with pytest.raises(SuiteNotRun) as exc:
            testing.run_suite("partition")

        assert (exc.value.code, exc.value.exit_code) == ("target_changed", 2)
        assert calls == ["ghcr.io/acme/partition@sha256:service"] * 2

    @pytest.mark.parametrize(
        ("resolver_exit", "code", "exit_code"),
        [(2, "descriptor_rejected", 1), (3, "env_not_ready", 2), (4, "facts_contradiction", 1)],
    )
    def test_a_resolver_refusal_keeps_its_category(self, cluster, resolver_exit, code, exit_code):
        _machinery(cluster["commit_tree"], exit=resolver_exit)

        with pytest.raises(SuiteNotRun, match="WHY: because") as exc:
            testing.run_suite("partition")

        assert (exc.value.code, exc.value.exit_code) == (code, exit_code)
        assert not _commands(cluster, ["docker", "run"])

    def test_a_checkout_of_another_service_is_refused_before_maven(self, cluster, tmp_path):
        checkout = _checkout(tmp_path, service="storage")

        with pytest.raises(SuiteNotRun, match="declares service 'storage'") as exc:
            testing.run_suite("partition", checkout=checkout)

        assert (exc.value.code, exc.value.exit_code) == ("descriptor_mismatch", 1)
        assert not _commands(cluster, ["mvn", "-B"])

    @pytest.mark.parametrize(
        "verdict_body",
        [
            "import sys; sys.exit(2)",  # argparse refused the arguments
            "",  # an empty script exits 0 and prints nothing
        ],
    )
    def test_a_verdict_script_that_gives_no_verdict_refuses_the_run(self, cluster, verdict_body):
        (cluster["commit_tree"] / testing.VERDICT_PATH).write_text(verdict_body)

        with pytest.raises(SuiteNotRun, match="without a verdict") as exc:
            testing.run_suite("partition")

        assert (exc.value.code, exc.value.exit_code) == ("verdict_failed", 1)

    def test_a_report_schema_this_cli_does_not_know_is_refused(self, cluster):
        _machinery(cluster["commit_tree"], schema=2)

        with pytest.raises(SuiteNotRun) as exc:
            testing.run_suite("partition")

        assert exc.value.code == "report_schema"


class TestCheckoutRun:
    def test_native_maven_runs_the_suite_under_the_allowlisted_environment(
        self, cluster, monkeypatch, tmp_path
    ):
        checkout = _checkout(tmp_path)
        head = _head(checkout)
        cluster["locks"] = [_lock(tag=f"sha-{head[:12]}")]
        monkeypatch.setenv("STRAY_EXPORT", "stale")

        result = testing.run_suite(
            "partition", checkout=checkout, maven_arguments=["-Dtest=One", "test"]
        )

        [(mvn, kwargs)] = _commands(cluster, ["mvn", "-B"])
        assert mvn[mvn.index("--settings") + 1] == str(checkout / testing.SETTINGS_PATH)
        assert mvn[-2:] == ["-Dtest=One", "test"]
        assert kwargs["cwd"] == str(checkout.resolve() / "suite")
        assert kwargs["env"]["TOKEN"] == "a=b"
        assert "STRAY_EXPORT" not in kwargs["env"]
        assert cluster["github"] == []
        assert (result.mode, result.label, result.commit) == ("checkout", "matched", head)

    def test_windows_keeps_what_a_process_needs_to_start(self, cluster, monkeypatch, tmp_path):
        monkeypatch.setattr(testing, "_ON_WINDOWS", True)
        monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
        monkeypatch.setenv("USERPROFILE", r"C:\Users\dev")
        seen: dict = {}
        real = testing.resolve_suite

        def spy(service, root, suite, facts, work, bearers, overrides):
            contract, env_file = real(service, root, suite, facts, work, bearers, overrides)
            seen.update(json.loads((work / "report.json").read_text()))
            return contract, env_file

        monkeypatch.setattr(testing, "resolve_suite", spy)

        testing.run_suite("partition", checkout=_checkout(tmp_path))

        [(_, kwargs)] = _commands(cluster, ["mvn", "-B"])
        assert {"SYSTEMROOT", "USERPROFILE"} <= set(kwargs["env"])
        assert "SYSTEMROOT" in seen["seen_env"] and "USERPROFILE" not in seen["seen_env"]

    def test_reports_an_earlier_build_left_are_not_counted(self, cluster, tmp_path):
        checkout = _checkout(tmp_path)
        stale = checkout / "suite" / "target" / "surefire-reports"
        stale.mkdir(parents=True)
        (stale / "TEST-stale.xml").write_text('<testsuite tests="40"/>')

        result = testing.run_suite("partition", checkout=checkout)

        assert result.tests["tests"] == 2

    def test_a_dirty_tree_is_unmatched(self, cluster, tmp_path):
        checkout = _checkout(tmp_path)
        cluster["locks"] = [_lock(tag=f"sha-{_head(checkout)[:12]}")]
        (checkout / "suite" / "Edited.java").write_text("class Edited {}")

        result = testing.run_suite("partition", checkout=checkout)

        assert result.label == "unmatched"

    def test_a_fork_canonical_without_a_pair_still_matches_its_checkout(self, cluster, tmp_path):
        checkout = _checkout(tmp_path)
        cluster["locks"] = [_lock(tag=f"sha-{_head(checkout)[:12]}", pair="")]

        result = testing.run_suite("partition", checkout=checkout)

        assert result.label == "matched"

    def test_a_community_service_runs_unpaired(self, cluster, tmp_path):
        cluster["locks"] = [_lock(source="")]

        result = testing.run_suite("partition", checkout=_checkout(tmp_path))

        assert result.label == "unpaired"


class TestMintBearers:
    @pytest.fixture
    def callers(self, monkeypatch):
        from spi import bootstrap, token

        config = {
            "DEPLOY_IDENTITY_CLIENT_ID": "d",
            "MEMBER_IDENTITY_CLIENT_ID": "m",
            "NO_ACCESS_IDENTITY_CLIENT_ID": "n",
        }
        failing: set = set()

        def mint(caller):
            if caller in failing:
                raise token.TokenError("token exchange failed: HTTP 500")
            return token.MintedToken(f"{caller}-bearer", "", "id", "sa", "aud")

        monkeypatch.setattr(bootstrap, "read_cluster_config", lambda: config)
        monkeypatch.setattr(token, "mint_token", mint)
        return config, failing

    def test_an_identity_the_environment_lacks_stays_unset(self, callers):
        config, _ = callers
        del config["MEMBER_IDENTITY_CLIENT_ID"]

        assert testing.mint_bearers() == {
            "RESOLVER_TOKEN": "deploy-bearer",
            "RESOLVER_NO_ACCESS_TOKEN": "no_access-bearer",
        }

    def test_a_provisioned_identity_that_fails_to_mint_refuses_the_run(self, callers):
        _, failing = callers
        failing.add("member")

        with pytest.raises(SuiteNotRun, match="RESOLVER_MEMBER_TOKEN") as exc:
            testing.mint_bearers()

        assert (exc.value.code, exc.value.exit_code) == ("token_failed", 1)


class TestCommand:
    @pytest.mark.parametrize(
        ("raised", "outcome", "exit_code"),
        [
            (SuiteNotRun("service_borrowed", "borrowed", 2), "refused", 2),
            (SuiteNotRun("unpaired", "no pair"), "error", 1),
        ],
    )
    def test_a_suite_not_run_keeps_its_exit_code(self, monkeypatch, raised, outcome, exit_code):
        monkeypatch.setattr(cli, "_guarded_context", lambda output_json: "ctx")

        def run_suite(service, **kwargs):
            raise raised

        monkeypatch.setattr(testing, "run_suite", run_suite)

        result = CliRunner().invoke(cli.app, ["test", "partition", "--json"])

        envelope = json.loads(result.output.strip().splitlines()[-1])
        assert result.exit_code == exit_code
        assert (envelope["outcome"], envelope["code"]) == (outcome, raised.code)

    def test_a_failed_suite_exits_3_and_arguments_after_dashes_reach_maven(self, monkeypatch):
        monkeypatch.setattr(cli, "_guarded_context", lambda output_json: "ctx")
        seen = {}

        def run_suite(service, **kwargs):
            seen.update(kwargs)
            return testing.SuiteResult(
                service, "acceptance", "paired", "", "img", SHA, False, "FAIL: 1 of 3", {}
            )

        monkeypatch.setattr(testing, "run_suite", run_suite)

        result = CliRunner().invoke(
            cli.app, ["test", "partition", "--set", "A=b=c", "--json", "--", "-Dtest=X", "test"]
        )

        envelope = json.loads(result.output.strip().splitlines()[-1])
        assert result.exit_code == 3
        assert envelope["outcome"] == "failed"
        assert seen["maven_arguments"] == ["-Dtest=X", "test"]
        assert seen["overrides"] == {"A": "b=c"}


def _jwt(exp) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{claims}.signature"


class TestDryRun:
    def test_a_paired_plan_binds_the_suite_and_starts_nothing(self, cluster):
        plan = testing.plan_suite("partition", env_file="ide.env")

        image = f"ghcr.io/acme/partition-acceptance@{PAIR}"
        assert [cmd for cmd, _ in cluster["commands"] if cmd[0] == "docker"] == []
        assert plan.variables == {"HOST": "https://gw", "TOKEN": "a=b"}
        assert plan.shown == {"HOST": "https://gw", "TOKEN": "[redacted]"}
        assert plan.command[plan.command.index("--env-file") + 1] == "ide.env"
        assert plan.command[plan.command.index(image) + 1 :] == ("verify",)
        assert (plan.mode, plan.commit, plan.image) == ("paired", SHA, image)
        assert (plan.directory, plan.timeout_minutes) == ("/suite/suite", 5)

    def test_a_checkout_plan_names_the_native_command_and_where_it_runs(self, cluster, tmp_path):
        checkout = _checkout(tmp_path)

        plan = testing.plan_suite("partition", checkout=checkout, maven_arguments=["-Dtest=One"])

        assert _commands(cluster, ["mvn", "-B"]) == []
        assert plan.command[:3] == ("mvn", "-B", "--no-transfer-progress")
        assert plan.command[-3:] == (
            "--settings",
            str(checkout / testing.SETTINGS_PATH),
            "-Dtest=One",
        )
        assert plan.directory == str(checkout.resolve() / "suite")

    def test_a_plan_refuses_where_the_run_would(self, cluster, monkeypatch):
        def verify(service, ref):
            raise VerifyError("pod_mismatch", "no running pod carries it")

        monkeypatch.setattr(testing, "verify_service_image", verify)

        with pytest.raises(SuiteNotRun) as exc:
            testing.plan_suite("partition")

        assert (exc.value.code, exc.value.exit_code) == ("not_deployable", 2)

    def test_a_bearer_is_left_out_wherever_it_sits_and_dates_the_plan(self, cluster, monkeypatch):
        soon, later = _jwt(1790000000), _jwt(1790003600)
        monkeypatch.setattr(
            testing,
            "mint_bearers",
            lambda: {"RESOLVER_TOKEN": later, "RESOLVER_MEMBER_TOKEN": soon},
        )
        resolved = {
            "CALLER": soon,
            "AUTH": f"Bearer {later}",
            "FLAG": "true",
            "TOKEN_CHECK": "true",
        }
        monkeypatch.setattr(testing, "read_env_file", lambda path: resolved)

        plan = testing.plan_suite("partition")

        assert plan.shown == {
            "CALLER": "[redacted]",
            "AUTH": "Bearer [redacted]",
            "FLAG": "true",
            "TOKEN_CHECK": "[redacted]",
        }
        assert plan.variables == resolved
        assert plan.expires == "2026-09-21 14:13 UTC"

    def test_a_credential_in_an_argument_is_left_out_of_the_command(self, cluster, tmp_path):
        plan = testing.plan_suite(
            "partition",
            checkout=_checkout(tmp_path),
            maven_arguments=["-Dclient_secret=hunter2hunter2", "-Dtest=One", "test"],
        )

        assert plan.command[-3:] == ("-Dclient_secret=[redacted]", "-Dtest=One", "test")

    @pytest.mark.parametrize("quoted", ["RESOLVER_TOKEN", "CLIENT_SECRET"])
    def test_a_resolver_that_quotes_a_credential_in_its_refusal_is_not_repeated(
        self, cluster, monkeypatch, quoted
    ):
        _machinery(cluster["commit_tree"], exit=3, quote=quoted)
        monkeypatch.setattr(testing, "mint_bearers", lambda: {"RESOLVER_TOKEN": "opaque-bearer-1"})

        with pytest.raises(SuiteNotRun, match=r"WHY: because \[redacted\]$") as exc:
            testing.plan_suite("partition", overrides={"CLIENT_SECRET": "hunter2hunter2"})

        assert exc.value.code == "env_not_ready"

    def test_a_bearer_with_no_readable_expiry_leaves_the_plan_undated(self, cluster, monkeypatch):
        monkeypatch.setattr(
            testing, "mint_bearers", lambda: {"RESOLVER_TOKEN": _jwt(1e30), "OTHER": "opaque"}
        )

        assert testing.plan_suite("partition").expires == ""

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
    def test_the_env_file_is_the_owners_alone_even_over_an_open_one(self, tmp_path):
        path = tmp_path / "ide.env"
        path.write_text("STALE=1\n")
        path.chmod(0o644)

        testing.write_env_file(path, {"HOST": "https://gw", "TOKEN": "a=b"})

        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert testing.read_env_file(path) == {"HOST": "https://gw", "TOKEN": "a=b"}


class TestDryRunCommand:
    @pytest.fixture
    def guarded(self, cluster, monkeypatch):
        monkeypatch.setattr(cli, "_guarded_context", lambda output_json: "ctx")
        monkeypatch.setattr(
            testing, "run_suite", lambda *args, **kwargs: pytest.fail("a suite was started")
        )
        return cluster

    def test_the_panel_leaves_credentials_out_and_the_file_holds_them(self, guarded, tmp_path):
        path = tmp_path / "ide.env"

        result = CliRunner().invoke(
            cli.app, ["test", "partition", "--dry-run", "--env-file", str(path)]
        )

        assert result.exit_code == 0, result.output
        assert "https://gw" in result.output and "[redacted]" in result.output
        assert "a=b" not in result.output
        assert testing.read_env_file(path) == {"HOST": "https://gw", "TOKEN": "a=b"}

    def test_json_names_the_plan_without_its_credentials(self, guarded):
        result = CliRunner().invoke(cli.app, ["test", "partition", "--dry-run", "--json"])

        envelope = json.loads(result.output.strip().splitlines()[-1])
        [suite] = envelope["suites"]
        assert (result.exit_code, envelope["outcome"]) == (0, "planned")
        assert suite["variables"] == {"HOST": "https://gw", "TOKEN": "[redacted]"}
        assert suite["command"][:2] == ["docker", "run"]
        assert "a=b" not in result.output

    def test_a_plan_refused_keeps_the_runs_exit_code_and_writes_no_file(self, guarded, tmp_path):
        guarded["locks"] = [_lock(pin=_pin())]
        path = tmp_path / "ide.env"

        result = CliRunner().invoke(
            cli.app, ["test", "partition", "--dry-run", "--env-file", str(path), "--json"]
        )

        envelope = json.loads(result.output.strip().splitlines()[-1])
        assert (result.exit_code, envelope["code"]) == (2, "service_borrowed")
        assert envelope["suite"] == "acceptance"
        assert not path.exists()

    def test_the_command_names_the_file_where_it_was_written(self, guarded, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        result = CliRunner().invoke(
            cli.app, ["test", "partition", "--dry-run", "--env-file", "~/ide.env", "--json"]
        )

        envelope = json.loads(result.output.strip().splitlines()[-1])
        command = envelope["suites"][0]["command"]
        assert command[command.index("--env-file") + 1] == envelope["env_file"]
        assert (
            Path(envelope["env_file"]) == tmp_path / "ide.env" and (tmp_path / "ide.env").exists()
        )

    @pytest.mark.parametrize(
        "arguments",
        [
            ["--env-file", "ide.env"],
            ["--dry-run", "--report"],
            ["--dry-run", "--env-file", "ide.env", "--suite", "all"],
        ],
    )
    def test_flags_that_do_not_combine_are_refused_before_anything_binds(
        self, guarded, monkeypatch, tmp_path, arguments
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            testing, "plan_suite", lambda *args, **kwargs: pytest.fail("a suite was bound")
        )

        result = CliRunner().invoke(cli.app, ["test", "partition", *arguments])

        assert result.exit_code == 1
        assert not (tmp_path / "ide.env").exists()


class TestReport:
    def test_the_inspector_reads_the_run_before_it_is_discarded(self, cluster):
        seen = {}

        def inspect(reports, secrets):
            seen["reports"] = sorted(p.name for p in reports.rglob("TEST-*.xml"))
            seen["secrets"] = set(secrets)
            seen["where"] = reports
            return {"totals": {"tests": 11}}

        result = testing.run_suite("partition", inspect=inspect)

        assert seen["reports"] == ["TEST-a.xml"]
        # The bearers and what the env file holds under a credential's name; never HOST.
        assert seen["secrets"] == {"t", "m", "a=b"}
        assert not seen["where"].exists()
        assert result.report == {"totals": {"tests": 11}}
        assert testing.run_suite("partition").report is None

    def test_a_discarded_run_is_not_inspected(self, cluster):
        cluster["revisions"] = ["7", "9"]
        seen = []

        with pytest.raises(SuiteNotRun) as exc:
            testing.run_suite("partition", inspect=lambda *run: seen.append(run))

        assert exc.value.code == "target_changed"
        assert seen == []

    def test_an_env_file_that_cannot_be_read_costs_the_report_and_not_the_run(
        self, cluster, monkeypatch
    ):
        def unreadable(path):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        monkeypatch.setattr(testing, "read_env_file", unreadable)
        seen = []

        result = testing.run_suite("partition", inspect=lambda *run: seen.append(run))

        assert seen == [] and result.report is None
        assert (result.passed, result.verdict) == (True, testing.run_suite("partition").verdict)

    def test_a_run_names_every_suite_the_descriptor_declares(self, cluster, monkeypatch):
        real = testing.resolve_suite

        def resolve_suite(*args, **kwargs):
            contract, env_file = real(*args, **kwargs)
            return {**contract, "suites": {"acceptance": "a", "integration": "testing"}}, env_file

        monkeypatch.setattr(testing, "resolve_suite", resolve_suite)

        assert testing.run_suite("partition").declared == ("acceptance", "integration")


FACTS = {
    "totals": {
        "tests": 1,
        "passed": 1,
        "empty": 0,
        "failed": 0,
        "error": 0,
        "skipped": 0,
        "seconds": 1.0,
    },
    "classes": [
        {
            "name": "p.TestList",
            "seconds": 1.0,
            "tests": [{"name": "lists", "seconds": 1.0, "status": "passed"}],
        }
    ],
}


def _page_facts(envelope: dict) -> dict:
    from spi.suite_page import embedded

    facts = embedded(Path(envelope["report"]).read_text())
    assert facts is not None
    return facts


class TestReportCommand:
    @pytest.fixture
    def command(self, monkeypatch, tmp_path):
        """The command with its suites, contract, and reviewer past the process boundary."""
        import tempfile

        from spi import info, suite_contract, suite_review

        state: dict = {"runs": [], "failing": set(), "reviews": [], "declared": ("acceptance",)}

        def run_suite(service, *, suite, inspect, **kwargs):
            if suite in state.get("refusing", ()):
                raise SuiteNotRun("service_borrowed", "borrowed by run 4321", 2)
            state["runs"].append((suite, kwargs["maven_arguments"]))
            report = None
            if inspect is not None:
                suite_dir = tmp_path / "run" / suite
                (suite_dir / "src").mkdir(parents=True, exist_ok=True)
                (suite_dir / "src" / "TestList.java").write_text("class TestList {}")
                report = inspect(suite_dir, ("minted-bearer-value",))
            passed = suite not in state["failing"]
            verdict = "pass: 1 tests, 0 skipped" if passed else "FAIL: 1 of 1"
            commit = state.get("commits", {}).get(suite, SHA)
            image = "img" if commit == SHA else f"img@{commit[:4]}"
            return testing.SuiteResult(
                service, suite, "paired", "", image, commit, passed, verdict, {"tests": 1},
                report, state["declared"],
            )  # fmt: skip

        def review_suites(bundle, suites, contract, secrets):
            files = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()}
            state["reviews"].append((sorted(suites), files, contract, set(secrets)))
            if state.get("no_review"):
                raise suite_review.ReviewUnavailable("copilot is not on PATH")
            return {
                "reviewer": "copilot",
                "summary": "Proves the list answers.",
                "determination": {"suites": sorted(suites), "reason": "r"},
                "rows": [],
                "findings": [],
                "gaps": [],
                "unrecognized": 0,
            }

        def fetch_contract(endpoint):
            if state.get("no_contract"):
                raise suite_contract.ContractUnavailable("401")
            return {"source": endpoint + "api-docs", "rows": []}

        monkeypatch.setattr(cli, "_guarded_context", lambda output_json: "ctx")
        monkeypatch.setattr(cli, "_environment_facts", lambda: {"name": "dks"})
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "temp"))
        (tmp_path / "temp").mkdir()
        monkeypatch.setattr(testing, "run_suite", run_suite)
        monkeypatch.setattr(suite_review, "review_suites", review_suites)
        monkeypatch.setattr(suite_contract, "fetch_contract", fetch_contract)
        monkeypatch.setattr(info, "collect_endpoint", lambda service: f"https://gw/{service}/")
        monkeypatch.setattr("spi.suite_report.collect", lambda reports, secrets: dict(FACTS))
        state["temp"] = tmp_path / "temp"
        return state

    def _invoke(self, *args):
        result = CliRunner().invoke(cli.app, ["test", "partition", "--json", *args])
        return result.exit_code, json.loads(result.output.strip().splitlines()[-1])

    @pytest.mark.parametrize(("failing", "exit_code"), [(set(), 0), ({"acceptance"}, 3)])
    def test_a_report_is_written_for_either_verdict_and_changes_neither(
        self, command, failing, exit_code
    ):
        command["failing"] = failing

        code, envelope = self._invoke("--report")

        page = Path(envelope["report"])
        assert code == exit_code
        assert envelope["outcome"] == ("failed" if failing else "passed")
        assert page.parent.parent == command["temp"]
        assert page.name == "spi-test-partition-acceptance.html"
        [suite] = _page_facts(envelope)["suites"]
        assert (suite["name"], suite["passed"]) == ("acceptance", not failing)
        assert (page.parent / "spi-test-scoreboard.html").exists()
        assert command["reviews"] == []

    def test_without_the_flag_nothing_is_inspected_or_written(self, command):
        code, envelope = self._invoke()

        assert code == 0 and "report" not in envelope
        assert list(command["temp"].iterdir()) == []

    def test_all_runs_every_declared_suite_once_and_reviews_them_together(self, command):
        command["declared"] = ("acceptance", "integration")
        command["failing"] = {"integration"}

        code, envelope = self._invoke("--suite", "all", "--review")

        assert [suite for suite, _ in command["runs"]] == ["acceptance", "integration"]
        assert code == 3 and envelope["outcome"] == "failed"
        assert [(s["suite"], s["outcome"]) for s in envelope["suites"]] == [
            ("acceptance", "passed"),
            ("integration", "failed"),
        ]
        [(suites, files, contract, secrets)] = command["reviews"]
        assert suites == ["acceptance", "integration"]
        assert files == {
            "suites/acceptance/facts.json",
            "suites/acceptance/src/src/TestList.java",
            "suites/integration/facts.json",
            "suites/integration/src/src/TestList.java",
        }
        assert contract["source"] == "https://gw/partition/api-docs"
        assert secrets == {"minted-bearer-value"}
        page = Path(envelope["report"])
        assert page.name == "spi-test-partition-acceptance+integration.html"
        assert _page_facts(envelope)["review"]["reviewer"] == "copilot"

    def test_every_declared_suite_runs_once(self, command):
        command["declared"] = ("acceptance", "integration", "load")

        code, envelope = self._invoke("--suite", "all")

        assert [suite for suite, _ in command["runs"]] == ["acceptance", "integration", "load"]
        assert code == 0 and len(envelope["suites"]) == 3

    def test_a_rollout_between_suites_leaves_the_run_without_one_commit(self, command):
        command["declared"] = ("acceptance", "integration")

        _, same = self._invoke("--suite", "all", "--report")
        assert (same["commit"], same["image"]) == (SHA, "img")
        assert _page_facts(same)["run"]["commit"] == SHA

        command["commits"] = {"integration": "2" * 40}
        code, envelope = self._invoke("--suite", "all", "--report")
        told = CliRunner().invoke(cli.app, ["test", "partition", "--suite", "all"])

        assert code == 0 and envelope["commit"] is None and envelope["image"] is None
        assert [(s["suite"], s["image"], s["commit"]) for s in envelope["suites"]] == [
            ("acceptance", "img", SHA),
            ("integration", "img@2222", "2" * 40),
        ]
        facts = _page_facts(envelope)
        assert facts["run"]["commit"] == ""
        assert [suite["commit"] for suite in facts["suites"]] == [SHA, "2" * 40]
        assert "ran at different commits" in " ".join(told.output.split())

    def test_a_suite_not_run_ends_the_command_and_keeps_what_ran(self, command):
        command["declared"] = ("acceptance", "integration", "load")
        command["failing"] = {"acceptance"}
        command["refusing"] = {"integration"}

        code, envelope = self._invoke("--suite", "all", "--report")

        assert [suite for suite, _ in command["runs"]] == ["acceptance"]
        assert code == 2
        assert (envelope["outcome"], envelope["code"], envelope["suite"]) == (
            "refused",
            "service_borrowed",
            "integration",
        )
        assert [(s["suite"], s["outcome"]) for s in envelope["suites"]] == [
            ("acceptance", "failed")
        ]
        [suite] = _page_facts(envelope)["suites"]
        assert (suite["name"], suite["passed"]) == ("acceptance", False)

    def test_named_suites_run_in_the_order_given(self, command):
        code, envelope = self._invoke("--suite", "integration", "--suite", "acceptance")

        assert [suite for suite, _ in command["runs"]] == ["integration", "acceptance"]
        assert code == 0 and len(envelope["suites"]) == 2

    @pytest.mark.parametrize("missing", ["no_contract", "no_review"])
    def test_a_contract_or_review_that_cannot_be_had_costs_no_report(self, command, missing):
        command[missing] = True

        code, envelope = self._invoke("--review")

        facts = _page_facts(envelope)
        assert code == 0
        assert (facts["contract"] is None) is (missing == "no_contract")
        assert (facts["review"] is None) is (missing == "no_review")

    @pytest.mark.parametrize(
        ("broken", "written"),
        [
            ("spi.suite_page.render", False),
            ("spi.suite_report.write", False),
            ("spi.suite_page.render_scoreboard", True),
            ("spi.suite_review.review_suites", True),
            ("spi.info.collect_endpoint", True),
        ],
    )
    def test_nothing_that_breaks_in_the_report_reaches_the_verdict(
        self, command, monkeypatch, broken, written
    ):
        command["failing"] = {"acceptance"}

        def breaks(*args, **kwargs):
            # Rich reads [/login] as markup, and would raise on it.
            raise RuntimeError("run [/login] first")

        monkeypatch.setattr(broken, breaks)

        code, envelope = self._invoke("--review")

        assert code == 3 and envelope["outcome"] == "failed"
        assert ("report" in envelope) is written

    def test_sources_that_cannot_be_set_aside_cost_the_review_and_keep_the_report(
        self, command, monkeypatch
    ):
        def full(*args, **kwargs):
            raise OSError("No space left on device")

        monkeypatch.setattr("spi.suite_review.add_suite", full)

        code, envelope = self._invoke("--review")

        facts = _page_facts(envelope)
        assert code == 0 and command["reviews"] == []
        assert [suite["name"] for suite in facts["suites"]] == ["acceptance"]
        assert "review" not in facts

    def test_a_review_with_nowhere_to_set_sources_aside_still_runs_the_suite(
        self, command, monkeypatch
    ):
        import tempfile

        def full(*args, **kwargs):
            raise OSError("No space left on device")

        monkeypatch.setattr(tempfile, "TemporaryDirectory", full)

        code, envelope = self._invoke("--review")

        assert code == 0 and envelope["outcome"] == "passed"
        assert [suite for suite, _ in command["runs"]] == ["acceptance"]
        assert command["reviews"] == [] and "review" not in _page_facts(envelope)

    def test_a_review_that_cannot_be_drawn_leaves_the_runs_own_page(self, command, monkeypatch):
        from spi import suite_page

        drawn = suite_page.render

        def render(facts):
            if facts.get("review"):
                raise KeyError("severity")
            return drawn(facts)

        monkeypatch.setattr("spi.suite_page.render", render)

        code, envelope = self._invoke("--review")

        facts = _page_facts(envelope)
        assert code == 0 and len(command["reviews"]) == 1
        assert facts["review"] is None and facts["suites"][0]["name"] == "acceptance"

    def test_the_scoreboard_lands_beside_the_page(self, command, monkeypatch):
        made = []

        def fresh():
            made.append(command["temp"] / f"fallback-{len(made)}")
            made[-1].mkdir()
            return made[-1]

        monkeypatch.setattr("spi.suite_report.report_folder", fresh)

        _, envelope = self._invoke("--report")

        page = Path(envelope["report"])
        assert {path.name for path in page.parent.iterdir()} == {
            "spi-test-partition-acceptance.html",
            "spi-test-scoreboard.html",
        }

    def test_a_page_that_cannot_be_written_is_a_warning(self, command, monkeypatch):
        def full(*args, **kwargs):
            raise OSError("No space left on device")

        monkeypatch.setattr("spi.suite_report.write", full)

        result = CliRunner().invoke(cli.app, ["test", "partition", "--report"])

        assert result.exit_code == 0
        assert "The report could not be written: No space left on device" in result.output

    def test_facts_that_cannot_be_collected_cost_no_verdict(self, command, monkeypatch):
        def broken(reports, secrets):
            raise ValueError("unreadable")

        monkeypatch.setattr("spi.suite_report.collect", broken)

        code, envelope = self._invoke("--report")

        assert code == 0 and envelope["outcome"] == "passed" and "report" not in envelope

    def test_maven_arguments_go_to_one_named_suite(self, command):
        code, envelope = self._invoke("--suite", "all", "--", "-Dtest=X")

        assert code == 1 and envelope["outcome"] == "error"
        assert command["runs"] == []

        code, _ = self._invoke("--suite", "integration", "--", "-Dtest=X")

        assert code == 0 and command["runs"] == [("integration", ["-Dtest=X"])]


class TestLocalPinLabel:
    def _local(self, source_sha: str) -> dict:
        pin = _pin(
            repository="r.azurecr.io/local/partition",
            origin="local",
            ephemeral=False,
            run_id="",
            source_repo="",
            source_sha=source_sha,
        )
        return _lock(pin=pin)

    def test_a_checkout_at_the_built_commit_matches(self, tmp_path):
        checkout = _checkout(tmp_path)
        head = _head(checkout)
        deployed = testing.deployed_fork_commit(self._local(head), "partition")
        assert testing.checkout_label(checkout, deployed) == ("matched", head)

    def test_a_dirty_build_matches_no_checkout(self, tmp_path):
        checkout = _checkout(tmp_path)
        head = _head(checkout)
        deployed = testing.deployed_fork_commit(self._local(f"{head}-dirty"), "partition")
        assert testing.checkout_label(checkout, deployed) == ("unmatched", head)

    def test_a_local_pin_that_names_no_commit_is_unpaired(self, tmp_path):
        checkout = _checkout(tmp_path)
        deployed = testing.deployed_fork_commit(self._local(""), "partition")
        assert testing.checkout_label(checkout, deployed)[0] == "unpaired"
