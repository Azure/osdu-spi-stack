# ADR-024: Windows Batch Shim Launcher via an Escaped cmd.exe Command Line

## Context

On native Windows, Azure CLI and other prerequisite tools install as `.cmd` batch shims. `CreateProcess` cannot execute a batch file, so the OS relaunches it through `cmd.exe`, which re-parses the flat command line built from Python's argv list; `shell=False` constrains Python, not the OS. Arguments containing CMD metacharacters corrupt in transit: a path such as `C:\src\a&b\template.bicep` splits at `&`, and `100%PATH%` expands from the environment. The CLI passes user paths, JMESPath queries, and secret values through exactly this channel. This is the CVE-2024-24576 (BatBadBut) class.

## Decision

Escape and launch through an explicit `cmd.exe` command line at the one chokepoint every subprocess already flows through, `spi.shell.run_process`, applying the published BatBadBut mitigations: MSVCRT quote doubling plus `%%cd:~,%` percent neutralization. Every argument is quoted, with no unquoted fast path, and POSIX behavior is unchanged. The only rejected inputs are newline and NUL, which cmd.exe cannot deliver; they fail as an ordinary command error whose message never echoes the value, which may be a secret.

The guarantee rests on an end-to-end test, not on lineage: a Windows CI job asserts the decoded argv a target receives through a real `%*`-forwarding shim, which is what Azure CLI ships. Two limits are documented rather than papered over: a shim that re-parses its arguments again (`call`, `%~1` re-expansion, delayed expansion) defeats any command-line escaping scheme, and cmd.exe caps the command line at 8,191 characters.

Rejected: keep `shell=True` and pre-quote per call site. A missed site silently reintroduces the bug.

Rejected: bypass shims by invoking each tool's underlying executable. Requires per-tool knowledge of shim internals that change with the vendor's installer.

Rejected: reject arguments containing `%` when the target is a batch shim. The CLI legitimately passes such values.

## Consequences

- Every subprocess launch flows through one audited path; the CI job fails if the Azure CLI shim is absent rather than silently skipping.
- Unrepresentable arguments surface as ordinary failed launches with the reason on stderr.
- On Windows `argv[0]` is rewritten to the resolved absolute path, and the transparency panel shows the logical argv rather than the serialized `cmd.exe` line.
- Quoting plus percent neutralization grows arguments against the 8,191-character ceiling.
