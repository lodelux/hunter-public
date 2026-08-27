"""Unit tests for resume compiling plus the retained legacy layout helpers.

The production path generates an evidence-linked ResumeSpec and a fresh,
validated one-page RenderCV PDF. The older extraction/replacement helpers remain
covered because they still support parsing and compatibility behavior.

Testing strategy:
- Pure helpers (_url_hash, _wrap_text, _identify_tailorable_sections) tested directly.
- _extract_sections tested against real, minimal PDFs built in-memory with fitz.
- The LLM is always mocked (create_llm + ainvoke) — NO network.
- File outputs land in the isolated tmp data dir via the ``data_dir`` fixture.
"""
import hashlib
import json
from pathlib import Path

import fitz
import pytest

import resume.tailor as tailor
from resume.compiler import (
    ResumeSpec,
    _city_level_location,
    _heading_line_positions,
    _missing_claims,
)
from resume.tailor import (
    _MEASURE_FONT,
    _BODY_SIZE,
    _TEXT_WIDTH,
    _BULLET_INDENT,
    _PAGE_HEIGHT,
    _PAGE_WIDTH,
    _extract_editable_regions,
    _extract_sections,
    _fit_bullet_lines,
    _fit_text_lines,
    _generate_fresh_pdf,
    _generate_tailored_text,
    _identify_tailorable_sections,
    _is_bullet,
    _is_entry_header,
    _replace_text_in_place,
    _split_entries,
    _strip_bullet,
    _url_hash,
    _wrap_text,
    delete_tailored_resume,
    get_tailored_content,
    get_tailored_resume_path,
    refine_resume,
    tailor_resume,
)


# ── PDF builders ─────────────────────────────────────────────────────────────

def _make_pdf(path: Path, blocks: list[tuple[float, float, str]]):
    """Write a single-page PDF placing each (x, y, text) string as a block.

    fitz groups text into blocks; placing each string far apart vertically keeps
    them in distinct blocks so _extract_sections can treat them independently.
    """
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    for x, y, text in blocks:
        page.insert_text(fitz.Point(x, y), text, fontsize=11)
    doc.save(str(path))
    doc.close()


def test_heading_positions_ignore_heading_words_inside_prose():
    text = """Profile
Software engineer with formal software engineering education.
Experience
Built production systems.
Education
MSc in Computer Science Engineering
Skills
Python, TypeScript
"""

    assert _heading_line_positions(
        text, ["profile", "experience", "education", "skills"]
    ) == [0, 2, 4, 6]


def _resume_blocks() -> list[tuple[float, float, str]]:
    """A canonical resume with skills, experience, education sections."""
    return [
        (40, 60, "Skills"),
        (40, 90, "Python, AWS, Docker"),
        (40, 140, "Experience"),
        (40, 170, "Senior Engineer"),
        (40, 195, "Acme Corp"),
        (40, 220, "- Built scalable systems"),
        (40, 280, "Education"),
        (40, 310, "BS Computer Science, MIT"),
    ]


@pytest.fixture
def resume_pdf(tmp_path):
    p = tmp_path / "resume.pdf"
    _make_pdf(p, _resume_blocks())
    return p


# ── Pure helpers ─────────────────────────────────────────────────────────────

class TestUrlHash:
    def test_deterministic_and_truncated(self):
        h = _url_hash("https://example.com/job/1")
        assert h == hashlib.md5(b"https://example.com/job/1").hexdigest()[:12]
        assert len(h) == 12

    def test_distinct_urls_distinct_hashes(self):
        assert _url_hash("a") != _url_hash("b")


class TestCompilerPrivacy:
    def test_location_removes_street_and_postal_address(self):
        assert _city_level_location(
            "Bornholmer Str. 80A, Berlin, Berlin, 10439, Germany"
        ) == "Berlin, Germany"


class TestCompilerPdfTextValidation:
    def test_accepts_claim_from_native_order_when_sorted_order_interleaves_date(self):
        assert _missing_claims(
            ["Personal Project"],
            "Personal Jan 2026 - present Project",
            "Personal Project Jan 2026 - present",
        ) == []

    def test_rejects_claim_missing_from_every_extraction_order(self):
        assert _missing_claims(
            ["Personal Project"],
            "Hunter Jan 2026 - present",
            "Hunter Jan 2026 - present",
        ) == ["Personal Project"]


class TestWrapText:
    def test_short_text_single_line(self):
        assert _wrap_text("hello world", 500, 9) == ["hello world"]

    def test_wraps_long_text(self):
        text = "word " * 50
        lines = _wrap_text(text.strip(), 100, 9)
        assert len(lines) > 1
        assert all(line.strip() for line in lines)

    def test_empty_text(self):
        assert _wrap_text("", 100, 9) == []

    def test_single_word_longer_than_width_still_emitted(self):
        # A word wider than max_chars cannot be split; it occupies its own line.
        lines = _wrap_text("supercalifragilisticexpialidocious", 20, 9)
        assert lines == ["supercalifragilisticexpialidocious"]


