import pytest
from django.conf import settings
from django.urls import reverse

from . import factories as f


def test_secure_cookie_and_header_settings():
    assert settings.SESSION_COOKIE_HTTPONLY
    assert settings.SESSION_COOKIE_SECURE
    assert settings.CSRF_COOKIE_SECURE
    assert settings.X_FRAME_OPTIONS == "DENY"


def test_private_storage_has_no_public_url():
    assert settings.STORAGES["default"]["OPTIONS"]["base_url"] is None
    assert "media" not in " ".join(str(p) for p in settings.TEMPLATES[0]["DIRS"])


def test_production_issuance_disabled_by_default():
    assert settings.CERT_PRODUCTION_ISSUANCE_ENABLED is False


@pytest.mark.django_db
def test_rendered_pages_never_contain_smtp_password(client, settings):
    settings.EMAIL_HOST_PASSWORD = "super-secret-app-password"
    client.force_login(f.hr_admin())
    body = client.get(reverse("core:dashboard")).content.decode()
    assert "super-secret-app-password" not in body


@pytest.mark.django_db
def test_health_endpoint(client):
    assert client.get(reverse("core:health")).json() == {"status": "ok"}
