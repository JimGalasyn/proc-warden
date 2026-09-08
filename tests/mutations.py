"""Mutation contracts: each recorded defect, restored, must turn its regression
test red -- and only that test, unless a second detector is named here.

Checked by `mutgate run tests/mutations.py` in CI's `mutations` job. It needs
the same live systemd user manager the integration tests need: without one
`tests/test_proc.py` skips wholesale, skips are invisible to mutgate, and every
contract whose test lives there reads DECORATION. A green suite guards only the
defects that shaped it, and `docs/CONTRIBUTING.md` asks that every regression
test be seen to fail against the unfixed code once. This file keeps that check
alive after the fix: `old` is the fixed code, `new` is the bug as it shipped,
`fires` is the test that was written for it. A NOT_APPLIED verdict means a
refactor moved the site and the contract needs re-anchoring; a DECORATION means
the guard no longer goes red; an OVERREACH means a test outside the contract
fired, which is a finding about the code before it is a `may_fire` entry.

Anchors are the shortest text that occurs exactly once at the site -- code, not
the comments beside it -- so a reword does not read as a moved site.

Every mutation is applied in a throwaway copy of the checkout; the working tree
is never touched. Locally, a stray global pytest plugin can break collection
exactly as it does for pytest itself (see CONTRIBUTING); the same
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 is inherited by the runs mutgate starts.
"""

from mutgate import Mutation

TESTS = ["tests/test_unit.py", "tests/test_proc.py"]
PATHS = ["src", "."]

CLI = "src/proc_warden/cli.py"

# Integration tests that launch a command which exits nonzero within
# milliseconds. Without the finished_anyway() guard, a contended systemd
# reports such a launch as failed and the record is lost -- nondeterministically.
FAST_EXIT_TESTS = (
    "test_a_failed_launch_keeps_the_previous_runs_record",
    "test_wait_distinguishes_clean_exit_from_failure",
    "test_exit_code_is_readable_after_death",
    "test_run_reports_an_immediate_death_in_its_exit_code",
    "test_wait_reports_death_before_readiness",
)

MUTATIONS = [
    # --- 0.1.1 -------------------------------------------------------------
    Mutation("ready-marker-not-carried", CLI,
             old='        lines = (tail + chunk).split("\\n")',
             new='        lines = chunk.split("\\n")',
             fires=("test_ready_marker_split_across_writes_is_still_matched",),
             note="wait --ready searched the partial trailing line but did not carry it "
                  "into the next read, so a marker split across two reads never matched. "
                  "This is the 0.1.0 code's behaviour exactly; a marker within one read "
                  "still matches, so the no-trailing-newline test stays green."),
    Mutation("immediate-death-reads-ok", CLI,
             old='    if state in ("FAILED", "KILLED", "OOM", "LOST"):\n'
                 '        return EX_FAILED\n',
             new='',
             fires=("test_run_reports_an_immediate_death_in_its_exit_code",),
             may_fire=("test_a_run_that_dies_before_its_start_job_completes_is_a_failed_run",),
             note="`proc run` printed FAILED but exited 0, so `|| handle` missed a crash. "
                  "The fast-death test asserts that exit code too; it is the same guard."),
    Mutation("gpu-wait-without-gpu-ignored", CLI,
             old="    if args.gpu_wait is not None and args.gpu is None:",
             new="    if False:",
             fires=("test_gpu_wait_without_gpu_is_refused_not_ignored",),
             note="the typo launched an unserialized GPU run that looked fine"),
    # --- 0.1.4 -------------------------------------------------------------
    Mutation("tail-reads-whole-file", CLI,
             old='    while pos > 0 and buf.count(b"\\n") <= n:',
             new='    while pos > 0:',
             fires=("test_tail_does_not_read_the_whole_file",
                    "test_logs_tail_reads_a_bounded_slice_of_the_file"),
             note="logs -n slurped a multi-gigabyte log to print twenty lines; only the "
                  "byte-counting tests can see it, and both must"),
    Mutation("logs-tail-bypasses-the-bounded-reader", CLI,
             old="            text = tail_text(fh, args.tail)",
             new='            text = "\\n".join(fh.read().decode("utf-8", "replace")'
                 '.splitlines()[-args.tail:])',
             fires=("test_logs_tail_reads_a_bounded_slice_of_the_file",),
             note="the shipped site: 0.1.3 sliced the whole file in cmd_logs itself, and "
                  "a slurp there never touches tail_text or its unit test"),
    Mutation("logs-stream-slurps", CLI,
             old="                chunk = fh.read(LOG_BLOCK)",
             new="                chunk = fh.read()",
             fires=("test_logs_stream_never_holds_the_whole_log",),
             note="logs with no -n buffered the whole log; the output is byte-identical, "
                  "so the largest single read is the only witness"),
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
             fires=("test_name_lock_excludes_a_second_launcher",),
             may_fire=("test_concurrent_launches_of_one_name_do_not_corrupt_each_other",),
             note="two concurrent runs of one name both passed the RUNNING check. The "
                  "integration test races real launches and the scheduler can serialize "
                  "them by accident, so the mechanism is pinned by the unit test."),
    Mutation("meta-world-readable", CLI,
             old="os.O_TRUNC, 0o600)",
             new="os.O_TRUNC, 0o644)",
             fires=("test_meta_json_is_not_world_readable",),
             note="--env is the one path by which a secret reaches meta.json. The test's "
                  "proc() helper pins umask 022, or a 077 box would mask this to 0600 and "
                  "read DECORATION on a sound guard."),
    Mutation("follow-drops-final-bytes", CLI,
             old="emit(fh.read(), final=True)",
             new="pass",
             fires=("test_follow_drains_what_the_process_wrote_as_it_died",),
             may_fire=("test_logs_follow_terminates_when_the_process_dies",
                       "test_logs_tail_then_follow_picks_up_from_the_end"),
             note="logs -f discarded what the process wrote in its last moments. The "
                  "window is one systemctl call wide: the unit test stages it, and the "
                  "two integration follow tests hit it by chance a few percent of the time."),
    # --- unreleased --------------------------------------------------------
    Mutation("fast-death-reads-as-a-failed-launch", CLI,
             old="    if r.returncode != 0 and not finished_anyway(status_path):",
             new="    if r.returncode != 0:",
             fires=("test_a_run_that_dies_before_its_start_job_completes_is_a_failed_run",),
             may_fire=FAST_EXIT_TESTS,
             note="found by review of this file: a command that exits nonzero within "
                  "milliseconds fails its Type=exec start job under load, and the launcher "
                  "threw the record away. The integration tests that exit fast lose that "
                  "race some of the time, which is why they are second detectors."),
]
