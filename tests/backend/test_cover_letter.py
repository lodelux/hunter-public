from types import SimpleNamespace

import pytest

import cover_letter
from core import llm_factory


class _LLM:
    def __init__(self, response):
        self.response = response
        self.messages = None

    async def ainvoke(self, messages, **kwargs):
        self.messages = messages
        self.output_format = kwargs.get("output_format")
        return self.response


def _cover_letter_spec(
    text="Dear team,\n\nHello.",
    requirement_id="J003",
    evidence_id="P004",
):
    return cover_letter.CoverLetterSpec(
        text=text,
        positioning="Connect reliable API delivery to the role.",
        decisions=[cover_letter.CoverLetterDecision(
            decision="Lead with reliable backend delivery.",
            requirement_ids=[requirement_id],
            evidence_ids=[evidence_id],
        )],
    )


def test_saves_letter_beside_tailored_resume(monkeypatch, tmp_path):
    monkeypatch.setattr(cover_letter, "get_data_dir", lambda: tmp_path)

    path = cover_letter.save_cover_letter_text(
        "https://example.com/jobs/1",
        "Dear team",
        "Backend Developer (m/w/d)",
    )

    assert path.parent.parent == tmp_path / "tailored_resumes"
    assert (
        path.name
        == "Candidate_Backend_Developer_m_w_d_Cover_Letter.txt"
    )
    assert path.read_text(encoding="utf-8") == "Dear team"
    assert (
        cover_letter.get_cover_letter_path("https://example.com/jobs/1")
        == path
    )
    assert cover_letter.get_cover_letter_path("https://example.com/missing") is None


def test_renders_uploadable_cover_letter_pdf(monkeypatch, tmp_path):
    monkeypatch.setattr(cover_letter, "get_data_dir", lambda: tmp_path)

    path = cover_letter.save_cover_letter_pdf(
        "https://example.com/jobs/1",
        "Dear team,\n\nI would like to apply.\n\nSincerely,\nJordan",
        "Backend Developer (m/w/d)",
    )

    assert path.name == "Candidate_Backend_Developer_m_w_d_Cover_Letter.pdf"
    assert path.read_bytes().startswith(b"%PDF")
    document = cover_letter.fitz.open(path)
    assert document.page_count == 1
    assert "I would like to apply." in document[0].get_text()
    assert cover_letter.get_cover_letter_pdf_path("https://example.com/jobs/1") == path
    assert cover_letter.get_cover_letter_pdf_path("https://example.com/missing") is None


@pytest.mark.asyncio
async def test_generates_plain_text_with_profile_instructions(monkeypatch):
    llm = _LLM(SimpleNamespace(completion=_cover_letter_spec()))
    received_settings = []
    monkeypatch.setattr(
        cover_letter,
        "load_llm_settings",
        lambda: {"provider": "openai"},
    )
    monkeypatch.setattr(
        cover_letter,
        "load_profile",
        lambda: {
            "country": "DE",
            "markdown": (
                "# Personal Details\nName: Ada\n"
                "# Application Materials\n"
                "## Cover Letter Instructions\nKeep it direct and concise."
            ),
        },
    )
    monkeypatch.setattr(
        llm_factory,
        "create_llm",
        lambda settings: received_settings.append(settings) or llm,
    )

    result, audit = await cover_letter.generate_cover_letter_text(
        "Build reliable APIs.",
        "Backend Developer",
        "Acme",
        include_audit=True,
    )

    assert result == "Dear team,\n\nHello."
    assert audit["decisions"][0]["requirements"][0]["text"] == "Build reliable APIs."
    assert audit["decisions"][0]["evidence"][0]["text"] == "# Personal Details"
    assert llm.output_format is cover_letter.CoverLetterSpec
    assert len(llm.messages) == 2
    system_prompt = llm.messages[0].content
    user_prompt = llm.messages[1].content
    assert "Keep it direct and concise." in system_prompt
    assert "user's additional instructions" in system_prompt
    assert "Build reliable APIs." not in system_prompt
    assert "Build reliable APIs." in user_prompt
    assert "Keep it direct and concise." not in user_prompt
    assert received_settings[0]["provider"] == "openai"
    assert received_settings[0]["openai"]["model"] == "gpt-5.6-sol"
    assert received_settings[0]["openai"]["reasoning_effort"] == "medium"
    assert received_settings[0]["openai"]["max_completion_tokens"] == 20_000


@pytest.mark.asyncio
async def test_requires_configured_llm(monkeypatch):
    monkeypatch.setattr(
        cover_letter,
        "load_llm_settings",
        lambda: {"provider": ""},
    )

    with pytest.raises(ValueError, match="No LLM configured"):
        await cover_letter.generate_cover_letter_text("Description")


@pytest.mark.asyncio
async def test_rejects_empty_response(monkeypatch):
    llm = _LLM(SimpleNamespace(completion=_cover_letter_spec("   ", "J001", "P001")))
    monkeypatch.setattr(
        cover_letter,
        "load_llm_settings",
        lambda: {"provider": "openai"},
    )
    monkeypatch.setattr(
        cover_letter,
        "load_profile",
        lambda: {"markdown": ""},
    )
    monkeypatch.setattr(llm_factory, "create_llm", lambda settings: llm)

    with pytest.raises(ValueError, match="empty cover letter"):
        await cover_letter.generate_cover_letter_text("Description")
