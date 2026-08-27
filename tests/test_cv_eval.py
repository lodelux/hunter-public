import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cv-eval.py"
SPEC = importlib.util.spec_from_file_location("cv_eval", SCRIPT)
cv_eval = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cv_eval)


def _jobs():
    return [
        {
            "id": f"job-{index}",
            "url": f"https://example.com/{index}",
            "title": f"Role {index}",
            "company": f"Company {index}",
            "location": "Berlin",
            "description": "Relevant engineering responsibilities. " * 10,
        }
        for index in range(5)
    ]


def test_safe_name_rejects_path_traversal():
    assert cv_eval._safe_name("prompt-v2", "run label") == "prompt-v2"
    with pytest.raises(ValueError, match="Invalid run label"):
        cv_eval._safe_name("../main", "run label")


def test_load_jobs_requires_five_complete_unique_jobs(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps({"jobs": _jobs()}), encoding="utf-8")
    assert [job["id"] for job in cv_eval.load_jobs(path)] == [
        f"job-{index}" for index in range(5)
    ]

    duplicate = _jobs()
    duplicate[-1]["id"] = duplicate[0]["id"]
    path.write_text(json.dumps({"jobs": duplicate}), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate job id"):
        cv_eval.load_jobs(path)


def test_render_preview_creates_poppler_png(tmp_path):
    pdf_path = tmp_path / "resume.pdf"
    preview_path = tmp_path / "preview.png"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Rendered CV")
    document.save(pdf_path)
    document.close()

    cv_eval._render_preview(pdf_path, preview_path)

    assert preview_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_render_preview_falls_back_to_pymupdf(monkeypatch, tmp_path):
    pdf_path = tmp_path / "resume.pdf"
    preview_path = tmp_path / "preview.png"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Rendered CV")
    document.save(pdf_path)
    document.close()
    monkeypatch.setattr(cv_eval.shutil, "which", lambda _command: None)

    cv_eval._render_preview(pdf_path, preview_path)

    assert preview_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_selection_rationale_explains_displayed_facts():
    audit = {
        "positioning": "Lead with agentic AI delivery.",
        "priorities": [{
            "decision": "Show the explicit AI-agent match.",
            "requirements": [{"id": "J001", "text": "build autonomous AI agents"}],
            "evidence": [{"id": "P001", "text": "Agentic AI Developer"}],
        }],
        "selections": [{
            "section": "Skills",
            "label": "AI Engineering",
            "text": "Python, structured outputs",
            "relevance": 95,
            "requirements": [{"id": "J002", "text": "Python and structured outputs"}],
            "evidence": [{"id": "P002", "text": "Python, structured outputs"}],
        }],
    }

    rationale = cv_eval._selection_rationale_markdown(
        audit, {"title": "AI Engineer", "company": "Example AI"}
    )

    assert "Agentic AI Developer" in rationale
    assert "Show the explicit AI-agent match." in rationale
    assert "build autonomous AI agents" in rationale
    assert "Python, structured outputs" in rationale


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 5])
async def test_generate_run_starts_selected_jobs_concurrently(
    monkeypatch, tmp_path, count
):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(json.dumps({"jobs": _jobs()}), encoding="utf-8")
    monkeypatch.setattr(cv_eval, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(cv_eval, "load_profile_markdown", lambda: "Complete profile")
    monkeypatch.setattr(
        cv_eval,
        "load_llm_settings",
        lambda: {"openai": {"api_key": "test-key"}},
    )
    monkeypatch.setattr(cv_eval, "resume_llm_settings", lambda settings: settings)
    monkeypatch.setattr(cv_eval, "create_llm", lambda _settings: object())
    monkeypatch.setattr(cv_eval, "_git_value", lambda *_args: "clean")

    started = 0
    all_started = asyncio.Event()

    async def generate_spec(**_kwargs):
        nonlocal started
        started += 1
        if started == count:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=1)
        return object()

    rendered = SimpleNamespace(
        experience=[], projects=[], achievements=[], education=[], skills=[]
    )

    def render_resume(_spec, pdf_path):
        pdf_path.write_bytes(b"%PDF-1.4")
        return rendered, {"page_count": 1, "characters": 500, "links": 0}

    monkeypatch.setattr(cv_eval, "generate_resume_spec", generate_spec)
    monkeypatch.setattr(cv_eval, "render_resume", render_resume)
    monkeypatch.setattr(
        cv_eval,
        "resume_generation_audit",
        lambda *_args, **_kwargs: {"positioning": "Test", "priorities": [], "selections": []},
    )
    monkeypatch.setattr(
        cv_eval,
        "_selection_rationale_markdown",
        lambda _spec, _job: "# Selection rationale\n",
    )
    monkeypatch.setattr(
        cv_eval,
        "_render_preview",
        lambda _pdf, preview: preview.write_bytes(b"png"),
    )
    monkeypatch.setattr(cv_eval, "_extract_text", lambda _pdf: "Generated resume\n")

    run_dir = await cv_eval.generate_run(
        "parallel",
        count=count,
        jobs_path=jobs_path,
        model="gpt-5.6-terra",
        reasoning_effort="low",
    )

    assert started == count
    metadata = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert metadata["job_count"] == count
    assert metadata["model"] == "gpt-5.6-terra"
    assert metadata["reasoning_effort"] == "low"
    assert "source_resume_sha256" not in metadata
    assert all(job["status"] == "done" for job in metadata["jobs"])
    assert (run_dir / "job-0" / "selection-rationale.md").is_file()


@pytest.mark.asyncio
async def test_generate_run_rejects_invalid_count():
    with pytest.raises(ValueError, match="count must be between 1 and 5"):
        await cv_eval.generate_run("invalid", count=0)


def test_compare_runs_writes_preview_report_and_diffs(monkeypatch, tmp_path):
    output_root = tmp_path / "output"
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(json.dumps({"jobs": _jobs()}), encoding="utf-8")
    monkeypatch.setattr(cv_eval, "OUTPUT_ROOT", output_root)
    monkeypatch.setattr(cv_eval, "JOBS_PATH", jobs_path)

    for label, suffix, jobs in (
        ("baseline", "old", _jobs()),
        ("candidate", "new", _jobs()[:1]),
    ):
        for job in jobs:
            job_dir = output_root / "runs" / label / job["id"]
            job_dir.mkdir(parents=True)
            (job_dir / "extracted.txt").write_text(
                f"{job['title']} {suffix}\n", encoding="utf-8"
            )
            (job_dir / "resume-spec.json").write_text(
                json.dumps({"summary": suffix}), encoding="utf-8"
            )
            if label == "candidate":
                (job_dir / "selection-rationale.md").write_text(
                    f"Why this fact: {suffix}\n", encoding="utf-8"
                )
            (job_dir / "preview.png").write_bytes(b"png")

    report_path = cv_eval.compare_runs("baseline", "candidate")

    report = report_path.read_text(encoding="utf-8")
    assert "CV comparison: baseline vs candidate" in report
    assert "Company 0: Role 0" in report
    assert "Company 1: Role 1" not in report
    assert "../../runs/baseline/job-0/preview.png" in report
    assert "baseline/extracted.txt" in (
        report_path.parent / "job-0.text.diff"
    ).read_text(encoding="utf-8")
    assert "Selection rationale unavailable for this legacy run" in (
        report_path.parent / "job-0.rationale.diff"
    ).read_text(encoding="utf-8")
    assert "baseline (not recorded)" in report
