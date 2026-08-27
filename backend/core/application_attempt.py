"""Permanent, human-browsable audit artifacts for one application attempt."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from core.config import get_data_dir, load_settings
except ImportError:
    from backend.core.config import get_data_dir, load_settings


SCHEMA_VERSION = 1
TERMINAL_STATUSES = {"applied", "failed", "retry", "cancelled"}
VIDEO_RETENTION_DEFAULT_DAYS = 14
DOSSIER_RETENTION_DEFAULT_DAYS = 30
RETENTION_MAX_DAYS = 3650
_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
_ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


def _slug(value: Any, fallback: str, limit: int = 48) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = _SAFE_CHARS_RE.sub("_", text).strip("._-")
    return (text or fallback)[:limit]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, stat.S_IRWXU)
    except OSError:
        pass


def _secure_file(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def find_attempt_directory(attempt_id: str, root: Path | None = None) -> Path | None:
    """Find a dossier by its exact manifest attempt ID."""
    if not _ATTEMPT_ID_RE.fullmatch(attempt_id):
        return None
    attempts_root = root or (get_data_dir() / "application_attempts")
    if not attempts_root.is_dir():
        return None
    try:
        date_dirs = sorted(
            (path for path in attempts_root.iterdir() if path.is_dir()),
            reverse=True,
        )
        for date_dir in date_dirs:
            for candidate in date_dir.iterdir():
                if not candidate.is_dir() or not candidate.name.endswith(f"_{attempt_id}"):
                    continue
                manifest_path = candidate / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("attempt_id") == attempt_id:
                    return candidate
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return None


def _tree_size(path: Path) -> int:
    """Return regular-file bytes without following symlinks."""
    if path.is_symlink():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    try:
        children = path.iterdir()
    except OSError:
        return 0
    for child in children:
        total += _tree_size(child)
    return total


def _video_size(directory: Path) -> int:
    return sum(
        _tree_size(directory / relative)
        for relative in ("recording.mp4", ".video")
    )


def _manifest_time(manifest: dict, directory: Path) -> datetime:
    for key in ("finished_at", "started_at"):
        value = manifest.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    try:
        return datetime.fromtimestamp(directory.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _retention_records(
    *,
    video_retention_days: int,
    dossier_retention_days: int,
    root: Path | None = None,
    now: datetime | None = None,
) -> tuple[Path, list[dict]]:
    attempts_root = root or (get_data_dir() / "application_attempts")
    video_retention_days = max(
        1, min(int(video_retention_days), RETENTION_MAX_DAYS)
    )
    dossier_retention_days = max(
        video_retention_days,
        min(int(dossier_retention_days), RETENTION_MAX_DAYS),
    )
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    video_cutoff = current - timedelta(days=video_retention_days)
    dossier_cutoff = current - timedelta(days=dossier_retention_days)
    records: list[dict] = []

    if not attempts_root.is_dir():
        return attempts_root, records

    try:
        date_directories = [
            path
            for path in attempts_root.iterdir()
            if path.is_dir() and not path.is_symlink()
        ]
    except OSError:
        return attempts_root, records

    for date_directory in date_directories:
        try:
            directories = [
                path
                for path in date_directory.iterdir()
                if path.is_dir() and not path.is_symlink()
            ]
        except OSError:
            continue
        for directory in directories:
            manifest: dict = {}
            invalid_manifest = False
            try:
                loaded = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    manifest = loaded
                else:
                    invalid_manifest = True
            except (OSError, json.JSONDecodeError):
                invalid_manifest = True
            timestamp = _manifest_time(manifest, directory)
            status = str(manifest.get("status") or "unknown").lower()
            video_bytes = _video_size(directory)
            action: str | None = None
            protection_reason: str | None = None
            if invalid_manifest:
                protection_reason = "invalid_manifest"
            elif status not in TERMINAL_STATUSES:
                protection_reason = "active_or_incomplete"
            elif timestamp <= dossier_cutoff:
                action = "delete_dossier"
            elif timestamp <= video_cutoff and video_bytes > 0:
                action = "remove_video"
            elif timestamp <= video_cutoff:
                protection_reason = "video_already_removed"
            else:
                protection_reason = "within_retention_window"
            records.append(
                {
                    "directory": directory,
                    "manifest": manifest,
                    "attempt_id": str(manifest.get("attempt_id") or directory.name),
                    "job_key": str(manifest.get("job_key") or ""),
                    "job_title": str(manifest.get("job_title") or ""),
                    "company": str(manifest.get("company") or ""),
                    "status": status,
                    "started_at": manifest.get("started_at"),
                    "timestamp": timestamp,
                    "total_bytes": _tree_size(directory),
                    "video_bytes": video_bytes,
                    "action": action,
                    "protection_reason": protection_reason,
                }
            )

    records.sort(key=lambda record: record["timestamp"], reverse=True)
    return attempts_root, records


def application_attempt_storage_report(
    *,
    video_retention_days: int = VIDEO_RETENTION_DEFAULT_DAYS,
    dossier_retention_days: int = DOSSIER_RETENTION_DEFAULT_DAYS,
    root: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Summarize dossier storage and preview the configured cleanup."""
    attempts_root, records = _retention_records(
        video_retention_days=video_retention_days,
        dossier_retention_days=dossier_retention_days,
        root=root,
        now=now,
    )
    video_candidates = [
        record for record in records if record["action"] == "remove_video"
    ]
    dossier_candidates = [
        record for record in records if record["action"] == "delete_dossier"
    ]
    candidates = video_candidates + dossier_candidates
    return {
        "root": str(attempts_root),
        "video_retention_days": max(
            1, min(int(video_retention_days), RETENTION_MAX_DAYS)
        ),
        "dossier_retention_days": max(
            int(video_retention_days),
            min(int(dossier_retention_days), RETENTION_MAX_DAYS),
        ),
        "total_dossiers": len(records),
        "total_bytes": sum(record["total_bytes"] for record in records),
        "video_bytes": sum(record["video_bytes"] for record in records),
        "video_cleanup_dossiers": len(video_candidates),
        "video_cleanup_bytes": sum(
            record["video_bytes"] for record in video_candidates
        ),
        "dossier_cleanup_dossiers": len(dossier_candidates),
        "dossier_cleanup_bytes": sum(
            record["total_bytes"] for record in dossier_candidates
        ),
        "reclaimable_dossiers": len(candidates),
        "reclaimable_bytes": sum(
            record["video_bytes"] for record in video_candidates
        )
        + sum(record["total_bytes"] for record in dossier_candidates),
        "protected_incomplete_dossiers": sum(
            record["protection_reason"] in {"active_or_incomplete", "invalid_manifest"}
            for record in records
        ),
        "candidates": [
            {
                "attempt_id": record["attempt_id"],
                "job_title": record["job_title"],
                "company": record["company"],
                "status": record["status"],
                "started_at": record["started_at"],
                "action": record["action"],
                "reclaimable_bytes": (
                    record["total_bytes"]
                    if record["action"] == "delete_dossier"
                    else record["video_bytes"]
                ),
            }
            for record in candidates[:100]
        ],
    }


