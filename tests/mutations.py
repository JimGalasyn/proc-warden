"""Mutation contracts: each recorded defect, restored, must turn its regression
test red -- and only that test, unless a second detector is named here.

Checked by `mutgate run tests/mutations.py` (CI's integration job; it needs the
systemd user manager the integration tests need). A green suite guards only the
defects that shaped it, and `docs/CONTRIBUTING.md` asks that every regression
test be seen to fail against the unfixed code once. This file keeps that check
alive after the fix: `old` is the fixed code, `new` is the bug as it shipped,
`fires` is the test that was written for it. A NOT_APPLIED verdict means a
refactor moved the site and the contract needs re-anchoring; a DECORATION means
the guard no longer goes red.

Every mutation is applied in a throwaway copy of the checkout; the working tree
is never touched. Locally, a stray global pytest plugin can break collection
exactly as it does for pytest itself (see CONTRIBUTING); the same
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 is inherited by the runs mutgate starts.
"""

from mutgate import Mutation

TESTS = ["tests/test_unit.py", "tests/test_proc.py"]
PATHS = ["src", "."]

CLI = "src/proc_warden/cli.py"

MUTATIONS = [
    # --- 0.1.1 -------------------------------------------------------------
    Mutation("ready-marker-not-carried", CLI,
             old="        tail = lines.pop()",
             new="        lines.pop()",
             fires=("test_ready_marker_split_across_writes_is_still_matched",),
             may_fire=("test_ready_marker_without_trailing_newline_is_matched",),
             note="wait --ready consumed the partial trailing line, so a marker split "
                  "across two reads never matched. The carried tail also serves the "
                  "no-trailing-newline case, so that test is a second detector."),
    Mutation("immediate-death-reads-ok", CLI,
             old='    if state in ("FAILED", "KILLED", "OOM", "LOST"):\n'
                 '        return EX_FAILED\n'
                 '    return EX_OK\n',
             new='    return EX_OK\n',
             fires=("test_run_reports_an_immediate_death_in_its_exit_code",),
             note="`proc run` printed FAILED but exited 0, so `|| handle` missed a crash"),
    Mutation("gpu-wait-without-gpu-ignored", CLI,
             old="    if args.gpu_wait is not None and args.gpu is None:",
             new="    if False:",
             fires=("test_gpu_wait_without_gpu_is_refused_not_ignored",),
             note="the typo launched an unserialized GPU run that looked fine"),
    # --- 0.1.4 -------------------------------------------------------------
    Mutation("tail-reads-whole-file", CLI,
             old='    while pos > 0 and buf.count(b"\\n") <= n:',
             new='    while pos > 0:',
             fires=("test_tail_does_not_read_the_whole_file",),
             note="logs -n slurped a multi-gigabyte log to print twenty lines; the "
                  "byte-counting test is the only one that can see it"),
    Mutation("failed-launch-destroys-record", CLI,
             old='        stale = d.with_name(f".{name}.replacing.{os.getpid()}")\n'
                 '        shutil.rmtree(stale, ignore_errors=True)\n'
                 '        d.rename(stale)\n',
             new='        shutil.rmtree(d)\n',
             fires=("test_a_failed_launch_keeps_the_previous_runs_record",),
             note="the run dir was deleted before systemd-run was known to have "
                  "succeeded; a typo in the command destroyed the previous run's record"),
    Mutation("name-lock-is-a-no-op", CLI,
             old="        fcntl.flock(fh, fcntl.LOCK_EX)\n",
             new="",
             fires=("test_concurrent_launches_of_one_name_do_not_corrupt_each_other",),
             note="two concurrent runs of one name both passed the RUNNING check. The "
                  "guard is a real race but fired on every one of several trial runs."),
    Mutation("meta-world-readable", CLI,
             old="os.O_TRUNC, 0o600)",
             new="os.O_TRUNC, 0o644)",
             fires=("test_meta_json_is_not_world_readable",),
             note="--env is the one path by which a secret reaches meta.json"),
    Mutation("follow-drops-final-bytes", CLI,
             old="                emit(fh.read(), final=True)  # drain what it wrote as it died\n",
             new="                pass\n",
             fires=("test_follow_drains_what_the_process_wrote_as_it_died",),
             note="logs -f discarded what the process wrote in its last moments. The "
                  "integration follow test cannot see this: the window is one "
                  "systemctl call wide, so the unit test stages the interleaving."),
]
