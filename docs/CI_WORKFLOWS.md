# CI workflow contract

Every workflow in `.github/workflows` follows this contract:

- workflow-level `permissions` grant only the capabilities required by its jobs;
- workflow-level `concurrency` has a stable group and explicitly sets cancellation behavior;
- each job declares `runs-on` and `timeout-minutes`;
- jobs that execute repository code use `./.github/actions/python-setup` for checkout, supported Python setup, pip caching and locked dependency installation;
- every `actions/upload-artifact` upload declares `retention-days` and uses a deterministic name;
- workflow environment values are declared at workflow scope unless they are secret or step-specific.

The shared composite accepts `dependency-set` (`runtime`, `test`, `security`, or `none`) and `python-version`. It installs from `uv.lock` using the pinned uv release; runtime workflows include the TimesFM/Torch model extra, test workflows install the test group, and the security workflow installs its audit tooling. Use `none` for interpreter-only compatibility jobs.

Dependabot checks Python dependencies weekly, including the model and development groups, and proposes reviewed lockfile refreshes. CI uses `uv sync --locked`, so stale or out-of-sync lockfiles fail installation instead of resolving new versions implicitly.

`python scripts/lint_workflows.py` validates this contract. It runs in the required `Unit tests + quality` check, so a workflow that bypasses the composite or omits required metadata blocks merge.

## Required checks

The repository ruleset requires only `CI quality gates / Unit tests + quality`. Earlier compatibility aliases `Unit tests` and `unit-tests` were duplicate jobs that mirrored that result and have been removed; no gate was relaxed.