def _remove_video(directory: Path) -> tuple[int, int]:
    removed_bytes = 0
    removed_files = 0
    for relative in ("recording.mp4", ".video"):
        target = directory / relative
        if not target.exists() and not target.is_symlink():
            continue
        removed_bytes += _tree_size(target)
        if target.is_symlink() or target.is_file():
            target.unlink(missing_ok=True)
            removed_files += 1
            continue
        try:
            removed_files += sum(1 for path in target.rglob("*") if path.is_file())
        except OSError:
            pass
        shutil.rmtree(target)
    return removed_bytes, removed_files


def _record_compaction(
    manifest_path: Path,
    manifest: dict,
    *,
    removed_bytes: int,
    removed_files: int,
) -> None:
    artifacts = manifest.get("artifacts")
    if isinstance(artifacts, dict):
        artifacts["recording"] = None
    files = manifest.get("files")
    if isinstance(files, dict):
        manifest["files"] = {
            path: metadata
            for path, metadata in files.items()
            if path != "recording.mp4"
            and not path.startswith(".video/")
        }
    manifest["retention"] = {
        "compacted_at": datetime.now(timezone.utc).isoformat(),
        "removed_bytes": removed_bytes,
        "removed_files": removed_files,
        "preserved": (
            "screenshots, manifest, text evidence, history, submitted materials, "
            "and review notes"
        ),
    }
    tmp = manifest_path.with_name(f".{manifest_path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=_json_default)
        + "\n",
        encoding="utf-8",
    )
    _secure_file(tmp)
    tmp.replace(manifest_path)
    _secure_file(manifest_path)


