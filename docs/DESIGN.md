# Design

## The thesis

Almost every local-process failure mode I hit is a consequence of **inferring
identity from text**. `pgrep -f "python.*train"`, `ps aux | grep`, "the log stopped
moving so it's probably done" — all of these ask a question the operating system
can answer exactly, and answer it with a guess instead.

The fix is not a better regex or a more careful wrapper. It is to assign identity
**at launch** and let the kernel hold it. Once you do that, the other problems
dissolve: killing becomes exact (a cgroup), exclusion becomes safe (an fd), and
status survives the process (a recorded exit code).

`systemd --user` already provides all three. So this repo is deliberately thin:
**a protocol and its enforcement, not a supervisor.**

## The six invariants

Each one exists to forbid a specific failure I have actually hit.

### 1. Identity is a name, never a pattern

Every run is `proc-<name>.service`. Nothing in the codebase greps `ps` output.
Launching a second run under a live name is refused (`EX_BUSY`) rather than
silently racing.

*Forbids:* the self-matching `pgrep` deadlock. `tests/test_proc.py::test_own_argv_never_self_matches`
launches two processes whose argv both contain `sleep 27` — the exact ambiguity
that hangs a text-matching wrapper — and distinguishes them by name.

### 2. Kill is cgroup-wide

`proc stop` calls `systemctl --user stop`, which SIGTERMs every process in the
unit's cgroup and SIGKILLs the remainder after `TimeoutStopSec`.

*Forbids:* orphaned GPU memory. A child that calls `setsid` to escape its process
group is *still in the cgroup*; verified directly:

```
grandchild pid=620292 alive_before=yes
cgroup members: 620259 620292
alive_after=no-clean
```

`kill $MAINPID` would have left that `sleep 300` — and, in the real case, an XLA
compile worker holding VRAM — running forever.

### 3. Exclusion is a kernel lease

A GPU lease is `flock --nonblock --conflict-exit-code=75` on
`leases/gpu<N>.lock`, taken as the unit's `ExecStart` prefix so the *child* holds
the fd.

Three properties follow for free:

- **No stale locks.** The kernel drops the fd when the holder dies, however it dies.
  There is no cleanup path to get wrong.
- **No self-deadlock.** The lock is on an inode, not on a description of a process,
  so a job can never match itself.
- **Busy is distinguishable from failed.** `--conflict-exit-code=75` means "lease
  held" never gets confused with "your program exited 1".

The one remaining deadlock — a `--gpu` run whose command itself calls
`proc run --gpu` on the same device, blocking on a second fd — is detected via the
`PROC_GPU_LEASE` environment variable and **refused with an explanation**.

### 4. `wait` always terminates, and silence is never success

`proc wait` has exactly three outcomes, with distinct exit codes: ready (0), died
(1), timed out (2). A default 900 s timeout means an unattended `wait` cannot hang
forever. `--ready REGEX` succeeds only on a positive match — if the process exits
without ever printing the marker, that is exit 1, not exit 0.

*Forbids:* "the log went quiet, so it must be fine." Also `tail -f`: `proc logs -f`
stops when the process is gone, where `tail -f` waits on a file that will never
grow again.

Note the deliberate ordering in `cmd_wait`: scan for the marker, check liveness,
then **scan once more** before declaring death — otherwise a marker printed in the
same instant as exit would be missed.

The scanner must also carry an incomplete trailing line across polls. `stdout` is
read incrementally, and `PYTHONUNBUFFERED=1` means `print("bodies:", n)` reaches
the file as one `write()` *per argument* — so a poll landing mid-line splits the
marker across two reads. Consuming that partial line (as the first version did)
makes `--ready 'bodies: [0-9]+'` match nothing and report `TIMEOUT` for a process
that is ready. The same failure this invariant forbids, arriving through the
reader instead of the writer.

### 5. Status outlives the process, and unknown is not "fine"

The unit carries an `ExecStopPost` that writes `code=/status=/result=` to
`runs/<name>/status`. systemd runs it however the process ended, including SIGKILL
of the main process, so an exit code is readable long after the process is gone —
including `result=oom-kill`, which matters when a GPU job dies from memory
pressure.

The residual case is honest rather than hidden: if there is no status file *and* no
unit, the state is **`LOST`** (e.g. `wsl --shutdown` took the user manager). `LOST`
is never reported as `RUNNING` and never as success.

*Why not ask systemd for the exit code?* Transient units are garbage-collected
after they finish, so `systemctl show -p ExecMainStatus` is a race. Writing our own
record is not duplication — it is the only thing that survives.

### 6. The caller's environment is what runs

`systemd-run --user` inherits the *user manager's* environment, not your shell's.
With venvs everywhere (`Morphospace/.venv`, `Planktonica/.venv`), naive use would
silently launch the system `python` — the single most confusing failure mode of
plain `systemd-run`.