class TestIdentifyTailorableSections:
    SECTIONS = [
        {"section_type": "skills", "text": "s"},
        {"section_type": "overview", "text": "o"},
        {"section_type": "experience", "text": "e"},
        {"section_type": "education", "text": "edu"},
    ]

    @pytest.mark.parametrize("options,expected", [
        ({"skills": True}, ["skills"]),
        ({"overview": True}, ["overview"]),
        ({"experience": True}, ["experience"]),
        ({"skills": True, "experience": True}, ["skills", "experience"]),
        ({}, []),
        ({"skills": False}, []),
        ({"education": True}, []),  # education is never tailorable
    ])
    def test_filtering(self, options, expected):
        result = _identify_tailorable_sections(self.SECTIONS, options)
        assert [s["section_type"] for s in result] == expected


class TestInPlaceLayoutHelpers:
    def test_fit_text_lines_fails_closed_when_text_overflows(self):
        font = fitz.Font("helv")
        assert _fit_text_lines("short text", font, 11, 200, 1) == ["short text"]
        assert _fit_text_lines("too many words for one narrow line", font, 11, 40, 1) is None

    def test_fit_bullet_lines_supports_a_different_bullet_count(self):
        font = fitz.Font("helv")
        fitted = _fit_bullet_lines(
            "First result\nSecond result\nThird result",
            font,
            11,
            200,
            4,
        )
        assert fitted == (
            ["First result", "Second result", "Third result"],
            [0, 1, 2],
        )
        assert _fit_bullet_lines(
            "One\nTwo\nThree\nFour\nFive",
            font,
            11,
            200,
            4,
        ) is None

    def test_editable_regions_include_mixed_style_bullets_but_not_titles(self):
        regular = {
            "text": "Built reliable systems",
            "bbox": (160, 100, 300, 112),
            "page": 0,
            "lines": [{
                "text": "Built reliable systems",
                "bbox": (160, 100, 300, 112),
                "spans": [{
                    "text": "Built reliable systems",
                    "bbox": (160, 100, 300, 112),
                    "origin": (160, 110),
                    "font": "Helvetica",
                    "size": 11,
                    "color": 0,
                }],
            }],
            "bullet_lines": [0],
        }
        title = {
            **regular,
            "text": "Software Engineer",
            "bbox": (145, 80, 250, 92),
            "bullet_lines": [],
        }
        mixed = {
            **regular,
            "text": "Built 3000 users",
            "bbox": (160, 120, 260, 132),
            "lines": [{
                "text": "Built 3000 users",
                "bbox": (160, 120, 260, 132),
                "spans": [
                    {
                        "text": "Built ",
                        "bbox": (160, 120, 190, 132),
                        "origin": (160, 130),
                        "font": "Helvetica",
                        "size": 11,
                        "color": 0,
                    },
                    {
                        "text": "3000 users",
                        "bbox": (190, 120, 260, 132),
                        "origin": (190, 130),
                        "font": "Helvetica-Bold",
                        "size": 11,
                        "color": 0,
                    },
                ],
            }],
            "bullet_lines": [0],
        }
        sections = [{
            "section_type": "experience",
            "content_blocks": [title, regular, mixed],
        }]

        regions = _extract_editable_regions(sections, {"experience": True})

        assert [region["text"] for region in regions] == [
            "Built reliable systems",
            "Built 3000 users",
        ]
        assert all(region["region_kind"] == "bullet_group" for region in regions)

    def test_vector_bullets_become_one_flexible_bullet_group(self, tmp_path):
        path = tmp_path / "vector-bullets.pdf"
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text(fitz.Point(40, 60), "Experience", fontsize=11)
        page.insert_text(fitz.Point(145, 85), "Software Engineer", fontsize=11)
        page.insert_text(fitz.Point(160, 110), "Built reliable systems", fontsize=11)
        page.insert_text(fitz.Point(160, 124), "Improved test coverage", fontsize=11)
        page.draw_circle(fitz.Point(150, 106), 2, fill=(0, 0, 0))
        page.draw_circle(fitz.Point(150, 120), 2, fill=(0, 0, 0))
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        regions = _extract_editable_regions(
            _extract_sections(doc),
            {"experience": True},
        )
        doc.close()

        assert len(regions) == 1
        assert regions[0]["region_kind"] == "bullet_group"
        assert regions[0]["text"] == (
            "Built reliable systems\nImproved test coverage"
        )

    def test_expanded_regions_cover_titles_achievements_and_skills(self, tmp_path):
        path = tmp_path / "expanded.pdf"
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text(
            fitz.Point(190, 30),
            "Software Engineer - Remote",
            fontsize=14,
        )
        page.insert_text(fitz.Point(40, 60), "Work Experience", fontsize=11)
        page.insert_text(
            fitz.Point(145, 90),
            "Software Engineer",
            fontsize=11,
            fontname="hebo",
        )
        page.insert_text(fitz.Point(450, 90), "Since 2025", fontsize=11)
        page.insert_text(fitz.Point(145, 110), "Acme, Remote", fontsize=11)
        page.insert_text(fitz.Point(160, 130), "Built reliable systems", fontsize=11)
        page.draw_circle(fitz.Point(150, 126), 2, fill=(0, 0, 0))
        page.insert_text(fitz.Point(40, 180), "Hackathons & CTFs", fontsize=11)
        page.insert_text(
            fitz.Point(145, 210),
            "Security Finalist",
            fontsize=11,
            fontname="hebo",
        )
        page.insert_text(fitz.Point(500, 210), "2026", fontsize=11)
        page.insert_text(fitz.Point(145, 230), "Placed in a live CTF", fontsize=11)
        page.insert_text(
            fitz.Point(40, 280),
            "Languages &\nTechnologies",
            fontsize=11,
        )
        page.insert_text(
            fitz.Point(145, 320),
            "Python, AWS",
            fontsize=11,
            fontname="hebo",
        )
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        regions = _extract_editable_regions(
            _extract_sections(doc),
            {
                "experience": True,
                "title": True,
                "achievements": True,
                "skills": True,
            },
        )
        doc.close()

        kinds = [region["region_kind"] for region in regions]
        assert "profile_title" in kinds
        assert "job_title" in kinds
        assert "bullet_group" in kinds
        assert "achievement_title" in kinds
        assert "achievement_text" in kinds
        assert "skills" in kinds
        skills_header = next(
            region for region in regions if region["region_kind"] == "section_header"
        )
        assert skills_header["fixed_text"] == "SKILLS"

    def test_removing_profile_title_preserves_name_location(self, tmp_path):
        source = tmp_path / "headline.pdf"
        output = tmp_path / "without-headline.pdf"
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text(
            fitz.Point(190, 25),
            "Jordan Rivera - Berlin, CET",
            fontsize=14,
            fontname="hebo",
        )
        page.insert_text(
            fitz.Point(190, 50),
            "Software Engineer - Remote",
            fontsize=14,
        )
        page.insert_text(fitz.Point(40, 80), "Experience", fontsize=11)
        page.insert_text(fitz.Point(160, 110), "- Built systems", fontsize=11)
        doc.save(source)
        doc.close()

        doc = fitz.open(source)
        regions = _extract_editable_regions(
            _extract_sections(doc),
            {"title": True},
        )
        headline_index = next(
            index
            for index, region in enumerate(regions)
            if region["region_kind"] == "profile_title"
        )
        applied = _replace_text_in_place(
            doc,
            regions,
            {headline_index: ""},
            output,
        )
        doc.close()

        assert applied == {headline_index: ""}
        result = fitz.open(output)
        output_text = result[0].get_text()
        assert "Software Engineer - Remote" not in output_text
        assert "Jordan Rivera - Berlin, CET" in output_text
        result.close()


