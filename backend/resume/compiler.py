"""Compile a job-specific, ATS-readable CV from structured candidate evidence."""

from __future__ import annotations

import copy
import re
from datetime import date
from pathlib import Path
from typing import Annotated

import fitz
import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

_DASH_TRANSLATION = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
)
_LEADING_BULLET_RE = re.compile(r"^\s*(?:[-*\u2022\u25e6\u25aa\u25cf]\s*)+")


def _plain_text(value: str) -> str:
    """Normalize model-written text before it reaches Markdown/Typst."""
    value = value.translate(_DASH_TRANSLATION).replace("\r", " ").replace("\n", " ")
    value = re.sub(r"(?<!\w)([CF])#(?!\w)", r"\1 Sharp", value)
    value = re.sub(r"#(?=\d)", "No. ", value)
    value = value.replace("#", "")
    value = _LEADING_BULLET_RE.sub("", value)
    return re.sub(r"\s+", " ", value).strip()


def _abbreviate_degree(value: str) -> str:
    """Keep common science degree labels compact and consistent."""
    value = re.sub(r"\bMaster of Science\b", "MSc", value, flags=re.IGNORECASE)
    return re.sub(r"\bBachelor of Science\b", "BSc", value, flags=re.IGNORECASE)


def _city_level_location(value: str) -> str:
    """Remove street and postal-address components from a CV location."""
    street_pattern = re.compile(
        r"\b(?:street|st\.?|str\.?|strasse|straße|road|rd\.?|avenue|ave\.?|boulevard|blvd\.?)\b",
        re.IGNORECASE,
    )
    parts = [
        _plain_text(part)
        for part in value.split(",")
        if not re.search(r"\d", part) and not street_pattern.search(part)
    ]
    unique_parts = []
    for part in parts:
        if part and part.casefold() not in {item.casefold() for item in unique_parts}:
            unique_parts.append(part)
    return ", ".join(unique_parts)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _reference_ids(value) -> list[str]:
    """Normalize occasional model-packed citation strings into individual IDs."""
    values = value if isinstance(value, list) else [value]
    found = [
        reference
        for item in values
        for reference in re.findall(r"[PJ]\d{3}", str(item).upper())
    ]
    return list(dict.fromkeys(found))


ReferenceIds = Annotated[list[str], BeforeValidator(_reference_ids)]


class StrategyPriority(_StrictModel):
    requirement_ids: ReferenceIds = Field(min_length=1, max_length=12)
    evidence_ids: ReferenceIds = Field(min_length=1, max_length=12)
    decision: str = Field(min_length=1, max_length=240)

    @field_validator("decision")
    @classmethod
    def normalize_decision(cls, value: str) -> str:
        return _plain_text(value)


class ResumeStrategy(_StrictModel):
    positioning: str = Field(min_length=1, max_length=320)
    priorities: list[StrategyPriority] = Field(min_length=1, max_length=6)

    @field_validator("positioning")
    @classmethod
    def normalize_positioning(cls, value: str) -> str:
        return _plain_text(value)


class EvidenceClaim(_StrictModel):
    text: str = Field(min_length=1)
    evidence_ids: ReferenceIds = Field(min_length=1, max_length=12)
    requirement_ids: ReferenceIds = Field(min_length=1, max_length=12)
    relevance: int = Field(default=50, ge=0, le=100)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return _plain_text(value)


class Candidate(_StrictModel):
    name: str = Field(min_length=1)
    headline: str = Field(min_length=1)
    headline_requirement_ids: ReferenceIds = Field(min_length=1, max_length=12)
    location: str = ""
    email: str = ""
    phone: str = ""
    website: str = ""

    @field_validator(
        "name", "headline", "email", "phone", "website"
    )
    @classmethod
    def normalize_fields(cls, value: str) -> str:
        return _plain_text(value)

    @field_validator("location")
    @classmethod
    def normalize_location(cls, value: str) -> str:
        return _city_level_location(value)


