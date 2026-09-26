# Contributing

Thank you for helping improve BTC Forecast Ensemble. Contributions to code,
tests, documentation, and bug reports are welcome.

## Before you start

- For a substantial change, open an issue or discussion first so the scope and
  approach can be agreed before implementation.
- Check existing issues and pull requests to avoid duplicating work.
- For security vulnerabilities, follow the private reporting guidance in
  [SECURITY.md](SECURITY.md) rather than opening a public issue.
- By contributing, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

The project supports Python 3.11, 3.12, and 3.13. From a fresh clone, create an
environment and install the locked test and quality dependencies:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install uv==0.12.19
uv sync --locked --no-default-groups --group test
```

Run tests and the checks used by CI before submitting a pull request:

```bash
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py'
ruff check .
ruff format --check .
python scripts/lint_workflows.py
python scripts/lint_action_pins.py
```

Some integration or browser tests may require additional local setup. Mention
any tests you could not run and why in your pull request.

## Change guidelines

- Keep changes focused and explain the motivation and user impact.
- Add or update tests for behavior changes and bug fixes.
- Update documentation when commands, configuration, APIs, or behavior change.
- Preserve reproducibility and avoid look-ahead/data leakage in forecasting or
  backtesting code.
- Do not commit credentials, cookies, private keys, generated forecasts, local
  state, or unrelated formatting changes.
- Prefer clear, typed code and keep security-sensitive logging free of secrets.

## Pull requests

Open a pull request against `main` and complete the repository's pull request
template. Include a concise summary, rationale, testing performed, and any
configuration or operational impact. Link related issues and include screenshots
for user-facing dashboard changes when useful.

Pull requests are reviewed for correctness, security, maintainability, and CI
results. Please respond to review feedback and keep the branch up to date where
practical.