# ── _extract_sections against real PDFs ──────────────────────────────────────

class TestExtractSections:
    def test_extracts_known_sections(self, resume_pdf):
        doc = fitz.open(str(resume_pdf))
        sections = _extract_sections(doc)
        doc.close()
        types = [s["section_type"] for s in sections]
        assert "skills" in types
        assert "experience" in types
        assert "education" in types

    def test_section_has_content_and_metadata(self, resume_pdf):
        doc = fitz.open(str(resume_pdf))
        sections = _extract_sections(doc)
        doc.close()
        skills = next(s for s in sections if s["section_type"] == "skills")
        assert "Python" in skills["text"]
        assert skills["char_count"] == len(skills["text"])
        assert len(skills["bbox"]) == 4
        assert skills["content_blocks"]

    def test_no_headers_yields_no_sections(self, tmp_path):
        p = tmp_path / "plain.pdf"
        _make_pdf(p, [(40, 60, "Just some plain text"), (40, 120, "More text here")])
        doc = fitz.open(str(p))
        sections = _extract_sections(doc)
        doc.close()
        assert sections == []

    def test_header_with_no_content_is_dropped(self, tmp_path):
        # A trailing header with no following content blocks is not emitted.
        p = tmp_path / "trailing.pdf"
        _make_pdf(p, [
            (40, 60, "Skills"),
            (40, 90, "Python"),
            (40, 700, "Education"),  # header but nothing after it
        ])
        doc = fitz.open(str(p))
        sections = _extract_sections(doc)
        doc.close()
        types = [s["section_type"] for s in sections]
        assert "skills" in types
        assert "education" not in types

    def test_header_synonyms_map_to_canonical_type(self, tmp_path):
        p = tmp_path / "syn.pdf"
        _make_pdf(p, [
            (40, 60, "Summary"),
            (40, 90, "A seasoned engineer"),
            (40, 150, "Technologies"),
            (40, 180, "Go, Rust"),
        ])
        doc = fitz.open(str(p))
        sections = _extract_sections(doc)
        doc.close()
        types = {s["section_type"] for s in sections}
        assert "overview" in types  # "Summary"
        assert "skills" in types    # "Technologies"

    def test_multiline_headers_and_visual_order(self, tmp_path):
        p = tmp_path / "designed.pdf"
        # Body blocks are deliberately drawn before the visually preceding
        # sidebar headers, matching the configured CV's internal PDF order.
        _make_pdf(p, [
            (145, 90, "Software Engineer"),
            (145, 120, "- Built reliable systems"),
            (145, 210, "Python, AWS, Docker"),
            (40, 60, "WORK\nEXPERIENCE"),
            (40, 180, "LANGUAGES &\nTECHNOLOGIES"),
        ])
        doc = fitz.open(str(p))
        sections = _extract_sections(doc)
        doc.close()

        assert [s["section_type"] for s in sections] == ["experience", "skills"]
        assert "Software Engineer" in sections[0]["text"]
        assert "Python" in sections[1]["text"]

    def test_achievements_header_ends_experience_section(self, tmp_path):
        p = tmp_path / "achievements.pdf"
        _make_pdf(p, [
            (40, 60, "Work Experience"),
            (145, 90, "Software Engineer"),
            (40, 150, "HACKATHONS &\nCTFS"),
            (145, 180, "Wonderland CTF"),
            (40, 240, "Education"),
            (145, 270, "MSc Computer Science"),
        ])
        doc = fitz.open(str(p))
        sections = _extract_sections(doc)
        doc.close()

        assert [s["section_type"] for s in sections] == [
            "experience", "achievements", "education"
        ]
        assert "Wonderland CTF" not in sections[0]["text"]
        assert "Wonderland CTF" in sections[1]["text"]

    def test_empty_pdf_yields_no_sections(self, tmp_path):
        p = tmp_path / "empty.pdf"
        doc = fitz.open()
        doc.new_page(width=612, height=792)
        doc.save(str(p))
        doc.close()
        doc = fitz.open(str(p))
        assert _extract_sections(doc) == []
        doc.close()


