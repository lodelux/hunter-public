"""Resume tailoring orchestration and legacy PDF-layout helpers.

New resumes are compiled from an evidence-linked ResumeSpec and rendered as a
fresh one-page PDF. The extraction helpers remain for reading older CV layouts
and for compatibility with existing focused tests.
"""
import hashlib
import json
import re
from pathlib import Path
from typing import Optional

import fitz  # pymupdf

from .compiler import (
    generate_resume_spec,
    render_resume,
    resume_generation_audit,
    resume_spec_markdown,
)

try:
    from core.config import (
        get_data_dir,
        load_llm_settings,
        load_profile_markdown,
    )
    from core.artifact_names import tailored_cv_filename
    from core.llm_factory import (
        DOCUMENT_LLM_REASONING_EFFORT,
        RESUME_LLM_MODEL,
        create_llm,
        resume_llm_settings,
    )
except ImportError:
    from backend.core.config import (
        get_data_dir,
        load_llm_settings,
        load_profile_markdown,
    )
    from backend.core.artifact_names import tailored_cv_filename
    from backend.core.llm_factory import (
        DOCUMENT_LLM_REASONING_EFFORT,
        RESUME_LLM_MODEL,
        create_llm,
        resume_llm_settings,
    )


def _get_tailored_dir() -> Path:
    d = get_data_dir() / "tailored_resumes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _url_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _get_job_artifact_dir(job_url: str) -> Path:
    directory = _get_tailored_dir() / _url_hash(job_url)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _vector_bullet_lines(
    drawings: list[dict],
    block_bbox: tuple,
    lines: list[dict],
) -> list[int]:
    """Find lines preceded by a small vector bullet outside the text box."""
    bullet_lines = []
    text_left = block_bbox[0]
    for index, line in enumerate(lines):
        line_rect = fitz.Rect(line["bbox"])
        for drawing in drawings:
            rect = drawing["rect"]
            gap = text_left - rect.x1
            if (
                "f" in drawing.get("type", "")
                and 0 < rect.width <= 8
                and 0 < rect.height <= 8
                and 0 <= gap <= 20
                and line_rect.y0 <= (rect.y0 + rect.y1) / 2 <= line_rect.y1
            ):
                bullet_lines.append(index)
                break
    return bullet_lines


def _extract_sections(doc: fitz.Document) -> list[dict]:
    """Extract text blocks from the PDF grouped into logical sections.

    Groups a section header block with all content blocks that follow it
    until the next section header. PDF drawing order is not necessarily visual
    reading order, so blocks are sorted by their page coordinates first.
    """
    SECTION_HEADERS = {
        "skills": "skills",
        "technologies": "skills",
        "languages & technologies": "skills",
        "technical skills": "skills",
        "tech stack": "skills",
        "tools": "skills",
        "summary": "overview",
        "objective": "overview",
        "overview": "overview",
        "about me": "overview",
        "profile": "overview",
        "work experience": "experience",
        "experience": "experience",
        "employment": "experience",
        "professional experience": "experience",
        "volunteer experience": "experience",
        "hackathons & ctfs": "achievements",
        "education": "education",
        "contact": "contact",
    }

    all_blocks = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        drawings = page.get_drawings()
        blocks = page.get_text("dict")["blocks"]
        for block in blocks:
            if block["type"] != 0:
                continue
            text = ""
            line_details = []
            for line in block.get("lines", []):
                line_text = ""
                span_details = []
                for span in line.get("spans", []):
                    span_text = span.get("text", "")
                    text += span_text
                    line_text += span_text
                    span_details.append({
                        "text": span_text,
                        "bbox": span.get("bbox"),
                        "origin": span.get("origin"),
                        "font": span.get("font", ""),
                        "size": span.get("size", 0),
                        "color": span.get("color", 0),
                    })
                text += "\n"
                if line_text.strip():
                    line_details.append({
                        "text": line_text,
                        "bbox": line.get("bbox"),
                        "spans": span_details,
                    })
            text = text.strip()
            if not text:
                continue
            all_blocks.append({
                "page": page_num,
                "bbox": block["bbox"],
                "text": text,
                "lines": line_details,
                "bullet_lines": _vector_bullet_lines(
                    drawings,
                    block["bbox"],
                    line_details,
                ),
            })

    # PyMuPDF normally returns content in drawing order. Designed resumes often
    # draw the body first and section labels last, even though the labels appear
    # to the left of the body. Restore visual top-to-bottom, left-to-right order.
    all_blocks.sort(key=lambda b: (b["page"], b["bbox"][1], b["bbox"][0]))

    # Identify which blocks are section headers
    sections = []
    current_section = None

    for block in all_blocks:
        normalized = re.sub(r"\s+", " ", block["text"]).strip().casefold().rstrip(":")
        section_type = SECTION_HEADERS.get(normalized)

        if section_type:
            # This block is a header — start a new section
            if current_section and current_section["content_blocks"]:
                sections.append(current_section)
            current_section = {
                "section_type": section_type,
                "header_block": block,
                "content_blocks": [],
                "page": block["page"],
            }
        elif current_section:
            # This block is content under the current header
            current_section["content_blocks"].append(block)

    if current_section and current_section["content_blocks"]:
        sections.append(current_section)

    # Build final section objects with combined text and bounding boxes
    result = []
    for section in sections:
        content_text = "\n".join(b["text"] for b in section["content_blocks"])
        if not content_text.strip():
            continue
        # Compute bounding box that covers all content blocks
        all_bboxes = [b["bbox"] for b in section["content_blocks"]]
        combined_bbox = (
            min(b[0] for b in all_bboxes),
            min(b[1] for b in all_bboxes),
            max(b[2] for b in all_bboxes),
            max(b[3] for b in all_bboxes),
        )
        result.append({
            "page": section["page"],
            "bbox": combined_bbox,
            "text": content_text,
            "char_count": len(content_text),
            "section_type": section["section_type"],
            "section_title": re.sub(
                r"\s+", " ", section["header_block"]["text"]
            ).strip(),
            "header_block": section["header_block"],
            "content_blocks": section["content_blocks"],
        })

    # The centered professional headline sits above the first section and has
    # no explicit label. Detect the largest uniform non-bold line in that area;
    # this excludes the bold name and the smaller contact details.
    first_header_y = min(
        (
            section["header_block"]["bbox"][1]
            for section in sections
            if section["header_block"]["page"] == 0
        ),
        default=float("inf"),
    )
    headline_candidates = []
    for block in all_blocks:
        if (
            block["page"] != 0
            or block["bbox"][1] >= first_header_y
            or len(block["lines"]) != 1
        ):
            continue
        spans = [
            span
            for line in block["lines"]
            for span in line["spans"]
            if span["text"].strip()
        ]
        if (
            len(spans) == 1
            and "bold" not in spans[0]["font"].casefold()
            and spans[0]["size"] >= 12
        ):
            headline_candidates.append(block)

    if headline_candidates:
        headline = max(
            headline_candidates,
            key=lambda block: block["lines"][0]["spans"][0]["size"],
        )
        result.insert(0, {
            "page": headline["page"],
            "bbox": headline["bbox"],
            "text": headline["text"],
            "char_count": len(headline["text"]),
            "section_type": "headline",
            "section_title": "Professional headline",
            "header_block": None,
            "content_blocks": [headline],
        })
    return result


