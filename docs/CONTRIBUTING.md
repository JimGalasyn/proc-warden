# Contributing

## Layout

| Path | What it is |
| --- | --- |
| `src/proc_warden/cli.py` | the whole CLI |
| `proc` | a shim so a checkout runs without installing (`realpath`, so a symlink into it still works) |
| `tests/test_unit.py` | pure logic; no systemd; runs anywhere |
| `tests/test_proc.py` | drives a real systemd user manager |
| `tests/mutations.py` | each recorded defect, restored, and the test that must catch it |
| `docs/DESIGN.md` | the six invariants and why each exists — **read this first** |

## Running the tests

```bash
pip install -e ".[test]"
pytest -q
```

Two things are worth knowing, because both produce a number that looks fine and
isn't.

**Coverage needs subprocess tracking.** The integration tests exercise `proc` as
a subprocess — that is the point of them, since the tool's job is to survive
process boundaries — so coverage sees nothing unless it is told to follow. The
difference is not small: 38% without, 86% with.

```bash
COVERAGE_PROCESS_START=$PWD/pyproject.toml pytest -q --cov=proc_warden --cov-report=term
```

**A skipped suite is not a passing suite.** `tests/test_proc.py` skips wholesale
without a systemd user manager, which is right on a dev box and wrong in CI. Set
`PROC_REQUIRE_SYSTEMD=1` to turn that skip into a hard error; CI does.

**Mutation contracts.** `tests/mutations.py` restores each defect recorded in
`CHANGELOG.md` and requires its regression test to go red, and only that test.
Run them with

```bash
mutgate run --timeout 300 tests/mutations.py
```

which applies each mutation in a throwaway copy of the checkout (the working
tree is never touched) and runs the suite against it. **This needs a systemd
user manager too**: most contracts fire tests in `test_proc.py`, a skip is
invisible to mutgate, and on a box without one every such contract reads
`DECORATION`. Without systemd, check a contract whose test is in
`test_unit.py` on its own:

```bash
mutgate run tests/mutations.py --only tail-reads-whole-file --tests tests/test_unit.py
```

The `mutations` job in CI runs the full file and is gated on its exit code.
`DECORATION` means a guard no longer fires; `NOT_APPLIED` means a refactor
moved the site and the `old` text needs re-anchoring; `OVERREACH` means a test
outside the contract fired, which is a finding about the code before it is a
`may_fire` entry. Anchor on code, not on the comment beside it, so a reword is
not a moved site.

### If collection dies before any test runs

A globally installed pytest plugin can crash collection with a traceback that
never mentions this repo (a system-wide `hydra`/`omegaconf` does exactly that,
failing in an ANTLR deserializer). Plugin autoload happens before any config in
`pyproject.toml` is read, so nothing in this repo can prevent it:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q -p pytest_cov   # -p: re-enable what you need
```

## Adding a test

Name it after the failure mode it forbids, not after the function it calls —
`test_stop_reaps_a_child_that_escaped_with_setsid`, not `test_stop`. Every test
in `test_proc.py` corresponds to something that actually went wrong once, and
the name is how that stays legible.

For a bug fix, check that the new test **fails against the unfixed code** before
you fix it. A regression test that passes either way documents nothing.
Then keep that check alive: add a `Mutation` to `tests/mutations.py` whose
`new` is the bug as it shipped and whose `fires` is the new test.

## Things deliberately not done

Before proposing any of these, see the rejected-alternatives table in
`docs/DESIGN.md`: no PID files, no bespoke supervisor daemon, no `ps`/`pgrep`
anywhere, and no queueing beyond a depth-1 GPU lease.