# ── _generate_tailored_text (LLM mocked) ─────────────────────────────────────

class _FakeLLM:
    """Stand-in for a browser_use chat model. Records the prompt it receives."""

    def __init__(self, response):
        self._response = response
        self.last_messages = None
        self.last_output_format = None

    async def ainvoke(self, messages, output_format=None):
        self.last_messages = messages
        self.last_output_format = output_format
        if output_format is not None:
            completion = output_format.model_validate_json(self._response.completion)
            return _Completion(completion)
        return self._response


class _Completion:
    def __init__(self, completion):
        self.completion = completion


class _Content:
    def __init__(self, content):
        self.content = content


def _patch_llm(monkeypatch, response, settings=None):
    """Patch load_llm_settings + create_llm so the engine uses a fake LLM."""
    fake = _FakeLLM(response)
    monkeypatch.setattr(tailor, "load_llm_settings", lambda: settings or {"provider": "anthropic"})
    monkeypatch.setattr(
        tailor,
        "load_profile_markdown",
        lambda: "# Professional Profile\nBackend developer\n# Skills\nPython",
    )
    monkeypatch.setattr(tailor, "create_llm", lambda s: fake)
    return fake


SECTIONS = [
    {"section_type": "skills", "text": "Python, AWS", "char_count": 11},
    {"section_type": "overview", "text": "Engineer with 5 years", "char_count": 21},
]


class TestGenerateTailoredText:
    async def test_profile_title_prompt_is_constrained_and_may_be_empty(
        self,
        monkeypatch,
    ):
        sections = [{
            "section_type": "headline",
            "region_kind": "profile_title",
            "text": "Software Engineer",
            "char_count": 70,
            "lines": [{}],
        }]
        fake = _patch_llm(monkeypatch, _Completion('{"0": ""}'))

        out = await _generate_tailored_text(sections, "Chief Financial Officer", {})

        assert out == {0: ""}
        prompt = fake.last_messages[0].content
        assert "Backend Developer" in prompt
        assert "Penetration Tester" in prompt
        assert "Smart Contract Auditor" in prompt
        assert "Blockchain Security Engineer" in prompt
        assert "Software Security Engineer" in prompt
        assert "Blockchain Software Engineer" in prompt
        assert "Frontend Developer" not in prompt
        assert "return an empty string" in prompt
        assert "Never copy or approximate an unrelated listing title" in prompt

    async def test_happy_path_completion_attr(self, monkeypatch):
        resp = _Completion(json.dumps({"0": "Python, GCP", "1": "Cloud engineer"}))
        fake = _patch_llm(monkeypatch, resp)
        out = await _generate_tailored_text(SECTIONS, "Cloud role", {})
        assert out == {0: "Python, GCP", 1: "Cloud engineer"}
        # Prompt was assembled with the section originals + char limits.
        prompt = fake.last_messages[0].content
        assert "Python, AWS" in prompt
        assert "max 11 characters" in prompt
        assert "Cloud role" in prompt
        assert "CANDIDATE MARKDOWN PROFILE:" in prompt
        assert "Backend developer" in prompt

    async def test_uses_content_attr_when_no_completion(self, monkeypatch):
        resp = _Content(json.dumps({"0": "ok", "1": "fine"}))
        _patch_llm(monkeypatch, resp)
        out = await _generate_tailored_text(SECTIONS, "jd", {})
        assert out == {0: "ok", 1: "fine"}

    async def test_falls_back_to_str_for_unknown_response(self, monkeypatch):
        # An object lacking completion/content (and non-str content) → str(response).
        resp = json.dumps({"0": "x", "1": "y"})  # plain string, no attrs
        _patch_llm(monkeypatch, resp)
        out = await _generate_tailored_text(SECTIONS, "jd", {})
        assert out == {0: "x", 1: "y"}

    async def test_oversized_output_is_truncated(self, monkeypatch):
        long = "z" * 100
        resp = _Completion(json.dumps({"0": long, "1": "ok"}))
        _patch_llm(monkeypatch, resp)
        out = await _generate_tailored_text(SECTIONS, "jd", {})
        assert len(out[0]) == 11  # truncated to char_count
        assert out[0] == "z" * 11

    async def test_json_embedded_in_prose_is_extracted(self, monkeypatch):
        resp = _Completion('Here you go: {"0": "a", "1": "b"} — done!')
        _patch_llm(monkeypatch, resp)
        out = await _generate_tailored_text(SECTIONS, "jd", {})
        assert out == {0: "a", 1: "b"}

    async def test_missing_section_keys_omitted(self, monkeypatch):
        resp = _Completion(json.dumps({"0": "only zero"[:11]}))
        _patch_llm(monkeypatch, resp)
        out = await _generate_tailored_text(SECTIONS, "jd", {})
        assert 0 in out
        assert 1 not in out

    async def test_no_provider_raises(self, monkeypatch):
        monkeypatch.setattr(tailor, "load_llm_settings", lambda: {"provider": ""})
        with pytest.raises(ValueError, match="No LLM configured"):
            await _generate_tailored_text(SECTIONS, "jd", {})

    async def test_non_json_response_raises(self, monkeypatch):
        resp = _Completion("I cannot help with that.")
        _patch_llm(monkeypatch, resp)
        with pytest.raises(ValueError, match="did not return valid JSON"):
            await _generate_tailored_text(SECTIONS, "jd", {})

    async def test_refinement_note_included_in_prompt(self, monkeypatch):
        resp = _Completion(json.dumps({"0": "x", "1": "y"}))
        fake = _patch_llm(monkeypatch, resp)
        await _generate_tailored_text(SECTIONS, "jd", {}, refinement="Be concise")
        assert "ADDITIONAL INSTRUCTION: Be concise" in fake.last_messages[0].content

    async def test_long_job_description_is_included_in_full(self, monkeypatch):
        resp = _Completion(json.dumps({"0": "x", "1": "y"}))
        fake = _patch_llm(monkeypatch, resp)
        jd = "Q" * 5000
        await _generate_tailored_text(SECTIONS, jd, {})
        prompt = fake.last_messages[0].content
        assert jd in prompt