def cleanup_application_attempts(
    *,
    video_retention_days: int = VIDEO_RETENTION_DEFAULT_DAYS,
    dossier_retention_days: int = DOSSIER_RETENTION_DEFAULT_DAYS,
    root: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Remove old video, then delete completed dossiers at the configured age."""
    attempts_root, records = _retention_records(
        video_retention_days=video_retention_days,
        dossier_retention_days=dossier_retention_days,
        root=root,
        now=now,
    )
    compacted = 0
    deleted = 0
    removed_bytes = 0
    removed_files = 0
    for record in records:
        if record["action"] is None:
            continue
        directory = record["directory"]
        manifest_path = directory / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or manifest.get(
                "attempt_id"
            ) != record["manifest"].get("attempt_id"):
                continue
            if record["action"] == "delete_dossier":
                dossier_bytes = _tree_size(directory)
                dossier_files = sum(
                    1 for path in directory.rglob("*") if path.is_file()
                )
                shutil.rmtree(directory)
                deleted += 1
                try:
                    directory.parent.rmdir()
                except OSError:
                    pass
            else:
                dossier_bytes, dossier_files = _remove_video(directory)
                if dossier_bytes <= 0:
                    continue
                _record_compaction(
                    manifest_path,
                    manifest,
                    removed_bytes=dossier_bytes,
                    removed_files=dossier_files,
                )
                compacted += 1
        except (OSError, json.JSONDecodeError):
            continue
        removed_bytes += dossier_bytes
        removed_files += dossier_files

    report = application_attempt_storage_report(
        video_retention_days=video_retention_days,
        dossier_retention_days=dossier_retention_days,
        root=attempts_root,
        now=now,
    )
    return {
        "success": True,
        "compacted_dossiers": compacted,
        "deleted_dossiers": deleted,
        "removed_bytes": removed_bytes,
        "removed_files": removed_files,
        "storage": report,
    }


class ApplicationAttempt:
    """Owns the on-disk dossier and per-attempt persistent log."""

    def __init__(
        self,
        job: dict,
        profile: dict,
        worker_id: int,
        easy_apply: bool,
        *,
        sensitive_values: list[str] | None = None,
        metrics_store: Any = None,
        root: Path | None = None,
        now: datetime | None = None,
        attempt_id: str | None = None,
    ):
        self.job = dict(job)
        self.profile = dict(profile)
        self.worker_id = worker_id
        self.easy_apply = easy_apply
        self.metrics_store = metrics_store
        self.started_at = now or datetime.now(timezone.utc)
        if self.started_at.tzinfo is None:
            self.started_at = self.started_at.replace(tzinfo=timezone.utc)
        self.attempt_id = attempt_id or uuid.uuid4().hex[:12]
        self.job_key = hashlib.sha256(str(job.get("url", "")).encode()).hexdigest()[:12]
        self.sensitive_values = [
            value for value in (sensitive_values or []) if isinstance(value, str) and value
        ]

        attempts_root = root or (get_data_dir() / "application_attempts")
        self.attempts_root = attempts_root
        date_dir = attempts_root / self.started_at.strftime("%Y-%m-%d")
        folder_name = "_".join(
            [
                self.started_at.strftime("%H%M%SZ"),
                _slug(job.get("company"), "Unknown_company"),
                _slug(job.get("title"), "Unknown_role"),
                self.attempt_id,
            ]
        )
        self.directory = date_dir / folder_name
        self.inputs_dir = self.directory / "inputs"
        self.materials_dir = self.directory / "materials"
        self.screenshots_dir = self.directory / "screenshots"
        self.conversation_dir = self.directory / "conversation"
        self.video_dir = self.directory / ".video"
        for directory in (attempts_root, date_dir):
            _secure_directory(directory)
        self.directory.mkdir(mode=stat.S_IRWXU, exist_ok=False)
        for directory in (
            self.inputs_dir,
            self.materials_dir,
            self.screenshots_dir,
            self.conversation_dir,
            self.video_dir,
        ):
            _secure_directory(directory)

        self.manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "attempt_id": self.attempt_id,
            "job_id": job.get("id"),
            "job_key": self.job_key,
            "status": "preparing",
            "job_url": str(job.get("url", "")),
            "job_title": str(job.get("title", "")),
            "company": str(job.get("company", "")),
            "worker_id": worker_id,
            "easy_apply": easy_apply,
            "started_at": self.started_at.isoformat(),
            "finished_at": None,
            "duration_seconds": None,
            "agent_claimed_success": None,
            "judge_verdict": None,
            "submission_confirmed": False,
            "submission_checkpoint_recorded_at": None,
            "linkedin_outreach": None,
            "judgement": None,
            "error": None,
            "step_count": 0,
            "cost_usd": None,
            "cost_breakdown": None,
            "token_breakdown": None,
            "provider": None,
            "model": None,
            "confirmation_evidence": None,
            "errors": [],
            "recording_error": None,
            "artifacts": {},
            "files": {},
        }
        self._write_json("job.json", self.job)
        self._write_profile()
        self._write_review_template()
        self._write_manifest()
        self._write_summary()
        self.log("Attempt dossier created")

    def relative(self, path: Path) -> str:
        return path.relative_to(self.directory).as_posix()

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self.sensitive_values:
                value = value.replace(secret, "<redacted:password>")
            return value
        if isinstance(value, dict):
            return {str(key): self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return [self.redact(item) for item in value]
        return value

    def _atomic_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(self.redact(content), encoding="utf-8")
        _secure_file(tmp)
        tmp.replace(path)
        _secure_file(path)

    def _write_json(self, relative_path: str, data: Any) -> Path:
        path = self.directory / relative_path
        content = json.dumps(
            self.redact(data),
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        )
        self._atomic_text(path, content + "\n")
        return path

    def _write_profile(self) -> None:
        markdown = str(self.profile.get("markdown") or "")
        fixed = {key: value for key, value in self.profile.items() if key != "markdown"}
        self._atomic_text(self.inputs_dir / "profile.md", markdown)
        self._write_json("inputs/candidate_profile.json", fixed)

    def _write_review_template(self) -> None:
        path = self.directory / "review.md"
        if path.exists():
            return
        self._atomic_text(
            path,
            "# Manual Review\n\n"
            "- [ ] Outcome is correct\n"
            "- [ ] Answers are correct\n"
            "- [ ] Navigation was sensible\n"
            "- [ ] Correct resume and documents were used\n"
            "- [ ] Submission was visibly confirmed\n\n"
            "## Problems noticed\n\n"
            "\n\n## Changes to make\n\n",
        )

    def _write_manifest(self) -> None:
        self._write_json("manifest.json", self.manifest)

    def save_inputs(
        self,
        *,
        task: str,
        context: str,
        qa: dict,
        memories: list[Any],
        runtime_settings: dict,
    ) -> None:
        self._atomic_text(self.inputs_dir / "task.txt", task)
        self._atomic_text(self.inputs_dir / "agent_context.txt", context)
        self._write_json("inputs/qa.json", qa)
        self._write_json("inputs/memories.json", memories)
        self._write_json("inputs/runtime_settings.json", runtime_settings)
        self.manifest["artifacts"]["inputs"] = "inputs"
        self.manifest["provider"] = runtime_settings.get("provider")
        self.manifest["model"] = runtime_settings.get("model")
        self._write_manifest()

    def set_running(self) -> None:
        self.manifest["status"] = "running"
        self._write_manifest()
        self._write_summary()
        self.log("Browser agent started")

    def log(self, message: str, level: str = "INFO") -> None:
        clean = str(self.redact(message)).replace("\r", " ").strip()
        timestamp = datetime.now(timezone.utc).isoformat()
        log_path = self.directory / "application.log"
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} [{level}] {clean}\n")
        _secure_file(log_path)
        self.manifest["artifacts"]["log"] = "application.log"
        if self.metrics_store is not None:
            try:
                self.metrics_store.log(
                    self.attempt_id,
                    clean,
                    level=level,
                    job_url=str(self.job.get("url", "")),
                )
            except Exception:
                pass

    def log_step(self, browser_state: Any, agent_output: Any, step_num: int) -> None:
        url = getattr(browser_state, "url", "") if browser_state else ""
        goal = getattr(agent_output, "next_goal", "") if agent_output else ""
        actions = getattr(agent_output, "action", []) if agent_output else []
        action_names = []
        for action in actions or []:
            try:
                dumped = action.model_dump(exclude_none=True)
                action_names.extend(dumped.keys())
            except Exception:
                action_names.append(type(action).__name__)
        detail = f"Step {step_num}: {url}"
        if goal:
            detail += f" | next: {goal}"
        if action_names:
            detail += f" | actions: {', '.join(action_names)}"
        self.log(detail)

    def copy_material(self, source: str | Path, preferred_name: str | None = None) -> dict[str, Any] | None:
        source_path = Path(source)
        if not source_path.is_file():
            return None
        target_name = _slug(preferred_name or source_path.name, "document", limit=100)
        suffix = source_path.suffix
        if suffix and not target_name.lower().endswith(suffix.lower()):
            target_name += suffix
        target = self.materials_dir / target_name
        counter = 2
        while target.exists() and _sha256(target) != _sha256(source_path):
            target = self.materials_dir / f"{Path(target_name).stem}_{counter}{Path(target_name).suffix}"
            counter += 1
        if not target.exists():
            shutil.copy2(source_path, target)
        _secure_file(target)
        artifact = {
            "path": self.relative(target),
            "sha256": _sha256(target),
            "size_bytes": target.stat().st_size,
            "source_name": source_path.name,
        }
        return artifact

    def save_materials(
        self,
        resume_path: str,
        cover_letter_path: str,
        cover_letter_pdf_path: str | None = None,
    ) -> None:
        resume = self.copy_material(resume_path, "tailored_resume.pdf")
        cover_letter_text = self.copy_material(cover_letter_path, "cover_letter.txt")
        cover_letter = self.copy_material(
            cover_letter_pdf_path or cover_letter_path,
            "cover_letter.pdf" if cover_letter_pdf_path else "cover_letter.txt",
        )
        if resume:
            self.manifest["artifacts"]["resume"] = resume
        if cover_letter:
            self.manifest["artifacts"]["cover_letter"] = cover_letter
        if cover_letter_text and cover_letter_pdf_path:
            self.manifest["artifacts"]["cover_letter_text"] = cover_letter_text
        self._write_manifest()
        self.log("Application materials copied into dossier")

    def save_generation_audit(
        self,
        resume_audit: dict | None,
        cover_letter_audit: dict | None,
    ) -> None:
        """Persist the reasoning and evidence trail behind generated documents."""
        if not resume_audit and not cover_letter_audit:
            return
        path = self._write_json(
            "materials/generation-audit.json",
            {
                "resume": resume_audit,
                "cover_letter": cover_letter_audit,
            },
        )
        self.manifest["artifacts"]["generation_audit"] = self.relative(path)
        self._write_manifest()
        self.log("Document generation audit saved")

    def save_outreach_message(self, message: str) -> None:
        """Persist the exact pre-generated LinkedIn message given to the agent."""
        if not message.strip():
            return
        path = self.inputs_dir / "linkedin_outreach.txt"
        self._atomic_text(path, message.strip() + "\n")
        self.manifest["artifacts"]["linkedin_outreach_message"] = self.relative(path)
        self._write_manifest()
        self.log("LinkedIn outreach message saved")

    def _copy_step_screenshots(self, result: Any, history_dump: dict[str, Any]) -> list[str]:
        copied: list[str] = []
        dumped_items = history_dump.get("history", [])
        for index, item in enumerate(getattr(result, "history", []), start=1):
            dumped_state = (
                dumped_items[index - 1].setdefault("state", {})
                if index - 1 < len(dumped_items)
                else None
            )
            source_value = getattr(getattr(item, "state", None), "screenshot_path", None)
            if not source_value:
                if dumped_state is not None:
                    dumped_state["screenshot_path"] = None
                continue
            source = Path(source_value)
            if not source.is_file():
                if dumped_state is not None:
                    dumped_state["screenshot_path"] = None
                self.log(f"Step {index} screenshot was missing: {source}", level="WARNING")
                continue
            target = self.screenshots_dir / f"step_{index:03d}.png"
            shutil.copy2(source, target)
            _secure_file(target)
            relative = self.relative(target)
            copied.append(relative)
            if dumped_state is not None:
                dumped_state["screenshot_path"] = relative
        return copied

    @staticmethod
    def _uploaded_paths(history_dump: dict[str, Any]) -> list[str]:
        paths: list[str] = []
        for step in history_dump.get("history", []):
            output = step.get("model_output") or {}
            for action in output.get("action") or []:
                if not isinstance(action, dict):
                    continue
                action_name = next(
                    (name for name in ("upload_file", "upload_file_by_label") if name in action),
                    None,
                )
                if action_name is None:
                    continue
                params = action.get(action_name) or {}
                if not isinstance(params, dict):
                    continue
                for key in ("path", "file_path"):
                    value = params.get(key)
                    if isinstance(value, str) and value:
                        paths.append(value)
        return list(dict.fromkeys(paths))

    def save_final_screenshot(self, screenshot: str | bytes | None) -> bool:
        if not screenshot:
            return False
        try:
            payload = base64.b64decode(screenshot) if isinstance(screenshot, str) else screenshot
            path = self.screenshots_dir / "final.png"
            path.write_bytes(payload)
            _secure_file(path)
            self.manifest["artifacts"]["final_screenshot"] = self.relative(path)
            self._write_manifest()
            return True
        except Exception as exc:
            self.log(f"Final screenshot could not be saved: {exc}", level="WARNING")
            return False

    def save_submission_screenshot(self, screenshot: str | bytes | None) -> bool:
        """Save the page evidence captured before optional post-application work."""
        if not screenshot:
            return False
        try:
            payload = base64.b64decode(screenshot) if isinstance(screenshot, str) else screenshot
            path = self.screenshots_dir / "submission_confirmation.png"
            path.write_bytes(payload)
            _secure_file(path)
            self.manifest["artifacts"]["submission_confirmation"] = self.relative(path)
            self.manifest["submission_checkpoint_recorded_at"] = datetime.now(timezone.utc).isoformat()
            self._write_manifest()
            self.log("Application submission checkpoint saved")
            return True
        except Exception as exc:
            self.log(f"Submission checkpoint could not be saved: {exc}", level="WARNING")
            return False

    def save_history(self, result: Any) -> None:
        try:
            sensitive_data = {
                f"password_{index}": value
                for index, value in enumerate(self.sensitive_values, start=1)
            }
            history_dump = result.model_dump(sensitive_data=sensitive_data)
        except (TypeError, ValueError):
            history_dump = result.model_dump()
        history_dump = self.redact(history_dump)
        screenshots = self._copy_step_screenshots(result, history_dump)
        if screenshots and not self.manifest["artifacts"].get("final_screenshot"):
            source = self.directory / screenshots[-1]
            target = self.screenshots_dir / "final.png"
            shutil.copy2(source, target)
            _secure_file(target)
            self.manifest["artifacts"]["final_screenshot"] = self.relative(target)

        usage = getattr(result, "usage", None)
        if usage is not None:
            try:
                history_dump["usage"] = usage.model_dump(mode="json")
            except Exception:
                history_dump["usage"] = str(usage)

        self._write_json("history.json", history_dump)
        self.manifest["artifacts"]["history"] = "history.json"
        self.manifest["artifacts"]["screenshots"] = screenshots
        self.manifest["step_count"] = len(getattr(result, "history", []))

        uploaded = []
        existing_hashes = {
            artifact.get("sha256")
            for artifact in self.manifest["artifacts"].values()
            if isinstance(artifact, dict)
        }
        for source in self._uploaded_paths(history_dump):
            artifact = self.copy_material(source, f"uploaded_{Path(source).name}")
            if artifact and artifact["sha256"] not in existing_hashes:
                uploaded.append(artifact)
                existing_hashes.add(artifact["sha256"])
        if uploaded:
            self.manifest["artifacts"]["uploaded_documents"] = uploaded

        self._write_timeline(history_dump)
        self.manifest["artifacts"]["timeline"] = "timeline.md"
        self._write_manifest()
        self._redact_conversations()
        self.log(f"Saved structured history with {self.manifest['step_count']} steps")

    def _write_timeline(self, history_dump: dict[str, Any]) -> None:
        items = history_dump.get("history", [])
        starts = [
            item.get("metadata", {}).get("step_start_time")
            for item in items
            if item.get("metadata")
        ]
        first_start = next((value for value in starts if isinstance(value, (int, float))), None)
        lines = ["# Application Timeline", ""]
        for index, item in enumerate(items, start=1):
            state = item.get("state") or {}
            output = item.get("model_output") or {}
            metadata = item.get("metadata") or {}
            start = metadata.get("step_start_time")
            elapsed = (
                f"{max(0.0, start - first_start):.1f}s"
                if isinstance(start, (int, float)) and isinstance(first_start, (int, float))
                else "—"
            )
            lines.extend(
                [
                    f"## Step {index} · {elapsed}",
                    "",
                    f"- URL: {state.get('url') or '—'}",
                    f"- Previous result: {output.get('evaluation_previous_goal') or '—'}",
                    f"- Next goal: {output.get('next_goal') or '—'}",
                ]
            )
            screenshot = state.get("screenshot_path")
            if screenshot:
                lines.extend(["", f"![Step {index} screenshot]({screenshot})"])
            actions = output.get("action") or []
            if actions:
                lines.extend(["", "Actions:", "", "```json", json.dumps(actions, ensure_ascii=False, indent=2), "```"])
            results = item.get("result") or []
            if results:
                lines.extend(["", "Results:", ""])
                for result in results:
                    if result.get("error"):
                        lines.append(f"- Error: {result['error']}")
                    elif result.get("extracted_content"):
                        lines.append(f"- {result['extracted_content']}")
                    elif result.get("long_term_memory"):
                        lines.append(f"- {result['long_term_memory']}")
            lines.append("")
        self._atomic_text(self.directory / "timeline.md", "\n".join(lines).rstrip() + "\n")

    def _redact_conversations(self) -> None:
        for path in self.conversation_dir.glob("*.txt"):
            try:
                content = path.read_text(encoding="utf-8")
                redacted = self.redact(content)
                if redacted != content:
                    self._atomic_text(path, redacted)
                else:
                    _secure_file(path)
            except OSError:
                continue
        if any(self.conversation_dir.iterdir()):
            self.manifest["artifacts"]["conversation"] = "conversation"

    def finalize_recording(self) -> bool:
        candidates = sorted(self.video_dir.glob("*.mp4"), key=lambda path: path.stat().st_mtime)
        candidates = [path for path in candidates if path.is_file() and path.stat().st_size > 0]
        if not candidates:
            self.manifest["artifacts"]["recording"] = None
            self.manifest["recording_error"] = "No finalized MP4 was produced"
            self._write_manifest()
            self.log("No finalized MP4 was produced", level="WARNING")
            return False
        source = candidates[-1]
        target = self.directory / "recording.mp4"
        source.replace(target)
        _secure_file(target)
        try:
            self.video_dir.rmdir()
        except OSError:
            pass
        artifact = {
            "path": "recording.mp4",
            "sha256": _sha256(target),
            "size_bytes": target.stat().st_size,
        }
        self.manifest["artifacts"]["recording"] = artifact
        try:
            import imageio.v2 as imageio

            reader = imageio.get_reader(target)
            try:
                reader.get_data(0)
            finally:
                reader.close()
            self.manifest["recording_error"] = None
        except Exception as exc:
            self.manifest["recording_error"] = f"MP4 could not be decoded: {exc}"
            self._write_manifest()
            self.log(self.manifest["recording_error"], level="WARNING")
            return False
        self._write_manifest()
        self.log("Video recording finalized")
        return True

    def finish(
        self,
        status: str,
        *,
        error: str | None = None,
        result: Any = None,
        cost_breakdown: dict[str, float] | None = None,
        token_breakdown: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"Invalid terminal attempt status: {status}")
        finished_at = datetime.now(timezone.utc)
        checkpointed = bool(self.manifest.get("submission_checkpoint_recorded_at"))
        claimed = (
            True
            if checkpointed
            else result.is_successful()
            if result is not None
            else None
        )
        judge_verdict = result.is_validated() if result is not None and hasattr(result, "is_validated") else None
        judgement = result.judgement() if result is not None and hasattr(result, "judgement") else None
        confirmation_evidence = (
            result.final_result()
            if result is not None and hasattr(result, "final_result")
            else None
        )
        usage = getattr(result, "usage", None) if result is not None else None
        cost = getattr(usage, "total_cost", None) if usage is not None else None
        if cost_breakdown is not None:
            cost = sum(float(value or 0.0) for value in cost_breakdown.values())
        errors = []
        if result is not None and hasattr(result, "errors"):
            errors.extend(str(item) for item in result.errors() if item)
        if error and error not in errors:
            errors.append(error)

        self.manifest.update(
            {
                "status": status,
                "finished_at": finished_at.isoformat(),
                "duration_seconds": round((finished_at - self.started_at).total_seconds(), 3),
                "agent_claimed_success": claimed,
                "judge_verdict": judge_verdict,
                "submission_confirmed": claimed is True and judge_verdict is True,
                "judgement": judgement,
                "confirmation_evidence": confirmation_evidence,
                "error": self.redact(error) if error else None,
                "errors": self.redact(errors),
                "cost_usd": cost,
                "cost_breakdown": cost_breakdown,
                "token_breakdown": token_breakdown,
                "step_count": len(getattr(result, "history", [])) if result is not None else self.manifest["step_count"],
            }
        )
        self._redact_conversations()
        self.log(
            "Outcome signals: "
            f"agent_claimed_success={claimed}, "
            f"judge_verdict={judge_verdict}, "
            f"submission_confirmed={claimed is True and judge_verdict is True}"
        )
        self.log(f"Attempt finished with status={status}", level="ERROR" if status == "failed" else "INFO")
        self._write_summary()
        self._write_manifest()
        try:
            settings = load_settings()
            if settings.get("dossier_retention_enabled", True):
                cleanup_application_attempts(
                    video_retention_days=int(
                        settings.get(
                            "dossier_retention_days",
                            VIDEO_RETENTION_DEFAULT_DAYS,
                        )
                    ),
                    dossier_retention_days=int(
                        settings.get(
                            "dossier_delete_after_days",
                            DOSSIER_RETENTION_DEFAULT_DAYS,
                        )
                    ),
                    root=self.attempts_root,
                )
        except Exception:
            # Retention is best-effort and must never change the attempt outcome.
            self.log("Automatic dossier retention could not run", level="WARNING")
        self._refresh_file_index()
        self._write_manifest()

    def _refresh_file_index(self) -> None:
        """Index final dossier files without creating a self-referential manifest hash."""
        files: dict[str, dict[str, Any]] = {}
        for path in sorted(self.directory.rglob("*")):
            if not path.is_file() or path.name == "manifest.json" or self.video_dir in path.parents:
                continue
            files[self.relative(path)] = {
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
            }
        self.manifest["files"] = files

    def _write_summary(self) -> None:
        manifest = self.manifest
        judgement = manifest.get("judgement") or {}
        artifacts = manifest.get("artifacts") or {}
        highlights: list[str] = []
        issues = list(manifest.get("errors") or [])
        history_path = self.directory / "history.json"
        if history_path.is_file():
            try:
                history = json.loads(history_path.read_text(encoding="utf-8"))
                for step in history.get("history", []):
                    output = step.get("model_output") or {}
                    if output.get("evaluation_previous_goal"):
                        highlights.append(str(output["evaluation_previous_goal"]))
                    for result in step.get("result") or []:
                        if result.get("error"):
                            issues.append(str(result["error"]))
                        elif result.get("long_term_memory"):
                            highlights.append(str(result["long_term_memory"]))
                        elif result.get("extracted_content"):
                            highlights.append(str(result["extracted_content"]))
            except (OSError, json.JSONDecodeError):
                issues.append("Structured application history could not be read.")
        if manifest.get("recording_error"):
            issues.append(str(manifest["recording_error"]))
        unique_highlights = dict.fromkeys(" ".join(item.split()) for item in highlights if item.strip())
        highlights = [
            re.split(r"(?<=[.!?])\s+", item, maxsplit=1)[0]
            for item in list(unique_highlights)[:2]
        ]
        issues = list(dict.fromkeys(item.strip() for item in issues if item.strip()))
        lines = [
            f"# {manifest.get('job_title') or 'Application'} at {manifest.get('company') or 'Unknown company'}",
            "",
            "> Sensitive local audit dossier. The video and screenshots may show Gmail, OTPs, personal data, and form answers.",
            "",
            f"- Attempt ID: `{manifest['attempt_id']}`",
            f"- Job key: `{manifest['job_key']}`",
            f"- Status: **{manifest['status']}**",
            f"- Job URL: {manifest.get('job_url') or '—'}",
            f"- Started: {manifest.get('started_at') or '—'}",
            f"- Finished: {manifest.get('finished_at') or '—'}",
            f"- Duration: {manifest.get('duration_seconds') if manifest.get('duration_seconds') is not None else '—'} seconds",
            f"- Steps: {manifest.get('step_count', 0)}",
            f"- Agent claimed success: {manifest.get('agent_claimed_success')}",
            f"- Judge verdict: {manifest.get('judge_verdict')}",
            f"- Submission confirmed: **{manifest.get('submission_confirmed', False)}**",
            f"- LinkedIn outreach: **{str((manifest.get('linkedin_outreach') or {}).get('status') or 'unknown')}**",
            f"- Cost: ${manifest.get('cost_usd'):.4f}" if isinstance(manifest.get("cost_usd"), (int, float)) else "- Cost: —",
            "",
            "## Application overview",
            "",
            (
                f"The application finished with status **{manifest['status']}** after "
                f"{manifest.get('step_count', 0)} step(s). Submission confirmation: "
                f"**{'confirmed' if manifest.get('submission_confirmed') else 'not confirmed'}**."
            ),
            "",
        ]
        outreach = manifest.get("linkedin_outreach") or {}
        if outreach:
            lines.extend(
                [
                    "### LinkedIn outreach",
                    "",
                    f"- Status: **{outreach.get('status') or 'unknown'}**",
                    f"- Recipient: {outreach.get('recipient') or '—'}",
                    f"- CV attached: {outreach.get('cv_attached') is True}",
                    f"- Evidence: {outreach.get('evidence') or outreach.get('reason') or '—'}",
                    "",
                ]
            )
        if manifest.get("cost_breakdown"):
            labels = {
                "classification": "Classification",
                "cover_letter": "Cover letter",
                "outreach": "Outreach message",
                "cv": "CV",
                "application_agent": "Application agent",
                "judge": "Judge",
                "memory_extract": "Memory extract",
            }
            lines.extend(["### Cost breakdown", ""])
            lines.extend(
                f"- {labels.get(category, category)}: ${float(value):.4f}"
                for category, value in manifest["cost_breakdown"].items()
            )
            lines.append("")
        if manifest.get("token_breakdown"):
            lines.extend(["### Token usage", ""])
            for category, usage in manifest["token_breakdown"].items():
                if not usage.get("requests"):
                    continue
                lines.append(
                    f"- {category}: {int(usage.get('prompt_tokens') or 0):,} prompt, "
                    f"{int(usage.get('cached_prompt_tokens') or 0):,} cached, "
                    f"{int(usage.get('cache_write_tokens') or 0):,} cache-write, "
                    f"{int(usage.get('visible_completion_tokens') or 0):,} visible output, "
                    + (
                        f"{int(usage.get('reasoning_tokens') or 0):,} reasoning tokens"
                        if usage.get("reasoning_tokens") is not None
                        else f"{int(usage.get('completion_tokens') or 0):,} aggregate completion tokens"
                    )
                )
            lines.append("")
        lines.extend(["### Highlights", ""])
        if highlights:
            lines.extend(f"- {item}" for item in highlights)
        else:
            lines.append("- No application highlights were recorded.")
        lines.extend(["", "### Issues encountered", ""])
        if issues:
            lines.extend(f"- {item}" for item in issues)
        else:
            lines.append("- No issues were recorded.")
        if manifest.get("error"):
            lines.extend(["", "## Error", "", str(manifest["error"])])
        if manifest.get("confirmation_evidence"):
            lines.extend(
                [
                    "",
                    "## Confirmation evidence",
                    "",
                    str(manifest["confirmation_evidence"]),
                ]
            )
        if judgement:
            lines.extend(
                [
                    "",
                    "## Judge",
                    "",
                    str(judgement.get("reasoning") or judgement.get("failure_reason") or judgement),
                ]
            )
        lines.extend(["", "## Artifacts", ""])
        for label, value in artifacts.items():
            if not value:
                continue
            if isinstance(value, str):
                lines.append(f"- {label.replace('_', ' ').title()}: [{value}]({value})")
            elif isinstance(value, dict) and value.get("path"):
                lines.append(f"- {label.replace('_', ' ').title()}: [{value['path']}]({value['path']})")
            elif isinstance(value, list):
                lines.append(f"- {label.replace('_', ' ').title()}: {len(value)} item(s)")
        lines.extend(["", "Open `review.md` to record problems and changes to make.", ""])
        self._atomic_text(self.directory / "summary.md", "\n".join(lines))
