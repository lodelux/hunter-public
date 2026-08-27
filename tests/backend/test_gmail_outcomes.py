import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from gmail_outcomes import (
    GmailOutcomeManager,
    OutcomeClassification,
    deterministic_outcome,
    deterministic_positive_next_step,
    gmail_deep_link,
    match_message_to_job,
    parse_gmail_message,
)


def message(*, body: str, subject: str = "Application update", received_at: str | None = None):
    return {
        "id": "message-1",
        "thread_id": "thread-1",
        "label_ids": ["INBOX"],
        "sender": "Acme Recruiting <jobs@acme.example>",
        "recipient": "candidate@example.com",
        "subject": subject,
        "received_at": received_at or datetime.now(timezone.utc).isoformat(),
        "body": body,
    }


def manager_for(tmp_path, jobs, *, classifier=None):
    notifications = []

    def update(url, **fields):
        jobs[url].update(fields)
        return True

    async def no_outcome(_message, _job):
        return OutcomeClassification(
            outcome="none",
            explicit=False,
            confidence=1,
            evidence="",
            reason="Acknowledgement only",
        )

    manager = GmailOutcomeManager(
        tmp_path,
        jobs_reader=lambda: jobs,
        job_updater=update,
        classifier=classifier or no_outcome,
        notifier=lambda text: notifications.append(text) is None or True,
    )
    return manager, notifications


def applied_job(title="Backend Engineer", **fields):
    return {
        "status": "applied",
        "application_status": "applied",
        "title": title,
        "company": "Acme GmbH",
        **fields,
    }


def test_oauth_completion_exchanges_validated_code_directly(tmp_path, monkeypatch):
    manager, _ = manager_for(tmp_path, {})
    client_config = {
        "installed": {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    manager._pending_oauth = {
        "state": "expected-state",
        "redirect_uri": "http://127.0.0.1:8743/gmail/oauth/callback",
        "client_config": client_config,
    }
    fetched = {}

    class FakeCredentials:
        refresh_token = "refresh-token"
        id_token = None

        @staticmethod
        def to_json():
            return json.dumps({"refresh_token": "refresh-token"})

    class FakeFlow:
        credentials = FakeCredentials()
        redirect_uri = None

        @staticmethod
        def fetch_token(**kwargs):
            fetched.update(kwargs)

    from google_auth_oauthlib.flow import Flow

    monkeypatch.setattr(
        Flow,
        "from_client_config",
        staticmethod(lambda *_args, **_kwargs: FakeFlow()),
    )

    manager.complete_authorization(
        state="expected-state",
        query_string="state=expected-state&code=authorization%2Fcode",
    )

    assert fetched == {"code": "authorization/code"}
    assert manager.is_connected() is True
    assert manager._pending_oauth is None


def test_hosted_oauth_uses_web_client_config(tmp_path, monkeypatch):
    manager, _ = manager_for(tmp_path, {})
    captured = {}

    class FakeFlow:
        redirect_uri = None

        @staticmethod
        def authorization_url(**_kwargs):
            return "https://accounts.google.com/o/oauth2/auth", None

    from google_auth_oauthlib.flow import Flow

    def from_client_config(config, **kwargs):
        captured.update({"config": config, **kwargs})
        return FakeFlow()

    monkeypatch.setattr(Flow, "from_client_config", staticmethod(from_client_config))
    redirect_uri = "https://hunter.example.ts.net/gmail/oauth/callback"

    authorization_url = manager.start_authorization(
        client_id="web-client",
        client_secret="web-secret",
        redirect_uri=redirect_uri,
    )

    assert authorization_url == "https://accounts.google.com/o/oauth2/auth"
    assert set(captured["config"]) == {"web"}
    assert captured["config"]["web"]["redirect_uris"] == [redirect_uri]
    assert manager._pending_oauth["redirect_uri"] == redirect_uri


def test_saved_oauth_clients_are_reused_only_for_their_redirect_type(tmp_path, monkeypatch):
    manager, _ = manager_for(tmp_path, {})
    manager.client_path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "desktop-client",
                    "client_secret": "desktop-secret",
                }
            }
        )
    )

    assert manager._client_config("installed") == {
        "installed": {
            "client_id": "desktop-client",
            "client_secret": "desktop-secret",
        }
    }
    assert manager._client_config("web") is None

    monkeypatch.setenv("HUNTER_WEB_ORIGIN", "https://hunter.example.ts.net")
    assert manager.status()["client_configured"] is False