def _identify_tailorable_sections(sections: list[dict], options: dict) -> list[dict]:
    """Filter sections that match the user's selected tailoring options."""
    tailorable = []
    for s in sections:
        st = s["section_type"]
        if st == "skills" and options.get("skills"):
            tailorable.append(s)
        elif st == "overview" and options.get("overview"):
            tailorable.append(s)
        elif st == "experience" and options.get("experience"):
            tailorable.append(s)
    return tailorable


def _extract_editable_regions(sections: list[dict], options: dict) -> list[dict]:
    """Return only text blocks that can be replaced without moving layout."""
    page_right_edges: dict[int, float] = {}
    page_left_edges: dict[int, float] = {}
    for section in sections:
        header = section.get("header_block")
        if header:
            page = header["page"]
            page_left_edges[page] = min(
                page_left_edges.get(page, header["bbox"][0]),
                header["bbox"][0],
            )
        for block in section["content_blocks"]:
            page = block["page"]
            page_right_edges[page] = max(
                page_right_edges.get(page, 0),
                block["bbox"][2],
            )
            page_left_edges[page] = min(
                page_left_edges.get(page, block["bbox"][0]),
                block["bbox"][0],
            )

    def make_region(
        block: dict,
        section_type: str,
        *,
        kind: str = "text",
        available_x1: Optional[float] = None,
        lines: Optional[list[dict]] = None,
        fixed_text: Optional[str] = None,
    ) -> dict:
        region_lines = lines or block["lines"]
        bboxes = [line["bbox"] for line in region_lines]
        text = "\n".join(line["text"] for line in region_lines)
        region = {
            **block,
            "bbox": (
                min(bbox[0] for bbox in bboxes),
                min(bbox[1] for bbox in bboxes),
                max(bbox[2] for bbox in bboxes),
                max(bbox[3] for bbox in bboxes),
            ),
            "text": text,
            "lines": region_lines,
            "section_type": section_type,
            "region_kind": kind,
            "char_count": len(text),
            "available_x1": (
                available_x1
                if available_x1 is not None
                else page_right_edges[block["page"]]
            ),
        }
        if fixed_text is not None:
            region["fixed_text"] = fixed_text
        return region

    def is_title_and_date(block: dict) -> bool:
        lines = block.get("lines", [])
        if len(lines) != 2:
            return False
        first_spans = lines[0].get("spans", [])
        return (
            bool(first_spans)
            and "bold" in first_spans[0].get("font", "").casefold()
            and abs(lines[0]["bbox"][1] - lines[1]["bbox"][1]) < 2
        )

    regions = []
    for section in sections:
        section_type = section["section_type"]
        blocks = section["content_blocks"]

        if section_type == "headline" and options.get("title"):
            block = blocks[0]
            region = make_region(block, "headline", kind="profile_title")
            region["char_count"] = max(region["char_count"], 70)
            region["align"] = "center"
            region["center_x"] = (block["bbox"][0] + block["bbox"][2]) / 2
            region["max_width"] = (
                page_right_edges[block["page"]] - page_left_edges[block["page"]]
            )
            regions.append(region)
            continue

        if section_type == "skills" and options.get("skills"):
            header = section["header_block"]
            for block in blocks:
                if not _uniform_block_style(block):
                    continue
                region = make_region(block, "skills", kind="skills")
                # The original second line has ample unused space. Let the model
                # retain every hard skill and add a few relevant soft skills.
                region["char_count"] = max(region["char_count"] + 60, 150)
                regions.append(region)
            header_right = min(block["bbox"][0] for block in blocks) - 8
            if header_right <= header["bbox"][0]:
                header_right = page_right_edges[header["page"]]
            regions.append(make_region(
                header,
                "skills",
                kind="section_header",
                available_x1=header_right,
                fixed_text="SKILLS",
            ))
            continue

        if section_type == "experience":
            for block in blocks:
                if options.get("title") and is_title_and_date(block):
                    title_line, date_line = block["lines"]
                    region = make_region(
                        block,
                        "experience",
                        kind="job_title",
                        available_x1=date_line["bbox"][0] - 8,
                        lines=[title_line],
                    )
                    region["char_count"] = max(region["char_count"], 40)
                    regions.append(region)
                elif options.get("experience") and (
                    block.get("bullet_lines") or _is_bullet(block["text"])
                ):
                    # Keep the whole vertical description area together so the
                    # model may choose a different number of bullets.
                    region = make_region(
                        block,
                        "experience",
                        kind="bullet_group",
                    )
                    region["char_count"] = max(
                        region["char_count"],
                        len(region["lines"]) * 80,
                    )
                    regions.append(region)
            continue

        if section_type == "achievements" and options.get("achievements"):
            for block in blocks:
                if is_title_and_date(block):
                    title_line, date_line = block["lines"]
                    region = make_region(
                        block,
                        "achievements",
                        kind="achievement_title",
                        available_x1=date_line["bbox"][0] - 8,
                        lines=[title_line],
                    )
                    region["char_count"] = max(region["char_count"], 40)
                    regions.append(region)
                else:
                    regions.append(make_region(
                        block,
                        "achievements",
                        kind="achievement_text",
                    ))
            continue

        if section_type == "overview" and options.get("overview"):
            for block in blocks:
                if _uniform_block_style(block):
                    regions.append(make_region(block, "overview"))
    return regions