# ── tailor_resume orchestration (LLM mocked, real PDF) ───────────────────────

class TestTailorResume:
    @staticmethod
    def _resume_spec_response() -> _Completion:
        return _Completion(json.dumps({
            "strategy": {
                "positioning": "Present a backend engineer with concrete Python delivery evidence.",
                "priorities": [{
                    "requirement_ids": ["J001"],
                    "evidence_ids": ["P004", "P005", "P007"],
                    "decision": "Lead with backend delivery and the requested Python stack.",
                }],
            },
            "candidate": {
                "name": "Jane Doe",
                "headline": "Backend Engineer",
                "headline_requirement_ids": ["J001"],
                "location": "Berlin",
                "email": "j@x.com",
                "phone": "",
                "website": "",
            },
            "summary": {
                "text": "Backend engineer building scalable cloud systems.",
                "evidence_ids": ["P004"],
                "requirement_ids": ["J001"],
                "relevance": 95,
            },
            "experience": [{
                "company": "Acme Corp",
                "position": "Senior Engineer",
                "start_date": "2020-01",
                "end_date": "2024-12",
                "location": "Berlin",
                "relevance": 95,
                "evidence_ids": ["P006"],
                "requirement_ids": ["J001"],
                "highlights": [{
                    "text": "Built scalable systems with Python and AWS.",
                    "evidence_ids": ["P007"],
                    "requirement_ids": ["J001"],
                    "relevance": 95,
                }],
            }],
            "projects": [{
                "name": "Queue Guard",
                "summary": "Personal Project",
                "date": "2025",
                "location": "Berlin",
                "relevance": 90,
                "evidence_ids": ["P008"],
                "requirement_ids": ["J001"],
                "highlights": [{
                    "text": "Built reliable queue automation with Python.",
                    "evidence_ids": ["P009"],
                    "requirement_ids": ["J001"],
                    "relevance": 90,
                }],
            }],
            "achievements": [],
            "education": [{
                "institution": "MIT",
                "area": "Computer Science",
                "degree": "Bachelor of Science",
                "start_date": "2016-09",
                "end_date": "2020-06",
                "location": "",
                "relevance": 80,
                "evidence_ids": ["P010"],
                "requirement_ids": ["J001"],
                "highlights": [{
                    "text": "Relevant exam: Distributed Systems.",
                    "evidence_ids": ["P011"],
                    "requirement_ids": ["J001"],
                    "relevance": 85,
                }],
            }],
            "skills": [{
                "label": "Engineering",
                "details": "Python, AWS, Docker",
                "evidence_ids": ["P005"],
                "requirement_ids": ["J001"],
                "relevance": 95,
            }],
        }))

    def _configure(self, monkeypatch, data_dir, llm_resp,
                   provider="anthropic", profile=None):
        """Wire up profile/LLM for an end-to-end tailor_resume call."""
        monkeypatch.setattr(tailor, "load_llm_settings", lambda: {"provider": provider})
        fake = _FakeLLM(llm_resp)
        fake.created_with_settings = []
        monkeypatch.setattr(
            tailor,
            "create_llm",
            lambda settings: fake.created_with_settings.append(settings) or fake,
        )
        profile_text = (
            "Jane Doe\nj@x.com\nBerlin\nBackend engineer\n"
            "Python, AWS, Docker\nSenior Engineer at Acme Corp\n"
            "Built scalable systems\nQueue Guard Personal Project\n"
            "reliable queue automation\nBS Computer Science, MIT\nDistributed Systems"
        )
        monkeypatch.setattr(tailor, "load_profile_markdown", lambda: profile_text)
        return fake

    def test_resume_spec_deduplicates_compact_evidence_citations(self):
        payload = json.loads(self._resume_spec_response().completion)
        payload["summary"]["evidence_ids"] = ["P004"] * 8
        payload["education"][0]["evidence_ids"] = ["P010"] * 5

        spec = ResumeSpec.model_validate(payload)

        assert spec.summary.evidence_ids == ["P004"]
        assert spec.education[0].evidence_ids == ["P010"]

    def test_resume_spec_unpacks_model_concatenated_citation_ids(self):
        payload = json.loads(self._resume_spec_response().completion)
        payload["summary"]["evidence_ids"] = ["P004','P005','P007"]
        payload["summary"]["requirement_ids"] = ["J001】【、】【J002"]

        spec = ResumeSpec.model_validate(payload)

        assert spec.summary.evidence_ids == ["P004", "P005", "P007"]
        assert spec.summary.requirement_ids == ["J001", "J002"]

    async def test_end_to_end_produces_pdf_and_md(self, monkeypatch, data_dir):
        resp = self._resume_spec_response()
        fake = self._configure(monkeypatch, data_dir, resp,
                               profile={"name": "Jane", "email": "j@x.com"})
        result = await tailor_resume(
            "https://job/1",
            "Python role",
            {"skills": True, "experience": True},
            "Backend Engineer (m/w/d)",
        )
        assert Path(result["path"]).exists()
        assert result["path"].endswith(
            "/Candidate_Backend_Engineer_m_w_d.pdf"
        )
        assert result["sections_total"] >= 1
        assert result["sections_tailored"] >= 1
        output = fitz.open(result["path"])
        assert len(output) == 1
        assert output[0].rect.width == pytest.approx(595.28, abs=0.1)
        assert "pdfaid:part" in output.get_xml_metadata()
        output_text = output[0].get_text()
        output.close()
        assert "Python, AWS, Docker" in output_text
        assert "Senior Engineer" in output_text
        assert "Jan 2020 - Dec 2024" in output_text
        assert "Sept 2016 - June 2020" in output_text
        assert "Selected Projects" in output_text
        assert "Queue Guard" in output_text
        assert "2025" in output_text
        assert "Jan 2025" not in output_text
        assert "present" not in output_text.casefold()
        assert "MIT" in output_text
        assert "BSc in Computer Science" in output_text
        assert "Relevant exam: Distributed Systems" in output_text
        assert "Bachelor of Science" not in output_text
        assert result["validation"]["page_count"] == 1
        artifact_dir = Path(result["path"]).parent
        assert (artifact_dir / "rendercv.yaml").is_file()
        assert (artifact_dir / "resume-spec.json").is_file()
        audit = json.loads((artifact_dir / "resume-generation-audit.json").read_text())
        assert audit["model"] == "gpt-5.6-sol"
        assert audit["priorities"][0]["requirements"][0]["text"] == "Backend Engineer (m/w/d)"
        assert audit["priorities"][0]["evidence"][0]["text"] == "Backend engineer"
        assert audit["selections"][0]["requirements"][0]["id"] == "J001"
        assert Path(result["path"]).with_suffix(".typ").is_file()
        # Markdown sidecar written and retrievable.
        content = get_tailored_content("https://job/1")
        assert content is not None
        assert "## SKILLS" in content["content"] or "## EXPERIENCE" in content["content"]
        assert "## SELECTED PROJECTS" in content["content"]
        assert len(fake.last_messages) == 2
        system_prompt = fake.last_messages[0].content
        user_prompt = fake.last_messages[1].content
        assert "projects, never in experience" in system_prompt
        assert "ORIGINAL CV" not in system_prompt
        assert "only candidate evidence inventory" in system_prompt
        assert "Never combine URLs" in system_prompt
        assert "job-specific ATS keyword section" in system_prompt
        assert "Education may have" in system_prompt
        assert "Make the CV result-oriented" in system_prompt
        assert "without inventing an impact" in system_prompt
        assert "Jane Doe" in system_prompt
        assert "Python role" not in system_prompt
        assert "Python role" in user_prompt
        assert "Jane Doe" not in user_prompt
        assert fake.last_output_format is ResumeSpec
        assert fake.created_with_settings[0]["provider"] == "openai"
        assert fake.created_with_settings[0]["openai"]["model"] == "gpt-5.6-sol"
        assert fake.created_with_settings[0]["openai"]["reasoning_effort"] == "medium"
        assert fake.created_with_settings[0]["openai"]["max_completion_tokens"] == 20_000

    async def test_options_do_not_limit_fresh_resume(self, monkeypatch, data_dir):
        resp = self._resume_spec_response()
        self._configure(monkeypatch, data_dir, resp)
        result = await tailor_resume("u", "Python role", {})
        assert Path(result["path"]).is_file()

    async def test_structured_output_rejects_invalid_resume_spec(
        self, monkeypatch, data_dir
    ):
        resp = _Completion(json.dumps({"99": "irrelevant"}))
        self._configure(monkeypatch, data_dir, resp)
        with pytest.raises(ValueError, match="validation errors for ResumeSpec"):
            await tailor_resume(
                "u", "Python role", {"skills": True, "experience": True}
            )

    async def test_unsupported_evidence_raises(self, monkeypatch, data_dir):
        response = self._resume_spec_response()
        payload = json.loads(response.completion)
        payload["experience"][0]["highlights"][0]["evidence_ids"] = ["P999"]
        self._configure(monkeypatch, data_dir, _Completion(json.dumps(payload)))
        with pytest.raises(ValueError, match="evidence not present"):
            await tailor_resume("u", "Python role", {"skills": True})

    async def test_unsupported_job_excerpt_raises(self, monkeypatch, data_dir):
        response = self._resume_spec_response()
        payload = json.loads(response.completion)
        payload["skills"][0]["requirement_ids"] = ["J999"]
        self._configure(monkeypatch, data_dir, _Completion(json.dumps(payload)))
        with pytest.raises(ValueError, match="text not present in job listing"):
            await tailor_resume("u", "Python role", {"skills": True})

    async def test_profile_load_failure_fails_closed(self, monkeypatch, data_dir):
        resp = self._resume_spec_response()
        self._configure(monkeypatch, data_dir, resp)
        def _boom():
            raise RuntimeError("profile broken")
        monkeypatch.setattr(tailor, "load_profile_markdown", _boom)
        with pytest.raises(RuntimeError, match="profile broken"):
            await tailor_resume("https://job/2", "Python role", {"skills": True})

    async def test_refine_resume_delegates(self, monkeypatch, data_dir):
        resp = self._resume_spec_response()
        fake = self._configure(monkeypatch, data_dir, resp,
                               profile={"name": "Jane"})
        result = await refine_resume(
            "https://job/3", "Python role", "make it punchy", {"skills": True}
        )
        assert Path(result["path"]).exists()
        assert "ADDITIONAL USER INSTRUCTION" not in fake.last_messages[0].content
        assert "ADDITIONAL USER INSTRUCTION" in fake.last_messages[1].content
        assert "make it punchy" in fake.last_messages[1].content