class Experience(_StrictModel):
    company: str = Field(min_length=1)
    position: str = Field(min_length=1)
    start_date: str = Field(min_length=4)
    end_date: str = "present"
    location: str = ""
    relevance: int = Field(default=50, ge=0, le=100)
    evidence_ids: ReferenceIds = Field(min_length=1, max_length=12)
    requirement_ids: ReferenceIds = Field(min_length=1, max_length=12)
    highlights: list[EvidenceClaim] = Field(min_length=1, max_length=4)

    @field_validator(
        "company",
        "position",
        "start_date",
        "end_date",
        "location",
    )
    @classmethod
    def normalize_fields(cls, value: str) -> str:
        return _plain_text(value)


class Project(_StrictModel):
    name: str = Field(min_length=1)
    summary: str = ""
    date: str = Field(min_length=4)
    location: str = ""
    relevance: int = Field(default=50, ge=0, le=100)
    evidence_ids: ReferenceIds = Field(min_length=1, max_length=12)
    requirement_ids: ReferenceIds = Field(min_length=1, max_length=12)
    highlights: list[EvidenceClaim] = Field(min_length=1, max_length=4)

    @field_validator("name", "summary", "date", "location")
    @classmethod
    def normalize_fields(cls, value: str) -> str:
        return _plain_text(value)


class Education(_StrictModel):
    institution: str = Field(min_length=1)
    area: str = Field(min_length=1)
    degree: str = Field(min_length=1)
    start_date: str = Field(min_length=4)
    end_date: str = Field(min_length=4)
    location: str = ""
    relevance: int = Field(default=50, ge=0, le=100)
    evidence_ids: ReferenceIds = Field(min_length=1, max_length=12)
    requirement_ids: ReferenceIds = Field(min_length=1, max_length=12)
    highlights: list[EvidenceClaim] = Field(default_factory=list, max_length=3)

    @field_validator(
        "institution",
        "area",
        "degree",
        "start_date",
        "end_date",
        "location",
    )
    @classmethod
    def normalize_fields(cls, value: str) -> str:
        return _plain_text(value)


class Achievement(_StrictModel):
    name: str = Field(min_length=1)
    organization: str = ""
    date: str = Field(min_length=4)
    location: str = ""
    relevance: int = Field(default=50, ge=0, le=100)
    claim: EvidenceClaim
    evidence_ids: ReferenceIds = Field(min_length=1, max_length=12)
    requirement_ids: ReferenceIds = Field(min_length=1, max_length=12)

    @field_validator("name", "organization", "date", "location")
    @classmethod
    def normalize_fields(cls, value: str) -> str:
        return _plain_text(value)


class SkillGroup(_StrictModel):
    label: str = Field(min_length=1)
    details: str = Field(min_length=1)
    evidence_ids: ReferenceIds = Field(min_length=1, max_length=32)
    requirement_ids: ReferenceIds = Field(min_length=1, max_length=12)
    relevance: int = Field(default=50, ge=0, le=100)

    @field_validator("label", "details")
    @classmethod
    def normalize_fields(cls, value: str) -> str:
        return _plain_text(value)


class ResumeSpec(_StrictModel):
    strategy: ResumeStrategy
    candidate: Candidate
    summary: EvidenceClaim | None
    experience: list[Experience] = Field(min_length=1, max_length=6)
    projects: list[Project] = Field(default_factory=list, max_length=4)
    achievements: list[Achievement] = Field(default_factory=list, max_length=3)
    education: list[Education] = Field(min_length=1, max_length=4)
    skills: list[SkillGroup] = Field(min_length=1, max_length=5)


class PdfValidationError(ValueError):
    """Raised when the rendered CV fails deterministic PDF checks."""


