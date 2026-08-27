"""Authenticated LinkedIn search collection through Hunter's saved browser profile."""

from dataclasses import dataclass
import asyncio
from datetime import datetime, timedelta, timezone
import html
import random
import re
import sys
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

try:
    from core.browser_stealth import apply_stealth_sync
    from core.linkedin_auth import (
        LinkedInAuthenticationError,
        ensure_linkedin_login,
        linkedin_url_requires_authentication,
    )
    from sources.company_filter import blacklisted_company_match
except ImportError:
    from backend.core.browser_stealth import apply_stealth_sync
    from backend.core.linkedin_auth import (
        LinkedInAuthenticationError,
        ensure_linkedin_login,
        linkedin_url_requires_authentication,
    )
    from backend.sources.company_filter import blacklisted_company_match


_LINKEDIN_HOSTS = frozenset({"linkedin.com", "www.linkedin.com"})
_SEARCH_PATHS = frozenset({"/jobs/search", "/jobs/search/", "/jobs/search-results", "/jobs/search-results/"})
_JOB_ID_RE = re.compile(r"/jobs/view/(?:[^/?#]*-)?(\d+)(?:[/?#]|$)")
_POSTING_TEXT_RE = re.compile(
    r"(?:\b(?:re)?posted\b.*|.*\bago\b.*|.*\bvor\s+\d+\b.*|.*\bveröffentlicht\b.*)",
    re.IGNORECASE,
)
_RELATIVE_POSTING_RE = re.compile(
    r"(?P<count>\d+)\+?\s*"
    r"(?P<unit>minute|minutes|hour|hours|day|days|week|weeks|month|months)\s+ago\b",
    re.IGNORECASE,
)
_GERMAN_RELATIVE_POSTING_RE = re.compile(
    r"\bvor\s+(?P<count>\d+)\+?\s*"
    r"(?P<unit>minute|minuten|stunde|stunden|tag|tagen|woche|wochen|monat|monaten)\b",
    re.IGNORECASE,
)
_PAGE_SIZE = 25


def _random_delay(page: Page, minimum_ms: int, maximum_ms: int) -> None:
    page.wait_for_timeout(round(random.uniform(minimum_ms, maximum_ms)))


def _login_llm():
    try:
        from core.config import load_llm_settings
        from core.llm_factory import create_llm
        from core.shared_config import get_llm
    except ImportError:
        from backend.core.config import load_llm_settings
        from backend.core.llm_factory import create_llm
        from backend.core.shared_config import get_llm

    settings = load_llm_settings()
    return create_llm(settings) if settings.get("provider") else get_llm()


@dataclass
class LinkedInBrowserResult:
    jobs: list[dict]
    errors: list[str]
    stopped: bool


def validate_linkedin_search_url(url: str) -> str:
    """Return a normalized LinkedIn job-search URL or raise a useful error."""
    normalized = html.unescape(str(url or "").strip())
    parsed = urlsplit(normalized)
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "https"
        or host not in _LINKEDIN_HOSTS
        or parsed.username
        or parsed.password
        or parsed.path not in _SEARCH_PATHS
    ):
        raise ValueError("Enter a LinkedIn jobs search-results URL")
    return urlunsplit(("https", "www.linkedin.com", parsed.path, parsed.query, ""))


def linkedin_search_label(url: str) -> str:
    query = dict(parse_qsl(urlsplit(validate_linkedin_search_url(url)).query))
    return query.get("keywords", "").strip() or "LinkedIn search URL"


def linkedin_search_page_url(url: str, start: int) -> str:
    parsed = urlsplit(validate_linkedin_search_url(url))
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "start"]
    if start:
        query.append(("start", str(start)))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def linkedin_job_id(url: str) -> str | None:
    match = _JOB_ID_RE.search(str(url or ""))
    return match.group(1) if match else None


