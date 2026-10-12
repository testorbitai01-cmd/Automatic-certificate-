"""Single entry point for writing audit records."""
import ipaddress

from django.conf import settings

from .models import AuditLog

# Keys that must never reach the audit log, even if a caller passes them.
_FORBIDDEN_KEYS = {"password", "token", "secret", "smtp_password", "app_password", "otp", "session"}


def _clean(metadata):
    clean = {}
    for key, value in (metadata or {}).items():
        if any(word in key.lower() for word in _FORBIDDEN_KEYS):
            continue
        clean[key] = value
    return clean


def client_ip(request):
    """Client address, honouring X-Forwarded-For only for the configured number of trusted proxies.

    With TRUSTED_PROXY_COUNT = n, the n-th address from the right of X-Forwarded-For is used (each
    trusted proxy appends the address it received from). Left-most entries can be forged by the client,
    so they are never trusted. With 0 (default), REMOTE_ADDR is used.
    """
    if request is None:
        return None
    remote = request.META.get("REMOTE_ADDR") or None
    proxies = settings.TRUSTED_PROXY_COUNT
    if proxies <= 0:
        return remote
    chain = [p.strip() for p in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if p.strip()]
    if len(chain) < proxies:
        return remote
    candidate = chain[-proxies]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return remote
    return candidate


def record(action, *, actor=None, actor_label=None, entity=None, metadata=None, request=None):
    if actor is not None and not getattr(actor, "is_authenticated", False):
        actor = None
    if actor_label is None:
        actor_label = actor.email if actor is not None else "system"
    entity_type = entity_id = ""
    if entity is not None:
        entity_type = entity._meta.label
        entity_id = str(entity.pk)
    return AuditLog.objects.create(
        actor=actor,
        actor_label=actor_label,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        metadata=_clean(metadata),
        ip_address=client_ip(request),
    )
