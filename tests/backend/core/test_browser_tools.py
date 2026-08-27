from pathlib import Path

import pytest

from core.browser_tools import (
    _choose_file_input,
    _resolve_upload_path,
    _score_file_input,
    build_application_tools,
)


def test_exact_file_field_name_beats_related_cv_parser():
    candidates = [
        {"name": "cv_lebenslauf", "label": "Lebenslauf hochladen"},
        {"name": "lebenslauf", "label": "Lebenslauf"},
        {"name": "anschreiben", "label": "Anschreiben / Komplette Unterlagen"},
    ]

    selected = _choose_file_input("Lebenslauf", candidates)

    assert selected["name"] == "lebenslauf"
    assert _score_file_input("Lebenslauf", selected) == 100


def test_file_field_matching_rejects_unrelated_uploads():
    candidates = [
        {"name": "foto", "label": "Foto"},
        {"name": "anlage5", "label": "Hochschulzeugnis"},
    ]

    try:
        _choose_file_input("Lebenslauf", candidates)
    except RuntimeError as error:
        assert "No file input matched" in str(error)
    else:
        raise AssertionError("an unrelated upload field must not be selected")


def test_allowed_upload_path_must_exist_and_have_content(tmp_path):
    document = tmp_path / "resume.pdf"
    document.write_bytes(b"pdf")

    assert _resolve_upload_path(str(document), [str(document)], None) == str(document.resolve())


def test_application_tools_keep_builtin_and_add_application_helpers(tmp_path):
    pdf = tmp_path / "cover-letter.pdf"
    pdf.write_bytes(b"%PDF-test")
    actions = build_application_tools(
        str(pdf),
        "Dear hiring team",
    ).registry.registry.actions
    action = actions["upload_file_by_label"]
    cover_letter_action = actions["get_cover_letter"]
    cover_letter_text_action = actions["get_cover_letter_text"]
    checkpoint_action = actions["checkpoint_application_submission"]
    critical_action = actions["load_current_platform_critical_memories"]
    lookup_action = actions["lookup_current_platform_memories"]

    assert "upload_file" in actions
    assert set(action.param_model.model_fields) == {"path", "field_name"}
    assert "exact visible label" in action.description
    assert cover_letter_action.param_model.model_fields == {}
    assert "cover-letter PDF" in cover_letter_action.description
    assert cover_letter_text_action.param_model.model_fields == {}
    assert "cover-letter text area" in cover_letter_text_action.description
    assert checkpoint_action.param_model.model_fields == {}
    assert "submission receipt" in checkpoint_action.description
    assert critical_action.param_model.model_fields == {}
    assert set(lookup_action.param_model.model_fields) == {"issue", "category"}
    assert "store_current_platform_memory" not in actions


@pytest.mark.asyncio
async def test_cover_letter_tool_returns_pdf_path(tmp_path):
    pdf = tmp_path / "cover-letter.pdf"
    pdf.write_bytes(b"%PDF-test")
    action = build_application_tools(str(pdf), "Dear hiring team").registry.registry.actions[
        "get_cover_letter"
    ]

    result = await action.function()

    assert result.extracted_content == str(pdf.resolve())
    assert result.long_term_memory is None


@pytest.mark.asyncio
async def test_cover_letter_text_tool_returns_text_only_on_demand(tmp_path):
    pdf = tmp_path / "cover-letter.pdf"
    pdf.write_bytes(b"%PDF-test")
    action = build_application_tools(str(pdf), "Dear hiring team").registry.registry.actions[
        "get_cover_letter_text"
    ]

    result = await action.function()

    assert result.extracted_content == "Dear hiring team"
    assert result.long_term_memory is None


@pytest.mark.asyncio
async def test_submission_checkpoint_saves_current_page_evidence():
    screenshots = []

    class FakeBrowser:
        async def take_screenshot(self, full_page=False):
            assert full_page is True
            return b"png"

    action = build_application_tools(
        save_submission_screenshot=lambda screenshot: screenshots.append(screenshot) or True,
    ).registry.registry.actions["checkpoint_application_submission"]

    result = await action.function(browser_session=FakeBrowser())

    assert screenshots == [b"png"]
    assert result.extracted_content == "Saved application submission confirmation checkpoint"
    assert result.long_term_memory == result.extracted_content


@pytest.mark.asyncio
async def test_platform_memory_tools_load_and_lookup():
    calls = []

    class FakeMemoryStore:
        @staticmethod
        def extract_domain(_url):
            return "greenhouse.io"

        @staticmethod
        def detect_ats_platform(_domain):
            return "greenhouse"

        @staticmethod
        def get_critical_memories(_url, **kwargs):
            calls.append(("critical", kwargs))
            return [{"category": "navigation", "content": "Use the second Apply button."}]

        @staticmethod
        def find_relevant_memories(url, issue, category=None):
            calls.append(("lookup", url, issue, category))
            return [{"category": "failure_recovery", "content": "Reload once after timeout."}]

    actions = build_application_tools(
        memory_store=FakeMemoryStore(),
        critical_memory_max_count=7,
        critical_memory_max_tokens=900,
    ).registry.registry.actions

    critical = await actions["load_current_platform_critical_memories"].function(
        page_url="https://boards.greenhouse.io/acme/jobs/1"
    )
    duplicate = await actions["load_current_platform_critical_memories"].function(
        page_url="https://boards.greenhouse.io/acme/jobs/1"
    )
    lookup = await actions["lookup_current_platform_memories"].function(
        issue="The form timed out after upload",
        category="failure_recovery",
        page_url="https://boards.greenhouse.io/acme/jobs/1",
    )
    assert "Use the second Apply button" in critical.extracted_content
    assert calls[0] == ("critical", {"max_count": 7, "max_tokens": 900})
    assert critical.include_extracted_content_only_once is True
    assert "already loaded" in duplicate.extracted_content
    assert "Reload once" in lookup.extracted_content
