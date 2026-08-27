#!/usr/bin/env python3
"""Freeze remote jobs, generate CV evaluation runs, and compare their outputs."""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fitz


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path[:0] = [str(BACKEND), str(ROOT)]

from core.artifact_names import tailored_cv_filename  # noqa: E402
from core.config import load_llm_settings, load_profile_markdown  # noqa: E402
from core.llm_factory import (  # noqa: E402
    DOCUMENT_LLM_MAX_COMPLETION_TOKENS,
    DOCUMENT_LLM_REASONING_EFFORT,
    RESUME_LLM_MODEL,
    create_llm,
    resume_llm_settings,
)
from resume.compiler import (  # noqa: E402
    generate_resume_spec,
    render_resume,
    resume_generation_audit,
)


SELECTION_PATH = ROOT / "evals" / "cv" / "job-selection.json"
JOBS_PATH = ROOT / "evals" / "cv" / "jobs.json"
OUTPUT_ROOT = ROOT / "output" / "pdf" / "cv-eval"
REMOTE_HOST = os.environ.get("HUNTER_REMOTE_HOST", "").strip()
REMOTE_PORT = int(os.environ.get("HUNTER_REMOTE_PORT", "22"))
REMOTE_PROJECT = os.environ.get("HUNTER_REMOTE_DIR", "").strip()
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
EVALUATION_MODELS = ("gpt-5.6-terra", "gpt-5.6-sol")
REASONING_EFFORTS = ("low", "medium", "high")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _safe_name(value: str, kind: str) -> str:
    if not NAME_PATTERN.fullmatch(value):
        raise ValueError(
            f"Invalid {kind} {value!r}; use letters, numbers, dots, dashes, or underscores"
        )
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_value(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def load_jobs(path: Path | None = None) -> list[dict]:
    path = path or JOBS_PATH
    payload = _read_json(path)
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list) or len(jobs) != 5:
        raise ValueError(f"{path} must contain exactly five jobs")

    required = {"id", "url", "title", "company", "location", "description"}
    identifiers: set[str] = set()
    for job in jobs:
        if not isinstance(job, dict) or not required.issubset(job):
            raise ValueError(f"Each job in {path} must contain {sorted(required)}")
        identifier = _safe_name(str(job["id"]), "job id")
        if identifier in identifiers:
            raise ValueError(f"Duplicate job id: {identifier}")
        if len(str(job["description"]).strip()) < 200:
            raise ValueError(f"Job {identifier} has an incomplete description")
        identifiers.add(identifier)
    return jobs


def snapshot_remote_jobs(*, overwrite: bool = False) -> Path:
    if not REMOTE_HOST or not REMOTE_PROJECT:
        raise ValueError(
            "Set HUNTER_REMOTE_HOST and HUNTER_REMOTE_DIR before snapshotting remote jobs"
        )
    if JOBS_PATH.exists() and not overwrite:
        raise FileExistsError(f"{JOBS_PATH} exists; pass --overwrite to replace it")

    selection = _read_json(SELECTION_PATH)
    selected = selection.get("jobs", [])
    remote_script = """
import json, sys
from core.shared_config import read_jobs

selected = json.load(sys.stdin)
stored = read_jobs()
output = []
for choice in selected:
    job = stored.get(choice["url"])
    if not job:
        raise RuntimeError(f"Remote job not found: {choice['url']}")
    description = str(job.get("description") or "").strip()
    if not description:
        raise RuntimeError(f"Remote job has no description: {choice['url']}")
    output.append({
        "id": choice["id"],
        "url": choice["url"],
        "title": str(job.get("title") or ""),
        "company": str(job.get("company") or ""),
        "location": str(job.get("location") or ""),
        "description": description,
    })
json.dump(output, sys.stdout, ensure_ascii=False)
""".strip()
    remote_command = (
        f"cd {shlex.quote(REMOTE_PROJECT)} && "
        f"PYTHONPATH={shlex.quote(REMOTE_PROJECT + '/backend')}:{shlex.quote(REMOTE_PROJECT)} "
        f".venv/bin/python -c {shlex.quote(remote_script)}"
    )
    result = subprocess.run(
        ["ssh", "-p", str(REMOTE_PORT), REMOTE_HOST, remote_command],
        input=json.dumps(selected),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not read remote jobs")

    jobs = json.loads(result.stdout)
    payload = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": f"{REMOTE_HOST}:{REMOTE_PROJECT}",
        "jobs": jobs,
    }
    _write_json(JOBS_PATH, payload)
    load_jobs(JOBS_PATH)
    return JOBS_PATH