# ── Content lifecycle: get / delete / path ───────────────────────────────────

class TestContentLifecycle:
    def test_get_content_missing_returns_none(self, data_dir):
        assert get_tailored_content("https://absent") is None

    def test_get_path_missing_returns_none(self, data_dir):
        assert get_tailored_resume_path("https://absent") is None

    def test_delete_missing_returns_false(self, data_dir):
        assert delete_tailored_resume("https://absent") is False

    async def test_get_and_delete_after_tailoring(self, monkeypatch, data_dir):
        resp = TestTailorResume._resume_spec_response()
        TestTailorResume()._configure(monkeypatch, data_dir, resp)
        url = "https://job/lifecycle"
        await tailor_resume(url, "Python role", {"skills": True})

        assert get_tailored_resume_path(url) is not None
        content = get_tailored_content(url)
        assert content is not None and content["path"] is not None

        assert delete_tailored_resume(url) is True
        assert get_tailored_content(url) is None
        assert get_tailored_resume_path(url) is None
        assert not (tailor._get_tailored_dir() / _url_hash(url)).exists()
        # Second delete is a no-op.
        assert delete_tailored_resume(url) is False

    def test_get_content_without_pdf_returns_none_path(self, data_dir):
        # Markdown present but no PDF → path is None.
        from resume.tailor import _get_tailored_dir
        url = "https://job/mdonly"
        md = _get_tailored_dir() / f"{_url_hash(url)}.md"
        md.write_text("## SKILLS\n\nPython", encoding="utf-8")
        content = get_tailored_content(url)
        assert content is not None
        assert content["path"] is None
        assert "Python" in content["content"]


