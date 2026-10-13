"""Logging helpers.

Correction links carry a secret token in the URL path (/correct/<token>/). Django logs the request
path for 4xx/5xx responses (logger "django.request") and the development server logs every request
line ("django.server"). This filter replaces the token with "[redacted]" before any handler sees it.
Reverse-proxy and gunicorn access logs are outside Django and must be configured separately (README).
"""
import logging
import re

SECRET_PATH = re.compile(r"(/correct/)(?!done/)[^/\s?#\"']+")


def redact(text):
    return SECRET_PATH.sub(r"\1[redacted]", text)


class RedactSecretPaths(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        # Django attaches the request to these records; error reporters may print its path.
        request = getattr(record, "request", None)
        if request is not None and SECRET_PATH.search(getattr(request, "path", "") or ""):
            record.request = None
        return True
