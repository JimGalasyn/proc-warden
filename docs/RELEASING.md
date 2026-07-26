# Releasing

The version is static in three files and they must agree:

- `pyproject.toml` → `[project] version`
- `src/proc_warden/__init__.py` → `__version__`
- `CITATION.cff` → `version` (and `date-released`)

## Cutting a release

1. Bump those three, and move the `CHANGELOG.md` `Unreleased` section under the
   new version heading.
2. Commit, then tag and push:

   ```bash
   git tag vX.Y.Z && git push origin vX.Y.Z
   ```

3. Publish a GitHub Release for the tag. That triggers
   `.github/workflows/publish-pypi.yml`, which refuses to build unless the tag
   matches the `pyproject.toml` version, runs `twine check`, and uploads to PyPI.

```bash
gh release create vX.Y.Z --title "vX.Y.Z" --notes-file <(sed -n '/^## X.Y.Z/,/^## /p' CHANGELOG.md)
```

## Before the first publish (one-time)

All done — recorded here because these four values are load-bearing and easy to
break later.

- [x] `CODECOV_TOKEN` repository secret
- [x] GitHub Environment named `pypi`
- [x] **PyPI trusted publishing**, registered as a *pending* publisher (the form
      for a project that does not exist yet). The first successful publish
      creates `systemd-proc` and converts it to a normal publisher.
- [x] **Zenodo** synced to the repo, waiting on a release. It mints a DOI when
      one is published; it does **not** backfill, so a release published before
      the sync would never get one.

### Why the PyPI name is not `proc-warden`

PyPI compares project names with `-`, `_`, and `.` stripped out entirely, and
`procwarden` was taken in May 2026 by an unrelated Python library for supervising
subprocesses inside one program. So `proc-warden` is permanently unavailable and
the distribution is **`systemd-proc`**. Do not "fix" this back — the upload will
be rejected with *"This project name is too similar to an existing project"*.

Everything else keeps the original name: the repo, the import package
`proc_warden`, and both console scripts.

### Pending publisher fields

The pending publisher must match the workflow exactly, or the upload is rejected
with a confusing permissions error:

| Field | Value |
| --- | --- |
| PyPI Project Name | `systemd-proc` |
| Owner | `JimGalasyn` |
| Repository name | `proc-warden` |
| Workflow name | `publish-pypi.yml` |
| Environment name | `pypi` |

Renaming the repo, the workflow file, or the environment means updating it on
pypi.org too.

## After the first release: record the DOI

Zenodo mints two DOIs — a *concept* DOI that always resolves to the latest
version, and a *version* DOI for that specific release. Add them:

- `README.md` — badge, using the **concept** DOI:
  `[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.XXXXXXX.svg)](https://doi.org/10.5281/zenodo.XXXXXXX)`
- `CITATION.cff` — a top-level `doi:` (concept), plus an `identifiers:` list
  giving both. See `run-farm`'s `CITATION.cff` for the exact shape.

The badge is deliberately absent until then: a DOI badge pointing at nothing is
worse than no badge.

## Verifying a release

```bash
pip install --no-cache-dir systemd-proc==X.Y.Z
proc --help && proc-warden --help
```

Both console scripts are installed on purpose: `proc` is the name every doc and
`skills/proc-lifecycle/SKILL.md` uses, and `proc-warden` is there for anyone who
already has a `proc` on their PATH.
