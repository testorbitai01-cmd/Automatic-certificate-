"""Test settings: production settings with test-only environment values."""
import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key")
os.environ.setdefault("DJANGO_DEBUG", "false")
os.environ.setdefault("DJANGO_SECURE_SSL_REDIRECT", "false")
os.environ["CERT_PRODUCTION_ISSUANCE_ENABLED"] = "false"

from .settings import *  # noqa: E402,F401,F403

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.Argon2PasswordHasher"]