def _uniform_block_style(block: dict) -> Optional[dict]:
    """Return a block's single text style, or None for mixed-style blocks."""
    spans = [
        span
        for line in block.get("lines", [])
        for span in line.get("spans", [])
        if span.get("text", "").strip()
    ]
    if not spans:
        return None

    styles = {
        (
            span.get("font", ""),
            round(float(span.get("size", 0)), 3),
            int(span.get("color", 0)),
        )
        for span in spans
    }
    if len(styles) != 1:
        return None

    font, size, color = styles.pop()
    if not font or size <= 0:
        return None
    return {"font": font, "size": size, "color": color}


def _primary_block_style(block: dict) -> Optional[dict]:
    """Return the first visible span style for an intentionally restyled box."""
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            if span.get("text", "").strip():
                return {
                    "font": span.get("font", ""),
                    "size": float(span.get("size", 0)),
                    "color": int(span.get("color", 0)),
                }
    return None


def _fit_text_lines(
    text: str,
    font: fitz.Font,
    font_size: float,
    max_width: float,
    max_lines: int,
) -> Optional[list[str]]:
    """Wrap text using the original font, failing closed when it cannot fit."""
    words = re.sub(r"\s+", " ", text).strip().split()
    if not words or max_width <= 0 or max_lines <= 0:
        return None

    lines: list[str] = []
    current = ""
    for word in words:
        if font.text_length(word, font_size) > max_width:
            return None
        candidate = f"{current} {word}" if current else word
        if current and font.text_length(candidate, font_size) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        return None
    return lines


def _fit_bullet_lines(
    text: str,
    font: fitz.Font,
    font_size: float,
    max_width: float,
    max_lines: int,
) -> Optional[tuple[list[str], list[int]]]:
    """Wrap newline-separated bullets into a fixed number of available lines."""
    items = [
        _strip_bullet(item).strip()
        for item in text.splitlines()
        if _strip_bullet(item).strip()
    ]
    if not items:
        return None

    lines: list[str] = []
    bullet_starts: list[int] = []
    for item in items:
        wrapped = _fit_text_lines(
            item,
            font,
            font_size,
            max_width,
            max_lines,
        )
        if not wrapped:
            return None
        bullet_starts.append(len(lines))
        lines.extend(wrapped)
        if len(lines) > max_lines:
            return None
    return lines, bullet_starts


def _font_for_span(
    doc: fitz.Document,
    page: fitz.Page,
    span_font: str,
) -> Optional[dict]:
    """Resolve a span font to an isolated insertion font and metric object."""
    normalized = span_font.casefold()
    builtin_fonts = {
        "helvetica": "helv",
        "helvetica-bold": "hebo",
        "times-roman": "tiro",
        "times-bold": "tibo",
        "courier": "cour",
        "courier-bold": "cobo",
    }
    if normalized in builtin_fonts:
        font_name = builtin_fonts[normalized]
        return {
            "insert_name": font_name,
            "font": fitz.Font(font_name),
            "buffer": None,
        }

    for font_info in page.get_fonts(full=True):
        xref, _, _, base_font = font_info[:4]
        plain_name = base_font.split("+", 1)[-1]
        if plain_name != span_font and not base_font.endswith(span_font):
            continue
        _, _, _, font_buffer = doc.extract_font(xref)
        if not font_buffer:
            return None
        return {
            "insert_name": f"TailorFont{xref}",
            "font": fitz.Font(fontbuffer=font_buffer),
            "buffer": font_buffer,
        }
    return None


