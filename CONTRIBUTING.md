# Contributing

Thanks for contributing to `InteractiveBrokersPlatform`.

## Ground Rules

- Prefer small, low-risk pull requests.
- Keep refactors separate from behavior changes.
- Add or update tests when changing runtime behavior.
- Do not use deployment or scheduled workflows as a substitute for local verification.
- Changes touching live execution, credentials, permissions, Cloud Run, or a broker/exchange API must be verified in a test environment or dry-run first; do not edit production behavior based on examples alone.

## Branching and Pull Requests

- Create a topic branch for each change.
- Open a pull request with a short summary and a concrete test plan.
- Wait for CI to pass before merging.

## Local Verification

Run the main verification command before opening a pull request:

```bash
uv sync --frozen --extra test && PYTHONPATH=. PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run --no-sync python -m pytest -q
```
