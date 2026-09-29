# ADR-037: `spi test` Reports What a Suite Proves

## Context

A suite's verdict counts tests (ADR-036); it does not say what a passing test
proved. JUnit reports a test whose body holds no statement as a pass, so an
override that drops its parent's assertion keeps a green count. Which suite
guards a service is a second question the count cannot answer: it turns on
which rows of the service's contract each test asserts on, and how strongly,
and that is read from the suite's sources. Three conditions shape the answer.
The run's directory already holds the Surefire reports and the suite's
sources, and both are discarded when the run ends. The reports carry the
suite's captured output, where suites log the bearers the CLI minted. Reading
a test for what it proves is a judgment, and the CLI stores no credential a
model API would need (ADR-023).

## Decision

`spi test --report` writes one page per run from facts the CLI computes, and
`--review` adds a reviewer's reading of the sources to it. Neither takes part
in a verdict or an exit code. The facts live in `src/spi/suite_report.py`, the
contract in `src/spi/suite_contract.py`, the reviewer in
`src/spi/suite_review.py`, and the pages in `src/spi/suite_page.py`.

- **Facts are computed.** Status and duration come from the Surefire and
  Failsafe XML the verdict reads. A passing test is `empty` when the body
  that runs for it holds no statement. That body is the method of the
  reported name and parameter count in the reported class's own scope, or
  the one it inherits by following `extends` through the suite's Java
  sources to a parent the child's package or imports can see. A test the
  sources cannot settle (two declarations alike, a class two modules declare,
  a display name) is left unjudged. A test inherited from a parent the suite
  does not hold is marked `outside` with that parent's name. A file a
  link leads to outside the suite's directory is neither read nor handed to a
  reviewer: a suite copied out of an image can hold a link that names a file
  of the host.
- **Credentials stay behind.** The page keeps a failure's message, the first
  40 lines of its trace, and the last 40 lines of its captured output, and
  nothing a passing test wrote. Every text a report supplies, a test's name
  and class included, and the source comment shown beside an empty test, is
  redacted before it is cut to length. Removed: the
  bearers the run minted and each env file value whose name reads as a
  credential, where 8 characters or longer; any `Bearer` value; any
  JWT-shaped string; and any value of 6 characters or more assigned to a
  name holding `token`, `secret`, `password`, `credential`, or `api key`.
- **One file in the temporary directory.** The page is
  `spi-reports/spi-test-<service>-<suites>.html` under the system temporary
  directory: inline style, no JavaScript, no network reference, and the
  facts embedded as a JSON block. The folder is created with mode 0700, and a
  folder that is a link, another user's, or writable by others is left
  alone for a fresh private directory. A page replaces the last of its name, and each write deletes the
  CLI's pages older than 7 days. The page opens in the browser only at a
  terminal and never when `CI` is set.
- **Several suites, one page.** `--suite` repeats, and `--suite all` runs
  `acceptance` and then every suite the resolver's report lists under
  `contract.suites`. Each suite runs under its own guards and verdict
  (ADR-036). The first suite not run ends the command with its exit code,
  and the suites that ran before it keep their page. A failed suite makes
  the exit code 3 after the rest have run. Each suite resolves the deployed
  commit for itself, so a rollout between two suites leaves them at
  different commits; the page then says so and names each suite's commit.
- **The contract is the service's own.** `--review` reads `api-docs` at the
  service's endpoint over https, without a token. A row is one operation and
  one response it documents below 500, named `METHOD /path :: code`.
- **The reviewer maps and grades.** The reviewer reads a bundle of the
  contract rows, each suite's facts, and each suite's `.java` and `.feature`
  sources. It sees the Surefire reports only as the facts carry them: the
  statuses, the durations, and the redacted excerpts of failures. It names
  the row each test protects and grades what the test proves about it: 0
  nothing, 1 the status code, 2 the body or headers, 3 state read back after
  a write, or no grade for a body outside the sources. It adds the rows the
  suites exercise that the contract does not list, and names the suites the
  service needs: one, several, or none. The CLI keeps a grade
  only on tests that ran in that suite and were not skipped, sets a cell
  whose tests are all `empty` to 0, and sets a cell whose tests are all
  `outside` or `empty` to unread.