def _job_links(page: Page) -> list[dict[str, str]]:
    """Read the ordered job links from the most likely scrollable results list."""
    return page.evaluate(
        r"""
        () => {
          const jobId = (href) => {
            const match = String(href || '').match(/\/jobs\/view\/(?:[^/?#]*-)?(\d+)(?:[/?#]|$)/);
            return match ? match[1] : null;
          };
          const resultsRoot = document.querySelector(
            '[data-testid="lazy-column"][componentkey="SearchResultsMainContent"]'
          );
          if (resultsRoot) {
            return [...resultsRoot.querySelectorAll(
              '[role="button"][componentkey^="job-card-component-ref-"]'
            )].map((card) => {
              const id = card.getAttribute('componentkey').match(/(\d+)$/)?.[1];
              const lines = (card.innerText || '').split('\n')
                .map((line) => line.replace(/\s+/g, ' ').trim()).filter(Boolean);
              return {
                id,
                url: `https://www.linkedin.com/jobs/view/${id}`,
                title: lines[0] || '',
              };
            }).filter((job) => job.id);
          }

          const allAnchors = [...document.querySelectorAll('a[href*="/jobs/view/"]')]
            .filter((anchor) => jobId(anchor.href));
          if (!allAnchors.length) return [];

          const candidates = new Map();
          for (const anchor of allAnchors) {
            let node = anchor.parentElement;
            while (node && node !== document.body) {
              const style = getComputedStyle(node);
              const scrollable = node.scrollHeight > node.clientHeight + 40
                && ['auto', 'scroll'].includes(style.overflowY);
              if (scrollable) {
                const ids = new Set(
                  [...node.querySelectorAll('a[href*="/jobs/view/"]')]
                    .map((item) => jobId(item.href)).filter(Boolean)
                );
                const current = candidates.get(node) || 0;
                candidates.set(node, Math.max(current, ids.size));
              }
              node = node.parentElement;
            }
          }

          let root = document;
          let bestCount = 0;
          for (const [node, count] of candidates) {
            if (count > bestCount) {
              root = node;
              bestCount = count;
            }
          }

          const seen = new Set();
          const jobs = [];
          for (const anchor of root.querySelectorAll('a[href*="/jobs/view/"]')) {
            const id = jobId(anchor.href);
            if (!id || seen.has(id)) continue;
            seen.add(id);
            const title = (anchor.innerText || anchor.getAttribute('aria-label') || '')
              .replace(/\s+/g, ' ').trim();
            jobs.push({ id, url: `https://www.linkedin.com/jobs/view/${id}`, title });
          }
          return jobs;
        }
        """
    )


def _scroll_job_list(page: Page) -> bool:
    return bool(
        page.evaluate(
            r"""
            () => {
              const resultsRoot = document.querySelector(
                '[data-testid="lazy-column"][componentkey="SearchResultsMainContent"]'
              );
              if (resultsRoot) {
                let node = resultsRoot;
                let scrollable = null;
                while (node && node !== document.body) {
                  const style = getComputedStyle(node);
                  if (node.scrollHeight > node.clientHeight + 40
                    && ['auto', 'scroll'].includes(style.overflowY)) {
                    scrollable = node;
                    break;
                  }
                  node = node.parentElement;
                }
                if (scrollable) {
                  const before = scrollable.scrollTop;
                  scrollable.scrollBy(0, Math.max(scrollable.clientHeight * 0.8, 500));
                  return scrollable.scrollTop !== before;
                }
                const cards = resultsRoot.querySelectorAll(
                  '[role="button"][componentkey^="job-card-component-ref-"]'
                );
                if (cards.length) {
                  cards[cards.length - 1].scrollIntoView({ block: 'end' });
                  return true;
                }
                return false;
              }

              const jobId = (href) => /\/jobs\/view\/(?:[^/?#]*-)?\d+(?:[/?#]|$)/.test(String(href || ''));
              const anchors = [...document.querySelectorAll('a[href*="/jobs/view/"]')]
                .filter((anchor) => jobId(anchor.href));
              let best = null;
              let bestCount = 0;
              const visited = new Set();
              for (const anchor of anchors) {
                let node = anchor.parentElement;
                while (node && node !== document.body) {
                  if (!visited.has(node)) {
                    visited.add(node);
                    const style = getComputedStyle(node);
                    const scrollable = node.scrollHeight > node.clientHeight + 40
                      && ['auto', 'scroll'].includes(style.overflowY);
                    if (scrollable) {
                      const count = [...node.querySelectorAll('a[href*="/jobs/view/"]')]
                        .filter((item) => jobId(item.href)).length;
                      if (count > bestCount) {
                        best = node;
                        bestCount = count;
                      }
                    }
                  }
                  node = node.parentElement;
                }
              }
              if (!best) {
                window.scrollBy(0, Math.max(window.innerHeight * 0.8, 600));
                return true;
              }
              const before = best.scrollTop;
              best.scrollBy(0, Math.max(best.clientHeight * 0.8, 500));
              return best.scrollTop !== before;
            }
            """
        )
    )


def _first_text(page: Page, selectors: list[str]) -> str:
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count():
            try:
                text = locator.first.inner_text(timeout=2_000).strip()
            except PlaywrightError:
                continue
            if text:
                return text
    return ""


