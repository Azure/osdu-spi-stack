# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""spi test: pairing, the commit's own machinery, guards, and verdicts."""

import json
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
    report["error"] = {"code": "WHY", "detail": "because"}
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
    def test_an_identity_the_environment_lacks_stays_unset(self, monkeypatch):
        from spi import token

        def mint(caller):
            if caller == "member":
                raise token.TokenError("spi-cluster-config carries no MEMBER_IDENTITY_CLIENT_ID")
            return token.MintedToken(f"{caller}-bearer", "", "id", "sa", "aud")

        monkeypatch.setattr(token, "mint_token", mint)

        assert testing.mint_bearers() == {
            "RESOLVER_TOKEN": "deploy-bearer",
            "RESOLVER_NO_ACCESS_TOKEN": "no_access-bearer",
        }


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