def test_deterministic_outcomes_require_explicit_language():
    assert deterministic_outcome("Unfortunately, we are not moving forward with your application")[0] == "rejected"
    assert deterministic_outcome("We would like to invite you to an interview")[0] == "interview"
    assert deterministic_outcome("We are pleased to offer you the position")[0] == "offer"
    assert deterministic_outcome("We received your application. We will contact selected candidates") == ("unknown", "")


def test_deterministic_positive_next_steps_require_concrete_progression():
    assert deterministic_positive_next_step(
        "We're excited to invite you to the next step in the process: the coding assessment."
    )
    assert deterministic_positive_next_step(
        "As a first step, we'd like you to send two short videos answering these questions."
    )
    assert deterministic_positive_next_step(
        "I'm pleased to invite you to the next stage of our recruitment process. "
        "This stage is a short set of written questions."
    )
    assert deterministic_positive_next_step(
        "Wir freuen uns, dich zum nächsten Schritt, dem Online-Bewertungsverfahren, weiterleiten zu können."
    )
    assert not deterministic_positive_next_step(
        "If selected, we may contact you later with information about possible next steps."
    )


def test_gmail_deep_link_targets_connected_account_without_assuming_slot():
    assert gmail_deep_link("thread/1", "candidate+jobs@example.com") == (
        "https://mail.google.com/mail/u/?authuser=candidate%2Bjobs%40example.com#all/thread%2F1"
    )


def test_company_suffix_is_matched_but_multiple_roles_require_exact_title():
    jobs = {
        "one": applied_job("Backend Engineer"),
        "two": applied_job("Data Engineer"),
    }
    exact = match_message_to_job(
        message(body="Acme invites you for the Backend Engineer process"),
        jobs,
    )
    assert exact["kind"] == "matched"
    assert exact["url"] == "one"
    assert exact["method"] == "company_and_role"

    ambiguous = match_message_to_job(
        message(body="Application update from Acme: we have made a decision"),
        jobs,
    )
    assert ambiguous["kind"] == "review"
    assert "multiple applications" in ambiguous["reason"]


def test_related_company_names_with_same_role_require_manual_review():
    jobs = {
        "nvidia": applied_job(
            "Solutions Architect, Graduate Program",
            company="NVIDIA",
        ),
        "nvidia-ai": applied_job(
            "Solutions Architect, Graduate Program",
            company="NVIDIA AI",
        ),
    }

    match = match_message_to_job(
        message(
            subject="Thank you from NVIDIA",
            body="We have made a decision about your application.",
        ),
        jobs,
    )

    assert match["kind"] == "review"
    assert "2 saved applications" in match["reason"]
    assert "NVIDIA, NVIDIA AI" in match["reason"]
    assert "cannot determine which application" in match["reason"]


def test_again_company_matches_sender_identity_not_ordinary_word():
    jobs = {
        "again": applied_job(company="Again"),
        "sap": applied_job(company="SAP"),
    }

    sap_message = message(
        subject="Your SAP application",
        body="Unfortunately, we are not moving forward. Thank you again.",
    )
    sap_message["sender"] = "SAP Careers <system@successfactors.eu>"
    sap_match = match_message_to_job(sap_message, jobs)
    assert sap_match["kind"] == "matched"
    assert sap_match["url"] == "sap"

    again_message = message(body="Unfortunately, we are not moving forward.")
    again_message["sender"] = "Again Recruiting <jobs@again.com>"
    again_match = match_message_to_job(again_message, jobs)
    assert again_match["kind"] == "matched"
    assert again_match["url"] == "again"


