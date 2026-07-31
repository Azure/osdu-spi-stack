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

- Argument values must reach the tool byte-for-byte as written, through both
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

- Escape and launch through an explicit `cmd.exe` command line, mirroring the
  Rust standard library's CVE-2024-24576 mitigation
- Keep `shell=True` on Windows and pre-quote arguments per call site
- Bypass shims by invoking each tool's underlying executable directly

## Decision Outcome

Chosen option: "Escape and launch through an explicit `cmd.exe` command
line", because it is the only approach with a proven public lineage (Rust
std's `%%cd:~,%` percent neutralization and MSVCRT quote doubling), keeps a
single chokepoint (`spi.shell.run_process`), and needs no per-tool knowledge
of where a shim's real executable lives. Every argument is quoted; there is
no unquoted fast path and no input rejection beyond newline and NUL, which
cmd.exe genuinely cannot deliver.

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
