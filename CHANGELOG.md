# Changelog

## Unreleased

The three findings left over from the original review, each with a regression
test.

- **`logs` no longer reads the whole file to print the end of it.** `logs -n 20`
  slurped the entire log into memory first; on a multi-gigabyte training log —
  exactly what this tool exists to supervise — that is gigabytes to print twenty
  lines. It now reads backwards in 64 KiB blocks and stops as soon as it has the
  lines asked for. Streaming the whole log (`logs` with no `-n`) no longer buffers
  it either. Measured by a test that counts bytes read: ~4 MB file, 3 lines
  wanted, under 128 KiB read.
- **A failed launch no longer destroys the previous run's record.** The run
  directory was deleted before `systemd-run` was known to have succeeded, so a
  typo in the command threw away the logs and exit status of the run being
  replaced. The old directory is now moved aside and only discarded once the new
  unit is actually up; every failure path puts it back.
- **Two concurrent `proc run` of the same name can no longer corrupt each
  other.** Both passed the RUNNING check and the loser deleted the winner's run
  directory out from under a live unit. Launches are now serialized per name by
  an `flock`ed lock file, giving one clean winner and one clean `EX_BUSY`.
- **`meta.json` is written `0600`.** `--env` is the one path by which a real
  secret reaches that file, and it was world-readable. Created with the mode
  rather than `chmod`ed afterwards, so there is no window. Note that `--env` is
  still not a secret channel — the values are unit properties and visible to your
  own user; the README says so plainly now.
- Fixed in passing: when a followed process died, `logs -f` read the bytes
  written in its last moments and discarded them instead of printing them.

## 0.1.3 — 2026-07-27

Documentation only; no behaviour change.

- **`skills/proc-lifecycle/SKILL.md` no longer points at a checkout.** It opened
  by locating `proc` at `~/repos/proc-warden/proc`, symlinked to `~/bin/proc` —
  both wrong for anyone else, and the skill is read on its own, away from the
  README, so an agent following it would look for a CLI that is not there. It now
  leads with `pip install systemd-proc`, names the PyPI/repo discrepancy at the
  point someone hits it, and keeps the checkout shim as an alternative.
- Dropped the claim that `proc` is "one stdlib-only Python file", which stopped
  being true when the CLI moved to `src/proc_warden/cli.py`. Zero runtime
  dependencies is the claim that matters and is still exact.
- README's clone instructions now use `~/.local/bin` and create it first. On
  Debian and Ubuntu `~/.profile` adds that directory to PATH *only if it already
  exists*, so without the `mkdir -p` the symlink fails and the directory would
  not be on PATH even once created.

## 0.1.2 — 2026-07-26

First version actually on PyPI, under the name **`systemd-proc`**.

`0.1.1` was tagged, released on GitHub, and archived by Zenodo, but its PyPI
upload never happened: the distribution name `proc-warden` turns out to be
permanently unavailable. PyPI compares names with `-`, `_`, and `.` stripped, and
`procwarden` was registered in May 2026 by an unrelated library. Rather than move
a tag that Zenodo had already minted a DOI for, the corrected package ships here.

- `pip install systemd-proc`. The repo, the import package `proc_warden`, and
  both console scripts (`proc`, `proc-warden`) are unchanged — only the
  `pip install` string differs.
- `docs/RELEASING.md` records why, so it does not get "fixed" back.

## 0.1.1 — 2026-07-26

Tagged and archived, but never published to PyPI — see 0.1.2. Cited by
[10.5281/zenodo.21611650](https://doi.org/10.5281/zenodo.21611650).

0.1.0 existed only as a local commit.

### Packaging

- Made installable, providing both the `proc` and `proc-warden` console scripts. Still **zero runtime dependencies**; the CLI
  moved to `src/proc_warden/cli.py`, and the root `proc` script is now a shim so
  a clone (or a symlink into one) keeps working with nothing installed.
- MIT `LICENSE`, `CITATION.cff`, and `.zenodo.json`.
- CI on Python 3.10–3.13: unit tests everywhere, integration tests against a real
  systemd user manager, a packaging job that builds and `twine check`s the
  artifacts and runs both console scripts, plus CodeQL and Codecov.
- `docs/CONTRIBUTING.md` and `docs/RELEASING.md`.

### Tests

- 60 unit tests covering naming, status parsing, the state machine, environment
  assembly, argv resolution, and the `--` split — none of which need systemd, so
  the suite is no longer all-or-nothing on the host having a user manager.
- `PROC_REQUIRE_SYSTEMD=1` turns the integration suite's skip into a hard error,
  so CI cannot pass by skipping the half that proves the tool works.
- Coverage now follows the subprocesses the integration tests spawn, which is the
  difference between a reported 38% and the real 86%.

### Fixes

Three defects found by review, each with a regression test that fails against 0.1.0.

- **`wait --ready` could miss a marker that was printed.** The incremental
  scanner consumed the partial trailing line, so a regex spanning two reads never
  matched and a ready process was reported as `TIMEOUT` — the false negative that
  invariant 4 exists to prevent. Since `PYTHONUNBUFFERED=1` makes
  `print("bodies:", n)` one `write()` per argument, splitting mid-marker was the
  common case rather than a corner one. The incomplete line is now carried
  forward between polls.
- **`proc run` now exits 1 when the process is already dead when it looks.** It
  returned 0 while printing `-> FAILED`, so `proc run ... || handle` caught a busy
  lease but silently missed a crash. A clean fast exit is still 0; a process that
  dies later is still `wait`'s job to report.
- **`--gpu-wait` without `--gpu` is now a usage error.** It was silently ignored,
  so the typo launched an unserialized GPU run that looked fine.

## 0.1.0 — 2026-07-26

First version. `proc`: a stdlib-only Python CLI over `systemd-run --user` giving
named runs, cgroup-wide kill, exit status that outlives the process, and exclusive
GPU leases.

- `run` (with `--gpu`, `--gpu-wait`, `--replace`, `--cwd`, `--env`,
  `--stop-timeout`), `status`, `ls`, `wait` (with `--ready`, `--timeout`), `logs`
  (with `-f`, `-n`), `stop`, `gpu`, `gc` (with `--purge`).
- Stable exit-code contract: 0 ok, 1 failed, 2 timeout, 3 usage, 4 busy,
  5 environment. `argparse` is subclassed so a bad command line exits 3 rather than
  colliding with the timeout code.
- States include `LEASE_BUSY` and `LOST`, so "never started" and "record vanished"
  are never mistaken for running or for success.
- Nested `--gpu` on an already-leased device is refused with an explanation rather
  than deadlocking on a second fd.
- The caller's environment is what runs: `argv[0]` resolves on the caller's `PATH`
  (bare name) or against `--cwd` (a path), an env allowlist is forwarded, and
  `PYTHONUNBUFFERED=1` is defaulted so readiness markers aren't lost to Python's
  stdout buffer.
- 33 tests against the real systemd user manager, each named for the failure mode
  it forbids. No GPU required.
- `skills/proc-lifecycle/SKILL.md` for agent use.

Deferred by design: queueing beyond a depth-1 GPU lease (adopt `pueue` when a
campaign needs concurrency limits — see `docs/DESIGN.md`).