So `proc` resolves `argv[0]` through the **caller's** `PATH` and records the
absolute path in `meta.json`, and forwards an allowlist (`PATH`, `VIRTUAL_ENV`,
`PYTHONPATH`, `LD_LIBRARY_PATH`, `SDL_VIDEODRIVER`, `DISPLAY`, and the `CUDA_*`,
`XLA_*`, `JAX_*`, `NVIDIA_*`, `NCCL_*`, `LC_*` prefixes) plus any `--env K=V`.
An allowlist rather than the whole environment, so that tokens in the shell do not
land in unit properties.

`PYTHONUNBUFFERED=1` is also a default, and it is not cosmetic. stdout is a file
here, so Python block-buffers it in 4 KiB chunks — which means a readiness marker
printed at startup may not reach the log for minutes, or at all. Found the hard
way during the first real end-to-end run: Planktonica sat for 12 minutes with its
`[planktonica] bodies: ...` line stuck in the buffer, so `wait --ready 'bodies:'`
correctly reported `TIMEOUT` for a process that had in fact been ready almost
immediately. A lifecycle tool whose readiness checks read stdout must not leave
that buffer in place.

`--gpu` additionally sets `CUDA_VISIBLE_DEVICES=<dev>` and
`XLA_PYTHON_CLIENT_PREALLOCATE=false` — JAX otherwise claims ~75 % of VRAM on
first use, which turns one run into a hard blocker for everything else on the box.
Both are `setdefault`, so an explicit `--env` wins.

## Rejected alternatives

| Option | Why not |
| --- | --- |
| Fix the `pgrep` wrapper | Treats an instance of a class of bug. Text is not identity. |
| PID files | PIDs are reused; a stale file is indistinguishable from a live one without a start-time fingerprint, and that is a reimplementation of what the cgroup already knows. |
| `nohup` + `tail -f` | No identity, no exit status, no tree kill, and `tail -f` cannot tell "finished" from "quiet". |
| `tmux`/`screen` sessions | Fine for humans watching; terrible to script. Status means scraping a pane. |
| A bespoke supervisor daemon | Its own liveness, restart, and crash-recovery problems. systemd is already installed, already supervises, already has cgroups. |
| [pueue](https://github.com/Nukesor/pueue) | A genuinely good tool, but it is the *queue* layer, and it needs its own daemon. Deferred until a campaign actually needs concurrency limits — see below. |
| task-spooler | The maintained fork is a GPU-aware `ts`, but the original is unmaintained and per-GPU allocation matters little with one device. |
| Celery | Wildly out of proportion to a single node. |
| Third-party "background process manager" agent skills | Inspected: they wrap `ps aux | grep` and `kill -9`, i.e. they encode exactly the bug being fixed here. |

## Deferred: Layer B, queueing

Nothing here queues beyond a depth-1 lease per device. That is sufficient for one
8 GB laptop GPU, where JAX concurrency is effectively 1.

When a run-farm campaign wants "queue 20 configs, respect a concurrency limit,
walk away", adopt `pueue` (in Homebrew, 4.0.4) rather than growing a scheduler
here, and supervise `pueued` as a systemd user unit — so the layering stays
`systemd → pueued → proc run`. The seam that makes this cheap: `proc run` is a
single well-behaved command with a stable exit-code contract, which is exactly
what a queue wants to execute.

## Facts established by probe, not assumption

Recorded because they are the load-bearing environment assumptions, and because
two of them were surprises:

- `systemd-run --user` works under WSL2 here; user manager reports `running`,
  cgroup v2 (`cgroup2fs`).
- `ExecStopPost` receives `$EXIT_CODE`, `$EXIT_STATUS`, `$SERVICE_RESULT`, and runs
  even when the main process is killed by a signal.
- `StandardOutput=append:<file>` works, giving a plain greppable log file.
- A `setsid` child remains in the unit's cgroup and dies with it.
- `/usr/bin/flock` exists (no Homebrew dependency), and `--conflict-exit-code`
  cleanly separates contention from program failure.
- **Surprise 1:** `nvidia-smi --query-compute-apps` returns an *empty list* under
  WSL2 even with 1371 MiB in use. Per-process GPU attribution is therefore
  impossible here; leak detection must be device-wide baseline diffing.
- **Surprise 2:** `argparse` exits **2** on a bad command line, which collided with
  `EX_TIMEOUT=2` and made a typo indistinguishable from a timed-out wait. The
  parser is subclassed to exit 3. Caught by a test, not by reading.

## Repository

Public, at <https://github.com/JimGalasyn/proc-warden>.

An earlier draft of this document recorded the opposite — that the repo had to stay
local-only, with no `origin`, because the Airlock policy forbade remotes outside
the Confluent org. That constraint is stale: this is a personal project on a
personal machine, published under a personal account.

What does carry over is the reason the constraint existed. This is a public repo
developed alongside work on a machine that has work credentials on it, so:

- Nothing Confluent-internal belongs here — no internal hostnames, ticket
  references, or code.
- Commits should be authored with the personal identity, not `@confluent.io`.
  Check `git log --format='%an <%ae>'` before the first push of anything that
  started life on the work machine; 0.1.0 needed exactly that correction.
