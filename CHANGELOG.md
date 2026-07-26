# Changelog

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
