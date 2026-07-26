"""Unit tests for the pure logic in `proc_warden.cli`.

These need no systemd, so they run everywhere -- which is the point. The
integration suite in `test_proc.py` is the real proof that proc works, but it
skips wholesale on a machine (or a CI runner) with no user manager, and a suite
that skips is not a suite that passes. Everything here is the decision-making
that sits *around* the systemd calls: naming, status parsing, the state machine,
environment assembly, argv resolution, and the `--` split.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import time

import pytest

from proc_warden import __version__
from proc_warden import cli


# --- the exit-code contract --------------------------------------------------

def test_exit_codes_are_the_documented_contract():
    """README, SKILL.md, and every calling script branch on these numbers.
    Renumbering one is a breaking change that no other test would notice."""
    assert (cli.EX_OK, cli.EX_FAILED, cli.EX_TIMEOUT,
            cli.EX_USAGE, cli.EX_BUSY, cli.EX_ENV) == (0, 1, 2, 3, 4, 5)


def test_argparse_exit_code_does_not_collide_with_timeout():
    """argparse exits 2 by default, which is EX_TIMEOUT -- a typo would be
    indistinguishable from a timed-out wait. The parser is subclassed for this."""
    with pytest.raises(SystemExit) as e:
        cli.Parser(prog="proc").error("boom")
    assert e.value.code == cli.EX_USAGE


def test_version_is_a_single_source_of_truth():
    """The version is written in three places by hand (pyproject, __init__,
    CITATION.cff) and they must agree -- the publish workflow refuses to build
    when the tag disagrees with pyproject, but nothing else checks the other two.
    Asserting a literal here would just mean editing a fourth place per release."""
    # Read by regex, not tomllib/yaml: tomllib is 3.11+, and this has to run on
    # the whole supported matrix without pulling in a parser dependency.
    root = pathlib.Path(__file__).resolve().parent.parent

    def first(path, pattern):
        m = re.search(pattern, (root / path).read_text(), re.M)
        assert m, f"no version found in {path}"
        return m.group(1)

    pyproject = first("pyproject.toml", r'^version\s*=\s*"([^"]+)"')
    citation = first("CITATION.cff", r'^version:\s*"?([^"\s]+)"?')
    assert __version__ == pyproject == citation, (
        f"version mismatch: __init__={__version__} pyproject={pyproject} "
        f"CITATION.cff={citation}")


# --- naming ------------------------------------------------------------------

@pytest.mark.parametrize("good", ["a", "train", "run-1", "run_1", "v0.1", "A" * 64])
def test_valid_names_are_accepted(good):
    assert cli.check_name(good) == good


@pytest.mark.parametrize("bad", [
    "has space", "has/slash", "-leading", ".leading", "_leading", "",
    "A" * 65, "semi;colon", "dollar$", "back`tick", "new\nline", "star*",
])
def test_names_that_are_not_identities_are_refused(bad):
    """The name becomes a unit name and a directory, so anything shell-ish or
    path-ish has to be refused up front rather than escaped later."""
    with pytest.raises(SystemExit) as e:
        cli.check_name(bad)
    assert e.value.code == cli.EX_USAGE


def test_unit_name_is_derived_from_the_run_name():
    assert cli.unit_of("train") == "proc-train.service"


# --- status parsing ----------------------------------------------------------

def test_parse_status_file_reads_the_execstoppost_line(tmp_path):
    p = tmp_path / "status"
    p.write_text("code=exited status=42 result=exit-code\n")
    assert cli.parse_status_file(p) == {
        "code": "exited", "status": "42", "result": "exit-code"}


def test_parse_status_file_of_a_missing_file_is_empty(tmp_path):
    assert cli.parse_status_file(tmp_path / "nope") == {}


def test_parse_status_file_tolerates_empty_values(tmp_path):
    """systemd leaves a variable unset when it does not apply; the line still
    has to parse rather than throw."""
    p = tmp_path / "status"
    p.write_text("code= status= result=success\n")
    assert cli.parse_status_file(p) == {"code": "", "status": "", "result": "success"}


# --- the state machine -------------------------------------------------------

def _run_dir(tmp_path, monkeypatch, *, status: str, meta: dict | None = None):
    """Build a finished run on disk. With a status file present, read_state
    never consults systemd, so the state machine is testable in isolation."""
    monkeypatch.setattr(cli, "RUNS", tmp_path / "runs")
    d = cli.RUNS / "r"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"cmd": ["/bin/true"], "cwd": "/tmp", "started_at": time.time(),
         **(meta or {})}))
    (d / "status").write_text(status)
    return cli.read_state("r")


@pytest.mark.parametrize("status,meta,expected_state,expected_exit", [
    ("code=exited status=0 result=success",     {}, "EXITED", 0),
    ("code=exited status=42 result=exit-code",  {}, "FAILED", 42),
    ("code=exited status=1 result=exit-code",   {}, "FAILED", 1),
    # 75 is flock's --conflict-exit-code, but only a GPU run can mean "lease busy".
    ("code=exited status=75 result=exit-code", {"gpu": 0}, "LEASE_BUSY", 75),
    ("code=exited status=75 result=exit-code",  {}, "FAILED", 75),
])
def test_exit_codes_map_to_states(tmp_path, monkeypatch, status, meta,
                                  expected_state, expected_exit):
    st = _run_dir(tmp_path, monkeypatch, status=status, meta=meta)
    assert st["state"] == expected_state
    assert st["exit"] == expected_exit


def test_signal_death_is_killed_not_failed(tmp_path, monkeypatch):
    st = _run_dir(tmp_path, monkeypatch, status="code=killed status=TERM result=signal")
    assert st["state"] == "KILLED" and st["signal"] == "TERM" and st["exit"] is None


def test_oom_is_distinguished_from_an_ordinary_failure(tmp_path, monkeypatch):
    """Dying from memory pressure is the diagnosis you most want on a GPU box;
    it must not read as a generic nonzero exit."""
    st = _run_dir(tmp_path, monkeypatch, status="code=killed status=KILL result=oom-kill")
    assert st["state"] == "OOM"


def test_oom_is_detected_even_when_reported_as_an_exit(tmp_path, monkeypatch):
    st = _run_dir(tmp_path, monkeypatch, status="code=exited status=137 result=oom-kill")
    assert st["state"] == "OOM"


def test_unknown_run_reads_as_none(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "RUNS", tmp_path / "runs")
    assert cli.read_state("never-existed") is None


def test_a_corrupt_meta_file_does_not_crash_status(tmp_path, monkeypatch):
    """A truncated meta.json (killed mid-write) must degrade, not raise -- the
    status file is the part that matters and it is still readable."""
    monkeypatch.setattr(cli, "RUNS", tmp_path / "runs")
    d = cli.RUNS / "r"
    d.mkdir(parents=True)
    (d / "meta.json").write_text("{not valid json")
    (d / "status").write_text("code=exited status=0 result=success")
    st = cli.read_state("r")
    assert st["state"] == "EXITED" and st["cmd"] == []


# --- formatting --------------------------------------------------------------

@pytest.mark.parametrize("seconds,expected", [
    (None, "-"), (0, "00:00:00"), (61, "00:01:01"),
    (3661, "01:01:01"), (-5, "00:00:00"), (86399, "23:59:59"),
])
def test_duration_formatting(seconds, expected):
    assert cli.fmt_duration(seconds) == expected


def test_state_line_renders_a_killed_run_without_an_exit_code():
    # Real epoch timestamps: state_line guards with `if st.get("started_at")`,
    # so a literal 0.0 would read as "unknown" rather than as 1970.
    started = 1_800_000_000.0
    line = cli.state_line({
        "name": "trainer", "state": "KILLED", "signal": "TERM", "exit": None,
        "pid": None, "started_at": started, "finished_at": started + 5,
        "cmd": ["python", "train.py"], "gpu": 0})
    assert "trainer" in line and "KILLED" in line and "sig:TERM" in line
    assert "python train.py" in line and "00:00:05" in line


def test_state_line_survives_a_run_with_almost_no_metadata():
    """A LOST run has no cmd, no pid, no timings. `ls` still has to print it."""
    line = cli.state_line({"name": "x", "state": "LOST", "exit": None, "pid": None,
                           "started_at": None, "cmd": [], "gpu": None})
    assert "LOST" in line and "-" in line


# --- environment assembly ----------------------------------------------------

def test_allowlisted_variables_pass_through(monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/repo/.venv")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1")   # CUDA_ prefix
    monkeypatch.setenv("NCCL_DEBUG", "INFO")          # NCCL_ prefix
    env = cli.build_env([])
    assert env["VIRTUAL_ENV"] == "/repo/.venv"
    assert env["CUDA_VISIBLE_DEVICES"] == "1"
    assert env["NCCL_DEBUG"] == "INFO"


def test_secrets_in_the_shell_do_not_reach_the_unit(monkeypatch):
    """The allowlist exists so an ambient token is not copied into a unit
    property, where it would be world-readable via `systemctl show`."""
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "shhh")
    monkeypatch.setenv("GH_TOKEN", "ghp_shhh")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shhh")
    env = cli.build_env([])
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "GH_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env


def test_explicit_env_wins_over_the_ambient_one(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/from/shell")
    assert cli.build_env(["PYTHONPATH=/explicit"])["PYTHONPATH"] == "/explicit"


def test_env_value_may_contain_equals_signs():
    assert cli.build_env(["X=a=b=c"])["X"] == "a=b=c"


def test_env_value_may_be_empty():
    """`--env PYTHONUNBUFFERED=` is the documented way to restore buffering."""
    assert cli.build_env(["PYTHONUNBUFFERED="])["PYTHONUNBUFFERED"] == ""


def test_env_without_an_equals_sign_is_a_usage_error():
    with pytest.raises(SystemExit) as e:
        cli.build_env(["JUST_A_NAME"])
    assert e.value.code == cli.EX_USAGE


# --- argv[0] resolution ------------------------------------------------------

def test_a_bare_name_resolves_on_the_callers_path(tmp_path, monkeypatch):
    exe = tmp_path / "mytool"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    assert cli.resolve_exe("mytool", "/") == str(exe)


def test_a_relative_path_resolves_against_the_run_cwd(tmp_path):
    """`--cwd repo -- .venv/bin/python` is the way these repos are invoked."""
    venv = tmp_path / ".venv" / "bin"
    venv.mkdir(parents=True)
    exe = venv / "python"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert cli.resolve_exe(".venv/bin/python", str(tmp_path)) == str(exe)


def test_an_absolute_path_is_used_as_given():
    assert cli.resolve_exe("/bin/sh", "/nonexistent") == "/bin/sh"


def test_a_missing_command_resolves_to_none(tmp_path):
    assert cli.resolve_exe("definitely-not-real-xyz", str(tmp_path)) is None


def test_a_non_executable_file_is_not_a_command(tmp_path):
    """Found by pointing --cwd at a repo and naming a data file: it exists, so a
    naive check passes, and the failure surfaces much later out of systemd."""
    plain = tmp_path / "notes.txt"
    plain.write_text("hello")
    assert cli.resolve_exe("notes.txt", str(tmp_path)) is None


# --- the `--` split ----------------------------------------------------------

def _parsed(monkeypatch, argv):
    """Run main() far enough to inspect the parsed args, without launching."""
    captured = {}

    def fake(args):
        captured["args"] = args
        return cli.EX_OK

    monkeypatch.setattr(cli, "cmd_run", fake)
    assert cli.main(argv) == cli.EX_OK
    return captured["args"]


def test_everything_after_the_first_double_dash_is_the_command(monkeypatch):
    args = _parsed(monkeypatch, ["run", "job", "--", "python", "train.py"])
    assert args.name == "job" and args.cmd == ["python", "train.py"]


def test_our_own_flags_before_the_split_are_still_parsed(monkeypatch):
    args = _parsed(monkeypatch, ["run", "job", "--gpu", "1", "--", "python", "t.py"])
    assert args.gpu == 1 and args.cmd == ["python", "t.py"]


def test_a_double_dash_inside_the_command_is_left_alone(monkeypatch):
    """`python -m mod -- --flag` must reach the child intact: we split on the
    FIRST `--` only, and everything after it is verbatim."""
    args = _parsed(monkeypatch, ["run", "job", "--", "python", "-m", "mod",
                                 "--", "--inner-flag"])
    assert args.cmd == ["python", "-m", "mod", "--", "--inner-flag"]


def test_a_command_flag_is_not_stolen_by_our_parser(monkeypatch):
    """--gpu after the split belongs to the child, not to proc."""
    args = _parsed(monkeypatch, ["run", "job", "--", "trainer", "--gpu", "7"])
    assert args.gpu is None and args.cmd == ["trainer", "--gpu", "7"]


def test_bare_gpu_flag_means_device_zero(monkeypatch):
    args = _parsed(monkeypatch, ["run", "job", "--gpu", "--", "cmd"])
    assert args.gpu == 0


def test_an_unknown_subcommand_is_a_usage_error():
    with pytest.raises(SystemExit) as e:
        cli.main(["frobnicate"])
    assert e.value.code == cli.EX_USAGE
