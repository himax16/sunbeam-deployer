"""Browser tests for the embedded COS Grafana dashboards.

Mirrors the walkthrough in the Canonical OpenStack observability docs:
log into Grafana as ``admin``, find the OpenStack dashboards, and check
that each renders its panels without errors.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Browser, Page, expect

from sunbeam_deployer.testing.grafana import GRAFANA_USER, Screenshot, login

# Documented dashboards (title substrings, matched case-insensitively).
DASHBOARDS = (
    "OpenStack Service Overview",
    "OpenStack Cloud Usage",
    "OpenStack Compute Overview",
    "Capacity",
    "OpenStack Project Overview",
    "OpenStack Logging",
)

# Panel error markers across Grafana versions (9.x and 10+).
_PANEL_ERROR = (
    '.panel-info-corner--error, [data-testid="data-testid Panel status error"]'
)
_PANEL = ".react-grid-item"


def _find_dashboard(page: Page, url: str, title: str) -> dict:
    """Return the Grafana search hit whose title contains *title*."""
    resp = page.request.get(
        f"{url}/api/search", params={"type": "dash-db", "query": title}
    )
    assert resp.ok, f"/api/search failed: HTTP {resp.status}"
    hits = [h for h in resp.json() if title.lower() in h["title"].lower()]
    assert hits, f"Dashboard matching {title!r} not found"
    return hits[0]


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _open_dashboard(page: Page, url: str, title: str) -> None:
    """Navigate to the dashboard and wait for its panels to load."""
    hit = _find_dashboard(page, url, title)
    # ``hit["url"]`` already includes Grafana's sub-path.
    origin = re.match(r"https?://[^/]+", url)
    assert origin is not None
    page.goto(origin.group(0) + hit["url"] + "?from=now-1h&to=now")
    expect(page.locator(_PANEL).first).to_be_visible(timeout=60_000)
    page.wait_for_load_state("networkidle", timeout=60_000)


def test_login(
    browser: Browser,
    grafana_url: str,
    grafana_password: str,
    screenshot: Screenshot,
) -> None:
    """The admin user can log in and reaches the landing page."""
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{grafana_url}/login")
        page.locator('input[name="user"]').wait_for()
        screenshot(page, "login-page")
        login(page, grafana_url, grafana_password)
        page.wait_for_load_state("networkidle")
        screenshot(page, "landing-page")
        assert "/login" not in page.url
        resp = page.request.get(f"{grafana_url}/api/user")
        assert resp.ok
        assert resp.json()["login"] == GRAFANA_USER
    finally:
        context.close()


def test_login_rejects_bad_password(
    browser: Browser, grafana_url: str, screenshot: Screenshot
) -> None:
    """A wrong password keeps the user on the login page."""
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{grafana_url}/login")
        page.locator('input[name="user"]').fill(GRAFANA_USER)
        page.locator('input[name="password"]').fill("not-the-password")
        page.locator('input[name="password"]').press("Enter")
        error = page.get_by_text(re.compile("invalid", re.I))
        error.first.wait_for(timeout=15_000)
        screenshot(page, "login-bad-password")
        expect(error.first).to_be_visible()
        assert "/login" in page.url
    finally:
        context.close()


@pytest.mark.parametrize("title", DASHBOARDS)
def test_dashboard_listed(
    grafana_page: Page,
    grafana_url: str,
    screenshot: Screenshot,
    title: str,
) -> None:
    """Each documented dashboard shows up in the dashboard browser."""
    grafana_page.goto(f"{grafana_url}/dashboards?query={title}")
    entry = grafana_page.get_by_text(re.compile(re.escape(title), re.I))
    try:
        expect(entry.first).to_be_visible(timeout=30_000)
    finally:
        screenshot(grafana_page, f"list-{_slug(title)}")


@pytest.mark.parametrize("title", DASHBOARDS)
def test_dashboard_renders(
    grafana_page: Page,
    grafana_url: str,
    screenshot: Screenshot,
    title: str,
) -> None:
    """Each documented dashboard renders panels without panel errors."""
    _open_dashboard(grafana_page, grafana_url, title)
    screenshot(grafana_page, f"dashboard-{_slug(title)}")

    assert grafana_page.locator(_PANEL).count() > 0
    errors = grafana_page.locator(_PANEL_ERROR)
    assert errors.count() == 0, (
        f"{errors.count()} panel(s) on {title!r} report errors"
    )


def test_service_overview_has_data(
    grafana_page: Page, grafana_url: str, screenshot: Screenshot
) -> None:
    """The OpenStack Service Overview dashboard shows live metrics.

    At least one panel must render data rather than ``No data``.
    """
    _open_dashboard(grafana_page, grafana_url, "OpenStack Service Overview")
    screenshot(grafana_page, "service-overview-data")
    panels = grafana_page.locator(_PANEL)
    total = panels.count()
    empty = panels.filter(has_text=re.compile(r"^\s*No data\s*$", re.M))
    assert empty.count() < total, "Every panel reports 'No data'"