def _normalize_posted_datetime(value: object, reference: datetime) -> str | None:
    """Normalize LinkedIn's structured or relative posting time to UTC."""
    raw = " ".join(str(value or "").split())
    if not raw:
        return None

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()

    lowered = raw.casefold()
    if "just now" in lowered or "gerade eben" in lowered:
        return reference.astimezone(timezone.utc).isoformat()
    if re.search(r"\b(today|heute)\b", lowered):
        return reference.astimezone(timezone.utc).isoformat()

    match = _RELATIVE_POSTING_RE.search(raw)
    unit = ""
    if match is None:
        match = _GERMAN_RELATIVE_POSTING_RE.search(raw)
    if match is None:
        return None

    count = int(match.group("count"))
    unit = match.group("unit").casefold()
    if unit.startswith("minute"):
        delta = timedelta(minutes=count)
    elif unit.startswith(("hour", "stunde")):
        delta = timedelta(hours=count)
    elif unit.startswith(("day", "tag")):
        delta = timedelta(days=count)
    elif unit.startswith(("week", "woche")):
        delta = timedelta(weeks=count)
    else:
        delta = timedelta(days=30 * count)
    return (reference.astimezone(timezone.utc) - delta).isoformat()


def _extract_posted_datetime(
    page: Page,
    body_lines: list[str],
    *,
    reference: datetime | None = None,
) -> tuple[str | None, str | None]:
    """Prefer structured page data, retaining visible text for classifier fallback."""
    structured_values = page.evaluate(
        r"""
        () => {
          const values = [];
          const add = (value) => {
            const normalized = String(value || '').trim();
            if (normalized && !values.includes(normalized)) values.push(normalized);
          };
          for (const node of document.querySelectorAll(
            'meta[itemprop="datePosted"], meta[property="datePosted"], main time[datetime]'
          )) {
            add(node.getAttribute('content') || node.getAttribute('datetime'));
          }
          const visit = (value) => {
            if (!value || typeof value !== 'object') return;
            if (Object.prototype.hasOwnProperty.call(value, 'datePosted')) add(value.datePosted);
            for (const child of Object.values(value)) visit(child);
          };
          for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
            try { visit(JSON.parse(script.textContent || 'null')); } catch (_) {}
          }
          return values;
        }
        """
    ) or []
    posting_texts = [line for line in body_lines if _POSTING_TEXT_RE.search(line)]
    captured_at = reference or datetime.now(timezone.utc)
    for candidate in [*structured_values, *posting_texts]:
        normalized = _normalize_posted_datetime(candidate, captured_at)
        if normalized:
            return normalized, posting_texts[0] if posting_texts else None
    return None, posting_texts[0] if posting_texts else None


