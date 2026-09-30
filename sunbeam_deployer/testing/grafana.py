"""Grafana helpers shared by the end-to-end tests."""

from __future__ import annotations

from collections.abc import Callable

from playwright.sync_api import Page

GRAFANA_USER = "admin"

# ``screenshot(page, name)`` saves a full-page PNG to the artifacts dir.
Screenshot = Callable[[Page, str], None]


def login(page: Page, url: str, password: str) -> None:
    """Log into Grafana through the login form."""
    page.goto(f"{url}/login")
    page.locator('input[name="user"]').fill(GRAFANA_USER)
    page.locator('input[name="password"]').fill(password)
    page.locator('input[name="password"]').press("Enter")
    page.wait_for_url(lambda u: "/login" not in u, timeout=30_000)
