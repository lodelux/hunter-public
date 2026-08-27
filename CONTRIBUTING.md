# Contributing to Hunter

Hunter is maintained as a small personal application. Keep changes focused,
easy to review, and grounded in the current checkout.

## Setup

Requirements are Node.js 20+, Python 3.13+, uv, and Git.

```bash
npm ci
uv sync
uv run python -m playwright install chromium
npm run dev
```

`npm run dev` launches the React UI and FastAPI backend together. Open
`http://localhost:1420`.

## Architecture

```text
Browser -> React UI -> same-origin HTTP API -> FastAPI
                                           -> Browser Use / Playwright
                                           -> SQLite and application data
```

- UI code lives in `src/`.
- API and automation code lives in `backend/`.
- Command-line workflows live in `cli/`.
- Python tests live in `tests/`; frontend tests sit beside their source files.
- `pyproject.toml` and `uv.lock` are the Python dependency source of truth.
- `package.json` and `package-lock.json` are the frontend dependency source of truth.

Runtime state, credentials, browser profiles, generated documents, and logs do
not belong in Git.

## Making changes

- Prefer the smallest targeted implementation.
- Preserve unrelated work in the checkout.
- Add or update focused tests with behavior changes.
- Treat application completion as untrusted until visible submission evidence
  and the independent judge agree.
- Do not merge, push, or deploy unless that action is explicitly requested.

Before handing off a change, run the checks relevant to it:

```bash
npm test
npm run typecheck
npm run build
uv run pytest
git diff --check
```

Live browser, job-board, Gmail, and remote deployment checks should be run only
when the task requires them; they can consume external resources or mutate
runtime state.