def _font_supports_text(font: fitz.Font, text: str) -> bool:
    supported = set(font.valid_codepoints())
    return all(character.isspace() or ord(character) in supported for character in text)


def _pdf_color(color: int) -> tuple[float, float, float]:
    rgb = fitz.sRGB_to_rgb(color)
    return tuple(channel / 255 for channel in rgb)


def _replace_text_in_place(
    doc: fitz.Document,
    regions: list[dict],
    tailored_text: dict[int, str],
    output_path: Path,
) -> dict[int, str]:
    """Replace fitting text regions on the original pages and save a copy."""
    plans = []
    for index, new_text in tailored_text.items():
        if index >= len(regions):
            continue
        region = regions[index]
        kind = region.get("region_kind", "text")
        style = _uniform_block_style(region)
        if not style and kind == "bullet_group":
            # The final job uses bold emphasis inside its sole bullet. Replacing
            # the whole bullet intentionally uses its primary regular style.
            style = _primary_block_style(region)
        if not style:
            continue

        page = doc[region["page"]]
        font_info = _font_for_span(doc, page, style["font"])
        if not font_info:
            continue
        if not _font_supports_text(font_info["font"], new_text):
            fallback_name = (
                "tibo" if "bold" in style["font"].casefold() else "tiro"
            )
            font_info = {
                "insert_name": fallback_name,
                "font": fitz.Font(fallback_name),
                "buffer": None,
            }

        original_lines = region.get("lines", [])
        baselines = [
            line["spans"][0]["origin"][1]
            for line in original_lines
            if line.get("spans") and line["spans"][0].get("origin")
        ]
        max_width = region.get(
            "max_width",
            region["available_x1"] - region["bbox"][0],
        )
        bullet_starts: list[int] = []
        if kind == "bullet_group":
            fitted = _fit_bullet_lines(
                new_text,
                font_info["font"],
                style["size"],
                max_width,
                len(baselines),
            )
            if not fitted:
                continue
            fitted_lines, bullet_starts = fitted
        elif kind == "profile_title" and not new_text.strip():
            # Removing an unrelated headline is safer than claiming a role the
            # candidate has not held.
            fitted_lines = []
        else:
            fitted_lines = _fit_text_lines(
                new_text,
                font_info["font"],
                style["size"],
                max_width,
                len(baselines),
            )
            if not fitted_lines:
                continue

        rect = fitz.Rect(region["bbox"])
        if kind == "bullet_group":
            rect.x0 -= 15
        plans.append({
            "index": index,
            "page": region["page"],
            "rect": rect,
            "x": region["bbox"][0],
            "baselines": baselines,
            "lines": fitted_lines,
            "bullet_starts": bullet_starts,
            "font_name": font_info["insert_name"],
            "font_buffer": font_info["buffer"],
            "measure_font": font_info["font"],
            "font_size": style["size"],
            "color": _pdf_color(style["color"]),
            "text": new_text,
        })

    if not plans:
        raise ValueError("Tailored text did not fit any original resume text boxes")

    pages_with_redactions = set()
    for plan in plans:
        page = doc[plan["page"]]
        page.add_redact_annot(
            plan["rect"],
            fill=(1, 1, 1),
            cross_out=False,
        )
        pages_with_redactions.add(plan["page"])

    for page_number in pages_with_redactions:
        doc[page_number].apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
        )

    registered_fonts = set()
    for plan in plans:
        page = doc[plan["page"]]
        font_key = (plan["page"], plan["font_name"])
        if plan["font_buffer"] and font_key not in registered_fonts:
            page.insert_font(
                fontname=plan["font_name"],
                fontbuffer=plan["font_buffer"],
            )
            registered_fonts.add(font_key)
        for line, baseline in zip(plan["lines"], plan["baselines"]):
            x = plan["x"]
            if regions[plan["index"]].get("align") == "center":
                x = (
                    regions[plan["index"]]["center_x"]
                    - plan["measure_font"].text_length(line, plan["font_size"]) / 2
                )
            page.insert_text(
                fitz.Point(x, baseline),
                line,
                fontname=plan["font_name"],
                fontsize=plan["font_size"],
                color=plan["color"],
            )
        for line_index in plan["bullet_starts"]:
            baseline = plan["baselines"][line_index]
            page.draw_circle(
                fitz.Point(
                    plan["x"] - 10,
                    baseline - plan["font_size"] * 0.32,
                ),
                1.9,
                color=plan["color"],
                fill=plan["color"],
                width=0,
            )

    temp_path = output_path.with_suffix(".tmp.pdf")
    if temp_path.exists():
        temp_path.unlink()
    doc.save(temp_path, garbage=4, deflate=True)
    temp_path.replace(output_path)
    return {plan["index"]: plan["text"] for plan in plans}


