"""Tests for the six invariants in docs/DESIGN.md.

Each test names the failure mode it forbids. They exercise the real systemd user
manager -- there is no mock, because the whole point of proc is that the kernel
and systemd hold the state we used to guess at from `ps` output.

No GPU is required: a lease is a lock file, so lease tests run anywhere.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

PROC = Path(__file__).resolve().parent.parent / "proc"

EX_OK, EX_FAILED, EX_TIMEOUT, EX_USAGE, EX_BUSY, EX_ENV = 0, 1, 2, 3, 4, 5


def have_user_systemd() -> bool:
    try:
        r = subprocess.run(["systemctl", "--user", "is-system-running"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return (r.stdout or r.stderr).strip() not in ("offline", "unknown", "")


_HAVE_SYSTEMD = have_user_systemd()

# On a developer box without a user manager, skipping is right. In CI it is not:
# a suite that skips wholesale is green for the wrong reason, and this is the
# half of the suite that actually proves proc works. Set PROC_REQUIRE_SYSTEMD=1
# (the CI workflow does) to turn the skip into a hard failure.
if not _HAVE_SYSTEMD and os.environ.get("PROC_REQUIRE_SYSTEMD") == "1":
    raise RuntimeError(
        "PROC_REQUIRE_SYSTEMD=1 but there is no systemd user manager, so the "
        "integration tests would silently skip. Either provision one or unset "
        "PROC_REQUIRE_SYSTEMD.")

pytestmark = pytest.mark.skipif(
    not _HAVE_SYSTEMD, reason="needs a systemd user manager")


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "state"
    yield h
    for d in (h / "runs").glob("*"):  # never leave units behind
        unit = f"proc-{d.name}.service"
        subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True)
        subprocess.run(["systemctl", "--user", "reset-failed", unit], capture_output=True)


@pytest.fixture
def name():
    return "t" + uuid.uuid4().hex[:8]


def proc(*args, home, timeout=90, extra_env=None):
    env = dict(os.environ, PROC_HOME=str(home))
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, str(PROC), *args],
                          capture_output=True, text=True, timeout=timeout, env=env)


def state_of(name, home):
    r = proc("status", name, "--json", home=home)
    assert r.returncode == EX_OK, r.stderr
    return json.loads(r.stdout)


def wait_until(predicate, timeout=30, interval=0.25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


# --- invariant 1: identity is a name, never a command-line pattern -----------

@pytest.mark.parametrize("bad", ["has space", "has/slash", "-leading", "", "a" * 65])
def test_rejects_names_that_are_not_identities(bad, home):
    assert proc("run", bad, "--", "/bin/true", home=home).returncode == EX_USAGE


def test_duplicate_name_is_refused_not_raced(home, name):
    assert proc("run", name, "--", "/bin/sleep", "30", home=home).returncode == EX_OK
    r = proc("run", name, "--", "/bin/true", home=home)
    assert r.returncode == EX_BUSY
    assert "already RUNNING" in r.stderr


def test_replace_stops_the_previous_run(home, name):
    proc("run", name, "--", "/bin/sleep", "30", home=home)
    old_pid = state_of(name, home)["pid"]
    assert proc("run", name, "--replace", "--", "/bin/sleep", "30",
                home=home).returncode == EX_OK
    assert wait_until(lambda: not Path(f"/proc/{old_pid}").exists())


def test_own_argv_never_self_matches(home, name):
    """The original bug: a wrapper waiting on `pgrep -f <pattern>` matched
    itself and waited forever. proc must not care what the argv looks like."""
    pattern = "sleep 27"  # appears in the child argv AND in any pgrep we'd run
    proc("run", name, "--", "/bin/sh", "-c", f"exec /bin/{pattern}", home=home)
    assert state_of(name, home)["state"] == "RUNNING"
    r = proc("run", name + "b", "--", "/bin/sh", "-c", f"exec /bin/{pattern}", home=home)
    assert r.returncode == EX_OK  # a different name is a different process, period
    assert state_of(name + "b", home)["state"] == "RUNNING"


# --- invariant 2: kill is cgroup-wide ---------------------------------------

def test_stop_reaps_a_child_that_escaped_with_setsid(home, name):
    proc("run", name, "--",
         "/bin/bash", "-c", "setsid sleep 300 & echo gc=$!; sleep 300", home=home)
    stdout = home / "runs" / name / "stdout"
    assert wait_until(lambda: stdout.exists() and "gc=" in stdout.read_text())
    gc_pid = int(stdout.read_text().split("gc=")[1].split()[0])
    assert Path(f"/proc/{gc_pid}").exists()

    assert proc("stop", name, home=home).returncode == EX_OK
    assert wait_until(lambda: not Path(f"/proc/{gc_pid}").exists()), \
        "grandchild survived stop -- this is the orphaned-GPU-memory failure mode"


def test_stop_is_idempotent(home, name):
    proc("run", name, "--", "/bin/true", home=home)
    assert wait_until(lambda: state_of(name, home)["state"] != "RUNNING")
    r = proc("stop", name, home=home)
    assert r.returncode == EX_OK and "nothing to stop" in r.stdout


# --- invariant 3: exclusion is a kernel lease, which cannot deadlock ---------

def test_second_lease_fails_fast_instead_of_blocking(home, name):
    proc("run", name, "--gpu", "--", "/bin/sleep", "30", home=home)
    start = time.monotonic()
    r = proc("run", name + "b", "--gpu", "--", "/bin/sleep", "30", home=home, timeout=30)
    assert r.returncode == EX_BUSY
    assert time.monotonic() - start < 15, "contention should be immediate, not a hang"
    assert "lease is held by" in r.stderr


def test_lease_is_released_by_the_kernel_when_the_holder_dies(home, name):
    proc("run", name, "--gpu", "--", "/bin/sleep", "2", home=home)
    assert wait_until(lambda: state_of(name, home)["state"] != "RUNNING")
    # No stale-lock cleanup ran; the fd simply closed.
    assert proc("run", name + "b", "--gpu", "--", "/bin/true", home=home).returncode == EX_OK


def test_queued_lease_acquires_after_holder_exits(home, name):
    proc("run", name, "--gpu", "--", "/bin/sleep", "3", home=home)
    assert proc("run", name + "b", "--gpu", "--gpu-wait", "60", "--",
                "/bin/echo", "GOT-LEASE", home=home).returncode == EX_OK
    r = proc("wait", name + "b", "--ready", "GOT-LEASE", "--timeout", "60", home=home)
    assert r.returncode == EX_OK


def test_gpu_wait_without_gpu_is_refused_not_ignored(home, name):
    """Regression: `--gpu-wait 60` alone used to launch with no lease at all, so
    a typo produced an unserialized GPU run that looked fine."""
    r = proc("run", name, "--gpu-wait", "60", "--", "/bin/true", home=home)
    assert r.returncode == EX_USAGE
    assert "--gpu" in r.stderr
    assert not (home / "runs" / name).exists(), "a refused launch must leave no run dir"


def test_nested_lease_is_refused_rather_than_deadlocking(home, name):
    """A --gpu run whose command calls proc run --gpu on the same device would
    block on a second fd forever. Refuse, loudly."""
    r = proc("run", name, "--gpu", "0", "--", "/bin/true",
             home=home, extra_env={"PROC_GPU_LEASE": "0"})
    assert r.returncode == EX_BUSY
    assert "deadlock" in r.stderr


# --- invariant 4: wait always terminates, and silence is never success -------

def test_wait_timeout_is_distinct_and_leaves_the_process_alone(home, name):
    proc("run", name, "--", "/bin/sleep", "60", home=home)
    r = proc("wait", name, "--timeout", "2", home=home)
    assert r.returncode == EX_TIMEOUT
    assert state_of(name, home)["state"] == "RUNNING", "wait must not kill anything"


def test_wait_reports_death_before_readiness(home, name):
    proc("run", name, "--", "/bin/sh", "-c", "echo starting; exit 0", home=home)
    r = proc("wait", name, "--ready", "SERVER READY", "--timeout", "20", home=home)
    assert r.returncode == EX_FAILED, "a process that died without the marker is not ready"
    assert "before matching" in r.stderr


def test_wait_succeeds_on_the_readiness_marker(home, name):
    proc("run", name, "--",
         "/bin/sh", "-c", "sleep 1; echo SERVER READY; sleep 30", home=home)
    assert proc("wait", name, "--ready", "SERVER READY", "--timeout", "30",
                home=home).returncode == EX_OK


def test_wait_distinguishes_clean_exit_from_failure(home, name):
    proc("run", name, "--", "/bin/true", home=home)
    assert proc("wait", name, "--timeout", "20", home=home).returncode == EX_OK
    proc("run", name + "b", "--", "/bin/sh", "-c", "exit 7", home=home)
    assert proc("wait", name + "b", "--timeout", "20", home=home).returncode == EX_FAILED


def test_wait_on_unknown_run_is_usage_error(home):
    assert proc("wait", "nope", home=home).returncode == EX_USAGE


def test_ready_marker_split_across_writes_is_still_matched(home, name):
    """Regression: the incremental scanner used to consume the partial trailing
    line, so a marker arriving in two write()s was never matched and a ready
    process reported TIMEOUT. PYTHONUNBUFFERED makes `print("bodies:", n)` one
    write per argument, so this is the common case, not a contrived one."""
    proc("run", name, "--", sys.executable, "-c",
         "import sys,time\n"
         "sys.stdout.write('SERVER'); sys.stdout.flush()\n"
         "time.sleep(1.5)\n"
         "sys.stdout.write(' READY\\n'); sys.stdout.flush()\n"
         "time.sleep(30)\n", home=home)
    r = proc("wait", name, "--ready", "SERVER READY", "--timeout", "20", home=home)
    assert r.returncode == EX_OK, \
        f"marker was printed but not matched; log={(home / 'runs' / name / 'stdout').read_text()!r}"


def test_ready_marker_without_trailing_newline_is_matched(home, name):
    """The partial line must still be searched, or a marker printed without a
    newline would only match once something else flushed after it."""
    proc("run", name, "--", sys.executable, "-c",
         "import sys,time\n"
         "sys.stdout.write('READY-NO-NEWLINE'); sys.stdout.flush()\n"
         "time.sleep(30)\n", home=home)
    assert proc("wait", name, "--ready", "READY-NO-NEWLINE", "--timeout", "20",
                home=home).returncode == EX_OK


# --- invariant 5: status outlives the process, and unknown is not "fine" -----

def test_exit_code_is_readable_after_death(home, name):
    proc("run", name, "--", "/bin/sh", "-c", "exit 42", home=home)
    assert wait_until(lambda: state_of(name, home)["state"] != "RUNNING")
    st = state_of(name, home)
    assert st["state"] == "FAILED" and st["exit"] == 42


def test_signal_death_is_reported_as_killed(home, name):
    proc("run", name, "--", "/bin/sleep", "60", home=home)
    proc("stop", name, home=home)
    st = state_of(name, home)
    assert st["state"] == "KILLED" and st["signal"] == "TERM"


def test_vanished_unit_reads_as_lost_not_running(home, name):
    """After `wsl --shutdown` the user manager and its transient units are gone.
    A run with no status file and no unit must not look RUNNING."""
    proc("run", name, "--", "/bin/sleep", "60", home=home)
    subprocess.run(["systemctl", "--user", "kill", "-s", "KILL",
                    f"proc-{name}.service"], capture_output=True)
    assert wait_until(lambda: state_of(name, home)["state"] != "RUNNING")
    (home / "runs" / name / "status").unlink(missing_ok=True)  # simulate the lost record
    assert state_of(name, home)["state"] == "LOST"


def test_run_reports_an_immediate_death_in_its_exit_code(home, name):
    """Regression: `proc run` returned 0 for a process that was already FAILED by
    the time it looked, so `proc run ... || handle` caught a busy lease but
    silently missed a crash."""
    r = proc("run", name, "--", "/bin/false", home=home)
    assert r.returncode == EX_FAILED
    assert state_of(name, home)["state"] == "FAILED"


def test_run_of_a_fast_clean_exit_is_still_success(home, name):
    """The converse: exiting 0 before we look is not a failure."""
    r = proc("run", name, "--", "/bin/true", home=home)
    assert r.returncode == EX_OK
    assert wait_until(lambda: state_of(name, home)["state"] == "EXITED")


def test_status_of_unknown_run_is_usage_error(home):
    assert proc("status", "nope", home=home).returncode == EX_USAGE


def test_ls_reports_every_run(home, name):
    proc("run", name, "--", "/bin/true", home=home)
    proc("run", name + "b", "--", "/bin/true", home=home)
    r = proc("ls", home=home)
    assert name in r.stdout and name + "b" in r.stdout


# --- invariant 6: the caller's environment is what runs ---------------------

def test_command_is_resolved_on_the_callers_path_not_systemds(home, name, tmp_path):
    """A venv-activated shell must not silently launch the system python."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "mypython"
    fake.write_text("#!/bin/sh\necho i-am-the-venv-python\n")
    fake.chmod(0o755)
    r = proc("run", name, "--", "mypython", home=home,
             extra_env={"PATH": f"{fake_bin}:{os.environ['PATH']}"})
    assert r.returncode == EX_OK
    meta = json.loads((home / "runs" / name / "meta.json").read_text())
    assert meta["resolved"] == str(fake)
    assert wait_until(
        lambda: "i-am-the-venv-python" in (home / "runs" / name / "stdout").read_text())


