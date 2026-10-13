from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.choices import CertificateType


class CertificateRequest(models.Model):
    """Approval of one certificate type for one internship. Each type is decided independently."""

    class Approval(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CORRECTION_REQUESTED = "correction_requested", "Correction requested"

    internship = models.ForeignKey("internships.Internship", on_delete=models.PROTECT,
                                   related_name="certificate_requests")
    cert_type = models.CharField(max_length=20, choices=CertificateType.choices)
    approval_status = models.CharField(max_length=25, choices=Approval.choices, default=Approval.PENDING)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["internship", "cert_type"], name="one_request_per_type_per_internship"),
            models.CheckConstraint(
                condition=Q(approval_status="pending") | Q(decided_by__isnull=False, decided_at__isnull=False),
                name="certificate_decision_has_reviewer",
            ),
            models.CheckConstraint(
                condition=~Q(approval_status__in=["rejected", "correction_requested"]) | ~Q(reason=""),
                name="certificate_rejection_requires_reason",
            ),
        ]
        permissions = [
            ("approve_certificate", "Can approve or reject individual certificates"),
            # Office rule: the final LOR decision is reserved for Super Admins.
            ("approve_lor", "Can make the final decision on letters of recommendation"),
        ]

    def __str__(self):
        return f"{self.get_cert_type_display()} – {self.internship.student}"


class GeneratedFile(models.Model):
    """A generated PDF in private storage. Recorded only after the file is verified non-empty and valid."""

    file = models.FileField(upload_to="issued/%Y/%m/")
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveIntegerField()
    page_count = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)


class Issuance(models.Model):
    """One version of an issued certificate. Corrections create a new version that supersedes the old one."""

    class Status(models.TextChoices):
        NOT_ISSUED = "not_issued", "Not issued"
        ISSUED = "issued", "Issued"
        CANCELLED = "cancelled", "Cancelled"
        REVOKED = "revoked", "Revoked"
        SUPERSEDED = "superseded", "Superseded"

    class PdfStatus(models.TextChoices):
        NOT_GENERATED = "not_generated", "Not generated"
        PENDING = "pending", "Pending"
        GENERATED = "generated", "Generated"
        FAILED = "failed", "Failed"

    class Distribution(models.TextChoices):
        UNDECIDED = "undecided", "Not chosen"
        EMAIL = "email", "Email"
        HARD_COPY = "hard_copy", "Hard copy"
        BOTH = "both", "Email and hard copy"

    class PrintStatus(models.TextChoices):
        NOT_REQUIRED = "not_required", "Not required"
        PENDING_PRINT = "pending_print", "Pending print"
        PRINTED = "printed", "Printed"
        CANCELLED = "cancelled", "Cancelled"

    class HandoverStatus(models.TextChoices):
        NOT_REQUIRED = "not_required", "Not required"
        PENDING = "pending_handover", "Pending handover"
        HANDED_OVER = "handed_over", "Handed over"

    request = models.ForeignKey(CertificateRequest, on_delete=models.PROTECT, related_name="issuances")
    version_no = models.PositiveIntegerField(default=1)
    template_version = models.ForeignKey("doctemplates.TemplateVersion", on_delete=models.PROTECT,
                                         related_name="issuances")
    is_production = models.BooleanField(default=False)
    # Internal reference; printed only if the office approves adding it to the template.
    reference_no = models.CharField(max_length=40, unique=True, null=True, blank=True)
    issue_date = models.DateField(null=True, blank=True)
    # Frozen copy of the approved values used to fill the template.
    data_snapshot = models.JSONField(default=dict)

    status = models.CharField(max_length=15, choices=Status.choices, default=Status.NOT_ISSUED)
    pdf_status = models.CharField(max_length=15, choices=PdfStatus.choices, default=PdfStatus.NOT_GENERATED)
    pdf_error = models.TextField(blank=True)
    file = models.ForeignKey(GeneratedFile, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    distribution = models.CharField(max_length=10, choices=Distribution.choices, default=Distribution.UNDECIDED)
    print_status = models.CharField(max_length=15, choices=PrintStatus.choices, default=PrintStatus.NOT_REQUIRED)
    handover_status = models.CharField(max_length=20, choices=HandoverStatus.choices,
                                       default=HandoverStatus.NOT_REQUIRED)

    replaces = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="replaced_by")
    change_reason = models.TextField(blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                  on_delete=models.PROTECT, related_name="+")
    issued_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["request", "version_no"], name="unique_issuance_version"),
            # At most one live (draft or issued) issuance per certificate request: blocks
            # double clicks and concurrent admins from creating parallel issuances.
            models.UniqueConstraint(
                fields=["request"], condition=Q(status__in=["not_issued", "issued"]),
                name="one_live_issuance_per_request",
            ),
            models.CheckConstraint(
                condition=~Q(status="issued") | Q(pdf_status="generated", file__isnull=False,
                                                  issue_date__isnull=False),
                name="issued_requires_generated_pdf",
            ),
            models.CheckConstraint(
                condition=Q(replaces__isnull=True) | ~Q(change_reason=""),
                name="reissue_requires_reason",
            ),
        ]
        permissions = [
            ("generate_certificate", "Can generate certificate PDFs"),
            ("download_certificate", "Can download generated certificates"),
            ("send_certificate_email", "Can queue certificate emails"),
            ("record_print", "Can record certificate printing"),
            ("record_handover", "Can record certificate handover"),
            ("request_reissue", "Can request a certificate reissue"),
            ("authorize_reissue", "Can authorize a certificate reissue"),
            ("revoke_certificate", "Can cancel or revoke issued certificates"),
        ]

    def __str__(self):
        return f"{self.request} v{self.version_no} ({self.status})"


class EmailJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        DELIVERY_CONFIRMED = "delivery_confirmed", "Delivery confirmed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    issuance = models.ForeignKey(Issuance, on_delete=models.PROTECT, related_name="email_jobs")
    recipient = models.EmailField()
    idempotency_key = models.CharField(max_length=128, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    # Set when a send attempt ended without a definite answer (e.g. timeout after DATA).
    # Such jobs are never retried automatically; an admin must reconcile first.
    needs_reconciliation = models.BooleanField(default=False)
    is_resend = models.BooleanField(default=False)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # Only one in-flight job per issuance and recipient at a time.
            models.UniqueConstraint(
                fields=["issuance", "recipient"], condition=Q(status__in=["pending", "sending"]),
                name="one_inflight_email_per_recipient",
            ),
        ]


class EmailAttempt(models.Model):
    class Outcome(models.TextChoices):
        SUCCESS = "success", "Accepted by mail server"
        FAILURE = "failure", "Failed"
        UNCERTAIN = "uncertain", "Outcome uncertain"

    job = models.ForeignKey(EmailJob, on_delete=models.PROTECT, related_name="attempts")
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=10, choices=Outcome.choices, blank=True)
    smtp_code = models.IntegerField(null=True, blank=True)
    error = models.TextField(blank=True)


class PrintEvent(models.Model):
    issuance = models.ForeignKey(Issuance, on_delete=models.PROTECT, related_name="print_events")
    printed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    printed_at = models.DateTimeField()
    note = models.TextField(blank=True)


class Handover(models.Model):
    issuance = models.OneToOneField(Issuance, on_delete=models.PROTECT, related_name="handover")
    handed_over_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    received_by_name = models.CharField(max_length=150)
    handed_over_on = models.DateField()
    note = models.TextField(blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)
