"""Template upload, review and activation (Phase 5). Nothing here fills, prints or sends a certificate.

Lifecycle:  draft --submit--> pending_approval --approve--> active --deactivate/superseded--> retired
                                              \\--reject--> rejected
Only one version per certificate type is active. Activation and deactivation need the Super-Admin-only
`doctemplates.approve_template` permission (checked here, not only in views). Production use additionally
requires `is_production` and the CERT_PRODUCTION_ISSUANCE_ENABLED switch (see TemplateVersion).
"""
import hashlib
import io

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.core import audit
from apps.core.choices import CertificateType
from apps.core.workflow import STALE_MESSAGE, WorkflowError, clean_reason

from . import docx
from .models import CertificateTemplate, TemplateField, TemplateVersion
from .placeholders import REGISTRY

V = TemplateVersion.Status


def _require(actor, perm):
    if actor is None or not actor.has_perm(perm):
        raise PermissionDenied


def may_approve(user, version):
    """Super Admin, and not the person who uploaded this version (separation of duties)."""
    return (user is not None and user.has_perm("doctemplates.approve_template")
            and version.uploaded_by_id != user.pk)


def _meta(version, **extra):
    # IDs, hashes and counts only: no filenames (sample files are often named after people) and no content.
    return {"template_version_id": version.pk, "cert_type": version.template.cert_type,
            "version": version.version, **extra}


def _lock_version(version_id, expected_status):
    template_id = TemplateVersion.objects.values_list("template_id", flat=True).get(pk=version_id)
    template = CertificateTemplate.objects.select_for_update().get(pk=template_id)  # serializes per type
    version = TemplateVersion.objects.select_for_update().get(pk=version_id)
    version.template = template
    if expected_status is not None and version.status != expected_status:
        raise WorkflowError(STALE_MESSAGE)
    return template, version


def upload_version(cert_type, uploaded, *, actor, is_production=False, notes="", request=None):
    """Validate and store a new draft version. Raises WorkflowError (with all problems) if invalid."""
    _require(actor, "doctemplates.upload_template")
    if cert_type not in CertificateType.values:
        raise WorkflowError("Choose a certificate type.")
    notes = clean_reason(notes, required=False)
    name = (getattr(uploaded, "name", "") or "").strip()
    if not name.lower().endswith(".docx"):
        raise WorkflowError("Upload the original Word template as a .docx file. PDF and image templates are "
                            "not supported because their wording cannot be filled without re-typesetting it.")
    if uploaded.size > settings.TEMPLATE_UPLOAD_MAX_BYTES:
        raise WorkflowError(f"The file is larger than {settings.TEMPLATE_UPLOAD_MAX_BYTES // (1024 * 1024)} MB.")
    data = uploaded.read(settings.TEMPLATE_UPLOAD_MAX_BYTES + 1)
    if len(data) > settings.TEMPLATE_UPLOAD_MAX_BYTES:
        raise WorkflowError(f"The file is larger than {settings.TEMPLATE_UPLOAD_MAX_BYTES // (1024 * 1024)} MB.")
    try:
        inspection = docx.inspect_docx(io.BytesIO(data), cert_type)
    except docx.TemplateFileError as exc:
        raise WorkflowError("The template was not accepted: " + " ".join(exc.errors)) from None
    digest = hashlib.sha256(data).hexdigest()

    with transaction.atomic():
        # The four types are seeded by migration; get_or_create (race-safe) keeps upload working if a row is missing.
        CertificateTemplate.objects.get_or_create(cert_type=cert_type)
        template = CertificateTemplate.objects.select_for_update().get(cert_type=cert_type)
        same = template.versions.filter(source_sha256=digest).first()
        if same is not None:
            raise WorkflowError(f"This exact file is already version {same.version} of this template.")
        number = (template.versions.aggregate(m=Max("version"))["m"] or 0) + 1
        version = TemplateVersion(
            template=template, version=number, is_production=bool(is_production), source_filename=name[-255:],
            source_sha256=digest, source_size=len(data), source_format=TemplateVersion.SourceFormat.DOCX,
            inspection=inspection.as_json(), notes=notes, uploaded_by=actor,
        )
        version.source_file.save("template.docx", ContentFile(data), save=False)
        try:
            version.save()
            TemplateField.objects.bulk_create([
                TemplateField(version=version, placeholder=p, source=REGISTRY[p].source) for p in inspection.placeholders
            ])
            audit.record("template.uploaded", actor=actor, entity=version, request=request,
                         metadata=_meta(version, sha256=digest, size=len(data), is_production=version.is_production,
                                        placeholders=inspection.placeholders, images=inspection.image_count))
        except Exception:
            version.source_file.storage.delete(version.source_file.name)  # don't leave an orphaned file
            raise
    return version


