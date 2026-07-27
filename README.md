# proc-warden

[![CI](https://github.com/JimGalasyn/proc-warden/actions/workflows/ci.yml/badge.svg)](https://github.com/JimGalasyn/proc-warden/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/JimGalasyn/proc-warden/branch/main/graph/badge.svg)](https://codecov.io/gh/JimGalasyn/proc-warden)
[![CodeQL](https://github.com/JimGalasyn/proc-warden/actions/workflows/codeql.yml/badge.svg)](https://github.com/JimGalasyn/proc-warden/actions/workflows/codeql.yml)
[![Release](https://img.shields.io/github/v/release/JimGalasyn/proc-warden?include_prereleases)](https://github.com/JimGalasyn/proc-warden/releases)
[![PyPI](https://img.shields.io/pypi/v/systemd-proc)](https://pypi.org/project/systemd-proc/)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21611649.svg)](https://doi.org/10.5281/zenodo.21611649)
[![Python](https://img.shields.io/pypi/pyversions/systemd-proc)](https://pypi.org/project/systemd-proc/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **Status: alpha (0.1.x).** The exit-code contract and state names are stable;
> everything else may change without notice until 1.0.

**Process lifetime you can reason about.** One small CLI, `proc`, that makes
launching, watching, and killing local processes unambiguous — so that neither you
nor an agent ever has to guess whether something is running, why it stopped, or
who is holding the GPU.

It is a thin wrapper over `systemd-run --user`. systemd is already the supervisor;
this repo contributes the *protocol*, not the machinery.

## The problem it solves

Four familiar failure modes, three of which are the same bug:

| Symptom | Root cause |
| --- | --- |
| A queue wrapper waits forever on `pgrep -f "python.*train"` | **Identity inferred from command-line text** — the wrapper's own argv matches the pattern, so it waits for itself |
| "Is that job still running, or did it die an hour ago?" | Same: nothing durable ties a name to a process |
| GPU memory still occupied after the job is gone | `kill $PID` kills the parent; XLA compile workers and forked children survive |
| A log goes quiet and you can't tell finished from hung | No distinction between *ready*, *exited*, and *timed out* |

`proc` fixes the class, not the instances: a process is identified by a **name you
choose at launch**, held by the kernel in a cgroup, with its exit status recorded
where it outlives the process.

## Install

```bash
pip install systemd-proc
```

> The PyPI distribution is **`systemd-proc`**, not `proc-warden`: PyPI already has
> an unrelated `procwarden` (a Python library for supervising subprocesses inside
> one program), and it compares names with separators stripped, so `proc-warden`
> collides with it. The repo, the import package `proc_warden`, and the commands
> are unaffected.

That installs two console scripts, `proc` and `proc-warden`, which are the same
program. Every example here and in `skills/proc-lifecycle/SKILL.md` says `proc`;
use `proc-warden` if you already have something else called `proc`.

There are **no runtime dependencies**, deliberately — `proc` is a protocol over
tools you already have, and a dependency would undercut that claim. From a clone,
the repo runs with nothing installed at all:

```bash
ln -s "$PWD/proc" ~/.local/bin/proc               # or anywhere on PATH
mkdir -p ~/.claude/skills
ln -s "$PWD/skills/proc-lifecycle" ~/.claude/skills/proc-lifecycle
```

Requires Linux with a systemd **user** manager (`systemctl --user is-system-running`
must not say `offline`), `/usr/bin/flock`, and Python 3.10+. Works under WSL2 —
see *Limitations*.

## Use

```bash
# launch something long, come back to it later
proc run pond -- python -m planktonica
proc status pond
proc logs pond -f                       # follows, and stops when the process does
proc stop pond                          # SIGTERM then SIGKILL the whole cgroup

# a server, with a real readiness check instead of `sleep 5`
proc run api -- python -m myapp.server
proc wait api --ready 'Uvicorn running' --timeout 60 || echo "never came up"

# a GPU job that holds an exclusive lease on device 0
proc run train --gpu -- python train.py           # fails fast if the GPU is taken
proc run train2 --gpu --gpu-wait 3600 -- python train2.py   # queues for it instead

proc ls          # every run, one greppable line each
proc gpu         # device memory and who holds each lease
proc gc          # reap finished units, report GPU memory that never came back
```

Everything after `--` is the command, verbatim.

## Exit codes

`proc` is meant to be scripted, so the codes are a contract:

| Code | Meaning |
| --- | --- |
| 0 | the asked-for condition holds (ready, or exited cleanly) |
| 1 | the managed process failed, or died before becoming ready |
| 2 | **timeout** — we stopped waiting; the process is untouched and still running |
| 3 | usage error, or no such run |
| 4 | busy — that name is already running, or the GPU lease is held |
| 5 | this machine can't support the operation |

`wait` never kills anything. A timeout is a statement about *your* patience, not
about the process.

`run` reports what it can see by the time it returns: if the process is already
`FAILED`, `KILLED`, `OOM`, or `LOST` when it looks, that is exit 1. It looks only
once, so a process that dies a second later still exits 0 — detecting *that* is
what `wait` is for.

## States

`RUNNING`, `EXITED` (0), `FAILED` (nonzero), `KILLED` (signal), `OOM`,
`LEASE_BUSY` (never started; the GPU lease was held), and `LOST` — no exit status
was recorded and no unit exists, which is what you see after `wsl --shutdown`
takes the user manager with it. **`LOST` is not `RUNNING`, and it is not success.**

## Limitations

Stated plainly, because the whole point is to not be surprised:

- **No queue beyond depth 1.** A GPU lease serializes runs on one device, which is
  all an 8 GB laptop GPU can do anyway. For real campaign queueing (N configs,
  concurrency limits, groups) the right answer is [pueue](https://github.com/Nukesor/pueue),
  deliberately deferred — see `docs/DESIGN.md`.
- **`flock` ordering is not FIFO.** Several runs queued with `--gpu-wait` will each
  get the lease eventually, in no guaranteed order. To control order, serialize in
  the shell with `proc wait` between launches.
- **Per-process GPU attribution is unavailable under WSL2.** `nvidia-smi
  --query-compute-apps` returns an empty list even when memory is in use, so leak
  detection is baseline-diff and device-wide, not per-PID.
- **`PROC_HOME` must not contain spaces** (it is interpolated into a unit's shell
  redirection). `proc` refuses to start otherwise rather than misbehave.
- **A `--gpu-wait` run reports `RUNNING` while it is still queued** for the lease;
  its stdout stays empty until it acquires.
- **`PYTHONUNBUFFERED=1` is set by default**, because stdout is a file and Python
  would otherwise block-buffer a startup readiness marker out of existence. Override
  with `--env PYTHONUNBUFFERED=` if you need the default buffering.

## State on disk

`$PROC_HOME` (default `~/.local/state/proc`), reconstructible after a reboot:

```
runs/<name>/meta.json    # argv, resolved binary, cwd, env, GPU baseline, start time
runs/<name>/stdout       # stdout+stderr, merged, plain file
runs/<name>/status       # code=/status=/result= written by the unit's ExecStopPost
leases/gpu<N>.lock       # the lease; held by an fd, released by the kernel
```

## Tests

98 tests in two halves.

`tests/test_proc.py` drives the **real** systemd user manager — no mocks, because
the entire claim is that the kernel and systemd hold state we used to guess at.
Each test is named for the failure mode it forbids: the self-matching `pgrep`
deadlock, a `setsid`-escaped grandchild surviving `stop`, a vanished unit reading
as `RUNNING`, a readiness marker split across two writes.

`tests/test_unit.py` covers the decision-making around those calls — naming,
status parsing, the state machine, environment assembly, argv resolution, the
`--` split — and needs no systemd, so it runs anywhere.

```bash
pip install -e ".[test]"
pytest -q
```

No GPU is needed: a lease is just a lock file. Coverage requires subprocess
tracking to be meaningful (86% with, 38% without) — see
[docs/CONTRIBUTING.md](docs/CONTRIBUTING.md), which also covers what to do when a
globally installed pytest plugin breaks collection.

## Contributing

[docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) for layout and test conventions,
[docs/DESIGN.md](docs/DESIGN.md) for the six invariants and the rejected
alternatives, [docs/RELEASING.md](docs/RELEASING.md) for cutting a version.

## Citation

If you use this in published work, cite it via [CITATION.cff](CITATION.cff) —
GitHub renders a *Cite this repository* button from it.

## License

MIT — see [LICENSE](LICENSE).
