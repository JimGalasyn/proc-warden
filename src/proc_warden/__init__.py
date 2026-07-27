"""proc-warden -- process lifetime you can reason about.

A thin, dependency-free wrapper over `systemd-run --user`. Every managed process
gets a NAME (never a command-line pattern), a cgroup (so "kill" means kill), a
recorded exit status that outlives it, and optionally an exclusive GPU lease.

The CLI lives in `proc_warden.cli`; `run()` is the console-script entry point,
installed as both `proc-warden` and `proc`.
"""

from __future__ import annotations

__version__ = "0.1.3"

__all__ = ["__version__", "main", "run"]


def __getattr__(name: str):
    # Imported lazily so `import proc_warden` stays cheap and side-effect free
    # -- cli imports subprocess/argparse and reads the environment at module
    # scope (PROC_HOME), which a version check should not have to pay for.
    if name in ("main", "run"):
        from . import cli

        return getattr(cli, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