@pytest.mark.asyncio
async def test_unique_role_from_unknown_recruiter_requests_review_and_notifies(tmp_path):
    jobs = {
        "pixel": applied_job(
            "Associate Web Developer",
            company="Pixel Systems",
        ),
        "other": applied_job("Backend Engineer", company="Other Company"),
    }
    manager, notifications = manager_for(tmp_path, jobs)
    recruiting_message = message(
        subject="Next steps in your Associate Web Developer application",
        body=(
            "Thank you for your interest. I'm pleased to invite "
            "you to the next stage of our recruitment process. This stage is a short set "
            "of written questions."
        ),
    )
    recruiting_message["sender"] = "Example Hiring Partner <recruiting@partner.example>"

    result = await manager._process_message(recruiting_message, "candidate@example.com")

    assert result == "review"
    assert jobs["pixel"]["application_status"] == "applied"
    assert manager.status()["review_required"][0]["job_url"] == "pixel"
    assert len(notifications) == 1
    assert "Positive next step needs matching" in notifications[0]
    assert "Associate Web Developer at Pixel Systems" in notifications[0]
    assert "recruiting partner" in notifications[0]


@pytest.mark.asyncio
async def test_ambiguous_role_creates_one_review_and_one_notification(tmp_path):
    jobs = {
        "one": applied_job("Backend Engineer"),
        "two": applied_job("Data Engineer"),
    }
    manager, notifications = manager_for(tmp_path, jobs)

    result = await manager._process_message(
        message(body="Application update from Acme: we have made a decision"),
        "candidate@example.com",
    )

    assert result == "review"
    assert manager.status()["review_required_count"] == 1
    assert len(notifications) == 1
    assert "Why manual review is needed:" in notifications[0]
    assert "multiple applications" in notifications[0]
    assert "does not identify exactly one saved role" in notifications[0]


@pytest.mark.asyncio
async def test_ambiguous_positive_step_is_labeled_as_positive_in_telegram(tmp_path):
    jobs = {
        "one": applied_job("Backend Engineer"),
        "two": applied_job("Data Engineer"),
    }
    manager, notifications = manager_for(tmp_path, jobs)

    result = await manager._process_message(
        message(
            body="We're excited to invite you to the next step in the process: the coding assessment."
        ),
        "candidate@example.com",
    )

    assert result == "review"
    assert "Positive next step needs matching" in notifications[0]


@pytest.mark.asyncio
async def test_rejection_updates_job_without_telegram(tmp_path):
    jobs = {"job-url": applied_job()}
    manager, notifications = manager_for(tmp_path, jobs)

    result = await manager._process_message(
        message(body="Unfortunately, we are not moving forward with your application."),
        "candidate@example.com",
    )

    assert result == "updated"
    assert jobs["job-url"]["application_status"] == "rejected"
    assert jobs["job-url"]["application_status_source"] == "gmail"
    evidence = jobs["job-url"]["application_status_evidence"]
    assert evidence["message_id"] == "message-1"
    assert evidence["gmail_url"] == gmail_deep_link("thread-1", "candidate@example.com")
    assert notifications == []


@pytest.mark.asyncio
async def test_positive_outcome_sends_telegram_with_gmail_link(tmp_path):
    jobs = {"job-url": applied_job()}
    manager, notifications = manager_for(tmp_path, jobs)

    result = await manager._process_message(
        message(body="We would like to invite you to an interview."),
        "candidate@example.com",
    )

    assert result == "updated"
    assert jobs["job-url"]["application_status"] == "interview"
    assert len(notifications) == 1
    assert "Interview invitation" in notifications[0]
    assert gmail_deep_link("thread-1", "candidate@example.com") in notifications[0]


@pytest.mark.asyncio
async def test_positive_next_step_moves_job_to_online_assessment(tmp_path):
    jobs = {"job-url": applied_job()}
    manager, notifications = manager_for(tmp_path, jobs)

    result = await manager._process_message(
        message(
            subject="First round",
            body="As a first step, we'd like you to send two short videos answering these questions.",
        ),
        "candidate@example.com",
    )

    assert result == "updated"
    assert jobs["job-url"]["application_status"] == "online_assessment"
    assert jobs["job-url"]["application_status_source"] == "gmail"
    assert jobs["job-url"]["application_status_evidence"]["outcome"] == "online_assessment"
    assert jobs["job-url"]["gmail_outcome_evidence"][-1]["message_id"] == "message-1"
    assert len(notifications) == 1
    assert "Positive next step" in notifications[0]
    event = manager.status()["recent_events"][-1]
    assert event["type"] == "positive_next_step"
    assert event["telegram_sent"] is True