# ── Font-metric wrapping ─────────────────────────────────────────────────────

class TestFontMetricWrapping:
    """_wrap_text now measures with real font metrics, not a char-width guess."""

    def test_every_wrapped_line_fits_real_width(self):
        # A long realistic sentence wrapped to a narrow column.
        text = (
            "Led a cross functional team of engineers to design and ship a "
            "highly scalable distributed data processing platform handling "
            "billions of events per day across multiple AWS regions reliably."
        )
        width = 200.0
        lines = _wrap_text(text, width, _BODY_SIZE)
        assert len(lines) > 1
        for line in lines:
            assert _MEASURE_FONT.text_length(line, _BODY_SIZE) <= width

    def test_wrapping_is_tighter_than_old_estimate(self):
        # With real metrics the produced lines should pack close to the width
        # without exceeding it (the old 0.52 char-width estimate could overflow
        # or under-fill). Verify no line exceeds and at least one line uses
        # most of the available width.
        text = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda"
        width = 150.0
        lines = _wrap_text(text, width, _BODY_SIZE)
        assert all(_MEASURE_FONT.text_length(l, _BODY_SIZE) <= width for l in lines)
        assert max(_MEASURE_FONT.text_length(l, _BODY_SIZE) for l in lines) > width * 0.6

    def test_word_wider_than_width_emitted_alone(self):
        # A single token that overflows is still emitted (cannot be split).
        long = "x" * 300
        lines = _wrap_text(long, 80.0, _BODY_SIZE)
        assert lines == [long]


# ── Bullet / entry detection ─────────────────────────────────────────────────

