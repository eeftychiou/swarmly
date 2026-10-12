# Releasing to PyPI

Releases are built and uploaded by [`.github/workflows/publish.yml`](../.github/workflows/publish.yml)
through PyPI's trusted publishing: GitHub proves to PyPI which repository and workflow is
asking, so no API token exists to leak.

## One-time setup (repository owner)

1. **PyPI account.** Sign in at <https://pypi.org> (create the account and turn on two-factor
   authentication if needed). Do the same at <https://test.pypi.org>, which is a separate site
   with its own account.
2. **Pending publisher on PyPI.** <https://pypi.org/manage/account/publishing/> -> *Add a new
   pending publisher* -> *GitHub*, and enter exactly:

   | Field | Value |
   |---|---|
   | PyPI Project Name | `swarmly` |
   | Owner | `eeftychiou` |
   | Repository name | `swarmly` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

   The first successful upload creates the project and turns the pending publisher into a
   normal one.
3. **Pending publisher on TestPyPI.** The same form at
   <https://test.pypi.org/manage/account/publishing/>, with Environment name `testpypi`.
4. **GitHub environments.** In the repository: *Settings* -> *Environments* -> *New
   environment*, create `pypi`, and under *Deployment protection rules* tick *Required
   reviewers* and add yourself, so every upload waits for your click. Create `testpypi` the
   same way (a reviewer is optional there).

## Each release

1. Set `__version__` in [`swarmly/__init__.py`](../swarmly/__init__.py) and merge that to `main`.
   PyPI never accepts the same version twice, so a broken upload means a new version number.
2. Optional dress rehearsal: *Actions* -> *publish* -> *Run workflow* uploads the build to
   TestPyPI. Try it in a clean environment:
   `pipx install --index-url https://test.pypi.org/simple/ --pip-args="--extra-index-url https://pypi.org/simple/" swarmly`
3. Tag the merge commit and push the tag:

   ```bash
   git tag v0.9.0 && git push origin v0.9.0
   ```

   The workflow checks that the tag matches `__version__`, builds the sdist and wheel, runs
   `twine check` and a smoke test of the wheel, then waits for your approval on the `pypi`
   environment and uploads.
4. Check <https://pypi.org/project/swarmly/> and `pipx install swarmly` on a clean machine.
