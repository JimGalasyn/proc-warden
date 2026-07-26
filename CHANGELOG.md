# Changelog

## 0.1.1 — 2026-07-26

First published version: 0.1.0 existed only as a local commit.

### Packaging

- Installable from PyPI as `proc-warden`, providing both the `proc` and
  `proc-warden` console scripts. Still **zero runtime dependencies**; the CLI
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
