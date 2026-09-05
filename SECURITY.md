# Security policy

## Automated checks

The repository uses two complementary mechanisms:

- **Dependabot** checks Python and GitHub Actions dependencies every week and opens reviewable update pull requests.
- **Security audit** runs on relevant pull requests, on demand, and every Monday. It uses `pip-audit` for Python dependency advisories and `security_audit.py` to catch common credential literals and unsafe workflow diagnostic patterns.

The security workflow has read-only repository permissions and must not print `X_COOKIES_JSON`, `auth_token`, `ct0`, or complete environment dumps.

## Vulnerability policy

A known vulnerability reported by the dependency audit fails the security job. High or critical findings must not be merged until one of the following is true:

1. the affected dependency is upgraded or removed;
2. a documented upstream fix or safe version constraint is applied; or
3. the finding is demonstrated to be non-applicable to this project and the exception is recorded in the pull request with an explicit review decision.

Lower-severity findings should also be fixed promptly; suppressions should be narrow, documented, and temporary.

## Dependency policy

Runtime Python dependencies must use explicit compatible version constraints in `requirements.txt`. Security/quality tooling is isolated in dedicated requirements files. GitHub Actions used by the security workflow are pinned to immutable commit SHAs; Dependabot remains responsible for proposing reviewed SHA updates.

Never commit X cookies, session tokens, API credentials, private keys, or generated files containing them. Use GitHub Actions secrets for runtime credentials.