def _is_fresh_enough(raw_posted_at: object, posted_after: datetime) -> bool:
    try:
        posted_at = datetime.fromisoformat(str(raw_posted_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    return posted_at.astimezone(timezone.utc) >= posted_after.astimezone(timezone.utc)


def _extract_detail(page: Page, card: dict[str, str]) -> dict:
    canonical_url = f"https://www.linkedin.com/jobs/view/{card['id']}"
    _random_delay(page, 700, 2_200)
    page.goto(canonical_url, wait_until="domcontentloaded", timeout=60_000)
    if linkedin_url_requires_authentication(page.url):
        raise LinkedInAuthenticationError("The saved LinkedIn session is no longer signed in")

    try:
        page.wait_for_selector(
            '[data-sdui-component$=".aboutTheJob"], '
            ".jobs-description-content__text, "
            ".jobs-box__html-content, "
            ".show-more-less-html__markup",
            timeout=10_000,
        )
    except PlaywrightError:
        page.wait_for_timeout(500)
    title_parts = [part.strip() for part in page.title().split("|")]
    title = title_parts[0] if title_parts else card.get("title", "")
    company = title_parts[1] if len(title_parts) >= 3 else ""
    if not company:
        company = _first_text(page, ['main a[href*="/company/"]', 'a[href*="/company/"]'])

    about = _first_text(
        page,
        [
            '[data-sdui-component$=".aboutTheJob"]',
            ".jobs-description-content__text",
            ".jobs-box__html-content",
            ".show-more-less-html__markup",
        ],
    )
    description_lines = [line.strip() for line in about.splitlines() if line.strip()]
    if description_lines and description_lines[0].casefold() in {"about the job", "über den job"}:
        description_lines.pop(0)
    description = "\n".join(description_lines)

    body_lines = [
        line.strip()
        for line in page.locator("body").inner_text(timeout=5_000).splitlines()
        if line.strip()
    ]
    date_posted, date_posted_text = _extract_posted_datetime(page, body_lines)
    location = ""
    for line in body_lines:
        if " · " in line and any(
            marker in line.casefold()
            for marker in ("ago", "applicant", "bewerber", "vor ")
        ):
            location = line.split(" · ", 1)[0].strip()
            break

    normalized_lines = {line.casefold() for line in body_lines}
    job_type = next(
        (
            value
            for label, value in (
                ("full-time", "fulltime"),
                ("part-time", "parttime"),
                ("contract", "contract"),
                ("internship", "internship"),
                ("temporary", "temporary"),
            )
            if label in normalized_lines
        ),
        None,
    )

    return {
        "id": f"li-{card['id']}",
        "job_url": canonical_url,
        "job_url_direct": None,
        "title": title or card.get("title", ""),
        "company": company,
        "location": location,
        "date_posted": date_posted,
        "date_posted_text": date_posted_text,
        "job_type": job_type,
        "is_remote": "remote" in normalized_lines,
        "job_level": None,
        "job_function": None,
        "company_industry": None,
        "description": description or None,
        "easy_apply": any(line.casefold() == "easy apply" for line in body_lines),
    }


def _scrape_over_cdp(
    *,
    cdp_url: str,
    validated_url: str,
    max_jobs: int,
    max_results_to_inspect: int | None,
    known_ids: set[str],
    posted_after: datetime | None,
    blacklisted_companies: list[str] | None,
    stop_requested: Callable[[], bool],
    report: Callable[[str], None],
) -> LinkedInBrowserResult:
    jobs: list[dict] = []
    errors: list[str] = []
    queued_ids: set[str] = set()
    inspected_results = 0
    initial_query = dict(parse_qsl(urlsplit(validated_url).query))
    start = int(initial_query.get("start", "0")) if initial_query.get("start", "0").isdigit() else 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("Browser Use did not provide a browser context")
        context = browser.contexts[0]
        apply_stealth_sync(context)
        search_page = context.new_page()
        detail_page = context.new_page()
        try:
            empty_pages = 0
            found_results_page = False
            scan_limit_reached = False
            while len(jobs) < max_jobs and start < 1_000 and not stop_requested():
                page_url = linkedin_search_page_url(validated_url, start)
                report(f"Loading personalized LinkedIn results at offset {start}")
                _random_delay(search_page, 900, 2_800)
                search_page.goto(page_url, wait_until="domcontentloaded", timeout=60_000)
                if linkedin_url_requires_authentication(search_page.url):
                    raise LinkedInAuthenticationError(
                        "The saved LinkedIn session is no longer signed in"
                    )

                try:
                    search_page.wait_for_selector(
                        '[role="button"][componentkey^="job-card-component-ref-"], '
                        'a[href*="/jobs/view/"]',
                        timeout=20_000,
                    )
                except PlaywrightError as exc:
                    if found_results_page:
                        report(f"No more LinkedIn results at offset {start}")
                        break
                    raise RuntimeError(
                        "LinkedIn returned no job cards; the session may be expired or the page layout changed"
                    ) from exc

                page_cards: list[dict[str, str]] = []
                page_ids: set[str] = set()
                idle_rounds = 0
                for _ in range(40):
                    snapshot = _job_links(search_page)
                    new_on_page = 0
                    for card in snapshot:
                        if card["id"] not in page_ids:
                            page_ids.add(card["id"])
                            page_cards.append(card)
                            new_on_page += 1
                    idle_rounds = 0 if new_on_page else idle_rounds + 1
                    if len(page_cards) >= _PAGE_SIZE or idle_rounds >= 4:
                        break
                    if not _scroll_job_list(search_page):
                        idle_rounds += 1
                    _random_delay(search_page, 450, 1_250)

                if not page_cards:
                    empty_pages += 1
                    if empty_pages >= 2:
                        break
                else:
                    found_results_page = True
                    empty_pages = 0
                report(f"Found {len(page_cards)} ordered jobs at offset {start}")

                for card in page_cards:
                    if len(jobs) >= max_jobs or stop_requested():
                        break
                    if (
                        max_results_to_inspect is not None
                        and inspected_results >= max_results_to_inspect
                    ):
                        scan_limit_reached = True
                        break
                    inspected_results += 1
                    job_id = card["id"]
                    if job_id in known_ids or job_id in queued_ids:
                        continue
                    queued_ids.add(job_id)
                    try:
                        raw_job = _extract_detail(detail_page, card)
                    except LinkedInAuthenticationError:
                        raise
                    except Exception as exc:
                        message = f"LinkedIn job {job_id}: {exc}"
                        errors.append(message)
                        report(f"⚠️ {message}")
                        continue
                    blocked_company = blacklisted_company_match(
                        raw_job.get("company"),
                        blacklisted_companies,
                    )
                    if blocked_company:
                        report(
                            "Skipped blacklisted company before classification: "
                            f"{raw_job.get('company') or blocked_company}"
                        )
                        continue
                    if posted_after is not None:
                        raw_posted_at = raw_job.get("date_posted")
                        if not raw_posted_at:
                            report(f"Skipped LinkedIn job {job_id}: posting time is unknown")
                            continue
                        if not _is_fresh_enough(raw_posted_at, posted_after):
                            report(
                                f"Skipped LinkedIn job {job_id}: "
                                "older than this scan's freshness cutoff"
                            )
                            continue
                    jobs.append(raw_job)
                    report(
                        f"Collected {len(jobs)} of {max_jobs}: "
                        f"{raw_job.get('title') or job_id}"
                    )

                if scan_limit_reached:
                    report(
                        f"Stopped after inspecting the first {inspected_results} LinkedIn results"
                    )
                    break
                start += _PAGE_SIZE
        finally:
            for page in (detail_page, search_page):
                try:
                    page.close()
                except PlaywrightError:
                    pass

    if not jobs and errors and not stop_requested():
        raise RuntimeError("LinkedIn jobs could not be read: " + "; ".join(errors[:3]))

    return LinkedInBrowserResult(jobs=jobs, errors=errors, stopped=stop_requested())


async def scrape_linkedin_search_url(
    *,
    search_url: str,
    max_jobs: int,
    max_results_to_inspect: int | None = None,
    profile_dir: str | Path,
    user_agent: str | None = None,
    existing_job_ids: set[str] | None = None,
    posted_after: datetime | None = None,
    blacklisted_companies: list[str] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> LinkedInBrowserResult:
    """Collect up to ``max_jobs`` new jobs in the account-specific list order."""
    from browser_use import BrowserSession

    validated_url = validate_linkedin_search_url(search_url)
    if not 1 <= max_jobs <= 500:
        raise ValueError("max_jobs must be between 1 and 500")
    if max_results_to_inspect is not None and max_results_to_inspect < 1:
        raise ValueError("max_results_to_inspect must be at least 1")

    stop_requested = should_stop or (lambda: False)
    report = on_progress or print
    try:
        from core.shared_config import launch_profile_browser, stop_profile_browser
    except ImportError:
        from backend.core.shared_config import launch_profile_browser, stop_profile_browser

    cdp_url, browser_process = launch_profile_browser(
        profile_dir,
        headless=True,
        user_agent=user_agent,
    )
    if cdp_url:
        browser_session = BrowserSession(cdp_url=cdp_url, captcha_solver=False)
    else:
        browser_session = BrowserSession(
            user_data_dir=str(profile_dir),
            headless=True,
            user_agent=user_agent,
            chromium_sandbox=(sys.platform != "linux"),
            args=["--disable-blink-features=AutomationControlled"],
            captcha_solver=False,
        )

    report("Opening the saved LinkedIn session in headless Browser Use")
    try:
        await browser_session.start()
        if not browser_session.cdp_url:
            raise RuntimeError("Browser Use did not expose its Playwright connection")
        await ensure_linkedin_login(
            browser_session,
            llm_factory=_login_llm,
            report=report,
        )
        for scrape_attempt in range(2):
            try:
                return await asyncio.to_thread(
                    _scrape_over_cdp,
                    cdp_url=browser_session.cdp_url,
                    validated_url=validated_url,
                    max_jobs=max_jobs,
                    max_results_to_inspect=max_results_to_inspect,
                    known_ids=set(existing_job_ids or set()),
                    posted_after=posted_after,
                    blacklisted_companies=blacklisted_companies,
                    stop_requested=stop_requested,
                    report=report,
                )
            except LinkedInAuthenticationError:
                if scrape_attempt:
                    raise
                report("LinkedIn signed out during the scan; attempting one recovery login")
                await ensure_linkedin_login(
                    browser_session,
                    llm_factory=_login_llm,
                    report=report,
                )
        raise AssertionError("unreachable")
    except PlaywrightError as exc:
        raise RuntimeError(f"Could not control the LinkedIn browser: {exc}") from exc
    finally:
        try:
            await browser_session.close()
        except Exception as exc:
            report(f"⚠️ LinkedIn browser cleanup failed: {exc}")
        stop_profile_browser(browser_process)