- **The reviewer holds three tools that read.** The reviewer is
  `copilot -p`. The model is Opus 5.5 at medium effort, and
  `SPI_TEST_REVIEW_MODEL` and `SPI_TEST_REVIEW_EFFORT` replace them. Copilot
  is given `view`, `grep`, and `glob` with `--available-tools`, the shell,
  write, and url permissions denied, the temporary directory disallowed, and
  an empty `COPILOT_HOME`, which holds no plugin and no configured MCP
  server. The working directory is the bundle, and the reviewer reads no
  file outside it.
- **Scores are arithmetic over the map.** A row belongs to the suite that
  grades highest on it, to `equal` on a tie, and to `neither` when no suite
  proves anything about it; among those, a row with a test that could not be
  read is `unread`. Hollow counts the `empty` tests and the passing tests
  graded 0 on every row that cites them. A review that places no test on a
  row is shown and not scored.
- **The scoreboard is rebuilt from the pages.** Each write also writes
  `spi-test-scoreboard.html` from the facts embedded in the saved pages, one
  row per service, taking the newest reviewed page where one exists. The
  folder is shared by every environment the user runs against, so a
  scoreboard of pages from more than one names each row's environment.
- **A report that cannot be had costs nothing.** Facts that fail to collect,
  an env file whose credentials cannot be read, a contract that does not
  answer, a reviewer that is missing or exits nonzero, and a file that
  cannot be written each print a warning and leave the verdict and exit
  code as they were. Sources that cannot be set aside for the reviewer, and
  an answer the page cannot draw, cost the review and leave the run's own
  page.

Rejected: judge an empty test by its duration. It needs no source, but a test
that returns after its setup runs as long as one that asserts.

Rejected: call a model API from the CLI. It removes the dependency on an
installed reviewer, but the CLI would hold an API key.

Rejected: let a finding fail the run. It would turn a hollow suite red, but
two reviews of one commit differ in what they select, and a verdict that
varies between runs of the same image gates nothing.

Rejected: hand the reviewer the Surefire reports. They hold the most
evidence about a failure, but their captured output holds the run's bearers.

Rejected: write the page beside the working directory. It needs no printed
path to find, but the page names the environment and lands in checkouts.

## Consequences

- The map is a reading, not a measurement. Three reviews of one partition
  commit each placed 30 rows with the same split and found the fifteen
  empty authentication tests. They differed in the findings below high, and
  one graded two create rows as state proven where the others found none.
  Every score inherits that variance, and the page says which reviewer,
  model, and effort produced it.
- `--review` sends the suites' sources and the run's facts to the reviewer's
  service. `--report` alone sends nothing.
- Redaction is by value and by pattern. A credential logged in a failure's
  output under a name that does not read as one reaches the page.
- The Java reader is not a compiler. It reads `.java` only, and it does not
  follow `implements`; a suite written in another language, and a test
  inherited from an interface's default method, get counts and no `empty`
  finding.
- A contract omits what the service hides from it. Partition's create, update,
  and delete operations are absent from its `api-docs`, so their rows exist
  only where a reviewer added them, and a hidden operation no test exercises
  has no row.
- Three runs of partition's two suites, 53 tests against 12 published rows,
  took 348, 357, and 387 seconds, of which the review took 154, 167, and 185.
- A page stays in the temporary directory until a later write finds it older
  than 7 days. Windows does not clear that directory; the CLI's own deletion
  is the only cleanup there.
- Copilot's sign-in survives the empty `COPILOT_HOME` on macOS. That is
  unproven on Linux and Windows.
- `copilot` is not part of `spi check`.