async def _generate_tailored_text(
    sections: list[dict],
    job_description: str,
    options: dict,
    refinement: str = "",
    cost_tracker=None,
) -> dict[int, str]:
    """Call LLM to generate tailored text for each fixed-layout text box."""
    llm_settings = load_llm_settings()
    if not llm_settings.get("provider"):
        raise ValueError("No LLM configured")

    llm = create_llm(resume_llm_settings(llm_settings))
    if cost_tracker is not None:
        cost_tracker.register_llm("cv", llm)

    section_prompts = []
    tailored = {}
    for i, s in enumerate(sections):
        if s.get("fixed_text") is not None:
            tailored[i] = s["fixed_text"]
            continue
        max_chars = s["char_count"]
        kind = s.get("region_kind", "text")
        format_note = ""
        if kind == "bullet_group":
            format_note = (
                " Return one or more bullet bodies separated by newline characters; "
                "do not include bullet symbols. You may change the bullet count."
            )
        elif kind == "profile_title":
            format_note = (
                " This is the candidate's professional headline. Use only Software Engineer, "
                "Backend Developer, Full Stack Developer, Security Researcher, Penetration Tester, "
                "Smart Contract Security Researcher, Smart Contract Auditor, Blockchain Security "
                "Engineer, Security Engineer, Software Security Engineer, Blockchain Software "
                "Engineer, or a very close truthful variant. Never copy or approximate an unrelated listing "
                "title. If the target role is outside these families, return an empty string."
            )
        section_prompts.append(
            f"SECTION {i} ({s['section_type']} / {kind}, "
            f"max {max_chars} characters, max {len(s.get('lines', []))} lines):\n"
            f"ORIGINAL: \"{s['text']}\"\n"
            f"→ Rewrite for the target job. MUST be {max_chars} characters or fewer."
            f"{format_note}\n"
        )

    if not section_prompts:
        return tailored

    refinement_note = ""
    if refinement:
        refinement_note = f"\nADDITIONAL INSTRUCTION: {refinement}\n"

    prompt = (
        "You are a resume tailoring expert. Rewrite the following resume text boxes "
        "to better match the target job description.\n\n"
        "CRITICAL RULES:\n"
        "- Each text box output MUST NOT exceed its character limit\n"
        "- Return plain text without markdown or headings inside values\n"
        "- Emphasize skills and experience relevant to the job\n"
        "- Do NOT fabricate experience the candidate doesn't have\n"
        "- Preserve all factual employers, dates, locations, results, rankings, amounts, and technologies\n"
        "- Job titles may be reframed only when the new title remains truthful to the original work\n"
        "- The professional headline must stay within Software Engineer, Backend Developer, Full Stack Developer, Security Researcher, Penetration Tester, Smart Contract Security Researcher, Smart Contract Auditor, Blockchain Security Engineer, Security Engineer, Software Security Engineer, or Blockchain Software Engineer; use an empty string when the listing is not a close match\n"
        "- For skills, retain relevant original hard skill and optionally append basic soft skills explicitly required by the job\n"
        "- Do NOT add new hard skills, tools, or technologies.You may remove clearly not relevant skills for the current listing \n"
        "- For achievements, rephrase for relevance without inventing or changing the achievement\n"
        "- Reorder and rephrase to highlight what matters for this role\n"
        f"{refinement_note}\n"
        f"CANDIDATE MARKDOWN PROFILE:\n{load_profile_markdown()}\n\n"
        f"TARGET JOB DESCRIPTION:\n{job_description}\n\n"
        + "\n".join(section_prompts) + "\n\n"
        "Return ONLY a JSON object with section indices as keys and tailored text as values:\n"
        '{"0": "tailored text for section 0", "1": "tailored text for section 1", ...}\n'
    )

    from browser_use.llm.messages import UserMessage
    response = await llm.ainvoke([UserMessage(content=prompt)])
    if hasattr(response, "completion"):
        text = response.completion
    elif hasattr(response, "content") and isinstance(response.content, str):
        text = response.content
    else:
        text = str(response)

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("LLM did not return valid JSON")

    result = json.loads(match.group())

    for i, s in enumerate(sections):
        if s.get("fixed_text") is not None:
            continue
        key = str(i)
        if key in result:
            value = result[key]
            if isinstance(value, list):
                new_text = "\n".join(str(item).strip() for item in value if str(item).strip())
            else:
                new_text = str(value).strip()
            if len(new_text) <= s["char_count"]:
                tailored[i] = new_text
            else:
                truncated = new_text[:s["char_count"]].rstrip()
                if (
                    len(new_text) > s["char_count"]
                    and new_text[s["char_count"]].isalnum()
                    and truncated
                    and truncated[-1].isalnum()
                ):
                    truncated = truncated.rsplit(maxsplit=1)[0]
                tailored[i] = truncated

    return tailored


# ── PDF layout constants ─────────────────────────────────────────────────────
_PAGE_WIDTH = 612          # US Letter
_PAGE_HEIGHT = 792
_MARGIN_LEFT = 36
_MARGIN_RIGHT = 576
_TOP_MARGIN = 50
_BOTTOM_MARGIN = 50        # usable area ends at _PAGE_HEIGHT - _BOTTOM_MARGIN
_TEXT_WIDTH = _MARGIN_RIGHT - _MARGIN_LEFT
_BULLET_INDENT = 15        # x-offset for bullet glyph
_HANGING_INDENT = 27       # x-offset for wrapped bullet continuation lines
_BODY_SIZE = 9
_LINE_GAP = 3              # extra spacing added to fontsize for line-height
_ENTRY_GAP = 10            # consistent vertical gap between experience entries