class TestBulletDetection:
    @pytest.mark.parametrize("line", [
        "· dotted bullet",
        "- dash bullet",
        "• round bullet",
        "* asterisk bullet",
        "– en dash bullet",
        "— em dash bullet",
        "‣ triangle bullet",
        "● filled circle",
        "1. numbered bullet",
        "2) numbered paren",
        "  • indented bullet",
        "a. lettered",
    ])
    def test_recognizes_bullet_styles(self, line):
        assert _is_bullet(line) is True

    @pytest.mark.parametrize("line", [
        "Senior Engineer",
        "Acme Corp",
        "2019 - 2022",
        "",
        "Just a sentence with no marker.",
    ])
    def test_non_bullets_rejected(self, line):
        assert _is_bullet(line) is False

    @pytest.mark.parametrize("line,expected", [
        ("• built things", "built things"),
        ("- did work", "did work"),
        ("1. first item", "first item"),
        ("2) second item", "second item"),
        ("plain text", "plain text"),
    ])
    def test_strip_bullet(self, line, expected):
        assert _strip_bullet(line) == expected

    @pytest.mark.parametrize("line", [
        "2019 - 2022",
        "Jan 2019 – Present",
        "Senior Engineer — Acme Corp",
        "Backend Engineer @ Globex",
        "Developer at Initech",
    ])
    def test_entry_header_detection(self, line):
        assert _is_entry_header(line) is True

    @pytest.mark.parametrize("line", [
        "• a bullet is not a header",
        "Just a normal sentence here",
        "",
    ])
    def test_non_entry_headers(self, line):
        assert _is_entry_header(line) is False


class TestSplitEntries:
    def test_blank_line_separates_entries(self):
        content = "Title A\n• did x\n\nTitle B\n• did y"
        entries = _split_entries(content)
        assert len(entries) == 2
        assert entries[0][0] == "Title A"
        assert entries[1][0] == "Title B"

    def test_new_header_after_bullets_starts_entry(self):
        # No blank line, but a non-bullet line after bullets begins a new entry.
        content = "Title A\n• did x\nTitle B\n• did y"
        entries = _split_entries(content)
        assert len(entries) == 2

    def test_entry_header_pattern_splits_without_bullets(self):
        # Bullet-less resume using "Title — Company" headers.
        content = "Engineer — Acme\nShipped a product\nEngineer — Globex\nBuilt a tool"
        entries = _split_entries(content)
        assert len(entries) == 2
        assert entries[0][0] == "Engineer — Acme"
        assert entries[1][0] == "Engineer — Globex"


# ── _generate_fresh_pdf layout / overflow ────────────────────────────────────

def _make_sections(skills_text="", experience_text=""):
    secs = []
    if skills_text:
        secs.append({"section_type": "skills", "text": skills_text,
                     "char_count": len(skills_text)})
    if experience_text:
        secs.append({"section_type": "experience", "text": experience_text,
                     "char_count": len(experience_text)})
    return secs


def _all_text_spans(doc):
    """Yield (page, span_dict) for every text span in the document."""
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    yield page, span


class TestGenerateFreshPdf:
    def test_no_text_drawn_off_page(self, tmp_path):
        long_exp = "\n".join(
            f"Engineer {i} — Company {i}\n• " + ("achievement detail " * 8)
            for i in range(40)
        )
        out = tmp_path / "out.pdf"
        _generate_fresh_pdf(_make_sections(experience_text=long_exp), {},
                            "Jane Doe", "jane@x.com", "BS CS, MIT", out)
        doc = fitz.open(str(out))
        try:
            for page, span in _all_text_spans(doc):
                x0, y0, x1, y1 = span["bbox"]
                assert y1 <= _PAGE_HEIGHT, f"text below page bottom: {y1}"
                assert x1 <= _PAGE_WIDTH + 1, f"text past right edge: {x1}"
                assert y0 >= 0 and x0 >= 0
        finally:
            doc.close()

    def test_enough_content_produces_multiple_pages(self, tmp_path):
        long_exp = "\n".join(
            f"Role {i} — Org {i}\n• " + ("did meaningful impactful work " * 6)
            for i in range(50)
        )
        out = tmp_path / "multi.pdf"
        _generate_fresh_pdf(_make_sections(experience_text=long_exp), {},
                            "Jane Doe", "jane@x.com", "", out)
        doc = fitz.open(str(out))
        try:
            assert doc.page_count > 1
        finally:
            doc.close()

    def test_short_content_single_page(self, tmp_path):
        out = tmp_path / "short.pdf"
        _generate_fresh_pdf(
            _make_sections(skills_text="Python, AWS",
                           experience_text="Engineer — Acme\n• Built systems"),
            {}, "Jane", "jane@x.com", "BS CS", out,
        )
        doc = fitz.open(str(out))
        try:
            assert doc.page_count == 1
        finally:
            doc.close()

    def test_bullet_continuation_lines_share_hanging_indent(self, tmp_path):
        # A long bullet wraps; continuation lines align under the first line's
        # text, not back at the left margin.
        bullet = "• " + ("scaled the platform to handle massive traffic " * 6)
        out = tmp_path / "hang.pdf"
        _generate_fresh_pdf(_make_sections(experience_text=f"Role — Org\n{bullet}"),
                            {}, "Jane", "", "", out)
        doc = fitz.open(str(out))
        try:
            spans = [s for _, s in _all_text_spans(doc)
                     if "scaled" in s["text"] or "platform" in s["text"]]
            assert len(spans) > 1  # the bullet wrapped
            xs = sorted({round(s["bbox"][0]) for s in spans})
            # Bullet glyph line and continuation lines both indented past margin.
            assert all(x > 36 for x in xs)
        finally:
            doc.close()
