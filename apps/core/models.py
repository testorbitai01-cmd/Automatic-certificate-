from django.conf import settings
from django.db import models
from django.utils import timezone


class AuditLogImmutableError(Exception):
    """Raised on any attempt to change or remove audit history."""


class AuditLogQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AuditLogImmutableError("Audit log entries cannot be updated.")

    def delete(self):
        raise AuditLogImmutableError("Audit log entries cannot be deleted.")


class AuditLog(models.Model):
    """Append-only record of security-relevant and workflow actions.

    Immutability is enforced here and, on PostgreSQL, by a database trigger
    (see migration 0002) so it also holds for raw SQL.
    """

    created_at = models.DateTimeField(default=timezone.now, db_index=True, editable=False)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # Kept even if the actor is anonymous (student) or a system process.
    actor_label = models.CharField(max_length=255)
    action = models.CharField(max_length=100, db_index=True)
    entity_type = models.CharField(max_length=100, blank=True)
    entity_id = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["entity_type", "entity_id"])]
        default_permissions = ("view",)
        permissions = [
            ("view_sensitive_audit", "Can view sensitive audit events"),
            ("export_data", "Can export reports and records as CSV"),
        ]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise AuditLogImmutableError("Audit log entries cannot be updated.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditLogImmutableError("Audit log entries cannot be deleted.")

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.actor_label} {self.action}"


class Setting(models.Model):
    """Office-configurable policy values (never secrets; those live in the environment)."""

    key = models.CharField(max_length=100, unique=True)
    value = models.JSONField()
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [("manage_settings", "Can manage system settings")]

    def __str__(self):
        return self.key


class RateLimitCounter(models.Model):
    """Fixed-window request counter shared by all web processes (see apps/core/ratelimit.py)."""

    key = models.CharField(max_length=64)  # sha256 of scope + identifier; never raw emails/IPs
    window_start = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["key", "window_start"], name="unique_ratelimit_window")]


class Job(models.Model):
    """Persistent background job, claimed by workers with row locks."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    kind = models.CharField(max_length=50)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    idempotency_key = models.CharField(max_length=128, unique=True, null=True, blank=True)
    run_after = models.DateTimeField(default=timezone.now)
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=3)
    locked_at = models.DateTimeField(null=True, blank=True)
    locked_by = models.CharField(max_length=100, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status", "run_after"])]

    def __str__(self):
        return f"{self.kind}#{self.pk} ({self.status})"