# Bullet glyphs we treat as list markers (beyond the original ·/-).
_BULLET_CHARS = ("·", "-", "•", "*", "–", "—", "‣", "●", "▪", "◦", "○")
# Numbered bullets like "1." / "12)" / "a." / "iv)".
_NUMBERED_RE = re.compile(r"^\(?\s*([0-9]+|[a-zA-Z]|[ivxIVX]+)\s*[.)]\s+")
# A leading date range, e.g. "2019 - 2022", "Jan 2019 – Present", "2020-Now".
_DATE_RANGE_RE = re.compile(
    r"^\s*((?:[A-Za-z]{3,9}\.?\s+)?\d{4}|present|current|now)\s*[-–—to]+\s*"
    r"((?:[A-Za-z]{3,9}\.?\s+)?\d{4}|present|current|now)\b",
    re.IGNORECASE,
)
# "Title — Company" / "Title @ Company" / "Title at Company" style header.
_TITLE_COMPANY_RE = re.compile(r".+\s+(?:[–—@|]|\bat\b)\s+.+")


def _is_bullet(line: str) -> bool:
    """Return True if ``line`` looks like a list bullet of any common style."""
    s = line.lstrip()
    if not s:
        return False
    if s[0] in _BULLET_CHARS:
        return True
    return bool(_NUMBERED_RE.match(s))


def _strip_bullet(line: str) -> str:
    """Remove a leading bullet marker, returning the bullet text only."""
    s = line.lstrip()
    if s and s[0] in _BULLET_CHARS:
        return s[1:].lstrip()
    m = _NUMBERED_RE.match(s)
    if m:
        return s[m.end():].lstrip()
    return s


def _is_entry_header(line: str) -> bool:
    """Heuristic: a non-bullet line that begins a new experience entry.

    Catches lines with a leading date range or a "Title — Company" pattern even
    when the resume uses no explicit bullets.
    """
    s = line.strip()
    if not s or _is_bullet(s):
        return False
    return bool(_DATE_RANGE_RE.match(s) or _TITLE_COMPANY_RE.match(s))


def _split_entries(content: str) -> list[list[str]]:
    """Split experience-section text into a list of entries (each a line list).

    An entry boundary is a blank line, OR a new non-bullet header line that
    follows lines already containing bullets, OR a detected entry-header line.
    """
    entries: list[list[str]] = []
    current: list[str] = []
    for raw in content.split("\n"):
        stripped = raw.strip()
        if not stripped:
            if current:
                entries.append(current)
                current = []
            continue
        start_new = False
        if current:
            has_bullets = any(_is_bullet(l) for l in current)
            if not _is_bullet(stripped) and has_bullets:
                start_new = True
            elif _is_entry_header(stripped) and not _is_bullet(stripped):
                # A new header (date range / Title — Company) starts a fresh entry.
                start_new = True
        if start_new:
            entries.append(current)
            current = []
        current.append(stripped)
    if current:
        entries.append(current)
    return entries


