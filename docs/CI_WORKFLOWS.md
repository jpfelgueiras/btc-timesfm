# CI workflow contract

Every workflow in `.github/workflows` follows this contract:

- workflow-level `permissions` grant only the capabilities required by its jobs;
- workflow-level `concurrency` has a stable group and explicitly sets cancellation behavior;
- each job declares `runs-on` and `timeout-minutes`;
- jobs that execute repository code use `./.github/actions/python-setup` for checkout, Python 3.11, pip caching and optional requirements installation;
- every `actions/upload-artifact` upload declares `retention-days` and uses a deterministic name;
- workflow environment values are declared at workflow scope unless they are secret or step-specific.

The shared composite accepts `requirements-file` and `checkout-ref`. Omit `requirements-file` for workflows that only need a Python interpreter. The composite performs the same checkout, Python setup and dependency-install path for all consumers.

`python scripts/lint_workflows.py` validates this contract. It runs in the required `Unit tests + quality` check, so a workflow that bypasses the composite or omits required metadata blocks merge.

## Required checks

The repository ruleset requires only `CI quality gates / Unit tests + quality`. Earlier compatibility aliases `Unit tests` and `unit-tests` were duplicate jobs that mirrored that result and have been removed; no gate was relaxed.
