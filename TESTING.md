# Testing Hunter

Hunter has a Vitest suite for the React application and a pytest suite for the
FastAPI backend, automation helpers, storage, and CLI workflows.

## Frontend

```bash
npm test
npm run test:watch
npm run test:coverage
npm run typecheck
npm run build
```

Frontend tests use jsdom and stub network calls. Shared setup lives in
`src/test/setup.ts`; tests are colocated with their components and modules.

## Backend

```bash
uv run pytest
uv run pytest --cov --cov-report=term-missing
uv run pytest tests/backend/test_main_api.py -q
```

The `data_dir` fixture redirects application data into a temporary home so
tests do not read or write the real Hunter profile, browser session, jobs, or
memory database. External models, browsers, and network calls must be mocked in
the normal test suite.

## Integration checks

```bash
uv run python scripts/integration-test.py
uv run python scripts/jobspy-smoke-test.py
```

The integration script starts a source backend on an isolated port. The JobSpy
smoke test performs live external requests and should be run intentionally.
