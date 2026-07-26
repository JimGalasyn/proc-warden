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

- [x] `CODECOV_TOKEN` repository secret
- [x] `PYPI_API_TOKEN` repository secret
- [x] GitHub Environment named `pypi`
- [ ] **Zenodo:** sign in at <https://zenodo.org> with GitHub, enable the toggle
      for `JimGalasyn/proc-warden` under *GitHub*. It mints a DOI on the **next**
      release, so this has to be on *before* the first one. Then add the concept
      DOI to `README.md` (badge), `CITATION.cff` (`doi:` + `identifiers:`), and
      commit — see the `run-farm` versions of those files for the exact shape.

The Zenodo step is the only one that cannot be done from the command line, and
the only one that is order-sensitive: enabling it after a release does not
retroactively mint a DOI for that release.

## Moving to trusted publishing (recommended, later)

PyPI prefers OIDC, which stores no credential in the repo at all. One-time setup
on pypi.org → *Account* → *Publishing* → *Add a pending publisher*:

| Field | Value |
| --- | --- |
| PyPI Project Name | `proc-warden` |
| Owner | `JimGalasyn` |
| Repository name | `proc-warden` |
| Workflow name | `publish-pypi.yml` |
| Environment name | `pypi` |

Then in `publish-pypi.yml`: delete the `with: password:` block from the publish
step and add `permissions: id-token: write` to that job. Once it works, delete
the `PYPI_API_TOKEN` secret.

## Verifying a release

```bash
pip install --no-cache-dir proc-warden==X.Y.Z
proc --help && proc-warden --help
```

Both console scripts are installed on purpose: `proc` is the name every doc and
`skills/proc-lifecycle/SKILL.md` uses, and `proc-warden` is there for anyone who
already has a `proc` on their PATH.
