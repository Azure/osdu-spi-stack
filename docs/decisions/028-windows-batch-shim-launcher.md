---
status: "proposed"
contact: "danielscholl"
date: "2026-07-31"
deciders: "danielscholl"
---

# Launch Windows Batch Shims Through an Explicit, Escaped cmd.exe Command Line

## Context and Problem Statement

On native Windows, Azure CLI and some other prerequisite tools install as
`.cmd` batch shims. `CreateProcess` cannot execute a batch file, so the OS
relaunches it through `cmd.exe`, which re-parses the flat command line that
Python built from the argv list; `shell=False` constrains Python, not the OS.
Arguments containing CMD metacharacters are corrupted in transit: a path such
as `C:\src\a&b\template.bicep` splits at `&`, and `100%PATH%` expands from
the environment (issue #49). The CLI passes user paths, JMESPath queries, and
secret values through exactly this channel.

## Decision Drivers

- Argument values must reach the tool exactly as written, through both
  hostile parse stages (cmd.exe command-line phases, then the batch file's
  `%*` substitution into the target's argv parser).
- One launch path for the whole codebase; a fix that each call site must
  remember to apply will rot.
- A value that cannot be represented must fail loudly as a normal command
  failure, not corrupt silently or crash with a traceback; the failure
  message must never echo the value, which may be a secret.
- POSIX behavior must not change at all.
- The claim must be scoped to what is actually proven.

## Considered Options

- **Selected.** Escape and launch through an explicit `cmd.exe` command line,
  applying the mitigations published for the CVE-2024-24576 (BatBadBut) class
- Keep `shell=True` on Windows and pre-quote arguments per call site.
  Rejected: it puts the escaping burden on every call site, so a missed site
  silently reintroduces the bug.
- Bypass shims by invoking each tool's underlying executable directly.
  Rejected: it requires per-tool knowledge of each shim's internal layout,
  which changes with the vendor's installer.
- Reject any argument containing `%` when the target is a batch shim.
  Rejected: the CLI legitimately passes values containing `%`, so this trades
  working behavior for an unnecessary hard failure.

## Decision Outcome

Chosen option: "Escape and launch through an explicit `cmd.exe` command
line", because it keeps a single chokepoint (`spi.shell.run_process`), needs
no per-tool knowledge of where a shim's real executable lives, and preserves
the values the CLI actually passes. Every argument is quoted; there is no
unquoted fast path, and the escaping is verified end to end on a native
Windows runner rather than assumed. The only rejected inputs are newline and
NUL, which cmd.exe genuinely cannot deliver.

The percent neutralization (`%%cd:~,%`) and MSVCRT quote doubling are
established techniques for this class rather than inventions of this repo,
but the guarantee here rests on the end-to-end test, not on lineage: the
Windows CI job asserts the decoded argv a target receives through a real
`%*`-forwarding shim, so a wrong assumption fails the build.

Scope: the guarantee is stated for standard `%*`-forwarding shims (which is
what Azure CLI ships). A shim that re-parses its arguments again with
`call`, `%~1` re-expansion, or `setlocal enabledelayedexpansion` defeats any
command-line escaping scheme; cmd.exe also caps the command line at 8,191
characters. These limits are documented rather than papered over.

### Consequences

- Good, because every subprocess launch flows through one audited path and
  the end-to-end contract is enforced by a Windows CI job that fails if the
  Azure CLI shim is absent, rather than silently skipping.
- Good, because unrepresentable arguments surface as ordinary failed
  launches with a clear reason on stderr, reusing every existing error path.
- Bad, because on Windows `argv[0]` is rewritten to the resolved absolute
  path, and the transparency panel shows the logical argv rather than the
  serialized `cmd.exe` line; both are deliberate and documented in
  `spi.shell`.
- Bad, because arguments to batch shims grow slightly (quoting plus percent
  neutralization) against cmd.exe's 8,191-character ceiling.
