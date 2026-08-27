"""Human-readable filenames for generated application materials."""

import re


def _safe_position(position: str) -> str:
    cleaned = re.sub(r"[^\w]+", "_", position.strip(), flags=re.UNICODE)
    return re.sub(r"_+", "_", cleaned).strip("_") or "Position"


def tailored_cv_filename(position: str) -> str:
    return f"Candidate_{_safe_position(position)}.pdf"


def cover_letter_filename(position: str) -> str:
    return f"Candidate_{_safe_position(position)}_Cover_Letter.txt"


def cover_letter_pdf_filename(position: str) -> str:
    return f"Candidate_{_safe_position(position)}_Cover_Letter.pdf"