@pytest.mark.asyncio
async def test_ai_fallback_can_alert_for_an_unfamiliar_positive_next_step(tmp_path):
    async def classifier(_message, _job):
        return OutcomeClassification(
            outcome="next_step",
            explicit=True,
            confidence=0.97,
            evidence="record your responses in our candidate portal",
            reason="The candidate was explicitly advanced to a recorded screening round.",
        )

    jobs = {"job-url": applied_job()}
    manager, notifications = manager_for(tmp_path, jobs, classifier=classifier)
    result = await manager._process_message(
        message(body="Please record your responses in our candidate portal by Friday."),
        "candidate@example.com",
    )

    assert result == "updated"
    assert jobs["job-url"]["application_status"] == "online_assessment"
    assert "Positive next step" in notifications[0]


@pytest.mark.asyncio
async def test_ai_fallback_can_confirm_offer_with_verbatim_evidence(tmp_path):
    calls = []

    async def classifier(received_message, job):
        calls.append((received_message, job))
        return OutcomeClassification(
            outcome="offer",
            explicit=True,
            confidence=0.97,
            evidence="selected you for the position",
            reason="The sender explicitly selected the candidate and is extending an offer.",
        )

    jobs = {"job-url": applied_job()}
    manager, notifications = manager_for(tmp_path, jobs, classifier=classifier)
    result = await manager._process_message(
        message(body="We have selected you for the position and attached the proposed terms."),
        "candidate@example.com",
    )

    assert result == "updated"
    assert len(calls) == 1
    assert jobs["job-url"]["application_status"] == "offer"
    assert "Job offer" in notifications[0]


@pytest.mark.asyncio
async def test_unknown_classifier_result_requires_review_and_notifies(tmp_path):
    async def classifier(_message, _job):
        return OutcomeClassification(
            outcome="unknown",
            explicit=False,
            confidence=0.6,
            evidence="next steps",
            reason="The wording about next steps is conditional.",
        )

    jobs = {"job-url": applied_job()}
    manager, notifications = manager_for(tmp_path, jobs, classifier=classifier)
    result = await manager._process_message(
        message(body="We would like to discuss possible next steps with you."),
        "candidate@example.com",
    )

    assert result == "review"
    assert jobs["job-url"]["application_status"] == "applied"
    status = manager.status()
    assert status["review_required_count"] == 1
    assert status["review_required"][0]["gmail_url"] == gmail_deep_link(
        "thread-1", "candidate@example.com"
    )
    assert "needs a Gmail outcome review" in notifications[0]
    assert "the classifier could not determine one explicit hiring outcome" in notifications[0]
    assert "the classifier did not confirm that the outcome was explicit" in notifications[0]
    assert "below the required 85%" in notifications[0]
    assert "The wording about next steps is conditional." not in notifications[0]
    assert gmail_deep_link("thread-1", "candidate@example.com") in notifications[0]


@pytest.mark.asyncio
async def test_nonverbatim_classifier_evidence_reports_failed_safety_check(tmp_path):
    async def classifier(_message, _job):
        return OutcomeClassification(
            outcome="rejected",
            explicit=True,
            confidence=0.98,
            evidence="your application will not move forward",
            reason="The email explicitly states that this is a rejection.",
        )

    jobs = {"job-url": applied_job()}
    manager, notifications = manager_for(tmp_path, jobs, classifier=classifier)

    result = await manager._process_message(
        message(body="We have concluded our process and will end your candidacy here."),
        "candidate@example.com",
    )

    assert result == "review"
    assert "did not return a verbatim supporting excerpt" in notifications[0]
    assert "explicitly states that this is a rejection" not in notifications[0]


