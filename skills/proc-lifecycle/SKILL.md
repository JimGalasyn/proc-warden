---
name: proc-lifecycle
description: Launch, watch, and kill local long-running processes unambiguously via the `proc` CLI (a wrapper over systemd-run --user with cgroup kill, exit-status capture, and exclusive GPU leases). Use whenever starting anything that outlives one command — a dev server, a simulation, a JAX/GPU training or campaign run, a pygame front-end — or when asking "is it still running?", "why did it stop?", "who has the GPU?", or when a previous approach involved pgrep/ps-grep, nohup, a PID file, `tail -f` to detect completion, or `kill -9`. Also use when GPU memory appears stuck after a job ended.
---

# proc — process lifetime you can reason about

Install with **`pip install systemd-proc`** — the PyPI name differs from the repo
name, because `procwarden` was already taken. That puts `proc` (and `proc-warden`)
on PATH. From a checkout, `./proc` is a shim that works with nothing installed;
either entry point runs the same code. Zero runtime dependencies — it is all
`systemd-run --user`. Full rationale: `docs/DESIGN.md` in the repo.

## The rules

These exist because each forbids a bug that has actually bitten. Follow them even
when a shortcut looks obviously fine.

1. **Never identify a process by text.** No `pgrep -f`, no `ps aux | grep`, no
   `pkill`. A wrapper that greps for its own pattern matches *itself* and hangs
   forever. Use a name: `proc run <name>`, `proc status <name>`.
2. **Never `kill` a PID** for anything proc launched. `proc stop <name>` kills the
   whole cgroup, which is the only way to get forked children and XLA compile
   workers holding VRAM.
3. **Never use `tail -f` (or a bare `sleep`) to detect completion.** `tail -f` on a
   finished process's log waits forever. Use `proc wait` — it has a timeout and
   distinguishes died from timed-out.
4. **Never read silence as success.** If `wait` returns 1 or 2, say so plainly and
   look at the logs. `LOST` and `OOM` are outcomes, not noise.
5. **Always pass `--timeout`** to `wait` in unattended work (default is 900 s).
6. **Choose a durable name** — the run's handle. Reusing a live name is refused;
   reusing a finished name discards its logs and says so.

## What to run

| Goal | Command |
| --- | --- |
| Start something long | `proc run NAME -- CMD...` (everything after `--` is verbatim) |
| Start it on the GPU, exclusively | `proc run NAME --gpu -- CMD...` |
| …and queue instead of failing if busy | `proc run NAME --gpu --gpu-wait 3600 -- CMD...` |
| Is it up yet? | `proc wait NAME --ready 'REGEX' --timeout 60` |
| Did it finish, and how? | `proc wait NAME --timeout N` then `proc status NAME` |
| What's running? | `proc ls` (add `--json` to parse) |
| See output | `proc logs NAME` / `proc logs NAME -f` / `proc logs NAME -n 50` |
| Stop it | `proc stop NAME` (idempotent) |
| Restart it | `proc run NAME --replace -- CMD...` |
| Who has the GPU? | `proc gpu` |
| Clean up + check for leaked VRAM | `proc gc` (add `--purge` to delete finished run dirs) |

## Exit codes — branch on these, don't parse prose

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | ready, or exited cleanly | proceed |
| 1 | died, or died before becoming ready | read `proc logs NAME`; report the real error |
| 2 | timeout — **still running, untouched** | either keep waiting, or say it's slow. Do not assume failure |
| 3 | usage error / no such run | fix the command |
| 4 | busy: name live, or GPU lease held | pick another name, or `--gpu-wait`, or `proc gpu` to see the holder |
| 5 | environment can't do this | report it; don't retry |

`proc run` returns 1 if the process is already dead when it returns (a command
that crashes on startup), so `proc run ... || …` is worth writing. It checks only
once — for anything later, use `proc wait`.

## States

`RUNNING` · `EXITED` (0) · `FAILED` (nonzero) · `KILLED` (signal) · `OOM` ·
`LEASE_BUSY` (never started, GPU was taken) · `LOST` (no exit status and no unit —
typically `wsl --shutdown` killed the user manager). **`LOST` is not running and is
not success.** Report it as an unknown outcome and relaunch if appropriate.

## Recipes

**A server or dev process, with a real readiness gate**

```bash
proc run api --replace -- python -m myapp.server
proc wait api --ready 'Uvicorn running' --timeout 60 || { proc logs api -n 40; exit 1; }
```

**A GPU run (JAX/Planktonica/Morphospace)**

```bash
proc run pond --gpu --cwd ~/repos/Planktonica -- .venv/bin/python -m planktonica
proc wait pond --ready 'bodies:' --timeout 120
```

`--gpu` sets `CUDA_VISIBLE_DEVICES` and `XLA_PYTHON_CLIENT_PREALLOCATE=false`
(JAX would otherwise grab ~75 % of VRAM and block everything else). Override with
`--env` if a run genuinely needs preallocation.

Headless (no WSLg display) pygame front-end: add `--env SDL_VIDEODRIVER=dummy`.

**A campaign: N configs, one GPU, serialized**

`flock` ordering is not FIFO, so to control order, serialize in the shell rather
than launching all N with `--gpu-wait`:

```bash
for cfg in configs/*.yaml; do
  n="run-$(basename "$cfg" .yaml)"
  proc run "$n" --gpu -- python -m campaign --config "$cfg"
  proc wait "$n" --timeout 0        # 0 = no limit; this is the serializing step
  proc status "$n"
done
proc gc                              # flags VRAM that never came back
```

**Triage after something died**

```bash
proc status NAME --json     # state, exit code or signal, elapsed, GPU device
proc logs NAME -n 60
proc gpu                    # is VRAM still held? is the lease stuck?
```

`OOM` (systemd `result=oom-kill`) means memory pressure, not a code bug — say that
rather than hunting a phantom exception.

## Monitoring from an agent turn

`proc` composes with the harness's own tools; don't reinvent either side.

- **One notification when a job ends:** `Bash(run_in_background)` with
  `proc wait NAME --timeout 0`. It exits exactly when the process does, with a
  meaningful code.
- **Streaming progress events:** `Monitor` with
  `proc logs NAME -f 2>&1 | grep -E --line-buffered 'step=|Traceback|Error|OOM|end of log'`.
  Include the failure signatures — a filter that matches only the happy path stays
  silent through a crash. `proc logs -f` ends on its own when the process dies, so
  the monitor won't idle forever.

## Environment notes

- The command is resolved on **your** `PATH` (so a venv `python` is honored), and an
  allowlist of env vars is forwarded — `PATH`, `VIRTUAL_ENV`, `PYTHONPATH`,
  `LD_LIBRARY_PATH`, `SDL_VIDEODRIVER`, `DISPLAY`, plus `CUDA_*`, `XLA_*`, `JAX_*`,
  `NVIDIA_*`, `NCCL_*`, `LC_*`. Anything else must be passed with `--env K=V`.
- Use `--cwd DIR` for a relative interpreter path like `.venv/bin/python` — a path
  is resolved against `--cwd`, a bare name against your `PATH`.
- `PYTHONUNBUFFERED=1` is set by default, so a startup marker reaches the log
  immediately instead of sitting in Python's 4 KiB stdout buffer. Don't remove it
  when using `--ready`.
- Per-process GPU attribution does not work under WSL2, so `proc gpu` and `proc gc`
  report device-wide memory. Don't claim a specific PID is holding VRAM.
- State lives in `~/.local/state/proc/runs/<name>/` and survives reboots as `LOST`
  records; `$PROC_HOME` overrides it (must contain no spaces).
