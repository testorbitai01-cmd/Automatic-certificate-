import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.choices import CertificateType


def template_upload_path(instance, filename):
    # Random name; the original filename is kept in a separate field, not in the path.
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"templates/{instance.template.cert_type}/{uuid.uuid4().hex}.{ext}"


class CertificateTemplate(models.Model):
    """One per certificate type: Offer Letter, Experience, Project, LOR."""

    cert_type = models.CharField(max_length=20, choices=CertificateType.choices, unique=True)

    def active_version(self):
        return self.versions.filter(status=TemplateVersion.Status.ACTIVE).first()

    def __str__(self):
        return self.get_cert_type_display()


class TemplateVersion(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING_APPROVAL = "pending_approval", "Pending approval"
        ACTIVE = "active", "Active"
        RETIRED = "retired", "Retired"

    class SourceFormat(models.TextChoices):
        DOCX = "docx", "Word (.docx)"
        PDF = "pdf", "PDF"
        IMAGE = "image", "Image"

    template = models.ForeignKey(CertificateTemplate, on_delete=models.PROTECT, related_name="versions")
    version = models.PositiveIntegerField()
    # False for clearly marked test templates. Only official Gradtwin files uploaded by
    # authorized staff may be flagged production, and only Super Admins can activate them.
    is_production = models.BooleanField(default=False)
    source_file = models.FileField(upload_to=template_upload_path)
    source_filename = models.CharField(max_length=255)
    source_sha256 = models.CharField(max_length=64)
    source_format = models.CharField(max_length=10, choices=SourceFormat.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    notes = models.TextField(blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_note = models.TextField(blank=True)

    class Meta:
        ordering = ["template", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["template", "version"], name="unique_template_version"),
            models.UniqueConstraint(
                fields=["template"], condition=Q(status="active"), name="one_active_version_per_template"
            ),
            models.CheckConstraint(
                condition=~Q(status="active") | Q(approved_by__isnull=False, approved_at__isnull=False),
                name="active_version_requires_approval",
            ),
        ]
        permissions = [
            ("upload_template", "Can upload certificate template versions"),
            ("map_template", "Can configure template field mappings and generate previews"),
            ("approve_template", "Can approve and activate certificate templates"),
        ]

    @property
    def can_issue_production(self):
        return (
            self.is_production
            and self.status == self.Status.ACTIVE
            and settings.CERT_PRODUCTION_ISSUANCE_ENABLED
        )

    def __str__(self):
        marker = "" if self.is_production else " [TEST]"
        return f"{self.template} v{self.version}{marker}"


class TemplateField(models.Model):
    """Maps one placeholder in a template version to a verified data source."""

    version = models.ForeignKey(TemplateVersion, on_delete=models.CASCADE, related_name="fields")
    placeholder = models.CharField(max_length=100)
    source = models.CharField(max_length=100)  # e.g. "student.full_name"; validated against a registry
    formatter = models.CharField(max_length=50, blank=True)  # e.g. "ordinal_date", "month_year"
    required = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["version", "placeholder"], name="unique_placeholder_per_version"),
        ]

    def __str__(self):
        return f"{{{{{self.placeholder}}}}} ← {self.source}"
