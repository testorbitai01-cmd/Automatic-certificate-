import io
import shutil
import tempfile

import pytest
from django.core.management import call_command
from django.db import connection
from pytest_django.plugin import blocking_manager_key


@pytest.fixture(autouse=True)
def _private_storage(settings):
    path = tempfile.mkdtemp(prefix="gradtwin-test-")
    settings.STORAGES = {**settings.STORAGES,
                         "default": {"BACKEND": "django.core.files.storage.FileSystemStorage",
                                     "OPTIONS": {"location": path, "base_url": None}}}
    yield
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _roles(db):
    call_command("sync_roles", stdout=io.StringIO())


AUDIT_TRIGGER = "ALTER TABLE core_auditlog {} TRIGGER core_auditlog_append_only"


@pytest.fixture
def pg_transactional_db(transactional_db):
    """Transactional DB for tests that need real commits (e.g. concurrency).

    pytest-django cleans up after such tests with TRUNCATE, which the append-only audit
    trigger rightly blocks. The trigger is disabled only for that cleanup and re-enabled
    immediately afterwards (see pytest_runtest_teardown below). Production is unaffected.
    """
    yield
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(AUDIT_TRIGGER.format("DISABLE"))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    yield  # all fixture finalizers, including pytest-django's flush, have run
    # After the last test the test database has already been dropped, so there is nothing to re-enable.
    if (nextitem is not None and "pg_transactional_db" in getattr(item, "fixturenames", ())
            and connection.vendor == "postgresql"):
        with item.config.stash[blocking_manager_key].unblock(), connection.cursor() as cursor:
            cursor.execute(AUDIT_TRIGGER.format("ENABLE"))


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()