def _normalized(value: str) -> str:
    value = value.translate(_DASH_TRANSLATION).casefold()
    value = re.sub(r"[^\w@.+$%]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _all_evidence_ids(spec: ResumeSpec) -> list[str]:
    ids = [item for priority in spec.strategy.priorities for item in priority.evidence_ids]
    if spec.summary:
        ids.extend(spec.summary.evidence_ids)
    for experience in spec.experience:
        ids.extend(experience.evidence_ids)
        for highlight in experience.highlights:
            ids.extend(highlight.evidence_ids)
    for project in spec.projects:
        ids.extend(project.evidence_ids)
        for highlight in project.highlights:
            ids.extend(highlight.evidence_ids)
    for achievement in spec.achievements:
        ids.extend(achievement.evidence_ids)
        ids.extend(achievement.claim.evidence_ids)
    for education in spec.education:
        ids.extend(education.evidence_ids)
        for highlight in education.highlights:
            ids.extend(highlight.evidence_ids)
    for skill in spec.skills:
        ids.extend(skill.evidence_ids)
    return ids


def _all_requirement_ids(spec: ResumeSpec) -> list[str]:
    ids = [item for priority in spec.strategy.priorities for item in priority.requirement_ids]
    ids.extend(spec.candidate.headline_requirement_ids)
    if spec.summary:
        ids.extend(spec.summary.requirement_ids)
    for experience in spec.experience:
        ids.extend(experience.requirement_ids)
        for highlight in experience.highlights:
            ids.extend(highlight.requirement_ids)
    for project in spec.projects:
        ids.extend(project.requirement_ids)
        for highlight in project.highlights:
            ids.extend(highlight.requirement_ids)
    for achievement in spec.achievements:
        ids.extend(achievement.requirement_ids)
        ids.extend(achievement.claim.requirement_ids)
    for education in spec.education:
        ids.extend(education.requirement_ids)
        for highlight in education.highlights:
            ids.extend(highlight.requirement_ids)
    for skill in spec.skills:
        ids.extend(skill.requirement_ids)
    return ids


def build_reference_index(text: str, prefix: str) -> dict[str, str]:
    """Give each non-empty source line a stable compact citation ID."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return {
        f"{prefix}{index:03d}": line
        for index, line in enumerate((line for line in lines if line), start=1)
    }


def _reference_block(index: dict[str, str]) -> str:
    return "\n".join(f"[{key}] {value}" for key, value in index.items())


def validate_evidence(spec: ResumeSpec, evidence: str) -> None:
    """Require every candidate citation ID to resolve to the supplied profile."""
    evidence_index = build_reference_index(evidence, "P")
    normalized_evidence = _normalized(evidence)
    missing = [item for item in _all_evidence_ids(spec) if item not in evidence_index]
    for identity_value in (
        spec.candidate.name,
        spec.candidate.email,
        spec.candidate.website.removeprefix("https://").removeprefix("http://"),
    ):
        normalized_value = _normalized(identity_value)
        if normalized_value and normalized_value not in normalized_evidence:
            missing.append(identity_value)
    phone_digits = re.sub(r"\D", "", spec.candidate.phone)
    evidence_digits = re.sub(r"\D", "", evidence)
    if phone_digits and phone_digits not in evidence_digits:
        missing.append(spec.candidate.phone)
    if missing:
        preview = "; ".join(repr(item[:100]) for item in missing[:3])
        raise ValueError(f"ResumeSpec cites evidence not present in profile: {preview}")


def validate_job_excerpts(spec: ResumeSpec, job_listing: str) -> None:
    """Require every requirement citation ID to resolve to the target listing."""
    requirement_index = build_reference_index(job_listing, "J")
    missing = [item for item in _all_requirement_ids(spec) if item not in requirement_index]
    if missing:
        preview = "; ".join(repr(item[:100]) for item in missing[:3])
        raise ValueError(f"ResumeSpec cites text not present in job listing: {preview}")


async def generate_resume_spec(
    *,
    llm,
    profile_markdown: str,
    job_description: str,
    job_title: str,
    refinement: str = "",
) -> ResumeSpec:
    """Ask the configured LLM for a complete, evidence-linked resume model."""
    profile_index = build_reference_index(profile_markdown, "P")
    job_listing = f"{job_title}\n{job_description}"
    requirement_index = build_reference_index(job_listing, "J")
    system_prompt = f"""You are compiling a truthful, highly tailored, one-page ATS resume.

Use the candidate profile as the only candidate evidence inventory. Select and rewrite
only the most relevant material for the job.

STRICT RULES:
- Never invent or alter employers, dates, degrees, technologies, rankings, money,
  user counts, responsibilities, or outcomes.
- Candidate evidence and target requirements have stable citation IDs. Return only
  those compact IDs in evidence_ids and requirement_ids; never copy their text into
  diagnostic fields. Every returned ID is checked programmatically.
- Each rendered claim must be fully supported by its evidence_ids.
- Use no more than 12 evidence or requirement IDs per item, except a broad Skills
  group may use up to 32 compact evidence IDs to support all listed keywords.
- Use strategy.positioning plus 1-6 compact strategy.priorities to explain the overall
  tailoring choices once. Do not repeat a prose selection reason on every CV item.
- Every selectable item must cite the target requirement IDs that made it relevant.
- Use plain text only. Do not use Markdown, bullet characters, or Unicode dashes.
- Use YYYY-MM dates when the month is known, YYYY otherwise, and "present" for current
  experience or education. Use one year-only YYYY date for every project, even when it
  is ongoing; never give projects a date range or "present" end date.
- Preserve the email and phone from Personal Details. Set website to one verbatim URL
  only when Personal Details explicitly labels it as the candidate's personal or
  portfolio website; otherwise use an empty string. Never combine URLs or promote a
  project repository, article, or source link into the header. Show only city and
  country for location; never include a street or postal address.
- Always write Master of Science as MSc and Bachelor of Science as BSc.
  Education is factual context, not disposable job-specific prose. Education may have
  0-3 highlights for exams or coursework only when the profile explicitly names them
  and they are materially relevant to this listing. Do not invent course content.
- Put personal, academic, and university projects in projects, never in experience.
  Experience is only for employment, contracting, self-employment, or compensated
  open-source contributions already listed under Experience in the candidate profile.
- Prefer a conventional software/security headline that is truthful to the evidence;
  do not copy an unrelated target title.
- keep items always in reversed chronological order, no matter the individual relevancy
- Make the CV result-oriented. Prefer evidence that shows what changed because of the
  candidate's work: users served, money earned or protected, vulnerabilities found,
  speed gained, successful outcomes, reliability improvements, shipped systems,
  adoption, rankings, or other concrete impact. Preserve every metric exactly.
- Write highlights as concise action-and-impact statements when the profile supports
  both. Lead each role or project with its strongest job-relevant result, not a list of
  routine duties or technologies. If the profile provides no outcome, describe the
  concrete artifact delivered or responsibility owned without inventing an impact.
- Prefer fewer high-impact bullets over broader task coverage. Do not repeat the same
  outcome in the profile, entry, achievement, and project merely to sound impressive.
- Aim for one A4 page: 2-4 relevant roles, 1-3 concise bullets per role, at most
  2 achievements, and 2-4 compact skill groups. Keep every bullet under 240 characters.
- Treat Skills as a compact, job-specific ATS keyword section. Select the hard skills,
  tools, concepts, and equivalent terminology an ATS or recruiter is likely to search
  for in this exact listing, but include each keyword only when candidate evidence
  truthfully supports it. Prefer the listing's exact terminology when it is equivalent
  to supported candidate evidence. Omit generic or irrelevant inventory.
- relevance is an integer from 0 to 100 and is used only to remove the least relevant
  content if the first render overflows one page.

CANDIDATE EVIDENCE INDEX:
{_reference_block(profile_index)}
"""
    refinement_note = (
        f"\nADDITIONAL USER INSTRUCTION:\n{refinement}\n" if refinement else ""
    )
    user_prompt = f"""Compile the resume for this target listing.

TARGET REQUIREMENT INDEX:
{_reference_block(requirement_index)}
{refinement_note}
"""
    from browser_use.llm.messages import SystemMessage, UserMessage

    response = await llm.ainvoke(
        [
            SystemMessage(content=system_prompt),
            UserMessage(content=user_prompt),
        ],
        output_format=ResumeSpec,
    )
    spec = response.completion
    validate_evidence(spec, profile_markdown)
    validate_job_excerpts(spec, job_listing)
    return spec


def resume_generation_audit(
    spec: ResumeSpec,
    *,
    profile_markdown: str,
    job_title: str,
    job_description: str,
    model: str,
    reasoning_effort: str,
) -> dict:
    """Expand compact citations into a readable, dossier-safe decision trail."""
    evidence = build_reference_index(profile_markdown, "P")
    requirements = build_reference_index(f"{job_title}\n{job_description}", "J")

    def refs(ids: list[str], index: dict[str, str]) -> list[dict[str, str]]:
        return [{"id": item, "text": index[item]} for item in ids if item in index]

    selections: list[dict] = []

    def add(section: str, label: str, text: str, item) -> None:
        selections.append(
            {
                "section": section,
                "label": label,
                "text": text,
                "relevance": item.relevance,
                "requirements": refs(item.requirement_ids, requirements),
                "evidence": refs(item.evidence_ids, evidence),
            }
        )

    if spec.summary:
        add("Profile", "Summary", spec.summary.text, spec.summary)
    for item in spec.experience:
        add("Experience", f"{item.position} at {item.company}", item.position, item)
        for claim in item.highlights:
            add("Experience", f"{item.position} at {item.company}", claim.text, claim)
    for item in spec.projects:
        add("Projects", item.name, item.summary or item.name, item)
        for claim in item.highlights:
            add("Projects", item.name, claim.text, claim)
    for item in spec.achievements:
        add("Achievements", item.name, item.claim.text, item.claim)
    for item in spec.education:
        add("Education", item.institution, f"{item.degree} in {item.area}", item)
        for claim in item.highlights:
            add("Education", item.institution, claim.text, claim)
    for item in spec.skills:
        add("Skills", item.label, item.details, item)

    return {
        "model": model,
        "reasoning_effort": reasoning_effort,
        "positioning": spec.strategy.positioning,
        "headline": {
            "text": spec.candidate.headline,
            "requirements": refs(
                spec.candidate.headline_requirement_ids, requirements
            ),
        },
        "priorities": [
            {
                "decision": priority.decision,
                "requirements": refs(priority.requirement_ids, requirements),
                "evidence": refs(priority.evidence_ids, evidence),
            }
            for priority in spec.strategy.priorities
        ],
        "selections": selections,
    }


def _rendercv_document(spec: ResumeSpec) -> dict:
    def rendercv_date(value: str) -> str | int:
        return int(value) if re.fullmatch(r"\d{4}", value) else value

    cv: dict = {
        "name": spec.candidate.name,
        "headline": spec.candidate.headline,
        "sections": {},
    }
    for key in ("location", "email", "phone", "website"):
        value = getattr(spec.candidate, key)
        if value:
            cv[key] = value

    if spec.summary:
        cv["sections"]["profile"] = [spec.summary.text]
    cv["sections"]["experience"] = [
        {
            "company": item.company,
            "position": item.position,
            "start_date": rendercv_date(item.start_date),
            "end_date": rendercv_date(item.end_date),
            **({"location": item.location} if item.location else {}),
            "highlights": [claim.text for claim in item.highlights],
        }
        for item in spec.experience
    ]
    if spec.projects:
        cv["sections"]["selected_projects"] = [
            {
                "name": item.name,
                **({"summary": item.summary} if item.summary else {}),
                "date": rendercv_date(item.date),
                **({"location": item.location} if item.location else {}),
                "highlights": [claim.text for claim in item.highlights],
            }
            for item in spec.projects
        ]
    if spec.achievements:
        cv["sections"]["selected_achievements"] = [
            {
                "name": item.name,
                **({"summary": item.organization} if item.organization else {}),
                "date": rendercv_date(item.date),
                **({"location": item.location} if item.location else {}),
                "highlights": [item.claim.text],
            }
            for item in spec.achievements
        ]
    cv["sections"]["education"] = [
        {
            "institution": item.institution,
            "area": item.area,
            "degree": _abbreviate_degree(item.degree),
            "start_date": rendercv_date(item.start_date),
            "end_date": rendercv_date(item.end_date),
            **({"location": item.location} if item.location else {}),
            "highlights": [claim.text for claim in item.highlights],
        }
        for item in spec.education
    ]
    cv["sections"]["skills"] = [
        {"label": item.label, "details": item.details} for item in spec.skills
    ]

    return {
        "cv": cv,
        "design": {
            "theme": "engineeringresumes",
            "page": {
                "size": "a4",
                "top_margin": "0.68in",
                "bottom_margin": "0.5in",
                "left_margin": "0.55in",
                "right_margin": "0.55in",
                "show_footer": False,
                "show_top_note": False,
            },
            "typography": {
                "line_spacing": "0.72em",
                "alignment": "left",
                "date_and_location_column_alignment": "right",
                "font_family": {
                    "body": "XCharter",
                    "name": "XCharter",
                    "headline": "XCharter",
                    "connections": "XCharter",
                    "section_titles": "XCharter",
                },
                "font_size": {
                    "body": "10.3pt",
                    "name": "24pt",
                    "headline": "11pt",
                    "connections": "9pt",
                    "section_titles": "1.18em",
                },
                "bold": {
                    "name": True,
                    "headline": False,
                    "connections": False,
                    "section_titles": True,
                },
            },
            "links": {"underline": False, "show_external_link_icon": False},
            "header": {
                "alignment": "center",
                "space_below_name": "0.16cm",
                "space_below_headline": "0.14cm",
                "space_below_connections": "0.22cm",
                "connections": {
                    "phone_number_format": "international",
                    "hyperlink": True,
                    "show_icons": False,
                    "display_urls_instead_of_usernames": True,
                    "separator": "|",
                    "space_between_connections": "0.25cm",
                },
            },
            "section_titles": {
                "type": "with_full_line",
                "line_thickness": "0.5pt",
                "space_above": "0.28cm",
                "space_below": "0.14cm",
            },
            "sections": {
                "allow_page_break": False,
                "space_between_regular_entries": "0.34cm",
                "space_between_text_based_entries": "0.10cm",
                "show_time_spans_in": [],
            },
            "entries": {
                "date_and_location_width": "4.15cm",
                "side_space": "0cm",
                "space_between_columns": "0.08cm",
                "allow_page_break": False,
                "short_second_row": False,
                "summary": {"space_above": "0.05cm", "space_left": "0cm"},
                "highlights": {
                    "bullet": "-",
                    "nested_bullet": "-",
                    "space_left": "0cm",
                    "space_above": "0.04cm",
                    "space_between_items": "0.04cm",
                    "space_between_bullet_and_text": "0.25em",
                },
            },
            "templates": {
                "date_range": "START_DATE - END_DATE",
                "single_date": "MONTH_ABBREVIATION YEAR",
                "experience_entry": {
                    "main_column": "**POSITION**, COMPANY - LOCATION\nSUMMARY\nHIGHLIGHTS",
                    "date_and_location_column": "DATE",
                },
                "normal_entry": {
                    "main_column": "**NAME** - **LOCATION**\nSUMMARY\nHIGHLIGHTS",
                    "date_and_location_column": "DATE",
                },
                "education_entry": {
                    "main_column": "**INSTITUTION**, DEGREE in AREA - LOCATION\nSUMMARY\nHIGHLIGHTS",
                    "date_and_location_column": "DATE",
                },
            },
        },
        "settings": {"current_date": date.today().isoformat()},
    }


def _displayed_claims(spec: ResumeSpec) -> list[str]:
    claims = [spec.candidate.name, spec.candidate.headline]
    if spec.summary:
        claims.append(spec.summary.text)
    for experience in spec.experience:
        claims.extend((experience.company, experience.position))
        claims.extend(item.text for item in experience.highlights)
    for project in spec.projects:
        claims.extend((project.name, project.summary))
        claims.extend(item.text for item in project.highlights)
    for achievement in spec.achievements:
        claims.extend((achievement.name, achievement.claim.text))
    for education in spec.education:
        claims.extend(
            (
                education.institution,
                _abbreviate_degree(education.degree),
                education.area,
            )
        )
        claims.extend(item.text for item in education.highlights)
    for skill in spec.skills:
        claims.extend((skill.label, skill.details))
    return [claim for claim in claims if claim]


def _missing_claims(claims: list[str], *extracted_texts: str) -> list[str]:
    """Find claims absent from every valid PDF text extraction order."""
    normalized_texts = [_normalized(text) for text in extracted_texts]
    return [
        claim
        for claim in claims
        if not any(_normalized(claim) in text for text in normalized_texts)
    ]


def _heading_line_positions(text: str, headings: list[str]) -> list[int]:
    """Locate section headings without matching the same words inside prose."""
    lines = [_normalized(line) for line in text.splitlines() if line.strip()]
    positions = []
    for heading in headings:
        normalized_heading = _normalized(heading)
        positions.append(
            next((i for i, line in enumerate(lines) if line == normalized_heading), -1)
        )
    return positions


def validate_pdf(pdf_path: Path, spec: ResumeSpec) -> dict:
    """Validate the final artifact through extraction and page geometry."""
    if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
        raise PdfValidationError("RenderCV did not create a PDF")
    try:
        document = fitz.open(pdf_path)
    except Exception as exc:
        raise PdfValidationError(f"Rendered PDF cannot be opened: {exc}") from exc
    try:
        if document.page_count != 1:
            raise PdfValidationError(
                f"Rendered CV must be one page, got {document.page_count}"
            )
        xmp_metadata = document.get_xml_metadata()
        if (
            "pdfaid:part" not in xmp_metadata
            or "pdfaid:conformance" not in xmp_metadata
        ):
            raise PdfValidationError("Rendered PDF is not PDF/A compatible")
        page = document[0]
        text = page.get_text("text", sort=True)
        normalized_text = _normalized(text)
        native_text = page.get_text("text", sort=False)
        if len(normalized_text) < 200:
            raise PdfValidationError(
                "Rendered PDF contains too little extractable text"
            )
        if "\ufffd" in text:
            raise PdfValidationError("Rendered PDF contains replacement characters")

        missing_claims = _missing_claims(_displayed_claims(spec), text, native_text)
        if missing_claims:
            preview = "; ".join(repr(item[:100]) for item in missing_claims[:3])
            raise PdfValidationError(f"Rendered PDF lost expected content: {preview}")

        rect = page.rect
        for word in page.get_text("words"):
            x0, y0, x1, y1 = word[:4]
            if (
                x0 < -0.5
                or y0 < -0.5
                or x1 > rect.width + 0.5
                or y1 > rect.height + 0.5
            ):
                raise PdfValidationError(
                    f"Rendered text lies outside the page: {word[4]!r}"
                )

        expected_headings = ["experience"]
        if spec.summary:
            expected_headings.insert(0, "profile")
        if spec.projects:
            expected_headings.append("selected projects")
        if spec.achievements:
            expected_headings.append("selected achievements")
        expected_headings.extend(("education", "skills"))
        positions = _heading_line_positions(text, expected_headings)
        if any(position < 0 for position in positions) or positions != sorted(
            positions
        ):
            raise PdfValidationError("Rendered PDF section reading order is not stable")

        links = page.get_links()
        uris = [str(link.get("uri", "")) for link in links if link.get("uri")]
        invalid_uris = [
            uri
            for uri in uris
            if not uri.startswith(("https://", "http://", "mailto:", "tel:"))
        ]
        if invalid_uris:
            raise PdfValidationError(
                f"Rendered PDF contains invalid links: {invalid_uris[:2]}"
            )
        if spec.candidate.email and not any(
            uri.casefold() == f"mailto:{spec.candidate.email}".casefold()
            for uri in uris
        ):
            raise PdfValidationError("Rendered PDF lost the email link")
        if spec.candidate.website:
            website = spec.candidate.website.removeprefix("https://").removeprefix(
                "http://"
            )
            if not any(website.casefold() in uri.casefold() for uri in uris):
                raise PdfValidationError("Rendered PDF lost the website link")

        return {
            "page_count": document.page_count,
            "characters": len(text),
            "links": len(links),
        }
    finally:
        document.close()


def _remove_lowest_priority_content(spec: ResumeSpec) -> bool:
    """Reduce overflow without truncating sentences or changing facts."""
    removable_highlights = [
        (claim.relevance, entry, index)
        for entry in [*spec.experience, *spec.projects, *spec.education]
        for index, claim in enumerate(entry.highlights)
        if isinstance(entry, Education) or len(entry.highlights) > 1
    ]
    if removable_highlights:
        _, entry, index = min(removable_highlights, key=lambda item: item[0])
        entry.highlights.pop(index)
        return True
    if spec.achievements:
        lowest = min(
            range(len(spec.achievements)), key=lambda i: spec.achievements[i].relevance
        )
        spec.achievements.pop(lowest)
        return True
    if len(spec.projects) > 1:
        lowest = min(
            range(len(spec.projects)), key=lambda i: spec.projects[i].relevance
        )
        spec.projects.pop(lowest)
        return True
    if len(spec.experience) > 2:
        lowest = min(
            range(len(spec.experience)), key=lambda i: spec.experience[i].relevance
        )
        spec.experience.pop(lowest)
        return True
    if spec.summary is not None:
        spec.summary = None
        return True
    if len(spec.skills) > 1:
        lowest = min(range(len(spec.skills)), key=lambda i: spec.skills[i].relevance)
        spec.skills.pop(lowest)
        return True
    return False


def render_resume(spec: ResumeSpec, output_pdf: Path) -> tuple[ResumeSpec, dict]:
    """Render one A4 page, reducing only complete low-priority content if needed."""
    from rendercv.renderer.pdf_png import (
        copy_photo_next_to_typst_file,
        get_typst_compiler,
    )
    from rendercv.renderer.typst import generate_typst
    from rendercv.schema.rendercv_model_builder import (
        build_rendercv_dictionary_and_model,
    )

    output_pdf = output_pdf.resolve()
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    yaml_path = output_pdf.with_name("rendercv.yaml")
    typst_path = output_pdf.with_suffix(".typ")
    working = copy.deepcopy(spec)

    for _ in range(20):
        document = _rendercv_document(working)
        yaml_content = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
        yaml_path.write_text(yaml_content, encoding="utf-8")
        _, model = build_rendercv_dictionary_and_model(
            yaml_content,
            input_file_path=yaml_path,
            typst_path=typst_path,
            pdf_path=output_pdf,
            dont_generate_markdown=True,
            dont_generate_html=True,
            dont_generate_png=True,
        )
        generated_typst = generate_typst(model)
        if generated_typst is None:
            raise PdfValidationError(
                "RenderCV Typst generation was disabled unexpectedly"
            )
        copy_photo_next_to_typst_file(model, generated_typst)
        compiler = get_typst_compiler(yaml_path, generated_typst.parent)
        compiler.compile(
            input=generated_typst,
            format="pdf",
            output=output_pdf,
            pdf_standards=["a-2b"],
        )
        try:
            validation = validate_pdf(output_pdf, working)
        except PdfValidationError as exc:
            if "must be one page" not in str(
                exc
            ) or not _remove_lowest_priority_content(working):
                raise
            continue
        yaml_path.write_text(
            yaml.safe_dump(
                _rendercv_document(working), sort_keys=False, allow_unicode=True
            ),
            encoding="utf-8",
        )
        output_pdf.with_name("resume-spec.json").write_text(
            working.model_dump_json(indent=2), encoding="utf-8"
        )
        return working, validation
    raise PdfValidationError("Could not fit the tailored CV on one page")


def resume_spec_markdown(spec: ResumeSpec) -> str:
    sections = [f"# {spec.candidate.name}", f"\n{spec.candidate.headline}"]
    if spec.summary:
        sections.append(f"## PROFILE\n\n{spec.summary.text}")
    experience_lines = []
    for item in spec.experience:
        experience_lines.append(
            f"### {item.position}, {item.company}\n"
            + "\n".join(f"- {claim.text}" for claim in item.highlights)
        )
    sections.append("## EXPERIENCE\n\n" + "\n\n".join(experience_lines))
    if spec.projects:
        sections.append(
            "## SELECTED PROJECTS\n\n"
            + "\n\n".join(
                f"### {item.name}\n"
                + "\n".join(f"- {claim.text}" for claim in item.highlights)
                for item in spec.projects
            )
        )
    if spec.achievements:
        sections.append(
            "## SELECTED ACHIEVEMENTS\n\n"
            + "\n".join(
                f"- {item.name}: {item.claim.text}" for item in spec.achievements
            )
        )
    sections.append(
        "## EDUCATION\n\n"
        + "\n\n".join(
            f"- {_abbreviate_degree(item.degree)} in {item.area}, {item.institution}"
            + (
                "\n" + "\n".join(f"  - {claim.text}" for claim in item.highlights)
                if item.highlights
                else ""
            )
            for item in spec.education
        )
    )
    sections.append(
        "## SKILLS\n\n"
        + "\n".join(f"- {item.label}: {item.details}" for item in spec.skills)
    )
    return "\n\n".join(sections).strip() + "\n"