def _generate_fresh_pdf(
    sections: list[dict],
    tailored: dict[int, str],
    profile_name: str,
    contact_info: str,
    education_text: str,
    output_path: Path,
):
    """Generate a professional PDF matching the user's resume style.

    Uses real font metrics for wrapping and tracks a y-cursor with page-overflow
    handling so no text is ever drawn below the usable area.
    """
    doc = fitz.open()
    page = doc.new_page(width=_PAGE_WIDTH, height=_PAGE_HEIGHT)
    usable_bottom = _PAGE_HEIGHT - _BOTTOM_MARGIN

    # Colors
    header_color = (0.09, 0.09, 0.40)  # dark blue for section headers
    body_color = (0.19, 0.19, 0.19)    # dark gray for body
    light_color = (0.40, 0.40, 0.40)   # lighter gray for dates/secondary

    state = {"page": page, "y": _TOP_MARGIN}

    def ensure_space(needed: float):
        """Start a new page if drawing ``needed`` points would overflow."""
        if state["y"] + needed > usable_bottom:
            state["page"] = doc.new_page(width=_PAGE_WIDTH, height=_PAGE_HEIGHT)
            state["y"] = _TOP_MARGIN

    def draw_line_text(text: str, *, x: float, fontsize: float, fontname: str,
                       color: tuple, line_height: float, baseline_pad: float):
        ensure_space(line_height)
        state["page"].insert_text(
            fitz.Point(x, state["y"] + baseline_pad),
            text, fontsize=fontsize, fontname=fontname, color=color,
        )
        state["y"] += line_height

    def draw_wrapped(text: str, *, x: float, hang_x: float, width: float,
                     fontsize: float, fontname: str, color: tuple):
        line_height = fontsize + _LINE_GAP
        wrapped = _wrap_text(text, width, fontsize) or [""]
        for i, wl in enumerate(wrapped):
            draw_line_text(
                wl, x=(x if i == 0 else hang_x), fontsize=fontsize,
                fontname=fontname, color=color, line_height=line_height,
                baseline_pad=fontsize,
            )

    def draw_section_header(title: str):
        ensure_space(28)
        state["page"].insert_text(
            fitz.Point(_MARGIN_LEFT, state["y"] + 12), title,
            fontsize=12, fontname="helvetica-bold", color=header_color,
        )
        state["y"] += 18
        state["page"].draw_line(
            fitz.Point(_MARGIN_LEFT, state["y"]),
            fitz.Point(_MARGIN_RIGHT, state["y"]),
            color=(0.8, 0.8, 0.8), width=0.5,
        )
        state["y"] += 12

    # ─── Name ───
    ensure_space(42)
    state["page"].insert_text(
        fitz.Point(_MARGIN_LEFT, state["y"] + 30),
        profile_name.upper() if profile_name else "",
        fontsize=28, fontname="helvetica-bold", color=(0, 0, 0),
    )
    state["y"] += 42
    state["page"].draw_line(
        fitz.Point(_MARGIN_LEFT, state["y"]), fitz.Point(_MARGIN_RIGHT, state["y"]),
        color=(0.85, 0.20, 0.35), width=2,
    )
    state["y"] += 20

    # ─── Contact info (below name) ───
    for line in contact_info.split("\n"):
        if line.strip():
            draw_line_text(line.strip(), x=_MARGIN_LEFT, fontsize=_BODY_SIZE,
                           fontname="helv", color=body_color,
                           line_height=14, baseline_pad=10)
    state["y"] += 10

    # ─── Skills Section ───
    skills_section = next(
        ((i, s) for i, s in enumerate(sections) if s["section_type"] == "skills"),
        None,
    )
    if skills_section:
        idx, s = skills_section
        draw_section_header("Skills")
        content = tailored.get(idx, s["text"])
        for line in content.split("\n"):
            if line.strip():
                draw_wrapped(line.strip(), x=_MARGIN_LEFT, hang_x=_MARGIN_LEFT,
                             width=_TEXT_WIDTH, fontsize=_BODY_SIZE,
                             fontname="helv", color=body_color)
        state["y"] += 10

    # ─── Overview Section ───
    overview_sections = [
        (i, s) for i, s in enumerate(sections) if s["section_type"] == "overview"
    ]
    for idx, s in overview_sections:
        draw_section_header("Summary")
        content = tailored.get(idx, s["text"])
        for line in content.split("\n"):
            if line.strip():
                draw_wrapped(line.strip(), x=_MARGIN_LEFT, hang_x=_MARGIN_LEFT,
                             width=_TEXT_WIDTH, fontsize=_BODY_SIZE,
                             fontname="helv", color=body_color)
        state["y"] += 10

    # ─── Experience Section ───
    experience_sections = [
        (i, s) for i, s in enumerate(sections) if s["section_type"] == "experience"
    ]
    if experience_sections:
        draw_section_header("Experience")
        for idx, s in experience_sections:
            content = tailored.get(idx, s["text"])
            entries = _split_entries(content)
            for entry in entries:
                # Determine where the bullets begin so header lines (title /
                # company / date) before them get header styling.
                first_bullet_idx = next(
                    (i for i, l in enumerate(entry) if _is_bullet(l)), len(entry)
                )
                for ei, eline in enumerate(entry):
                    if _is_bullet(eline):
                        bullet_text = "• " + _strip_bullet(eline)
                        draw_wrapped(
                            bullet_text, x=_MARGIN_LEFT + _BULLET_INDENT,
                            hang_x=_MARGIN_LEFT + _HANGING_INDENT,
                            width=_TEXT_WIDTH - _BULLET_INDENT,
                            fontsize=_BODY_SIZE, fontname="helv", color=body_color,
                        )
                    elif ei == 0 and ei < first_bullet_idx:
                        # Job title
                        draw_wrapped(eline, x=_MARGIN_LEFT, hang_x=_MARGIN_LEFT,
                                     width=_TEXT_WIDTH, fontsize=9.5,
                                     fontname="helvetica-bold", color=body_color)
                    elif ei == 1 and ei < first_bullet_idx:
                        # Company
                        draw_wrapped(eline, x=_MARGIN_LEFT, hang_x=_MARGIN_LEFT,
                                     width=_TEXT_WIDTH, fontsize=_BODY_SIZE,
                                     fontname="helv", color=body_color)
                    else:
                        # Date / secondary line
                        draw_wrapped(eline, x=_MARGIN_LEFT, hang_x=_MARGIN_LEFT,
                                     width=_TEXT_WIDTH, fontsize=_BODY_SIZE,
                                     fontname="helv", color=light_color)
                state["y"] += _ENTRY_GAP

    # ─── Achievements Section ───
    achievement_sections = [
        (i, s) for i, s in enumerate(sections) if s["section_type"] == "achievements"
    ]
    for idx, s in achievement_sections:
        draw_section_header("Hackathons & CTFs")
        content = tailored.get(idx, s["text"])
        for line in content.split("\n"):
            if line.strip():
                draw_wrapped(line.strip(), x=_MARGIN_LEFT, hang_x=_MARGIN_LEFT,
                             width=_TEXT_WIDTH, fontsize=_BODY_SIZE,
                             fontname="helv", color=body_color)
        state["y"] += 10

    # ─── Education Section (at the bottom) ───
    if education_text:
        draw_section_header("Education")
        for line in education_text.split("\n"):
            if line.strip():
                draw_wrapped(line.strip(), x=_MARGIN_LEFT, hang_x=_MARGIN_LEFT,
                             width=_TEXT_WIDTH, fontsize=_BODY_SIZE,
                             fontname="helv", color=body_color)

    doc.save(str(output_path))
    doc.close()


