# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Run inside apache/airflow:3.2.2 after its entrypoint installs the core lock.

Mount the rendered spiDagFiles at /opt/spi/osdu and a writable project
directory at /opt/airflow/work. This check makes no Azure or cluster calls.
"""

import io
import json
import os
import subprocess
import sys
import tarfile
import time
from importlib.metadata import version
from pathlib import Path

import yaml

INPUTS = Path("/opt/spi/osdu")
WORK = Path("/opt/airflow/work")
DESTINATION = WORK / "dags"


def run(arguments: list[str], expected: int = 0) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, check=False)
    output = result.stdout + result.stderr
    print(output, flush=True)
    if result.returncode != expected:
        raise RuntimeError(f"expected exit {expected}, got {result.returncode}: {arguments}")
    return output


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    results: dict = {
        "python": sys.version,
        "packages": {
            package: version(package)
            for package in ("apache-airflow", "osdu-api", "osdu-airflow", "osdu-ingestion")
        },
    }
    if started := os.environ.get("SPI_PROOF_START_UNIX"):
        results["entrypoint_seconds"] = time.time() - float(started)
    start = time.monotonic()
    run([sys.executable, "-m", "pip", "check"])
    results["pip_check_seconds"] = time.monotonic() - start
    command = [sys.executable, str(INPUTS / "load.py"), "--destination", str(DESTINATION)]
    start = time.monotonic()
    run(command)
    results["load_seconds"] = time.monotonic() - start

    catalog = yaml.safe_load((INPUTS / "catalog.yaml").read_text())
    catalog["families"][1]["files"][0]["sha256"] = "0" * 64
    bad_catalog = WORK / "tampered.yaml"
    bad_catalog.write_text(yaml.safe_dump(catalog))
    start = time.monotonic()
    output = run(
        [
            sys.executable,
            str(INPUTS / "load.py"),
            "--catalog",
            str(bad_catalog),
            "--destination",
            str(WORK / "tampered-dags"),
        ],
        expected=1,
    )
    assert "core-ingestion/osdu-ingest-r3.py: sha256 mismatch" in output
    assert not (WORK / "tampered-dags").exists()
    results["tampered_hash_seconds"] = time.monotonic() - start

    bad_archive = WORK / "traversal.tar.gz"
    with tarfile.open(bad_archive, "w:gz") as archive:
        member = tarfile.TarInfo("repository/../../escape.py")
        member.size = 3
        archive.addfile(member, io.BytesIO(b"bad"))
    code = (
        "import sys; from pathlib import Path; sys.path.insert(0, '/opt/spi/osdu'); import load; "
        "load.fetch = lambda _: Path('/opt/airflow/work/traversal.tar.gz').read_bytes(); "
        "raise SystemExit(load.main())"
    )
    start = time.monotonic()
    output = run([sys.executable, "-c", code, "--destination", str(WORK / "traversal-dags")], 1)
    assert "unsafe relative path" in output and "escape.py" in output
    assert not (WORK / "traversal-dags").exists()
    results["traversal_seconds"] = time.monotonic() - start

    os.environ["AIRFLOW__CORE__LOAD_EXAMPLES"] = "false"
    os.environ["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] = "sqlite:////opt/airflow/work/airflow.db"
    run([sys.executable, "-m", "airflow", "db", "migrate"])
    start = time.monotonic()
    from airflow.dag_processing.dagbag import DagBag
    from osdu_airflow.utils.auth_utils import sanitize_impersonation_user_id
    from osdu_api.clients.base_client import BaseClient
    from osdu_api.model.http_method import HttpMethod

    bag = DagBag(dag_folder=str(DESTINATION), include_examples=False, safe_mode=False)
    declared = {dag for family in catalog["families"] for dag in family["dags"]}
    assert not bag.import_errors, bag.import_errors
    assert set(bag.dags) == declared, (sorted(bag.dags), sorted(declared))
    assert set(bag.dags["Echo_DAG"].task_ids) == {"list_packages_task"}
    assert set(bag.dags["airflow_monitoring"].task_ids) == {"echo"}
    results["parse_seconds"] = time.monotonic() - start
    results["dags"] = sorted(bag.dags)
    results["import_errors"] = bag.import_errors
    for caller in ("00000000-0000-0000-0000-000000000001", "person@example.com"):
        user = sanitize_impersonation_user_id(caller)
        headers = BaseClient.prepare_headers({}, "proof-partition", user, HttpMethod.GET)
        assert headers["on-behalf-of"] == caller
        assert headers["x-on-behalf-of"] == caller
    results["caller_headers"] = "deploy GUID and person forwarded"
    (WORK / "proof.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2), flush=True)


if __name__ == "__main__":
    main()
