# CV generation evaluation

This evaluation uses five synthetic frozen job descriptions chosen to exercise
different resume narratives: agentic AI, application security, backend
engineering, cloud/platform work, and automation consulting. The public
fixtures contain no live listing or applicant data.

For a private Hunter deployment, you can deliberately replace the fixtures from
its remote job store. The host and checkout path are required explicitly:

```bash
HUNTER_REMOTE_HOST=user@hunter-host \
HUNTER_REMOTE_DIR=/srv/hunter \
uv run python scripts/cv-eval.py snapshot --overwrite
```

Generate a baseline before changing the CV prompt or renderer:

```bash
npm run cv:gen -- baseline
```

The five CVs are generated concurrently. `profile.md` is the only candidate
evidence supplied to the model; the configured legacy resume PDF is not read.

For a smaller experiment, generate only the first one to four frozen jobs:

```bash
npm run cv:gen -- quick-check --count 1
```

All selected CVs are still generated concurrently. A comparison automatically
uses only jobs present in both named runs, so `quick-check` can be compared
directly with the full baseline.

After making a change, generate another named run and compare it:

```bash
npm run cv:gen -- candidate
npm run cv:compare -- baseline candidate
```

Runs are written under `output/pdf/cv-eval/runs/`. Each job keeps its PDF,
Poppler-rendered PNG, extracted text, structured resume specification, RenderCV
input, Typst source, and validation metrics. Comparisons are written under
`output/pdf/cv-eval/comparisons/` as a Markdown report with side-by-side previews
and unified text/spec diffs.

Use `--overwrite` to replace an existing snapshot or run deliberately. Generated
runs and comparisons are local artifacts and are ignored by Git.