# Shared font used for both measuring and rendering so wrapping matches output.
# "helv" maps to Helvetica, the same base-14 font used by insert_text below.
_MEASURE_FONT = fitz.Font("helv")


def _wrap_text(text: str, max_width: float, fontsize: float) -> list[str]:
    """Word-wrap ``text`` so each line renders within ``max_width`` points.

    Uses real font metrics (fitz.Font.text_length) rather than an approximate
    character-width estimate, so wrapped lines match the actually-rendered width.
    A single word that is wider than ``max_width`` cannot be split and is emitted
    on its own line.
    """
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        test = f"{current} {word}" if current else word
        if current and _MEASURE_FONT.text_length(test, fontsize) > max_width:
            lines.append(current)
            current = word
        else:
            current = test
    if current:
        lines.append(current)
    return lines


def _draw_wrapped(page, x: float, y: float, text: str, max_width: float, fontsize: float, fontname: str, color: tuple):
    """Draw text with word wrap. Returns the new y-cursor after drawing."""
    lines = _wrap_text(text, max_width, fontsize)
    for line in lines:
        page.insert_text(fitz.Point(x, y + 8), line, fontsize=fontsize, fontname=fontname, color=color)
        y += fontsize + 2
    return y


async def tailor_resume(
    job_url: str,
    job_description: str,
    options: dict,
    job_title: str = "",
    cost_tracker=None,
    refinement: str = "",
) -> dict:
    """Generate and validate a fresh, job-specific one-page resume PDF."""
    llm_settings = load_llm_settings()
    if not llm_settings.get("provider"):
        raise ValueError("No LLM configured")
    llm = create_llm(resume_llm_settings(llm_settings))
    if cost_tracker is not None:
        cost_tracker.register_llm("cv", llm)

    profile_markdown = load_profile_markdown()
    spec = await generate_resume_spec(
        llm=llm,
        profile_markdown=profile_markdown,
        job_description=job_description,
        job_title=job_title,
        refinement=refinement,
    )

    url_hash = _url_hash(job_url)
    artifact_dir = _get_job_artifact_dir(job_url)
    for existing_pdf in artifact_dir.glob("Candidate_*.pdf"):
        existing_pdf.unlink()
    output_pdf = artifact_dir / tailored_cv_filename(job_title)
    output_md = _get_tailored_dir() / f"{url_hash}.md"
    rendered_spec, validation = render_resume(spec, output_pdf)
    content = resume_spec_markdown(rendered_spec)
    output_md.write_text(content, encoding="utf-8")
    audit = resume_generation_audit(
        rendered_spec,
        profile_markdown=profile_markdown,
        job_title=job_title,
        job_description=job_description,
        model=RESUME_LLM_MODEL,
        reasoning_effort=DOCUMENT_LLM_REASONING_EFFORT,
    )
    audit_path = artifact_dir / "resume-generation-audit.json"
    audit_path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return {
        "path": str(output_pdf),
        "content": content,
        "sections_tailored": (
            3
            + bool(rendered_spec.summary)
            + bool(rendered_spec.projects)
            + bool(rendered_spec.achievements)
        ),
        "sections_total": 6,
        "validation": validation,
        "audit": audit,
        "audit_path": str(audit_path),
    }


async def refine_resume(job_url: str, job_description: str, instruction: str, options: dict) -> dict:
    """Refine an existing tailored resume with additional instructions."""
    return await tailor_resume(
        job_url,
        job_description,
        options,
        refinement=instruction,
    )


def get_tailored_content(job_url: str) -> Optional[dict]:
    """Get the tailored resume content for a job (if it exists)."""
    url_hash = _url_hash(job_url)
    md_path = _get_tailored_dir() / f"{url_hash}.md"
    pdf_path = get_tailored_resume_path(job_url)

    if not md_path.exists():
        return None

    return {
        "content": md_path.read_text(encoding="utf-8"),
        "path": pdf_path,
    }


def delete_tailored_resume(job_url: str) -> bool:
    """Delete the tailored resume files for a job."""
    url_hash = _url_hash(job_url)
    md_path = _get_tailored_dir() / f"{url_hash}.md"
    artifact_dir = _get_tailored_dir() / url_hash
    legacy_pdf = _get_tailored_dir() / f"{url_hash}.pdf"

    deleted = False
    if md_path.exists():
        md_path.unlink()
        deleted = True
    if legacy_pdf.exists():
        legacy_pdf.unlink()
        deleted = True
    if artifact_dir.is_dir():
        for artifact in artifact_dir.iterdir():
            if artifact.is_file():
                artifact.unlink()
                deleted = True
        try:
            artifact_dir.rmdir()
        except OSError:
            pass
    return deleted


def get_tailored_resume_path(job_url: str) -> Optional[str]:
    """Get the path to the tailored PDF for a job, or None if it doesn't exist."""
    url_hash = _url_hash(job_url)
    artifact_dir = _get_tailored_dir() / url_hash
    human_paths = sorted(artifact_dir.glob("Candidate_*.pdf"))
    if human_paths:
        return str(human_paths[0])
    legacy_path = _get_tailored_dir() / f"{url_hash}.pdf"
    if legacy_path.exists():
        return str(legacy_path)
    return None