@pytest.mark.asyncio
async def test_classifier_failure_becomes_manual_review(tmp_path):
    async def classifier(_message, _job):
        raise RuntimeError("provider unavailable")

    jobs = {"job-url": applied_job()}
    manager, notifications = manager_for(tmp_path, jobs, classifier=classifier)

    result = await manager._process_message(
        message(body="We would like to discuss possible next steps with you."),
        "candidate@example.com",
    )

    assert result == "review"
    assert manager.status()["review_required_count"] == 1
    assert "classifier was unavailable" in notifications[0]


@pytest.mark.asyncio
async def test_email_older_than_application_is_ignored(tmp_path):
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    jobs = {"job-url": applied_job(applied_at=tomorrow)}
    manager, notifications = manager_for(tmp_path, jobs)

    result = await manager._process_message(
        message(body="Unfortunately, we are not moving forward with your application."),
        "candidate@example.com",
    )

    assert result == "ignored"
    assert jobs["job-url"]["application_status"] == "applied"
    assert notifications == []


@pytest.mark.asyncio
async def test_email_just_before_application_checkpoint_is_still_processed(tmp_path):
    received_at = datetime.now(timezone.utc)
    applied_at = (received_at + timedelta(seconds=30)).isoformat()
    jobs = {"job-url": applied_job(applied_at=applied_at)}
    manager, notifications = manager_for(tmp_path, jobs)

    result = await manager._process_message(
        message(
            body="We're excited to invite you to the next step in the process: the coding assessment.",
            received_at=received_at.isoformat(),
        ),
        "candidate@example.com",
    )

    assert result == "updated"
    assert jobs["job-url"]["application_status"] == "online_assessment"
    assert "Positive next step" in notifications[0]


@pytest.mark.asyncio
async def test_positive_next_step_does_not_regress_a_later_outcome(tmp_path):
    jobs = {
        "job-url": applied_job(
            application_status="rejected",
            application_status_source="gmail",
            application_status_updated_at=datetime.now(timezone.utc).isoformat(),
        )
    }
    manager, notifications = manager_for(tmp_path, jobs)

    result = await manager._process_message(
        message(body="We're excited to invite you to the next step: the coding assessment."),
        "candidate@example.com",
    )

    assert result == "alerted"
    assert jobs["job-url"]["application_status"] == "rejected"
    assert jobs["job-url"]["gmail_outcome_evidence"][-1]["outcome"] == "online_assessment"
    assert "Positive next step" in notifications[0]


@pytest.mark.asyncio
async def test_new_email_does_not_overwrite_terminal_or_newer_manual_outcome(tmp_path):
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    jobs = {
        "job-url": applied_job(
            application_status="interview",
            application_status_source="manual",
            application_status_updated_at=tomorrow,
        )
    }
    manager, notifications = manager_for(tmp_path, jobs)
    result = await manager._process_message(
        message(body="We are pleased to offer you the position."),
        "candidate@example.com",
    )

    assert result == "review"
    assert jobs["job-url"]["application_status"] == "interview"
    assert "older than" in manager.status()["review_required"][0]["reason"]
    assert len(notifications) == 1


def test_parse_gmail_message_prefers_plain_text_and_trims_quoted_reply():
    body = "We would like to invite you to an interview.\nOn Monday someone wrote:\nOld thread"
    encoded = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    parsed = parse_gmail_message(
        {
            "id": "m1",
            "threadId": "t1",
            "internalDate": "1700000000000",
            "labelIds": ["INBOX"],
            "payload": {
                "mimeType": "multipart/alternative",
                "headers": [
                    {"name": "From", "value": "Acme <jobs@acme.example>"},
                    {"name": "Subject", "value": "Interview"},
                ],
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": encoded}},
                ],
            },
        }
    )
    assert parsed["body"] == "We would like to invite you to an interview."
    assert parsed["thread_id"] == "t1"


class FakeRequest:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeNotFound(Exception):
    resp = type("Response", (), {"status": 404})()


class FakeNotFoundRequest:
    def execute(self):
        raise FakeNotFound("message no longer exists")


class FakeMessages:
    def __init__(self, raw_message):
        self.raw_message = raw_message

    def list(self, **_kwargs):
        return FakeRequest({"messages": [{"id": self.raw_message["id"]}]})

    def get(self, **_kwargs):
        return FakeRequest(self.raw_message)


