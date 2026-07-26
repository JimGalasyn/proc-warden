"""Tests for the six invariants in docs/DESIGN.md.

Each test names the failure mode it forbids. They exercise the real systemd user
manager -- there is no mock, because the whole point of proc is that the kernel
and systemd hold the state we used to guess at from `ps` output.

No GPU is required: a lease is a lock file, so lease tests run anywhere.
"""

from __future__ import annotations

import json
import os
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


pytestmark = pytest.mark.skipif(
    not have_user_systemd(), reason="needs a systemd user manager")


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
