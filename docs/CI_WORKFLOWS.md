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

## Required merge checks and drift verification

`CI quality gates / Unit tests + quality` is the canonical quality gate. Its final `Enforce quality gates` step aggregates the unit-test, 75% coverage floor, Ruff lint/format, workflow-contract, action-pin, and staged mypy target outcomes; failures in any of them fail the job. The individual steps use `continue-on-error` so the other diagnostics still run.

The active `Protect main` ruleset currently requires these status contexts:

- `Unit tests + quality` (canonical quality gate);
- `Unit tests` and `unit-tests` (legacy compatibility aliases; both mirror the canonical job);
- `Dependency audit`.

The ruleset also requires pull-request review. Keep the workflow/job names and these contexts synchronized when renaming checks. The explicit mypy file list in `tests.yml` is the staged type-check scope; extending it should be done deliberately as modules become clean.

Maintainers can verify the live repository configuration with `gh auth login` and `python scripts/check_required_checks.py` (or pass `--repo OWNER/REPOSITORY`). The command reads repository and inherited rulesets through the GitHub API, considers only active rules, and fails if the canonical check, either compatibility context, dependency audit, or a pull-request review rule is missing. The authenticated account needs permission to view rulesets. Run this after ruleset edits and periodically; repository ruleset configuration is external to git and cannot be enforced by a workflow when the ruleset itself is removed or weakened.

`python scripts/check_required_checks.py` validates the contract against synthetic rulesets in unit tests. The live API drift check is a maintainer/runbook check because PR workflows do not have permission to inspect repository rulesets consistently across repository plans and token permissions.