class FakeHistory:
    def list(self, **_kwargs):
        return FakeRequest({"history": [], "historyId": "20"})


class FakeUsers:
    def __init__(self, raw_message):
        self._messages = FakeMessages(raw_message)

    def getProfile(self, **_kwargs):
        return FakeRequest({"emailAddress": "candidate@example.com", "historyId": "20"})

    def messages(self):
        return self._messages

    def history(self):
        return FakeHistory()


class FakeService:
    def __init__(self, raw_message):
        self._users = FakeUsers(raw_message)

    def users(self):
        return self._users


@pytest.mark.asyncio
async def test_first_sync_uses_bounded_backfill_then_persists_history(tmp_path):
    encoded = base64.urlsafe_b64encode(b"We received your application.").decode().rstrip("=")
    raw_message = {
        "id": "m1",
        "threadId": "t1",
        "internalDate": "1700000000000",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "From", "value": "Unrelated <mail@example.org>"},
                {"name": "Subject", "value": "Hello"},
            ],
            "mimeType": "text/plain",
            "body": {"data": encoded},
        },
    }
    manager = GmailOutcomeManager(
        tmp_path,
        jobs_reader=lambda: {},
        service_factory=lambda: FakeService(raw_message),
        notifier=lambda _text: True,
    )
    manager.credentials_path.write_text(json.dumps({"refresh_token": "test"}))

    result = await manager.sync_once()

    assert result["scanned"] == 1
    status = manager.status()
    assert status["account_email"] == "candidate@example.com"
    state = json.loads(manager.state_path.read_text())
    assert state["history_id"] == "20"
    assert state["processed_message_ids"] == ["m1"]


def test_fetch_skips_message_deleted_after_gmail_listed_it(tmp_path):
    raw_message = {
        "id": "still-there",
        "internalDate": "1700000000000",
    }

    class Messages:
        def get(self, *, id, **_kwargs):
            if id == "deleted":
                return FakeNotFoundRequest()
            return FakeRequest(raw_message)

    class History:
        def list(self, **_kwargs):
            return FakeRequest(
                {
                    "historyId": "20",
                    "history": [
                        {
                            "messagesAdded": [
                                {"message": {"id": "deleted"}},
                                {"message": {"id": "still-there"}},
                            ]
                        }
                    ],
                }
            )

    class Users:
        def getProfile(self, **_kwargs):
            return FakeRequest(
                {"emailAddress": "candidate@example.com", "historyId": "20"}
            )

        def messages(self):
            return Messages()

        def history(self):
            return History()

    manager, _ = manager_for(tmp_path, {})
    manager._service_factory = lambda: type(
        "Service",
        (),
        {"users": lambda _self: Users()},
    )()

    messages, history_id, account_email = manager._fetch_new_messages("10", "")

    assert messages == [raw_message]
    assert history_id == "20"
    assert account_email == "candidate@example.com"


@pytest.mark.asyncio
async def test_sync_failure_notifies_once_until_a_success(tmp_path):
    manager, notifications = manager_for(tmp_path, {})

    manager._record_error(RuntimeError("provider unavailable"))
    manager._record_error(RuntimeError("different failure in the same outage"))

    assert len(notifications) == 1
    assert notifications[0] == "Hunter Gmail sync failed\nprovider unavailable"
    assert manager.status()["last_error"] == "different failure in the same outage"

    encoded = base64.urlsafe_b64encode(b"No hiring outcome.").decode().rstrip("=")
    raw_message = {
        "id": "m1",
        "threadId": "t1",
        "internalDate": "1700000000000",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [{"name": "From", "value": "mail@example.org"}],
            "mimeType": "text/plain",
            "body": {"data": encoded},
        },
    }
    manager.credentials_path.write_text(json.dumps({"refresh_token": "test"}))
    manager._service_factory = lambda: FakeService(raw_message)

    await manager.sync_once()
    assert manager.status()["last_error"] is None
    manager._record_error(RuntimeError("provider unavailable"))

    assert len(notifications) == 2
