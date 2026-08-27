import job_screening as screening


def _profile(**overrides):
    value = {
        "languages": ["English"],
        "visa_sponsorship_needed": False,
        "salary_expectation": {
            "min": 50_000,
            "currency": "EUR",
            "period": "annual",
        },
    }
    value.update(overrides)
    return value


def _classified_job(**facts):
    base_facts = {
        "required_languages": [],
        "sponsorship": {
            "value": "unknown",
            "confidence": 0.0,
            "evidence": [],
        },
        "salary": {
            "minimum": None,
            "maximum": None,
            "currency": None,
            "interval": None,
            "confidence": 0.0,
            "evidence": [],
        },
    }
    base_facts.update(facts)
    return {
        "classification": {"status": "complete", "facts": base_facts},
    }


def test_required_language_rule_is_case_insensitive_and_rejects_absent_language():
    job = _classified_job(
        required_languages=[
            {
                "value": "German",
                "confidence": 0.95,
                "evidence": ["German required"],
            }
        ]
    )
    result = screening.evaluate_screening(job, _profile(languages=["English"]))
    assert result["status"] == "rejected"
    assert result["reasons"][0]["code"] == "required_language_missing"

    result = screening.evaluate_screening(job, _profile(languages=["german"]))
    assert result["status"] == "qualified"


def test_required_language_rule_normalizes_localized_names_to_english():
    job = _classified_job(
        required_languages=[
            {
                "value": "Englisch",
                "confidence": 0.95,
                "evidence": ["Sehr gute Englischkenntnisse erforderlich"],
            }
        ]
    )

    assert screening.evaluate_screening(job, _profile())["status"] == "qualified"


def test_high_confidence_senior_or_higher_seniority_is_rejected():
    for level in ("senior", "lead", "executive"):
        job = _classified_job(
            seniority={
                "value": level,
                "confidence": 0.95,
                "evidence": [f"{level} role"],
            }
        )
        result = screening.evaluate_screening(job, _profile())
        assert result["status"] == "rejected"
        assert result["reasons"][0]["code"] == "seniority_outside_target"


def test_mid_junior_unknown_or_low_confidence_seniority_does_not_reject():
    for value, confidence in (
        ("junior", 0.95),
        ("mid", 0.95),
        ("unknown", 0.95),
        ("senior", 0.5),
    ):
        job = _classified_job(
            seniority={"value": value, "confidence": confidence, "evidence": []}
        )
        assert screening.evaluate_screening(job, _profile())["status"] == "qualified"


def test_low_confidence_language_does_not_reject():
    job = _classified_job(
        required_languages=[
            {"value": "German", "confidence": 0.3, "evidence": []}
        ]
    )
    assert screening.evaluate_screening(job, _profile())["status"] == "qualified"


def test_sponsorship_rule_only_rejects_when_needed_and_explicitly_unavailable():
    job = _classified_job(
        sponsorship={
            "value": "unavailable",
            "confidence": 0.95,
            "evidence": ["No sponsorship"],
        }
    )
    assert (
        screening.evaluate_screening(
            job, _profile(visa_sponsorship_needed=True)
        )["status"]
        == "rejected"
    )
    assert screening.evaluate_screening(job, _profile())["status"] == "qualified"


def test_salary_prefers_jobspy_and_normalizes_monthly_to_annual():
    job = _classified_job(
        salary={
            "minimum": 90_000,
            "maximum": 100_000,
            "currency": "EUR",
            "interval": "annual",
            "confidence": 0.99,
            "evidence": [],
        }
    )
    job.update(
        {
            "max_amount": 4_000,
            "currency": "EUR",
            "interval": "monthly",
        }
    )

    resolved = screening.resolve_salary(job, _profile())
    assert resolved["source"] == "jobspy"
    assert resolved["value"] == 48_000
    result = screening.evaluate_screening(job, _profile())
    assert result["status"] == "rejected"
    assert result["reasons"][0]["code"] == "salary_below_minimum"


def test_salary_falls_back_to_high_confidence_classifier_fact():
    job = _classified_job(
        salary={
            "minimum": 40_000,
            "maximum": 45_000,
            "currency": "EUR",
            "interval": "annual",
            "confidence": 0.9,
            "evidence": ["EUR 40-45k"],
        }
    )
    resolved = screening.resolve_salary(job, _profile())
    assert resolved["source"] == "classifier"
    assert screening.evaluate_screening(job, _profile())["status"] == "rejected"


def test_incomparable_jobspy_salary_falls_back_to_classifier_fact():
    job = _classified_job(
        salary={
            "minimum": 40_000,
            "maximum": 45_000,
            "currency": "EUR",
            "interval": "annual",
            "confidence": 0.9,
            "evidence": [],
        }
    )
    job.update({"max_amount": 1_000, "currency": "USD", "interval": "annual"})
    resolved = screening.resolve_salary(job, _profile())
    assert resolved["source"] == "classifier"
    assert resolved["value"] == 45_000


def test_salary_unknown_low_confidence_or_currency_mismatch_does_not_reject():
    low_confidence = _classified_job(
        salary={
            "minimum": 10_000,
            "maximum": 20_000,
            "currency": "EUR",
            "interval": "annual",
            "confidence": 0.4,
            "evidence": [],
        }
    )
    mismatch = {
        **low_confidence,
        "max_amount": 20_000,
        "currency": "USD",
        "interval": "annual",
    }
    assert screening.evaluate_screening(low_confidence, _profile())["status"] == "qualified"
    assert screening.evaluate_screening(mismatch, _profile())["status"] == "qualified"


def test_generic_rule_supports_profile_operand_confidence_and_missing_review():
    rule = screening.HardFilterRule(
        code="experience",
        field="classification.facts.experience_years",
        operator="lte",
        profile_field="years_of_experience",
        min_confidence=0.8,
        on_missing="review",
        message="Too much experience required",
    )
    job = _classified_job(
        experience_years={
            "value": 5,
            "confidence": 0.95,
            "evidence": ["5 years"],
        }
    )
    assert screening.evaluate_rule(rule, job, {"years_of_experience": 3})[
        "outcome"
    ] == "reject"

    job["classification"]["facts"]["experience_years"]["confidence"] = 0.2
    assert screening.evaluate_rule(rule, job, {"years_of_experience": 3})[
        "outcome"
    ] == "review"


def test_failed_and_legacy_classifications_are_review_but_not_application_rejected():
    failed = {"classification": {"status": "failed"}}
    legacy = {}
    assert screening.evaluate_screening(failed, _profile())["status"] == "review"
    assert screening.evaluate_screening(legacy, _profile())["status"] == "review"
    assert screening.is_screening_rejected(failed) is False
    assert screening.is_screening_rejected(legacy) is False


def test_qualified_override_becomes_effective_status():
    job = {"screening": {"status": "rejected", "override": "qualified"}}
    assert screening.effective_screening_status(job) == "qualified"
    assert screening.is_screening_rejected(job) is False