@transaction.atomic
def submit_for_review(version_id, *, actor, expected_status=None, request=None):
    _require(actor, "doctemplates.upload_template")
    _, version = _lock_version(version_id, expected_status)
    if version.status != V.DRAFT:
        raise WorkflowError("Only draft versions can be submitted for review.")
    version.status = V.PENDING_APPROVAL
    version.submitted_by = actor
    version.submitted_at = timezone.now()
    version.save(update_fields=["status", "submitted_by", "submitted_at"])
    audit.record("template.submitted", actor=actor, entity=version, request=request, metadata=_meta(version))
    return version


@transaction.atomic
def approve_and_activate(version_id, *, actor, confirmed_compared, note="", expected_status=None, request=None):
    """Super Admin: activate a reviewed version; the previously active version is retired."""
    _require(actor, "doctemplates.approve_template")
    note = clean_reason(note, required=False)
    if not confirmed_compared:
        raise WorkflowError("Confirm that you compared the preview with the original Gradtwin template.")
    template, version = _lock_version(version_id, expected_status)
    if not may_approve(actor, version):
        # Separation of duties: checked after locking, on the stored uploader (not on anything the client sends).
        raise PermissionDenied("You uploaded this template version, so another Super Admin must approve it.")
    if version.status not in (V.PENDING_APPROVAL, V.RETIRED):
        raise WorkflowError("Only versions pending approval (or previously retired) can be activated.")
    previous = template.versions.select_for_update().filter(status=V.ACTIVE).first()
    if not version.is_production:
        if settings.CERT_PRODUCTION_ISSUANCE_ENABLED:
            raise WorkflowError("Test templates cannot be activated while production issuance is enabled.")
        if previous is not None and previous.is_production:
            raise WorkflowError(f"A test template cannot replace the active authentic template (version "
                                f"{previous.version}). Deactivate that version first if this is intended.")
    now = timezone.now()
    if previous is not None:
        previous.status = V.RETIRED
        previous.closed_by, previous.closed_at = actor, now
        previous.close_reason = f"Superseded by version {version.version}."
        previous.save(update_fields=["status", "closed_by", "closed_at", "close_reason"])
        audit.record("template.retired", actor=actor, entity=previous, request=request,
                     metadata=_meta(previous, reason="superseded", superseded_by=version.version))
    reactivated = version.status == V.RETIRED
    version.status = V.ACTIVE
    version.approved_by, version.approved_at, version.approval_note = actor, now, note
    version.closed_by, version.closed_at, version.close_reason = None, None, ""
    version.save(update_fields=["status", "approved_by", "approved_at", "approval_note",
                                "closed_by", "closed_at", "close_reason"])
    audit.record("template.activated", actor=actor, entity=version, request=request,
                 metadata=_meta(version, is_production=version.is_production, reactivated=reactivated,
                                replaced_version=previous.version if previous else None, note=note))
    return version


@transaction.atomic
def reject(version_id, *, actor, reason, expected_status=None, request=None):
    _require(actor, "doctemplates.approve_template")
    reason = clean_reason(reason, required=True, message="Please give a reason for rejecting this version.")
    _, version = _lock_version(version_id, expected_status)
    if version.status not in (V.DRAFT, V.PENDING_APPROVAL):
        raise WorkflowError("Only draft or pending versions can be rejected.")
    version.status = V.REJECTED
    version.closed_by, version.closed_at, version.close_reason = actor, timezone.now(), reason
    version.save(update_fields=["status", "closed_by", "closed_at", "close_reason"])
    audit.record("template.rejected", actor=actor, entity=version, request=request,
                 metadata=_meta(version, reason=reason))
    return version


@transaction.atomic
def deactivate(version_id, *, actor, reason, expected_status=None, request=None):
    """Super Admin: take the active version out of use, leaving the type without an active template."""
    _require(actor, "doctemplates.approve_template")
    reason = clean_reason(reason, required=True, message="Please give a reason for deactivating this version.")
    _, version = _lock_version(version_id, expected_status)
    if version.status != V.ACTIVE:
        raise WorkflowError("Only the active version can be deactivated.")
    version.status = V.RETIRED
    version.closed_by, version.closed_at, version.close_reason = actor, timezone.now(), reason
    version.save(update_fields=["status", "closed_by", "closed_at", "close_reason"])
    audit.record("template.deactivated", actor=actor, entity=version, request=request,
                 metadata=_meta(version, reason=reason))
    return version


def inspect_stored(version):
    """Re-inspect the stored file (for the preview). The stored file was validated at upload."""
    try:
        with version.source_file.open("rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        raise WorkflowError("The stored template file is missing. Do not use this version; "
                            "contact the administrator.") from None
    if hashlib.sha256(data).hexdigest() != version.source_sha256:
        raise WorkflowError("The stored template file does not match its recorded checksum. Do not use it; "
                            "contact the administrator.")
    return docx.inspect_docx(io.BytesIO(data), version.template.cert_type)
