"""Real-browser tests (Playwright driving the installed Microsoft Edge).

Run with:  .venv\\Scripts\\python -m pytest -m browser
They are excluded from the default run (see pytest.ini) and skipped if Playwright is not installed.
"""
import importlib
import os

import pytest

pytest.importorskip("playwright")

# Playwright's sync API runs an event loop in the test thread; the ORM calls made by the
# test code itself (setup/assertions) are still synchronous, which is safe here.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

INITIAL_DOMAINS = importlib.import_module("apps.registry.migrations.0003_seed_initial_domains").INITIAL_DOMAINS

DESKTOP = {"width": 1280, "height": 800}
MOBILE = {"width": 390, "height": 844}


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/browser" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.browser)


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    return {**browser_type_launch_args, "channel": os.environ.get("PLAYWRIGHT_CHANNEL", "msedge")}


@pytest.fixture
def site(live_server, pg_transactional_db, settings):
    """Live server with the seeded domains present (transactional tests flush migration data)."""
    from apps.registry.models import Domain, comparison_key

    for name in INITIAL_DOMAINS:
        Domain.objects.get_or_create(normalized_key=comparison_key(name),
                                     defaults={"name": name, "status": Domain.Status.ACTIVE})
    settings.PUBLIC_BASE_URL = live_server.url
    return live_server
