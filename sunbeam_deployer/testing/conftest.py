"""Shared fixtures for Grafana end-to-end tests.

Connection details come from the environment, set by
``sunbeam-deployer test observability``:

- ``GRAFANA_URL``: Grafana base URL (e.g. ``http://host/observability-grafana``)
- ``GRAFANA_PASSWORD``: Grafana ``admin`` password
- ``TESTING_ARTIFACTS_DIR``: where screenshots are saved
  (default: ``./testing-artifacts``)
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Page

from sunbeam_deployer.testing.grafana import Screenshot, login


@pytest.fixture(scope="session")
def grafana_url() -> str:
    """Grafana base URL without trailing slash."""
    url = os.environ.get("GRAFANA_URL")
    if not url:
        pytest.skip("GRAFANA_URL is not set")
    return url.rstrip("/")


@pytest.fixture(scope="session")
def grafana_password() -> str:
    """Grafana admin password."""
    password = os.environ.get("GRAFANA_PASSWORD")
    if not password:
        pytest.skip("GRAFANA_PASSWORD is not set")
    return password


@pytest.fixture(scope="session")
def artifacts_dir() -> Path:
    """Directory where screenshots are written."""
    path = Path(os.environ.get("TESTING_ARTIFACTS_DIR", "testing-artifacts"))
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="session")
def screenshot(artifacts_dir: Path) -> Screenshot:
    """Return a helper that saves a full-page screenshot by name."""

    def _take(page: Page, name: str) -> None:
        path = artifacts_dir / f"{name}.png"
        page.screenshot(path=str(path), full_page=True)

    return _take


@pytest.fixture(scope="session")
def grafana_storage_state(
    browser: Browser,
    grafana_url: str,
    grafana_password: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> str:
    """Log in once and persist the session cookies for other tests."""
    context = browser.new_context()
    page = context.new_page()
    login(page, grafana_url, grafana_password)
    state = tmp_path_factory.mktemp("grafana") / "state.json"
    context.storage_state(path=str(state))
    context.close()
    return str(state)


@pytest.fixture
def grafana_page(
    browser: Browser, grafana_storage_state: str
) -> Iterator[Page]:
    """A page already authenticated against Grafana."""
    context: BrowserContext = browser.new_context(
        storage_state=grafana_storage_state,
        viewport={"width": 1600, "height": 1000},
    )
    page = context.new_page()
    yield page
    context.close()
