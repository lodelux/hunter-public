from datetime import datetime, timezone

from sources import linkedin_browser_collector as collector


SEARCH_URL = (
    "https://www.linkedin.com/jobs/search-results/?"
    "keywords=software%20engineer&geoId=103035651&f_TPR=r86400&"
    "f_SAL=f_SA_id_227001%3A276001"
)


def test_validates_search_urls_and_preserves_linkedin_filters():
    assert collector.validate_linkedin_search_url(SEARCH_URL) == SEARCH_URL

    page_url = collector.linkedin_search_page_url(SEARCH_URL, 25)

    assert "keywords=software+engineer" in page_url
    assert "geoId=103035651" in page_url
    assert "f_TPR=r86400" in page_url
    assert "f_SAL=f_SA_id_227001%3A276001" in page_url
    assert "start=25" in page_url


def test_rejects_listing_and_non_linkedin_urls():
    for url in (
        "https://www.linkedin.com/jobs/view/4451533722",
        "https://example.com/jobs/search/?keywords=engineer",
        "http://www.linkedin.com/jobs/search/?keywords=engineer",
    ):
        try:
            collector.validate_linkedin_search_url(url)
        except ValueError as exc:
            assert "LinkedIn jobs search-results URL" in str(exc)
        else:
            raise AssertionError(f"Expected {url} to be rejected")


def test_normalizes_structured_and_relative_posting_datetimes():
    reference = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)

    assert collector._normalize_posted_datetime("2026-08-15T09:30:00Z", reference) == (
        "2026-08-15T09:30:00+00:00"
    )
    assert collector._normalize_posted_datetime("Reposted 3 hours ago", reference) == (
        "2026-08-16T09:00:00+00:00"
    )
    assert collector._normalize_posted_datetime("Vor 2 Tagen veröffentlicht", reference) == (
        "2026-08-14T12:00:00+00:00"
    )


def test_freshness_requires_a_known_posting_time_at_or_after_cutoff():
    cutoff = datetime(2026, 8, 16, 11, 0, tzinfo=timezone.utc)

    assert collector._is_fresh_enough("2026-08-16T11:00:00Z", cutoff)
    assert not collector._is_fresh_enough("2026-08-16T10:59:59Z", cutoff)
    assert not collector._is_fresh_enough(None, cutoff)


def test_extract_posted_datetime_keeps_unparsed_text_for_classifier_fallback():
    class FakePage:
        def evaluate(self, _script):
            return []

    posted_at, posting_text = collector._extract_posted_datetime(
        FakePage(),
        ["Berlin · Ungefähr kürzlich veröffentlicht · 12 Bewerber"],
        reference=datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc),
    )

    assert posted_at is None
    assert posting_text == "Berlin · Ungefähr kürzlich veröffentlicht · 12 Bewerber"


async def test_scraper_launches_headless_with_saved_profile_and_keeps_result_order(
    monkeypatch, tmp_path
):
    launch_kwargs = {}

    class FakePage:
        def __init__(self):
            self.url = "about:blank"

        def goto(self, url, **_kwargs):
            self.url = url

        def wait_for_selector(self, *_args, **_kwargs):
            return None

        def wait_for_timeout(self, *_args, **_kwargs):
            return None

        def close(self):
            return None

    search_page = FakePage()
    detail_page = FakePage()

    class FakeContext:
        page_number = 0
        def new_page(self):
            self.page_number += 1
            return search_page if self.page_number == 1 else detail_page

    class FakeBrowser:
        contexts = [FakeContext()]

    class FakeChromium:
        def connect_over_cdp(self, cdp_url):
            assert cdp_url == "http://127.0.0.1:9222"
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    class FakeManager:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, *_args):
            return None

    class FakeBrowserSession:
        cdp_url = "http://127.0.0.1:9222"

        def __init__(self, **kwargs):
            launch_kwargs.update(kwargs)

        async def start(self):
            return None

        async def close(self):
            return None

    cards = [
        {"id": "1", "url": "https://www.linkedin.com/jobs/view/1", "title": "Old"},
        {"id": "2", "url": "https://www.linkedin.com/jobs/view/2", "title": "Blocked"},
        {"id": "3", "url": "https://www.linkedin.com/jobs/view/3", "title": "First"},
        {"id": "4", "url": "https://www.linkedin.com/jobs/view/4", "title": "Second"},
    ]

    monkeypatch.setattr(collector, "sync_playwright", lambda: FakeManager())
    monkeypatch.setattr(collector, "apply_stealth_sync", lambda _context: None)
    async def fake_ensure_login(*_args, **_kwargs):
        return False

    monkeypatch.setattr(collector, "ensure_linkedin_login", fake_ensure_login)
    monkeypatch.setattr("browser_use.BrowserSession", FakeBrowserSession)
    monkeypatch.setattr(
        "core.shared_config.launch_profile_browser",
        lambda *_args, **_kwargs: (None, None),
    )
    monkeypatch.setattr("core.shared_config.stop_profile_browser", lambda _process: None)
    monkeypatch.setattr(collector, "_job_links", lambda _page: cards)
    monkeypatch.setattr(collector, "_scroll_job_list", lambda _page: False)
    monkeypatch.setattr(
        collector,
        "_extract_detail",
        lambda _page, card: {
            "id": f"li-{card['id']}",
            "job_url": card["url"],
            "title": card["title"],
            "company": "Outlier" if card["id"] == "2" else "Example",
            "location": "Berlin",
            "description": "Description",
            "easy_apply": False,
        },
    )

    result = await collector.scrape_linkedin_search_url(
        search_url=SEARCH_URL,
        max_jobs=2,
        max_results_to_inspect=4,
        profile_dir=tmp_path / "browser_profile",
        user_agent="headed-user-agent",
        existing_job_ids={"1"},
        blacklisted_companies=["outlier"],
    )

    assert launch_kwargs["headless"] is True
    assert launch_kwargs["user_data_dir"] == str(tmp_path / "browser_profile")
    assert launch_kwargs["user_agent"] == "headed-user-agent"
    assert "--disable-blink-features=AutomationControlled" in launch_kwargs["args"]
    assert [job["id"] for job in result.jobs] == ["li-3", "li-4"]
    assert "f_SAL=f_SA_id_227001%3A276001" in search_page.url
