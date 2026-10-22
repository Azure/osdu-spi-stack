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


"""The DAG smoke page says only what the smoke observed."""

from spi import dag_page

RUN = {"environment": "dev v1", "commit": "a" * 40, "generated": "now", "cli": "1.0"}


def step(name, state="held", phase="runs", evidence="seen"):
    return {"phase": phase, "name": name, "state": state, "evidence": evidence}


def page(**smoke):
    base = {"outcome": "passed", "code": None, "detail": "", "partition": "first", "checks": []}
    return dag_page.render({"run": RUN, "smoke": {**base, **smoke}})


def events(*pairs):
    return [{"event": name, "at": at} for name, at in pairs]


def test_a_run_that_ends_between_polls_is_not_drawn_as_running():
    held = dag_page.timing(events(("submitted", 0.5), ("success", 6.0)))
    assert held == {"submitted": 0.5, "running": None, "ended": 6.0, "last": 6.0}
    drawn = page(
        checks=[
            {
                "dag": "Echo_DAG",
                "state": "passed",
                "events": events(("submitted", 0.5), ("success", 6.0)),
            }
        ],
        started="2026-10-22T19:00:00+00:00",
    )
    assert "not seen" in drawn and "19:00:06" in drawn


def test_a_refusal_with_no_checks_is_not_a_green_tile():
    drawn = page(outcome="refused", code="service_borrowed", detail="workflow is borrowed")
    assert "0 of 0" in drawn
    assert "tile passed" not in drawn
    assert "Not run (service_borrowed)" in drawn


def test_read_back_is_timed_only_after_the_run_ended():
    check = {
        "dag": "Osdu_ingest",
        "state": "failed",
        "events": events(
            ("submitted", 0), ("running", 5), ("finished", 20), ("record_mismatch", 22)
        ),
    }
    drawn = page(outcome="failed", checks=[check], submitted={"id": "r"}, stored=None)
    assert "<td class=n>2.0 s</td>" in drawn and "Storage returned no record" in drawn


def test_a_failed_provenance_marks_createuser_in_the_stored_record():
    check = {"dag": "Osdu_ingest", "state": "failed", "events": events(("submitted", 0))}
    drawn = page(
        outcome="failed",
        checks=[check],
        steps=[step("createUser is the caller", "failed")],
        submitted={"id": "r"},
        stored={"id": "r", "createUser": "workload"},
    )
    assert '<span class="bad">  &quot;createUser&quot;: &quot;workload&quot;</span>' in drawn


def test_text_from_services_is_escaped():
    drawn = page(steps=[step("x", evidence="<script>alert(1)</script>")])
    assert "<script>alert" not in drawn and "&lt;script&gt;" in drawn