def test_a_failed_launch_keeps_the_previous_runs_record(home, name):
    """The run dir used to be deleted before systemd-run was known to have
    succeeded, so a typo in the command destroyed the only record that the
    previous run ever happened -- its logs and its exit status."""
    proc("run", name, "--", "/bin/sh", "-c", "echo FIRST-RUN; exit 3", home=home)
    assert wait_until(lambda: state_of(name, home)["state"] == "FAILED")

    r = proc("run", name, "--", "definitely-not-a-real-binary-xyz", home=home)
    assert r.returncode == EX_USAGE

    st = state_of(name, home)
    assert st["state"] == "FAILED" and st["exit"] == 3, "previous record was destroyed"
    assert "FIRST-RUN" in (home / "runs" / name / "stdout").read_text()


def test_concurrent_launches_of_one_name_do_not_corrupt_each_other(home, name):
    """Two `proc run <same name>` at once would both pass the RUNNING check, and
    the loser would delete the winner's run directory out from under a live
    unit. The name lock serializes them into a clean winner and a clean EX_BUSY."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(proc, "run", name, "--", "/bin/sleep", "20", home=home)
                   for _ in range(2)]
        a, b = [f.result() for f in futures]

    assert sorted([a.returncode, b.returncode]) == [EX_OK, EX_BUSY], \
        f"got {a.returncode}/{b.returncode}: {a.stderr!r} {b.stderr!r}"
    assert state_of(name, home)["state"] == "RUNNING"
    assert (home / "runs" / name / "meta.json").exists(), \
        "the winner's record was deleted by the loser"


def test_a_successful_replace_still_discards_the_old_logs(home, name):
    """The converse: once the new unit is up, the old run really is superseded."""
    proc("run", name, "--", "/bin/sh", "-c", "echo OLD-OUTPUT", home=home)
    assert wait_until(lambda: state_of(name, home)["state"] != "RUNNING")
    assert proc("run", name, "--", "/bin/sh", "-c", "echo NEW-OUTPUT",
                home=home).returncode == EX_OK
    assert wait_until(
        lambda: "NEW-OUTPUT" in (home / "runs" / name / "stdout").read_text())
    assert "OLD-OUTPUT" not in (home / "runs" / name / "stdout").read_text()
    # and nothing is left lying around in the runs directory
    assert not [p for p in (home / "runs").iterdir() if p.name.startswith(".")
                and p.is_dir()]


def test_meta_json_is_not_world_readable(home, name):
    """`--env` is the one path by which a real secret reaches meta.json, so the
    file must not be readable by other users on the box."""
    proc("run", name, "--env", "FAKE_TOKEN=hunter2", "--", "/bin/true", home=home)
    meta = home / "runs" / name / "meta.json"
    assert wait_until(meta.exists)
    mode = stat.S_IMODE(meta.stat().st_mode)
    assert mode == 0o600, f"meta.json is {oct(mode)}"
    assert "hunter2" in meta.read_text(), "the value IS recorded; that is why mode matters"


def test_missing_command_is_reported_before_launching(home, name):
    r = proc("run", name, "--", "definitely-not-a-real-binary-xyz", home=home)
    assert r.returncode == EX_USAGE
    assert not (home / "runs" / name).exists(), "a failed launch must leave no run dir"


def test_gpu_run_sets_jax_and_cuda_env(home, name):
    proc("run", name, "--gpu", "--", "/bin/sh", "-c",
         "echo dev=$CUDA_VISIBLE_DEVICES prealloc=$XLA_PYTHON_CLIENT_PREALLOCATE",
         home=home)
    out = home / "runs" / name / "stdout"
    assert wait_until(lambda: out.exists() and "dev=" in out.read_text())
    text = out.read_text()
    assert "dev=0" in text and "prealloc=false" in text


def test_python_stdout_is_unbuffered_so_readiness_markers_arrive(home, name):
    """stdout is a file, so Python would block-buffer a startup marker into
    oblivion -- found the hard way: a real run sat 12 minutes without printing
    the line `wait --ready` was looking for."""
    proc("run", name, "--", sys.executable, "-c",
         "print('MARKER READY'); import time; time.sleep(30)", home=home)
    r = proc("wait", name, "--ready", "MARKER READY", "--timeout", "20", home=home)
    assert r.returncode == EX_OK


def test_relative_interpreter_resolves_against_cwd(home, name, tmp_path):
    """`--cwd repo -- .venv/bin/python` is how these repos are actually run."""
    repo = tmp_path / "repo" / "venv" / "bin"
    repo.mkdir(parents=True)
    exe = repo / "python"
    exe.write_text("#!/bin/sh\necho ran-from-cwd\n")
    exe.chmod(0o755)
    r = proc("run", name, "--cwd", str(tmp_path / "repo"), "--", "venv/bin/python",
             home=home)
    assert r.returncode == EX_OK, r.stderr
    out = home / "runs" / name / "stdout"
    assert wait_until(lambda: out.exists() and "ran-from-cwd" in out.read_text())


def test_explicit_env_overrides_the_gpu_defaults(home, name):
    proc("run", name, "--gpu", "--env", "XLA_PYTHON_CLIENT_PREALLOCATE=true", "--",
         "/bin/sh", "-c", "echo prealloc=$XLA_PYTHON_CLIENT_PREALLOCATE", home=home)
    out = home / "runs" / name / "stdout"
    assert wait_until(lambda: out.exists() and "prealloc=" in out.read_text())
    assert "prealloc=true" in out.read_text()


# --- logs --------------------------------------------------------------------

def test_logs_follow_terminates_when_the_process_dies(home, name):
    """`tail -f` would hang forever here."""
    proc("run", name, "--", "/bin/sh", "-c", "echo one; sleep 2; echo two", home=home)
    r = proc("logs", name, "-f", home=home, timeout=45)  # timeout => hang => failure
    assert "one" in r.stdout and "two" in r.stdout
    assert "end of log" in r.stderr or "end of log" in r.stdout


def test_logs_tail_on_a_log_far_larger_than_one_read_block(home, name):
    """End-to-end version of the bounded tail: the interesting lines are megabytes
    from the start, so a correct answer means the backwards read really works."""
    proc("run", name, "--", sys.executable, "-c",
         "for i in range(300000): print(f'line-{i}')", home=home)
    assert wait_until(lambda: state_of(name, home)["state"] != "RUNNING", timeout=90)
    out = home / "runs" / name / "stdout"
    assert out.stat().st_size > 65536, "log needs to exceed one block to be a test"
    r = proc("logs", name, "-n", "3", home=home)
    assert r.stdout.splitlines() == ["line-299997", "line-299998", "line-299999"]


def test_logs_tail_then_follow_picks_up_from_the_end(home, name):
    """`-n` with `-f` must show the tail and then continue, not replay the file."""
    proc("run", name, "--", "/bin/sh", "-c",
         "echo old-1; echo old-2; echo old-3; sleep 1; echo NEW-LINE", home=home)
    assert wait_until(lambda: "old-3" in (home / "runs" / name / "stdout").read_text())
    r = proc("logs", name, "-f", "-n", "1", home=home, timeout=45)
    assert "old-3" in r.stdout and "NEW-LINE" in r.stdout
    assert "old-1" not in r.stdout, "-n 1 should not have replayed the whole log"


def test_logs_tail_limits_output(home, name):
    proc("run", name, "--", "/bin/sh", "-c", "for i in 1 2 3 4 5; do echo L$i; done",
         home=home)
    assert wait_until(lambda: state_of(name, home)["state"] != "RUNNING")
    r = proc("logs", name, "-n", "2", home=home)
    assert "L5" in r.stdout and "L1" not in r.stdout


# --- gc ----------------------------------------------------------------------

def test_gc_purge_removes_finished_runs_but_keeps_live_ones(home, name):
    proc("run", name, "--", "/bin/true", home=home)
    proc("run", name + "b", "--", "/bin/sleep", "60", home=home)
    assert wait_until(lambda: state_of(name, home)["state"] != "RUNNING")
    r = proc("gc", "--purge", home=home)
    assert r.returncode == EX_OK
    assert not (home / "runs" / name).exists()
    assert (home / "runs" / (name + "b")).exists()
