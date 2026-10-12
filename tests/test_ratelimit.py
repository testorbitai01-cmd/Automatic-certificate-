"""Database-backed rate limiting and proxy-aware client IP resolution."""
import datetime as dt
import threading

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.core import ratelimit
from apps.core.audit import client_ip
from apps.core.models import RateLimitCounter

from . import factories as f

pytestmark = pytest.mark.django_db


def test_hit_counts_per_scope_and_identifier():
    assert [ratelimit.hit("a", "x", 60) for _ in range(3)] == [1, 2, 3]
    assert ratelimit.hit("a", "y", 60) == 1
    assert ratelimit.hit("b", "x", 60) == 1
    assert ratelimit.count("a", "x", 60) == 3
    ratelimit.reset("a", "x")
    assert ratelimit.count("a", "x", 60) == 0
    assert ratelimit.count("a", "y", 60) == 1


def test_new_window_starts_from_zero(monkeypatch):
    start = timezone.now()
    ratelimit.hit("a", "x", 60)
    ratelimit.hit("a", "x", 60)
    monkeypatch.setattr(timezone, "now", lambda: start + dt.timedelta(seconds=61))
    assert ratelimit.hit("a", "x", 60) == 1


def test_identifiers_are_hashed_not_stored_raw():
    ratelimit.hit("login-failures", "person@test.invalid|203.0.113.9", 60)
    row = RateLimitCounter.objects.get()
    assert len(row.key) == 64 and "@" not in row.key and "203.0.113.9" not in row.key


def test_purge_removes_only_expired_windows(monkeypatch):
    ratelimit.hit("a", "old", 60)
    later = timezone.now() + dt.timedelta(minutes=5)
    monkeypatch.setattr(timezone, "now", lambda: later)
    ratelimit.hit("a", "new", 60)
    assert ratelimit.purge_expired() == 1
    assert ratelimit.count("a", "new", 60) == 1


def test_login_lockout_is_shared_across_processes(client, settings):
    """Clearing the local cache simulates the next request landing on another worker process."""
    settings.LOGIN_MAX_FAILURES = 3
    u = f.hr_admin()
    for _ in range(3):
        client.post(reverse("accounts:login"), {"username": u.email, "password": "wrong"})
        cache.clear()
    resp = client.post(reverse("accounts:login"), {"username": u.email, "password": f.PASSWORD})
    assert resp.status_code == 429
    assert "_auth_user_id" not in client.session


def test_successful_login_resets_failures(client, settings):
    settings.LOGIN_MAX_FAILURES = 3
    u = f.hr_admin()
    for _ in range(2):
        client.post(reverse("accounts:login"), {"username": u.email, "password": "wrong"})
    assert client.post(reverse("accounts:login"), {"username": u.email, "password": f.PASSWORD}).status_code == 302
    client.logout()
    for _ in range(2):
        client.post(reverse("accounts:login"), {"username": u.email, "password": "wrong"})
    assert client.post(reverse("accounts:login"), {"username": u.email, "password": f.PASSWORD}).status_code == 302


def test_registration_limit_is_shared_across_processes(client, settings):
    settings.REGISTRATION_POSTS_PER_HOUR = 2
    camp = f.campaign(f.hr_admin())
    target = reverse("registry:register", kwargs={"token": camp.public_token})
    for _ in range(2):
        assert client.post(target, {"mobile": "1"}).status_code == 200
        cache.clear()
    assert client.post(target, {"mobile": "1"}).status_code == 429


# --- client_ip ---------------------------------------------------------------

def req(remote="10.0.0.5", xff=None):
    extra = {"REMOTE_ADDR": remote}
    if xff is not None:
        extra["HTTP_X_FORWARDED_FOR"] = xff
    return RequestFactory().get("/", **extra)


def test_forwarded_header_ignored_without_trusted_proxy(settings):
    settings.TRUSTED_PROXY_COUNT = 0
    assert client_ip(req(xff="198.51.100.7")) == "10.0.0.5"


def test_one_trusted_proxy_uses_last_forwarded_address(settings):
    settings.TRUSTED_PROXY_COUNT = 1
    assert client_ip(req(xff="198.51.100.7")) == "198.51.100.7"
    # A client-supplied (forged) left-most entry is ignored.
    assert client_ip(req(xff="1.2.3.4, 198.51.100.7")) == "198.51.100.7"


def test_two_trusted_proxies(settings):
    settings.TRUSTED_PROXY_COUNT = 2
    assert client_ip(req(xff="1.2.3.4, 198.51.100.7, 10.0.0.2")) == "198.51.100.7"


@pytest.mark.parametrize("xff", [None, "", "not-an-ip"])
def test_missing_or_invalid_forwarded_header_falls_back(settings, xff):
    settings.TRUSTED_PROXY_COUNT = 1
    assert client_ip(req(xff=xff)) == "10.0.0.5"


def test_limits_apply_per_real_client_behind_proxy(client, settings):
    settings.TRUSTED_PROXY_COUNT = 1
    settings.REGISTRATION_POSTS_PER_HOUR = 1
    camp = f.campaign(f.hr_admin())
    target = reverse("registry:register", kwargs={"token": camp.public_token})
    proxy = {"REMOTE_ADDR": "10.0.0.2"}
    assert client.post(target, {"mobile": "1"}, HTTP_X_FORWARDED_FOR="198.51.100.1", **proxy).status_code == 200
    assert client.post(target, {"mobile": "1"}, HTTP_X_FORWARDED_FOR="198.51.100.1", **proxy).status_code == 429
    # A different student behind the same proxy is not affected.
    assert client.post(target, {"mobile": "1"}, HTTP_X_FORWARDED_FOR="198.51.100.2", **proxy).status_code == 200


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="true concurrency needs PostgreSQL")
def test_concurrent_hits_are_all_counted(pg_transactional_db):
    barrier = threading.Barrier(10)
    results = []

    def worker():
        from django.db import connection as conn
        barrier.wait()
        try:
            results.append(ratelimit.hit("race", "same-client", 600))
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == list(range(1, 11))
    assert ratelimit.count("race", "same-client", 600) == 10