def _render_preview(pdf_path: Path, preview_path: Path) -> None:
    renderer = shutil.which("pdftoppm")
    if renderer:
        result = subprocess.run(
            [
                renderer,
                "-f", "1",
                "-singlefile",
                "-png",
                "-r", "144",
                str(pdf_path),
                str(preview_path.with_suffix("")),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0 and preview_path.is_file():
            return

    document = fitz.open(pdf_path)
    try:
        if document.page_count < 1:
            raise RuntimeError(f"Could not render empty PDF {pdf_path}")
        document[0].get_pixmap(dpi=144, alpha=False).save(preview_path)
    finally:
        document.close()


def _extract_text(pdf_path: Path) -> str:
    document = fitz.open(pdf_path)
    try:
        return "\n".join(page.get_text("text", sort=True) for page in document).strip() + "\n"
    finally:
        document.close()


def _selection_rationale_markdown(audit: dict, job: dict) -> str:
    lines = [
        "# CV selection rationale",
        "",
        f"Job: {job['title']} at {job['company']}",
        "",
        audit["positioning"],
        "",
    ]
    lines.extend(("## Priorities", ""))
    for priority in audit["priorities"]:
        lines.append(f"- {priority['decision']}")
        lines.append(
            "  - Target: " + "; ".join(item["text"] for item in priority["requirements"])
        )
        lines.append(
            "  - Evidence: " + "; ".join(item["text"] for item in priority["evidence"])
        )
    lines.extend(("", "## Selected claims", ""))
    for selection in audit["selections"]:
        lines.extend(
            (
                f"### {selection['section']}: {selection['label']}",
                "",
                f"- Fact: {selection['text']}",
                f"- Relevance: {selection['relevance']}/100",
                "- Target: " + "; ".join(item["text"] for item in selection["requirements"]),
                "- Evidence: " + "; ".join(item["text"] for item in selection["evidence"]),
                "",
            )
        )
    return "\n".join(lines).rstrip() + "\n"


async def generate_run(
    label: str,
    *,
    overwrite: bool = False,
    count: int = 5,
    jobs_path: Path | None = None,
    model: str = RESUME_LLM_MODEL,
    reasoning_effort: str = DOCUMENT_LLM_REASONING_EFFORT,
) -> Path:
    label = _safe_name(label, "run label")
    if count < 1 or count > 5:
        raise ValueError("count must be between 1 and 5")

    jobs_path = jobs_path or JOBS_PATH
    jobs = load_jobs(jobs_path)[:count]
    run_dir = OUTPUT_ROOT / "runs" / label
    if run_dir.exists():
        if not overwrite:
            raise FileExistsError(f"{run_dir} exists; pass --overwrite to replace it")
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    profile = load_profile_markdown()
    if not profile.strip():
        raise ValueError("The local profile.md is empty")

    llm_settings = resume_llm_settings(load_llm_settings())
    llm_settings["openai"]["model"] = model
    llm_settings["openai"]["reasoning_effort"] = reasoning_effort
    if not str(llm_settings.get("openai", {}).get("api_key", "")).strip():
        raise ValueError("No local OpenAI API key is configured")

    run_metadata = {
        "schema_version": 1,
        "label": label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_value("rev-parse", "HEAD"),
        "git_dirty": bool(_git_value("status", "--porcelain")),
        "model": model,
        "reasoning_effort": reasoning_effort,
        "max_completion_tokens": DOCUMENT_LLM_MAX_COMPLETION_TOKENS,
        "profile_sha256": hashlib.sha256(profile.encode()).hexdigest(),
        "jobs_sha256": _sha256(jobs_path),
        "job_count": len(jobs),
        "jobs": [],
    }
    _write_json(run_dir / "run.json", run_metadata)

    semaphore = asyncio.Semaphore(len(jobs))

    async def generate_one(job: dict) -> dict:
        job_dir = run_dir / job["id"]
        job_dir.mkdir()
        _write_json(job_dir / "job.json", job)
        try:
            async with semaphore:
                llm = create_llm(llm_settings)
                spec = await generate_resume_spec(
                    llm=llm,
                    profile_markdown=profile,
                    job_description=job["description"],
                    job_title=job["title"],
                )
            pdf_path = job_dir / tailored_cv_filename(job["title"])
            rendered_spec, validation = render_resume(spec, pdf_path)
            audit = resume_generation_audit(
                rendered_spec,
                profile_markdown=profile,
                job_title=job["title"],
                job_description=job["description"],
                model=model,
                reasoning_effort=reasoning_effort,
            )
            (job_dir / "selection-rationale.md").write_text(
                _selection_rationale_markdown(audit, job),
                encoding="utf-8",
            )
            preview_path = job_dir / "preview.png"
            _render_preview(pdf_path, preview_path)
            extracted_text = _extract_text(pdf_path)
            (job_dir / "extracted.txt").write_text(extracted_text, encoding="utf-8")
            metrics = {
                **validation,
                "experience_entries": len(rendered_spec.experience),
                "project_entries": len(rendered_spec.projects),
                "achievement_entries": len(rendered_spec.achievements),
                "education_entries": len(rendered_spec.education),
                "skill_groups": len(rendered_spec.skills),
            }
            _write_json(job_dir / "metrics.json", metrics)
            return {"id": job["id"], "status": "done", "metrics": metrics}
        except Exception as exc:
            (job_dir / "error.txt").write_text(str(exc) + "\n", encoding="utf-8")
            return {"id": job["id"], "status": "error", "error": str(exc)}

    results = await asyncio.gather(*(generate_one(job) for job in jobs))
    run_metadata["jobs"] = results
    _write_json(run_dir / "run.json", run_metadata)
    failures = [item for item in results if item["status"] != "done"]
    if failures:
        failed_ids = ", ".join(item["id"] for item in failures)
        raise RuntimeError(f"CV evaluation run failed for: {failed_ids}")
    return run_dir


def _relative_link(target: Path, start: Path) -> str:
    return Path(os.path.relpath(target, start)).as_posix()


def compare_runs(before: str, after: str) -> Path:
    before = _safe_name(before, "run label")
    after = _safe_name(after, "run label")
    before_dir = OUTPUT_ROOT / "runs" / before
    after_dir = OUTPUT_ROOT / "runs" / after
    if not before_dir.is_dir() or not after_dir.is_dir():
        raise FileNotFoundError("Both named runs must exist before comparison")

    comparison_dir = OUTPUT_ROOT / "comparisons" / f"{before}_vs_{after}"
    if comparison_dir.exists():
        shutil.rmtree(comparison_dir)
    comparison_dir.mkdir(parents=True, exist_ok=True)
    jobs = [
        job
        for job in load_jobs()
        if (before_dir / job["id"]).is_dir() and (after_dir / job["id"]).is_dir()
    ]
    if not jobs:
        raise ValueError("The two runs do not contain any jobs in common")
    report = [
        f"# CV comparison: {before} vs {after}",
        "",
        "Each preview is the final rendered PDF. Text and ResumeSpec diffs are linked below.",
        "",
    ]
    summary = {"before": before, "after": after, "jobs": []}

    for job in jobs:
        identifier = job["id"]
        left = before_dir / identifier
        right = after_dir / identifier
        left_text = (left / "extracted.txt").read_text(encoding="utf-8")
        right_text = (right / "extracted.txt").read_text(encoding="utf-8")
        text_diff = "".join(
            difflib.unified_diff(
                left_text.splitlines(keepends=True),
                right_text.splitlines(keepends=True),
                fromfile=f"{before}/extracted.txt",
                tofile=f"{after}/extracted.txt",
            )
        )
        left_spec = json.dumps(_read_json(left / "resume-spec.json"), indent=2, ensure_ascii=False) + "\n"
        right_spec = json.dumps(_read_json(right / "resume-spec.json"), indent=2, ensure_ascii=False) + "\n"
        spec_diff = "".join(
            difflib.unified_diff(
                left_spec.splitlines(keepends=True),
                right_spec.splitlines(keepends=True),
                fromfile=f"{before}/resume-spec.json",
                tofile=f"{after}/resume-spec.json",
            )
        )
        left_rationale_path = left / "selection-rationale.md"
        right_rationale_path = right / "selection-rationale.md"
        left_rationale = (
            left_rationale_path.read_text(encoding="utf-8")
            if left_rationale_path.is_file()
            else "Selection rationale unavailable for this legacy run.\n"
        )
        right_rationale = (
            right_rationale_path.read_text(encoding="utf-8")
            if right_rationale_path.is_file()
            else "Selection rationale unavailable for this legacy run.\n"
        )
        rationale_diff = "".join(
            difflib.unified_diff(
                left_rationale.splitlines(keepends=True),
                right_rationale.splitlines(keepends=True),
                fromfile=f"{before}/selection-rationale.md",
                tofile=f"{after}/selection-rationale.md",
            )
        )
        text_diff_path = comparison_dir / f"{identifier}.text.diff"
        spec_diff_path = comparison_dir / f"{identifier}.spec.diff"
        rationale_diff_path = comparison_dir / f"{identifier}.rationale.diff"
        text_diff_path.write_text(text_diff or "No extracted-text changes.\n", encoding="utf-8")
        spec_diff_path.write_text(spec_diff or "No ResumeSpec changes.\n", encoding="utf-8")
        rationale_diff_path.write_text(
            rationale_diff or "No selection-rationale changes.\n", encoding="utf-8"
        )

        changed_lines = sum(
            1
            for line in text_diff.splitlines()
            if (line.startswith("+") or line.startswith("-"))
            and not line.startswith(("+++", "---"))
        )
        summary["jobs"].append({"id": identifier, "changed_text_lines": changed_lines})
        left_rationale_link = (
            f"[{before}]({_relative_link(left_rationale_path, comparison_dir)})"
            if left_rationale_path.is_file()
            else f"{before} (not recorded)"
        )
        right_rationale_link = (
            f"[{after}]({_relative_link(right_rationale_path, comparison_dir)})"
            if right_rationale_path.is_file()
            else f"{after} (not recorded)"
        )
        report.extend(
            [
                f"## {job['company']}: {job['title']}",
                "",
                f"Changed extracted-text lines: **{changed_lines}**",
                "",
                f"[Text diff]({_relative_link(text_diff_path, comparison_dir)}) | "
                f"[ResumeSpec diff]({_relative_link(spec_diff_path, comparison_dir)}) | "
                f"[Rationale diff]({_relative_link(rationale_diff_path, comparison_dir)})",
                "",
                f"Selection rationales: {left_rationale_link} | {right_rationale_link}",
                "",
                f"| {before} | {after} |",
                "| --- | --- |",
                f"| ![{before}]({_relative_link(left / 'preview.png', comparison_dir)}) "
                f"| ![{after}]({_relative_link(right / 'preview.png', comparison_dir)}) |",
                "",
            ]
        )

    report_path = comparison_dir / "comparison.md"
    report_path.write_text("\n".join(report), encoding="utf-8")
    _write_json(comparison_dir / "summary.json", summary)
    return report_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="Freeze the five selected remote jobs")
    snapshot.add_argument("--overwrite", action="store_true")

    generate = subparsers.add_parser("generate", help="Generate a named CV run")
    generate.add_argument("label")
    generate.add_argument("--overwrite", action="store_true")
    generate.add_argument(
        "--count",
        type=int,
        choices=range(1, 6),
        default=5,
        metavar="1-5",
        help="number of frozen jobs to generate (default: 5)",
    )
    generate.add_argument(
        "--model",
        choices=EVALUATION_MODELS,
        default=RESUME_LLM_MODEL,
    )
    generate.add_argument(
        "--reasoning-effort",
        choices=REASONING_EFFORTS,
        default=DOCUMENT_LLM_REASONING_EFFORT,
    )

    compare = subparsers.add_parser("compare", help="Compare two generated runs")
    compare.add_argument("before")
    compare.add_argument("after")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "snapshot":
            path = snapshot_remote_jobs(overwrite=args.overwrite)
        elif args.command == "generate":
            path = asyncio.run(
                generate_run(
                    args.label,
                    overwrite=args.overwrite,
                    count=args.count,
                    model=args.model,
                    reasoning_effort=args.reasoning_effort,
                )
            )
        else:
            path = compare_runs(args.before, args.after)
    except Exception as exc:
        print(f"cv-eval: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
