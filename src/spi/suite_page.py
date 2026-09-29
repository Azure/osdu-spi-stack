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

"""The pages: one service's suites with their summary, and the scoreboard across services.

A page embeds the facts it was drawn from, so the scoreboard is built by
reading the pages back.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Iterable, Mapping

from .suite_report import named_tests

PAGE_SCHEMA = 2
FACTS_ELEMENT_ID = "spi-test-facts"
SCOREBOARD_NAME = "scoreboard"
EQUAL = "equal"
NEITHER = "neither"
UNREAD = "unread"
GRADE_LABELS = {0: "nothing", 1: "status", 2: "body", 3: "state"}
STATUS_LABELS = {
    "passed": "Passed",
    "empty": "Empty",
    "failed": "Failed",
    "error": "Error",
    "skipped": "Skipped",
}
# Errors read as failures everywhere a color or a count is shown.
_GROUP = {"error": "failed"}
_EMBEDDED = re.compile(
    rf'<script type="application/json" id="{FACTS_ELEMENT_ID}">(.*?)</script>', re.S
)

# Suites take these in run order and start over past the last.
_SUITE_COLORS = ("a", "b", "note", "c", "d", "e")
_STYLE = """
:root{--bg:#f5f6f8;--surface:#fff;--text:#1a1f29;--muted:#5d6675;--line:#e1e5ea;
--pass:#1a7f4b;--pass-bg:#e4f4eb;--fail:#c4303c;--fail-bg:#fde9eb;--empty:#946200;
--empty-bg:#fff3d6;--skip:#5d6675;--skip-bg:#eceff3;--note:#3b6fd4;--note-bg:#e6eefb;
--a:#0d6b6b;--b:#b07a1c;--c:#7a4fb0;--d:#b04a72;--e:#5c7a1e;--eq:#9aa7a3;--none:#cfd5da;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#0f1318;--surface:#171c23;--text:#e7eaee;
--muted:#98a2b3;--line:#2a313b;--pass:#4cc38a;--pass-bg:#11301f;--fail:#ff6b76;
--fail-bg:#38161a;--empty:#f0b849;--empty-bg:#372a0b;--skip:#98a2b3;--skip-bg:#222831;
--note:#7aa2f7;--note-bg:#17233d;--a:#5cc0bd;--b:#e0a650;--c:#b79af0;--d:#ee8fb0;
--e:#a8c664;--eq:#6d7b77;--none:#3a4449}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:1080px;margin:0 auto;padding:32px 24px 56px}
h1{margin:0;font-size:30px;line-height:1.15;letter-spacing:-.01em}
h1 span{color:var(--muted);font-weight:500}
h2{margin:40px 0 12px;font-size:13px;letter-spacing:.08em;text-transform:uppercase;
color:var(--muted)}
h2.suite{font-size:20px;letter-spacing:0;text-transform:none;color:var(--text);
margin-top:56px;padding-top:24px;border-top:1px solid var(--line)}
h2.suite span{color:var(--muted);font-weight:500;font-size:14px}
p{margin:0}
a{color:var(--note)}
code,pre,.mono{font-family:var(--mono);font-size:13px}
.top{display:flex;gap:24px;justify-content:space-between;align-items:flex-start;flex-wrap:wrap}
.kicker{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);
margin-bottom:6px}
.where{margin-top:8px;color:var(--muted)}
.verdicts{display:flex;gap:10px;flex-wrap:wrap}
.verdict{border-radius:12px;padding:10px 16px;min-width:180px}
.verdict strong{display:block;font-size:16px}
.verdict span{font-family:var(--mono);font-size:12px}
.verdict.pass{background:var(--pass-bg);color:var(--pass)}
.verdict.fail{background:var(--fail-bg);color:var(--fail)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;
margin-top:28px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:12px;
padding:14px 16px}
.tile b{display:block;font-size:26px;line-height:1.1;font-variant-numeric:tabular-nums}
.tile span{font-size:12px;color:var(--muted)}
.tile.passed b{color:var(--pass)}.tile.empty b{color:var(--empty)}
.tile.failed b{color:var(--fail)}.tile.quiet b{color:var(--muted)}
.bar{display:flex;height:10px;border-radius:6px;overflow:hidden;background:var(--line);
min-width:140px}
.tiles+.bar{margin-top:14px}
.bar i{display:block}.bar .passed{background:var(--pass)}.bar .empty{background:var(--empty)}
.bar .failed{background:var(--fail)}.bar .skipped{background:var(--skip)}
.bar .eq{background:var(--eq)}.bar .none{background:var(--none)}.bar .unread{background:var(--skip)}
.bar .g1{background:var(--pass);opacity:.45}.bar .g2{background:var(--pass);opacity:.75}
.bar .g3{background:var(--pass)}.bar .g0{background:var(--empty)}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:13px;color:var(--muted);margin:10px 0}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
.note{margin-top:20px;border-radius:12px;padding:14px 18px;background:var(--empty-bg);
color:var(--empty)}
.judged{margin-top:20px;border-radius:12px;padding:16px 18px;background:var(--note-bg);
border-left:4px solid var(--note)}
.judged h3{margin:0 0 4px;font-size:17px}
.by{color:var(--muted);font-size:12px;margin-top:8px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;
padding:16px 18px;margin-bottom:12px}
.card.failed{border-left:4px solid var(--fail)}.card.empty{border-left:4px solid var(--empty)}
.card.skipped{border-left:4px solid var(--skip)}
.card h3{margin:0 0 4px;font-size:15px;font-family:var(--mono);overflow-wrap:anywhere}
.card .in{color:var(--muted);font-size:13px}
.card blockquote{margin:0 0 10px;padding:0;font-style:italic}
.card table{margin-top:10px}.card td{padding:8px 0}.card td.test{width:52%;padding-right:16px}
.chips{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.card td.why{color:var(--muted)}
.card.review ul{margin:8px 0 0;padding-left:18px}.card.review li{padding:3px 0;font-size:14px}
.card.review{border-left:4px solid var(--note)}
.summary{margin-top:10px;font-size:16px}
.columns{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:8px 32px;
margin-top:14px}
.columns h3{margin:0;font-family:inherit;font-size:12px;letter-spacing:.06em;
text-transform:uppercase;color:var(--muted)}
.card.finding{border-left:4px solid var(--skip)}.card.finding.high{border-left-color:var(--fail)}
.card.finding.medium{border-left-color:var(--empty)}
.card.finding h3{font-family:inherit;display:flex;gap:10px;align-items:center;
justify-content:space-between}
.message{margin-top:8px;overflow-wrap:anywhere}
pre{margin:8px 0 0;padding:12px;border-radius:8px;background:var(--bg);overflow:auto;
white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px}
details>summary{cursor:pointer}
details.more{margin-top:8px;color:var(--muted)}
.fold details{background:var(--surface);border:1px solid var(--line);border-radius:12px;
margin-bottom:8px}
.fold summary{display:flex;gap:12px;align-items:center;padding:12px 16px;list-style:none}
.fold summary::-webkit-details-marker{display:none}
.fold summary::before{content:"\\25B8";color:var(--muted)}
.fold details[open]>summary::before{content:"\\25BE"}
.fold .name{font-family:var(--mono);font-size:14px;overflow-wrap:anywhere}
.fold .package{color:var(--muted);font-size:12px;font-family:var(--mono)}
.fold .counts{margin-left:auto;display:flex;gap:6px;align-items:center;flex-wrap:wrap;
justify-content:flex-end}
.chip{border-radius:999px;padding:1px 9px;font-size:12px;font-variant-numeric:tabular-nums;
white-space:nowrap}
.chip.passed,.chip.g2,.chip.g3{background:var(--pass-bg);color:var(--pass)}
.chip.g1{background:var(--skip-bg);color:var(--pass)}
.chip.empty,.chip.g0,.chip.medium{background:var(--empty-bg);color:var(--empty)}
.chip.failed,.chip.high{background:var(--fail-bg);color:var(--fail)}
.chip.skipped,.chip.low,.chip.none{background:var(--skip-bg);color:var(--skip)}
.chip.ai{background:var(--note-bg);color:var(--note)}
.time{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums;white-space:nowrap}
table{width:100%;border-collapse:collapse}
.fold table{border-top:1px solid var(--line)}
td,th{padding:7px 16px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}
th{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);
font-weight:600;white-space:nowrap}
tr:last-child td{border-bottom:0}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.state{width:96px}td.took{width:90px;text-align:right}
td.test{font-family:var(--mono);font-size:13px;overflow-wrap:anywhere}
td.cell{width:24%}td.cell small{display:block;color:var(--muted);font-family:var(--mono);
font-size:11px;overflow-wrap:anywhere;margin-top:3px}
.sheet{background:var(--surface);border:1px solid var(--line);border-radius:12px;
overflow-x:auto}
.sheet tr.total td{font-weight:600;background:var(--bg)}
.undocumented{color:var(--muted);font-size:11px;margin-left:6px}
footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);
font-size:13px}
footer p+p{margin-top:6px}
@media (max-width:640px){main{padding:20px 16px 40px}h1{font-size:24px}
.fold .package{display:none}td,th{padding:7px 10px}}
@media print{body{background:#fff}.card,.tile,.fold details{break-inside:avoid}}
"""
_STYLE += "".join(
    f".bar .s{at}{{background:var(--{color})}}" for at, color in enumerate(_SUITE_COLORS)
)


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes} m {rest:02d} s"


def _percent(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "0%"


def _simple(classname: str) -> tuple[str, str]:
    package, _, simple = classname.rpartition(".")
    return simple, package


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


# --- scoring ---------------------------------------------------------------


def score(facts: Mapping) -> dict | None:
    """What the review's rows add up to, or None without a review.

    A row belongs to the one suite that grades highest on it, to ``equal``
    when suites tie, and to ``neither`` when none proves anything about it. A
    row whose only tests could not be read is ``unread``, and those tests are
    not counted as hollow.
    """

    review = facts.get("review")
    if not review or not any(row["suites"] for row in review.get("rows") or ()):
        return None
    names = [suite["name"] for suite in facts["suites"]]
    split = {EQUAL: 0, **{name: 0 for name in names}, UNREAD: 0, NEITHER: 0}
    grades = {name: {grade: 0 for grade in GRADE_LABELS} for name in names}
    unread = {name: 0 for name in names}
    proving: dict[str, set[str]] = {name: set() for name in names}
    nothing: dict[str, set[str]] = {name: set() for name in names}
    categories = {}
    for row in review["rows"]:
        cells = {name: cell for name, cell in row["suites"].items() if name in grades}
        for name, cell in cells.items():
            if cell["grade"] is None:
                unread[name] += 1
                continue
            grades[name][cell["grade"]] += 1
            (proving if cell["grade"] else nothing)[name].update(cell["tests"])
        held = {name: cells.get(name, {}).get("grade") or 0 for name in names}
        best = max(held.values(), default=0)
        leaders = [name for name, grade in held.items() if grade == best]
        if best:
            category = leaders[0] if len(leaders) == 1 else EQUAL
        else:
            blind = any(cell["grade"] is None for cell in cells.values())
            category = UNREAD if blind else NEITHER
        split[category] += 1
        categories[row["id"]] = category
    hollow = {}
    for suite in facts["suites"]:
        name = suite["name"]
        status = {test: case["status"] for test, case in named_tests(suite)}
        empty = {test for test, held in status.items() if held == "empty"}
        # Hollow is a pass that proves nothing; a failure proves nothing else.
        quiet = {test for test in nothing[name] - proving[name] if status.get(test) == "passed"}
        hollow[name] = len(empty | quiet)
    return {
        "rows": len(review["rows"]),
        "documented": sum(1 for row in review["rows"] if row["documented"]),
        "split": split,
        "grades": grades,
        "unread": unread,
        "hollow": hollow,
        "categories": categories,
    }


# --- one suite's detail ----------------------------------------------------


def _tests(suite: Mapping, *statuses: str) -> list[tuple[str, dict]]:
    return [
        (entry["name"], test)
        for entry in suite["classes"]
        for test in entry["tests"]
        if test["status"] in statuses
    ]


def _tiles(totals: Mapping) -> str:
    failed = totals["failed"] + totals["error"]
    tiles = (
        ("", "ran", totals["tests"]),
        ("passed", "passed", totals["passed"]),
        ("empty" if totals["empty"] else "quiet", "empty", totals["empty"]),
        ("failed" if failed else "quiet", "failed", failed),
        ("quiet", "skipped", totals["skipped"]),
        ("", "time", duration(totals["seconds"])),
    )
    cells = "".join(
        f'<div class="tile {kind}"><b>{_e(value)}</b><span>{label}</span></div>'
        for kind, label, value in tiles
    )
    shares = (
        ("passed", totals["passed"]),
        ("empty", totals["empty"]),
        ("failed", failed),
        ("skipped", totals["skipped"]),
    )
    return f'<section class="tiles">{cells}</section>{_bar(shares)}'


def _bar(shares: Iterable[tuple[str, int]]) -> str:
    parts = "".join(
        f'<i class="{kind}" style="flex:{count}" title="{count} {kind}"></i>'
        for kind, count in shares
        if count
    )
    return f'<div class="bar">{parts}</div>'


def _failures(suite: Mapping) -> str:
    cards = []
    for classname, test in _tests(suite, "failed", "error"):
        more = "".join(
            f'<details class="more"><summary>{label}</summary><pre>{_e(test[key])}</pre></details>'
            for key, label in (("detail", "Stack trace"), ("output", "Captured output"))
            if test.get(key)
        )
        kind = f"{STATUS_LABELS[test['status']]}: {test['type']}" if test.get("type") else ""
        cards.append(
            f'<article class="card failed"><h3>{_e(test["name"])}</h3>'
            f'<p class="in">{_e(_simple(classname)[0])} &middot; {_e(kind)} &middot; '
            f"{duration(test['seconds'])}</p>"
            f'<p class="message">{_e(test.get("message", ""))}</p>{more}</article>'
        )
    return f"<h2>Failures</h2>{''.join(cards)}" if cards else ""


def _grouped(tests: Iterable[tuple[str, str]], chip: str) -> str:
    classes: dict[str, list[str]] = {}
    for simple, name in tests:
        classes.setdefault(name, []).append(simple)
    rows = "".join(
        f'<tr><td class="test">{_e(name)}</td><td><div class="chips">'
        + "".join(f'<span class="chip {chip}">{_e(simple)}</span>' for simple in simples)
        + "</div></td></tr>"
        for name, simples in sorted(classes.items())
    )
    return f"<table>{rows}</table>" if rows else ""


def _empties(suite: Mapping) -> str:
    groups: dict[str, list[tuple[str, str]]] = {}
    for classname, test in _tests(suite, "empty"):
        groups.setdefault(test.get("note", ""), []).append((_simple(classname)[0], test["name"]))
    cards = []
    for note, tests in groups.items():
        quote = (
            f"<blockquote>&ldquo;{_e(note)}&rdquo;</blockquote>"
            if note
            else '<p class="in">No comment in the source.</p>'
        )
        cards.append(
            f'<article class="card empty">{quote}<p class="in">'
            f"{_plural(len(tests), 'test')} with this body</p>{_grouped(tests, 'empty')}</article>"
        )
    return f"<h2>Empty tests</h2>{''.join(cards)}" if cards else ""


def _skipped(suite: Mapping) -> str:
    rows = "".join(
        f'<tr><td class="test">{_e(_simple(classname)[0])}.{_e(test["name"])}</td>'
        f'<td class="why">{_e(test.get("message", ""))}</td></tr>'
        for classname, test in _tests(suite, "skipped")
    )
    if not rows:
        return ""
    return f'<h2>Skipped</h2><article class="card skipped"><table>{rows}</table></article>'


def _classes(suite: Mapping) -> str:
    blocks = []
    for entry in suite["classes"]:
        simple, package = _simple(entry["name"])
        counts: dict[str, int] = {}
        for test in entry["tests"]:
            kind = _GROUP.get(test["status"], test["status"])
            counts[kind] = counts.get(kind, 0) + 1
        chips = "".join(
            f'<span class="chip {kind}">{counts[kind]} {kind}</span>'
            for kind in ("failed", "empty", "skipped", "passed")
            if counts.get(kind)
        )
        rows = "".join(
            f'<tr><td class="state"><span class="chip '
            f'{_GROUP.get(test["status"], test["status"])}">'
            f"{STATUS_LABELS[test['status']]}</span></td>"
            f'<td class="test">{_e(test["name"])}</td>'
            f'<td class="took time">{duration(test["seconds"])}</td></tr>'
            for test in entry["tests"]
        )
        attention = " open" if counts.get("failed") else ""
        blocks.append(
            f'<details{attention}><summary><span class="name">{_e(simple)}</span>'
            f'<span class="package">{_e(package)}</span><span class="counts">{chips}'
            f'<span class="time">{duration(entry["seconds"])}</span></span></summary>'
            f"<table>{rows}</table></details>"
        )
    return f'<h2>All tests</h2><section class="fold">{"".join(blocks)}</section>'


def _suite(suite: Mapping, mixed: bool = False) -> str:
    totals = suite["totals"]
    note = ""
    if totals["empty"]:
        ran = totals["passed"] + totals["empty"]
        note = (
            f'<p class="note"><b>{totals["empty"]} of {ran} passing tests have an empty body.</b> '
            "They pass without checking anything, and the verdict counts them as passes.</p>"
        )
    image = f" &middot; <code>{_e(suite['image'])}</code>" if suite.get("image") else ""
    if mixed:
        image += f" &middot; at <code>{_e(str(suite.get('commit') or 'no commit')[:12])}</code>"
    return (
        f'<h2 class="suite" id="suite-{_e(suite["name"])}">{_e(suite["name"])} '
        f"<span>{_e(suite.get('verdict'))} &middot; {_e(suite.get('provenance'))}{image}</span>"
        f"</h2>{_tiles(totals)}{note}{_failures(suite)}{_empties(suite)}{_skipped(suite)}"
        f"{_classes(suite)}"
    )


# --- the summary -----------------------------------------------------------


def _verdicts(suites: Iterable[Mapping]) -> str:
    return "".join(
        f'<div class="verdict {"pass" if suite.get("passed") else "fail"}">'
        f"<strong>{_e(suite['name'])}: {'passed' if suite.get('passed') else 'failed'}</strong>"
        f"<span>{_e(suite.get('verdict'))}</span></div>"
        for suite in suites
    )


def _split(split: Mapping[str, int], names: list[str]) -> tuple[str, str]:
    """The bar of a split and its legend; suites take the colors in run order."""

    kinds = {
        EQUAL: "eq",
        UNREAD: "unread",
        NEITHER: "none",
        **{name: f"s{at % len(_SUITE_COLORS)}" for at, name in enumerate(names)},
    }
    single = len(names) == 1
    labels = {
        EQUAL: "suites equal",
        UNREAD: "could not be read",
        NEITHER: "no suite" if not single else "untested",
        **{name: f"{name}" + ("" if single else " stronger or only") for name in names},
    }
    order = [name for name in (EQUAL, *names, UNREAD, NEITHER) if name in split]
    shares = [(kinds.get(name, "none"), split[name]) for name in order]
    legend = "".join(
        f'<span><i class="{kinds.get(name, "none")}" '
        f'style="background:var(--{_color(kinds.get(name, "none"))})"></i>'
        f"{_e(labels.get(name, name))} {split[name]}</span>"
        for name in order
        if split[name] or name in (NEITHER, *names)
    )
    return _bar(shares), f'<div class="legend">{legend}</div>'


def _color(kind: str) -> str:
    colors = {"eq": "eq", "none": "none", "unread": "skip"}
    colors.update({f"s{at}": color for at, color in enumerate(_SUITE_COLORS)})
    return colors.get(kind, "none")


def _scoreboard(facts: Mapping, scored: dict | None) -> str:
    names = [suite["name"] for suite in facts["suites"]]
    head = (
        "<tr><th>Suite</th><th>Verdict</th><th class=n>Ran</th><th class=n>Passed</th>"
        "<th class=n>Hollow</th><th class=n>Failed</th><th class=n>Skipped</th>"
        "<th class=n>Time</th>"
        + ("<th class=n>Rows proven</th><th>Strength</th>" if scored else "")
        + "</tr>"
    )
    body = ""
    for suite in facts["suites"]:
        totals, name = suite["totals"], suite["name"]
        hollow = scored["hollow"][name] if scored else totals["empty"]
        failed = totals["failed"] + totals["error"]
        extra = ""
        if scored:
            grades = scored["grades"][name]
            proven = sum(count for grade, count in grades.items() if grade)
            shares = [(f"g{grade}", grades[grade]) for grade in (3, 2, 1, 0)]
            shares.append(("unread", scored["unread"][name]))
            extra = f"<td class=n>{proven} of {scored['rows']}</td><td>{_bar(shares)}</td>"
        verdict = "passed" if suite.get("passed") else "failed"
        body += (
            f'<tr><td><a href="#suite-{_e(name)}">{_e(name)}</a></td>'
            f'<td><span class="chip {verdict}">{verdict}</span></td>'
            f"<td class=n>{totals['tests']}</td><td class=n>{totals['passed'] + totals['empty']}"
            f"</td><td class=n>{hollow}</td><td class=n>{failed}</td>"
            f"<td class=n>{totals['skipped']}</td><td class=n>{duration(totals['seconds'])}</td>"
            f"{extra}</tr>"
        )
    table = f'<div class="sheet"><table><thead>{head}</thead><tbody>{body}</tbody></table></div>'
    if not scored:
        return f"<h2>Suites</h2>{table}"
    bar, legend = _split(scored["split"], names)
    strength = (
        '<div class="legend">Strength: '
        + "".join(
            f'<span><i style="background:var(--{"empty" if grade == 0 else "pass"});'
            f'opacity:{(0.45, 0.75, 1)[grade - 1] if grade else 1}"></i>'
            f"S{grade} {label}</span>"
            for grade, label in sorted(GRADE_LABELS.items(), reverse=True)
        )
        + '<span><i style="background:var(--skip)"></i>S? could not be read</span></div>'
    )
    return f"<h2>Suites</h2>{table}{strength}<h2>Who protects the contract</h2>{legend}{bar}"


def _headline(facts: Mapping, scored: dict | None) -> str:
    suites = facts["suites"]
    ran = sum(suite["totals"]["tests"] for suite in suites)
    seconds = sum(suite["totals"]["seconds"] for suite in suites)
    if not scored:
        totals = {
            key: sum(suite["totals"][key] for suite in suites)
            for key in ("tests", "passed", "empty", "failed", "error", "skipped", "seconds")
        }
        return _tiles(totals)
    untested = scored["split"][NEITHER]
    hollow = sum(scored["hollow"].values())
    added = scored["rows"] - scored["documented"]
    tiles = (
        ("", f"contract rows, {added} beyond the published contract", scored["rows"]),
        (
            "failed" if untested else "quiet",
            "rows no suite proves anything about",
            f"{untested} · {_percent(untested, scored['rows'])}",
        ),
        ("empty" if hollow else "quiet", "tests that pass and prove nothing", hollow),
        ("", f"tests ran in {duration(seconds)}", ran),
    )
    return (
        '<section class="tiles">'
        + "".join(
            f'<div class="tile {kind}"><b>{_e(value)}</b><span>{_e(label)}</span></div>'
            for kind, label, value in tiles
        )
        + "</section>"
    )


def _written_by(review: Mapping) -> str:
    model = (
        f" ({_e(review['model'])}, {_e(review.get('effort'))} effort)"
        if review.get("model")
        else ""
    )
    return (
        f'<p class="by">Judged by {_e(review.get("reviewer"))}{model} from the suites\' sources. '
        "It is commentary and takes no part in any verdict.</p>"
    )


def _judged(facts: Mapping) -> str:
    review = facts.get("review")
    if not review:
        return ""
    judged = review.get("determination") or {}
    title = needed(judged, [suite["name"] for suite in facts["suites"]])
    heading = f"<h3>{_e(title)}</h3><p>{_e(judged.get('reason'))}</p>" if title else ""
    unplaced = ""
    if score(facts) is None:
        unplaced = (
            '<p class="summary"><b>No test could be placed on the contract, so nothing is '
            f"scored.</b> The reviewer cited {_plural(review.get('unrecognized', 0), 'test')} "
            "this run did not report.</p>"
        )
    return (
        f'<section class="judged">{heading}'
        f'<p class="summary">{_e(review.get("summary"))}</p>{unplaced}{_written_by(review)}'
        "</section>"
    )


def needed(judged: Mapping, ran: list[str]) -> str:
    """The reviewer's judgment of which suites the service needs, as a heading."""

    suites = judged.get("suites")
    if suites is None:
        return ""
    if not suites:
        return "No suite protects this service"
    if len(ran) == 1:
        return f"{suites[0]} is the one suite that ran"
    if len(suites) == 1:
        return f"{suites[0]} protects this service better"
    if len(suites) == len(ran):
        return "Each suite guards what the others cannot"
    return f"{', '.join(suites[:-1])} and {suites[-1]} are needed here"


def _list(title: str, items: list[str]) -> str:
    if not items:
        return ""
    rows = "".join(f"<li>{_e(item)}</li>" for item in items)
    return f"<div><h3>{title}</h3><ul>{rows}</ul></div>"


def _cell(cell: Mapping | None) -> str:
    if not cell:
        return '<td class="cell"><span class="chip none">&ndash;</span></td>'
    grade = cell["grade"]
    tests = "".join(f"<small>{_e(test)}</small>" for test in cell["tests"])
    chip = (
        '<span class="chip none">S? unread</span>'
        if grade is None
        else f'<span class="chip g{grade}">S{grade} {GRADE_LABELS[grade]}</span>'
    )
    return f'<td class="cell">{chip}{tests}</td>'


def _matrix(facts: Mapping, scored: dict | None) -> str:
    review = facts.get("review")
    if not review or not scored:
        return ""
    names = [suite["name"] for suite in facts["suites"]]
    operations: dict[str, list[dict]] = {}
    for row in review["rows"]:
        operations.setdefault(row["operation"], []).append(row)
    blocks = []
    for operation, rows in operations.items():
        counts: dict[str, int] = {}
        for row in rows:
            category = scored["categories"][row["id"]]
            counts[category] = counts.get(category, 0) + 1
        kinds = {EQUAL: "passed", UNREAD: "none", NEITHER: "none", **{n: "ai" for n in names}}
        chips = "".join(
            f'<span class="chip {kinds[category]}">{counts[category]} '
            f"{'untested' if category == NEITHER else _e(category)}</span>"
            for category in (*names, EQUAL, UNREAD, NEITHER)
            if counts.get(category)
        )
        hidden = "" if rows[0]["documented"] else '<span class="undocumented">not published</span>'
        body = "".join(
            f'<tr><td class="test">{_e(row["behavior"])}'
            + ("" if row["documented"] else '<span class="undocumented">added</span>')
            + "</td>"
            + "".join(_cell(row["suites"].get(name)) for name in names)
            + "</tr>"
            for row in rows
        )
        head = "<tr><th>Behavior</th>" + "".join(f"<th>{_e(name)}</th>" for name in names) + "</tr>"
        blocks.append(
            f'<details><summary><span class="name">{_e(operation)}</span>{hidden}'
            f'<span class="counts">{chips}</span></summary>'
            f"<table><thead>{head}</thead><tbody>{body}</tbody></table></details>"
        )
    contract = facts.get("contract") or {}
    source = (
        f'<p class="by">Published rows come from <code>{_e(contract["source"])}</code>: one per '
        "operation and documented response below 500. Rows marked added are behavior the suites "
        "exercise that the published contract does not list.</p>"
        if contract.get("source")
        else '<p class="by">The service published no contract, so every row is one the suites '
        "exercise.</p>"
    )
    return f'<h2>Contract matrix</h2>{source}<section class="fold">{"".join(blocks)}</section>'


def _findings(facts: Mapping) -> str:
    review = facts.get("review")
    if not review:
        return ""
    cards = "".join(
        f'<article class="card finding {_e(finding["severity"])}"><h3>{_e(finding["title"])} '
        f'<span class="chip {_e(finding["severity"])}">{_e(finding["severity"])}</span></h3>'
        + (f'<p class="in">{_e(finding["suite"])}</p>' if finding.get("suite") else "")
        + f'<p class="message">{_e(finding["detail"])}</p>'
        + _grouped((test.partition(".")[::2] for test in finding["tests"]), "low")
        + "</article>"
        for finding in review["findings"]
    )
    gaps = _list("No suite checks", review.get("gaps") or [])
    unchecked = f'<article class="card review">{gaps}</article>' if gaps else ""
    return f"<h2>Findings</h2>{cards}{unchecked}" if cards or unchecked else ""


def embed(facts: Mapping) -> str:
    body = json.dumps(facts, sort_keys=True).replace("<", "\\u003c")
    return f'<script type="application/json" id="{FACTS_ELEMENT_ID}">{body}</script>'


def embedded(page: str) -> dict | None:
    """The facts a page was drawn from, or None when it carries none this CLI reads."""

    found = _EMBEDDED.search(page)
    if found is None:
        return None
    try:
        facts = json.loads(found.group(1))
    except json.JSONDecodeError:
        return None
    usable = isinstance(facts, dict) and facts.get("schema") == PAGE_SCHEMA
    if not usable or not isinstance(facts.get("run"), dict):
        return None
    return facts if isinstance(facts.get("suites"), list) else None


def _document(title: str, body: str, facts: Mapping) -> str:
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>{_e(title)} &middot; spi test</title>"
        f"<style>{_STYLE}</style></head><body><main>{body}</main>{embed(facts)}</body></html>\n"
    )


def render(facts: Mapping) -> str:
    """One service's page: the summary, the contract matrix, then each suite in detail."""

    run = facts["run"]
    scored = score(facts)
    suites = facts["suites"]
    # A rollout between two suites leaves them at different commits.
    mixed = len({suite.get("commit") for suite in suites}) > 1
    where = " &middot; ".join(
        _e(part)
        for part in (
            run.get("environment"),
            "suites ran at different commits" if mixed else str(run.get("commit") or "")[:12],
            run.get("generated"),
        )
        if part
    )
    names = " + ".join(suite["name"] for suite in suites)
    body = (
        '<header class="top"><div><p class="kicker">spi test</p>'
        f"<h1>{_e(run.get('service'))} <span>{_e(names)}</span></h1>"
        f'<p class="where">{where}</p></div>'
        f'<div class="verdicts">{_verdicts(suites)}</div></header>'
        f"{_headline(facts, scored)}{_judged(facts)}{_scoreboard(facts, scored)}"
        f"{_findings(facts)}{_matrix(facts, scored)}"
        f"{''.join(_suite(suite, mixed) for suite in suites)}"
        "<footer><p>Each verdict is the fork's own verdict script over that run's reports. "
        "An empty test is a passing test whose body, as the suite's source declares it, holds "
        "no statement; a test whose source is not in the suite is not checked. Hollow counts "
        "the empty tests and the tests the review graded as proving nothing. A test that "
        "inherits its body from outside the suite's sources could not be read, and is neither.</p>"
        f"<p>Written by spi {_e(run.get('cli'))}.</p></footer>"
    )
    return _document(f"{run.get('service')} {names}", body, facts)


# --- the scoreboard --------------------------------------------------------


def latest(pages: Iterable[tuple[str, Mapping]]) -> list[tuple[str, Mapping]]:
    """Per service, the page to score: the newest one reviewed, else the newest."""

    chosen: dict[str, tuple[str, Mapping]] = {}
    for link, facts in pages:
        service = str(facts["run"].get("service"))
        held = chosen.get(service)
        if held is None or score(facts) is not None or score(held[1]) is None:
            chosen[service] = (link, facts)
    return [chosen[service] for service in sorted(chosen)]


def render_scoreboard(pages: Iterable[tuple[str, Mapping]], run: Mapping) -> str:
    """Every service side by side, from the pages saved for them."""

    chosen = latest(pages)
    # The folder holds the pages of every environment this user ran against.
    places = {facts["run"].get("environment") for _, facts in chosen}
    several = len(places) > 1
    where = "several environments" if several else (*places, run.get("environment"))[0]
    names: list[str] = []
    for _, facts in chosen:
        names += [suite["name"] for suite in facts["suites"] if suite["name"] not in names]
    totals = {"rows": 0, EQUAL: 0, UNREAD: 0, NEITHER: 0, **{name: 0 for name in names}}
    hollow = ran = 0
    scores, runs = "", ""
    for link, facts in chosen:
        service = _e(facts["run"].get("service"))
        scored = score(facts)
        cells = "".join(
            "<td>"
            + (
                f"{suite['totals']['tests']} ran, "
                f"{suite['totals']['failed'] + suite['totals']['error']} failed, "
                f"{suite['totals']['skipped']} skipped, {duration(suite['totals']['seconds'])}"
                if (suite := next((s for s in facts["suites"] if s["name"] == name), None))
                else '<span class="chip none">not run</span>'
            )
            + "</td>"
            for name in names
        )
        empty = sum(suite["totals"]["empty"] for suite in facts["suites"])
        quiet = sum(scored["hollow"].values()) if scored else empty
        hollow += quiet
        ran += sum(suite["totals"]["tests"] for suite in facts["suites"])
        runs += (
            f'<tr><td><a href="{_e(link)}">{service}</a></td>{cells}<td class=n>{quiet}</td>'
            f"<td>{_e(facts['run'].get('environment')) + ' &middot; ' if several else ''}"
            f"{_e(facts['run'].get('generated'))}</td></tr>"
        )
        if not scored:
            scores += (
                f'<tr><td><a href="{_e(link)}">{service}</a></td>'
                f'<td colspan="{5 + len(names)}"><span class="chip none">not reviewed</span> '
                "run it with --review to score it</td></tr>"
            )
            continue
        split = scored["split"]
        totals["rows"] += scored["rows"]
        for key in (EQUAL, UNREAD, NEITHER, *names):
            totals[key] += split.get(key, 0)
        judged = (facts["review"].get("determination") or {}).get("suites")
        better = "unjudged" if judged is None else " + ".join(judged) or "no suite"
        bar, _ = _split(_ordered(split, names), names)
        scores += (
            f'<tr><td><a href="{_e(link)}">{service}</a></td>'
            f'<td><span class="chip ai">{_e(better)}</span></td>'
            f"<td class=n>{scored['rows']}</td><td class=n>{split.get(EQUAL, 0)}</td>"
            + "".join(f"<td class=n>{split.get(name, 0)}</td>" for name in names)
            + f"<td class=n>{split[NEITHER]}</td><td>{bar}</td></tr>"
        )
    bar, legend = _split(_ordered(totals, names), names)
    scores += (
        f'<tr class="total"><td>All {len(chosen)}</td><td></td><td class=n>{totals["rows"]}</td>'
        f"<td class=n>{totals[EQUAL]}</td>"
        + "".join(f"<td class=n>{totals[name]}</td>" for name in names)
        + f"<td class=n>{totals[NEITHER]}</td><td>{bar}</td></tr>"
    )
    columns = "".join(f"<th class=n>{_e(name)}</th>" for name in names)
    untested = totals[NEITHER]
    tiles = (
        ("", "services with a saved report", len(chosen)),
        ("", "contract rows across them", totals["rows"]),
        (
            "failed" if untested else "quiet",
            "rows no suite proves anything about",
            f"{untested} · {_percent(untested, totals['rows'])}",
        ),
        ("empty" if hollow else "quiet", f"of {ran} tests pass and prove nothing", hollow),
    )
    body = (
        '<header class="top"><div><p class="kicker">spi test</p>'
        "<h1>Scoreboard <span>which suite protects each service</span></h1>"
        f'<p class="where">{_e(where)} &middot; {_e(run.get("generated"))}</p>'
        "</div></header>"
        '<section class="tiles">'
        + "".join(
            f'<div class="tile {kind}"><b>{_e(value)}</b><span>{_e(label)}</span></div>'
            for kind, label, value in tiles
        )
        + "</section>"
        f"<h2>Who protects the contract</h2>{legend}"
        '<div class="sheet"><table><thead><tr><th>Service</th><th>Needs</th>'
        f"<th class=n>Rows</th><th class=n>Equal</th>{columns}<th class=n>Neither</th>"
        f"<th>Split</th></tr></thead><tbody>{scores}</tbody></table></div>"
        "<h2>What ran</h2>"
        '<div class="sheet"><table><thead><tr><th>Service</th>'
        + "".join(f"<th>{_e(name)}</th>" for name in names)
        + f"<th class=n>Hollow</th><th>Run</th></tr></thead><tbody>{runs}</tbody></table></div>"
        "<footer><p>Each row is read from the report saved for that service, the newest one "
        "reviewed when there is one. The split and the better suite are a reviewer's reading of "
        "the sources and take no part in any verdict.</p>"
        f"<p>Written by spi {_e(run.get('cli'))}.</p></footer>"
    )
    return _document("scoreboard", body, {"schema": PAGE_SCHEMA, "run": dict(run)})


def _ordered(split: Mapping[str, int], names: list[str]) -> dict[str, int]:
    return {key: split.get(key, 0) for key in (EQUAL, *names, UNREAD, NEITHER)}


def read_pages(paths: Iterable[Path]) -> list[tuple[str, dict]]:
    """The facts of each saved page this CLI wrote, beside the page's file name."""

    pages = []
    for path in paths:
        try:
            facts = embedded(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if facts is not None and facts["suites"]:
            pages.append((path.name, facts))
    return pages
