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

"""The core DAG smoke's page: what it proved, where the time went, and what it stored.

It shares the suite pages' look. Its embedded facts carry no suites, so the
scoreboard passes over it.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Iterable, Mapping

from .suite_page import _document, _e, duration

PAGE_KIND = "dags"
DAG_PAGE_SCHEMA = 1
PHASES = (("before", "Before the runs"), ("runs", "The runs"), ("after", "After the runs"))
STEP_CHIPS = {"held": "passed", "failed": "failed", "refused": "failed", "not reached": "skipped"}
QUEUED = {"submitted", "queued", "scheduled"}
FINISHED = {"success", "finished"}
# Provenance leads the stored record so it shows above the pane's fold.
LEADING = ("id", "kind", "version", "createUser", "createTime")

_STYLE = """
.verdict.skip{background:var(--skip-bg);color:var(--skip)}
tr.phase td{background:var(--bg);font-size:11px;letter-spacing:.06em;text-transform:uppercase;
color:var(--muted);font-weight:600;padding-top:6px;padding-bottom:6px}
tr.quiet td{color:var(--muted)}
td.ev{font-family:var(--mono);font-size:12.5px;color:var(--muted);overflow-wrap:anywhere}
td.step i{font-style:normal;color:var(--muted);font-variant-numeric:tabular-nums;font-size:12px;
display:inline-block;min-width:1.8em}
.gantt{background:var(--surface);border:1px solid var(--line);border-radius:12px;
padding:16px 18px 10px;margin-bottom:12px}
.lane,.axis{display:grid;grid-template-columns:110px minmax(0,1fr);gap:12px;align-items:center}
.lane{margin-bottom:12px}
.lane .name{font-family:var(--mono);font-size:13px}
.track{position:relative;height:22px;background:var(--bg);border-radius:5px}
.seg{position:absolute;top:0;bottom:0;border-radius:4px;min-width:2px}
.seg.queued{background:var(--none)}.seg.running{background:var(--pass)}
.seg.reading{background:var(--note)}
.seg.wait{background:repeating-linear-gradient(45deg,var(--fail) 0 6px,var(--fail-bg) 6px 10px)}
.ticks{position:relative;height:18px;font-size:11px;color:var(--muted);
font-variant-numeric:tabular-nums;border-top:1px solid var(--line)}
.ticks span{position:absolute;top:2px;transform:translateX(-50%);white-space:nowrap}
.ticks span:first-child{transform:none}.ticks span:last-child{transform:translateX(-100%)}
.pair{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
.pane{background:var(--surface);border:1px solid var(--line);border-radius:12px;
padding:14px 16px;min-width:0}
.pane h3{margin:0 0 2px;font-size:14px}.pane p{font-size:12px;color:var(--muted)}
.pane pre{white-space:pre}
pre .hl{background:var(--pass-bg);color:var(--pass)}pre .bad{background:var(--fail-bg);color:var(--fail)}
@media (max-width:640px){.lane,.axis{grid-template-columns:84px minmax(0,1fr)}}
"""


def _steps(smoke: Mapping) -> list[dict]:
    return list((smoke.get("trace") or {}).get("steps") or ())


def _step_state(smoke: Mapping, name: str) -> str:
    return next((step["state"] for step in _steps(smoke) if step["name"] == name), "")


def segments(events: Iterable[Mapping]) -> list[tuple[str, float, float]]:
    """The spans between what the smoke observed, each named for what it was doing."""

    events = list(events)
    spans = []
    for event, after in zip(events, events[1:]):
        if event["event"] in QUEUED:
            kind = "queued"
        elif event["event"] == "running":
            kind = "running"
        elif event["event"] in FINISHED:
            kind = "reading" if after["event"] == "stored" else "wait"
        else:
            continue
        spans.append((kind, event["at"], after["at"]))
    return spans


def _verdicts(smoke: Mapping) -> str:
    checks = smoke.get("checks") or ()
    if not checks:
        return (
            '<div class="verdict fail"><strong>not run</strong>'
            f"<span>{_e(smoke.get('code'))}</span></div>"
        )
    kinds = {"passed": "pass", "failed": "fail"}
    return "".join(
        f'<div class="verdict {kinds.get(check["state"], "skip")}">'
        f"<strong>{_e(check['dag'])}: {_e(check['state'])}</strong>"
        f"<span>{_e(check.get('code') or check.get('detail'))}</span></div>"
        for check in checks
    )


def _note(smoke: Mapping) -> str:
    if smoke.get("outcome") != "refused":
        return ""
    lead = "Verdict discarded" if smoke.get("checks") else "Not run"
    return (
        f'<p class="note"><strong>{lead} ({_e(smoke.get("code"))}).</strong> '
        f"{_e(smoke.get('detail'))}</p>"
    )


def _tiles(smoke: Mapping) -> str:
    checks = smoke.get("checks") or ()
    steps = _steps(smoke)
    held = sum(step["state"] == "held" for step in steps)
    missed = [
        f"{count} {state}"
        for state in ("failed", "refused", "not reached")
        if (count := sum(step["state"] == state for step in steps))
    ]
    tiles = [
        (
            "passed" if held == len(steps) else "failed",
            "checks held" + (", " + ", ".join(missed) if missed else ""),
            f"{held} of {len(steps)}",
        )
    ]
    if checks:
        succeeded = sum(
            any(event["event"] in FINISHED for event in check.get("events") or ())
            for check in checks
        )
        spans = [span for check in checks for span in segments(check.get("events") or ())]
        last = max((event["at"] for c in checks for event in c.get("events") or ()), default=0)
        queued = sum(end - start for kind, start, end in spans if kind == "queued")
        tiles[:0] = [
            (
                "passed" if succeeded == len(checks) else "failed",
                "Workflow runs succeeded",
                f"{succeeded} of {len(checks)}",
            )
        ]
        tiles += [
            ("", "from the first submit to the last answer", duration(last)),
            ("", "of that spent submitted or queued", duration(queued)),
        ]
    return (
        '<section class="tiles">'
        + "".join(
            f'<div class="tile {kind}"><b>{_e(value)}</b><span>{_e(label)}</span></div>'
            for kind, label, value in tiles
        )
        + "</section>"
    )


def _proof(smoke: Mapping) -> str:
    steps = _steps(smoke)
    if not steps:
        return ""
    rows, number = "", 0
    for phase, title in PHASES:
        held = [step for step in steps if step["phase"] == phase]
        if not held:
            continue
        rows += f'<tr class="phase"><td colspan="3">{title}</td></tr>'
        for step in held:
            number += 1
            quiet = ' class="quiet"' if step["state"] == "not reached" else ""
            rows += (
                f'<tr{quiet}><td class="step"><i>{number}</i>{_e(step["name"])}</td>'
                f'<td class="state"><span class="chip {STEP_CHIPS.get(step["state"], "skipped")}">'
                f'{_e(step["state"])}</span></td><td class="ev">{_e(step["evidence"])}</td></tr>'
            )
    return (
        '<h2>What was proven</h2><div class="sheet"><table><thead><tr><th>Check</th>'
        f"<th>Outcome</th><th>Evidence</th></tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _ticks(last: float) -> tuple[float, list[int]]:
    """The axis length in seconds and its minute marks, at most six of them."""

    minutes = max(1, math.ceil(last / 60))
    step = math.ceil(minutes / 5)
    marks = list(range(0, minutes + step, step))
    return float(marks[-1] * 60), marks


def _clock(started: str, at: float) -> str:
    try:
        return (datetime.fromisoformat(started) + timedelta(seconds=at)).strftime("%H:%M:%S")
    except ValueError:
        return f"+{duration(at)}"


def _timeline(smoke: Mapping) -> str:
    checks = [check for check in smoke.get("checks") or () if check.get("events")]
    if not checks:
        return ""
    started = (smoke.get("trace") or {}).get("started") or ""
    last = max(event["at"] for check in checks for event in check["events"])
    scale, marks = _ticks(last)
    lanes, rows = "", ""
    for check in checks:
        events = check["events"]
        spans = segments(events)
        bars = "".join(
            f'<i class="seg {kind}" style="left:{100 * start / scale:.2f}%;'
            f'width:{100 * (end - start) / scale:.2f}%" '
            f'title="{kind} {_e(duration(end - start))}"></i>'
            for kind, start, end in spans
        )
        lanes += (
            f'<div class="lane"><span class="name">{_e(check["dag"])}</span>'
            f'<div class="track">{bars}</div></div>'
        )
        queued = sum(end - start for kind, start, end in spans if kind == "queued")
        ran = sum(end - start for kind, start, end in spans if kind == "running")
        ended = next(
            (event["at"] for event in events if event["event"] in FINISHED | {"failed"}), None
        )
        rows += (
            f'<tr><td class="test">{_e(check["dag"])}</td>'
            f'<td class="mono">{_e(_clock(started, events[0]["at"]))}</td>'
            f'<td class="mono">{_e(_clock(started, ended) if ended is not None else "-")}</td>'
            f"<td class=n>{_e(duration(queued))}</td><td class=n>{_e(duration(ran))}</td>"
            f"<td class=n>{_e(duration(events[-1]['at'] - events[0]['at']))}</td></tr>"
        )
    ticks = "".join(
        f'<span style="left:{100 * mark * 60 / scale:g}%">{f"{mark} m" if mark else "0"}</span>'
        for mark in marks
    )
    legend = "".join(
        f'<span><i style="background:var(--{color})"></i>{label}</span>'
        for color, label in (
            ("none", "submitted or queued"),
            ("pass", "running in Airflow"),
            ("note", "reading the record back from Storage"),
            ("fail", "waiting for a record that did not arrive"),
        )
    )
    return (
        '<h2>Where the time went</h2><div class="gantt">'
        f'{lanes}<div class="axis"><span></span><div class="ticks">{ticks}</div></div>'
        f'<div class="legend">{legend}</div></div>'
        '<div class="sheet"><table><thead><tr><th>DAG</th><th>Submitted</th><th>Finished</th>'
        '<th class="n">Queued</th><th class="n">Ran</th><th class="n">Total</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )


def _json(value: object, mark: str = "") -> str:
    lines = json.dumps(value, indent=2).splitlines()
    return "\n".join(
        f'<span class="{mark}">{_e(line)}</span>'
        if mark and line.lstrip().startswith('"createUser"')
        else _e(line)
        for line in lines
    )


def _record(smoke: Mapping) -> str:
    trace = smoke.get("trace") or {}
    submitted, stored = trace.get("submitted"), trace.get("stored")
    ingested = any(
        check["dag"] == "Osdu_ingest" and check.get("events") for check in smoke.get("checks") or ()
    )
    if not submitted or not ingested:
        return ""
    proven = _step_state(smoke, "createUser is the caller")
    mark = {"held": "hl", "failed": "bad"}.get(proven, "")
    if stored is None:
        held = "<p>Storage returned no record.</p>"
    else:
        first = {key: stored[key] for key in LEADING if key in stored}
        stored = {**first, **stored}
        held = f"<p>GET /records/{{id}} after the run</p><pre>{_json(stored, mark)}</pre>"
    return (
        '<h2>The record</h2><div class="pair">'
        '<div class="pane"><h3>Submitted in the manifest</h3>'
        f"<p>executionContext.manifest.ReferenceData[0]</p><pre>{_json(submitted)}</pre></div>"
        f'<div class="pane"><h3>Stored by Storage</h3>{held}</div></div>'
    )


def _targets(smoke: Mapping, run: Mapping) -> str:
    trace = smoke.get("trace") or {}
    targets = trace.get("targets") or {}
    if not targets:
        return ""
    after = _step_state(smoke, "Target unchanged")
    chip = {
        "held": '<span class="chip passed">unchanged</span>',
        "failed": '<span class="chip failed">changed</span>',
    }.get(after, '<span class="chip skipped">not checked</span>')
    rows = ""
    for service, target in sorted(targets.items()):
        image = ":".join(part for part in (target.get("repository"), target.get("tag")) if part)
        rows += (
            f'<tr><td>{_e(service)}</td><td class="ev">{_e(image or "-")}</td>'
            f'<td class="ev">{_e(target.get("digest") or "-")}</td>'
            f'<td class="n">{_e(target.get("revision") or "-")}</td><td>{chip}</td></tr>'
        )
    commit = str(run.get("commit") or "")[:12]
    rows += (
        f'<tr><td>stack</td><td class="ev">{_e(run.get("environment"))}</td>'
        f'<td class="ev">{_e(commit)}</td><td class="n"></td><td>{chip}</td></tr>'
    )
    return (
        '<h2>What it ran against</h2><div class="sheet"><table><thead><tr><th>Part</th>'
        '<th>Image</th><th>Digest or commit</th><th class="n">Revision</th><th>After the run</th>'
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
    )


def render(facts: Mapping) -> str:
    """The smoke's page, from facts the CLI computed; nothing here decides the verdict."""

    run, smoke = facts["run"], facts["smoke"]
    caller = smoke.get("caller") or {}
    where = " &middot; ".join(
        _e(part)
        for part in (
            run.get("environment"),
            f"partition {smoke['partition']}" if smoke.get("partition") else "",
            f"as {caller['as']} ({caller.get('id')})" if caller.get("as") else "",
            run.get("generated"),
        )
        if part
    )
    names = " + ".join(check["dag"] for check in smoke.get("checks") or ()) or "core DAG smoke"
    summary = _note(smoke) or f'<p class="summary">{_e(smoke.get("detail"))}</p>'
    body = (
        '<header class="top"><div><p class="kicker">spi test</p>'
        f"<h1>dags <span>{_e(names)}</span></h1>"
        f'<p class="where">{where}</p></div>'
        f'<div class="verdicts">{_verdicts(smoke)}</div></header>'
        f"{_tiles(smoke)}{summary}"
        f"{_proof(smoke)}{_timeline(smoke)}{_record(smoke)}{_targets(smoke, run)}"
        "<footer><p>The verdict is the smoke's own. Times are what the CLI observed while "
        "polling Workflow and Storage every few seconds, so each is accurate to one poll. "
        "The smoke record stays in its partition. Nothing on this page holds a token.</p>"
        f"<p>Written by spi {_e(run.get('cli'))}.</p></footer>"
    )
    return _document(f"dags {smoke.get('partition') or ''}".strip(), body, facts, _STYLE)
