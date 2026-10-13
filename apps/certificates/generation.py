"""Certificate generation (Phase 6). No email is sent and nothing is issued while production is disabled.

generate_draft  -> fills the active template for one approved certificate, converts it to PDF, checks it and
                   stores it privately as a draft Issuance (status "not issued").
issue           -> marks a draft as issued. Requires CERT_PRODUCTION_ISSUANCE_ENABLED and an authentic template.
cancel_draft    -> withdraws a draft so it can be regenerated.
proof_pdf       -> PDF of a template version filled with labelled SAMPLE data, for review; never stored.

Production lock: while CERT_PRODUCTION_ISSUANCE_ENABLED is off, an authentic (production) template can only
produce sample-data proofs, never a real student's certificate. TEST templates can produce drafts for
testing, which can never be issued.
"""
import datetime as dt
import hashlib
import uuid

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from apps.core import audit
from apps.core.choices import CertificateType
from apps.core.workflow import WorkflowError, clean_reason
from apps.doctemplates import services as template_services
from apps.doctemplates.models import CertificateTemplate, TemplateVersion
from apps.doctemplates.placeholders import REGISTRY, sample_values
from apps.internships.models import Internship, lock_internship

from . import docxfill, pdf, values as value_builder
from .approvals import LIVE_ISSUANCE, approval_is_authorised
from .models import CertificateRequest, GeneratedFile, Issuance

# Official issuance is reserved for Super Admins (permission granted only to that role).
ISSUE_PERMISSION = "certificates.issue_certificate"


def may_issue(user):
    return bool(user is not None and user.is_authenticated and user.is_active and user.has_perm(ISSUE_PERMISSION))


# Values that must visibly appear in the PDF (a missing one usually means text overflowed or was lost).
VISIBLE_CHECK = ("title_and_name", "full_name", "short_name", "project_title", "college_name", "domain")


class GenerationError(WorkflowError):
    pass


def _require(actor, perm):
    if actor is None or not actor.has_perm(perm):
        raise PermissionDenied


def _meta(issuance, **extra):
    req = issuance.request
    return {"issuance_id": issuance.pk, "internship_id": req.internship_id,
            "submission_id": req.internship.submission_id, "cert_type": req.cert_type,
            "template_version_id": issuance.template_version_id, **extra}


def _approved_request(internship, cert_type):
    req = CertificateRequest.objects.filter(internship=internship, cert_type=cert_type).select_related(
        "decided_by").first()
    if req is None or req.approval_status != CertificateRequest.Approval.APPROVED:
        raise GenerationError("This certificate has not been approved.")
    if not approval_is_authorised(req):
        raise GenerationError("This approval was made by an account without final authority for this "
                              "certificate type. A Super Admin must review it first.")
    return req


def _active_version(cert_type):
    template = CertificateTemplate.objects.filter(cert_type=cert_type).first()
    version = template.active_version() if template else None
    if version is None:
        raise GenerationError("There is no active template for this certificate type.")
    return version


def _production_gate(version):
    if version.is_production and not settings.CERT_PRODUCTION_ISSUANCE_ENABLED:
        raise GenerationError("Production issuance is disabled. Authentic templates can only produce sample-data "
                              "proofs until production issuance is enabled.")


def _render(version, values, placeholders):
    try:
        data, inspection = template_services.read_verified(version)
    except WorkflowError as exc:
        raise GenerationError(str(exc)) from None
    filled, used = docxfill.fill_docx(data, values)
    if set(inspection.placeholders) - used:
        raise GenerationError("Some template placeholders were not filled.")
    pdf_bytes = pdf.convert_docx_to_pdf(filled)
    info = pdf.inspect_pdf(pdf_bytes)
    problems = pdf.check_pdf(info, expected_values=[values[p] for p in VISIBLE_CHECK if p in placeholders],
                             min_images=min(int(version.inspection.get("image_count", 0)), 2))
    if problems:
        raise GenerationError("The generated PDF failed its checks: " + " ".join(problems))
    return pdf_bytes, info


def generate_draft(internship_id, cert_type, *, actor, issue_date=None, request=None):
    _require(actor, "certificates.generate_certificate")
    if cert_type not in CertificateType.values:
        raise GenerationError("Unknown certificate type.")
    issue_date = issue_date or timezone.localdate()
    if issue_date > timezone.localdate():
        raise GenerationError("The issue date cannot be in the future.")

    # Unlocked pre-checks and rendering (conversion can take seconds; no locks are held meanwhile).
    internship = Internship.objects.select_related("student", "domain").get(pk=internship_id)
    req = _approved_request(internship, cert_type)
    version = _active_version(cert_type)
    _production_gate(version)
    placeholders = version.inspection.get("placeholders", [])
    values, problems = value_builder.build_values(internship, cert_type, issue_date)
    missing = value_builder.missing_for(placeholders, problems)
    if missing:
        raise GenerationError("Missing or unavailable data: " + "; ".join(
            f"{REGISTRY[name].label}: {reason}" for name, reason in sorted(missing.items())))
    try:
        pdf_bytes, info = _render(version, values, placeholders)
    except (docxfill.FillError, pdf.ConversionError) as exc:
        raise GenerationError(str(exc)) from None
    digest = hashlib.sha256(pdf_bytes).hexdigest()

    with transaction.atomic():
        lock_internship(internship_id)
        req = CertificateRequest.objects.select_for_update().get(pk=req.pk)
        # Re-check everything under the lock: approval, authority, template and production gate.
        if req.approval_status != CertificateRequest.Approval.APPROVED or not approval_is_authorised(req):
            raise GenerationError("The approval changed while the certificate was being prepared.")
        if _active_version(cert_type).pk != version.pk:
            raise GenerationError("The active template changed while the certificate was being prepared.")
        _production_gate(version)
        if req.issuances.filter(status__in=LIVE_ISSUANCE).exists():
            raise GenerationError("This certificate has already been generated. Review its history, or cancel "
                                  "the draft before generating it again.")
        stored = GeneratedFile(sha256=digest, size_bytes=len(pdf_bytes), page_count=info.page_count)
        stored.file.save(f"{uuid.uuid4().hex}.pdf", ContentFile(pdf_bytes), save=False)
        try:
            with transaction.atomic():
                stored.save()
                number = (req.issuances.aggregate(m=Max("version_no"))["m"] or 0) + 1
                issuance = Issuance.objects.create(
                    request=req, version_no=number, template_version=version, is_production=version.is_production,
                    issue_date=issue_date, data_snapshot={p: values[p] for p in placeholders},
                    pdf_status=Issuance.PdfStatus.GENERATED, file=stored, created_by=actor)
        except IntegrityError:
            stored.file.storage.delete(stored.file.name)
            raise GenerationError("This certificate has already been generated.") from None
        except Exception:
            stored.file.storage.delete(stored.file.name)
            raise
        audit.record("certificate.generated", actor=actor, entity=issuance, request=request,
                     metadata=_meta(issuance, sha256=digest, pages=info.page_count,
                                    is_production=issuance.is_production))
    return issuance


@transaction.atomic
def issue(issuance_id, *, actor, request=None):
    """Officially issue a generated draft (Super Admin only). Refused while production issuance is disabled.

    The authority check comes first, before any lookup or state check, so a non-Super-Admin learns nothing
    and changes nothing. The production gate, draft state, template and approval are then re-checked
    under row locks, so retries and concurrent requests cannot issue twice.
    """
    if not may_issue(actor):
        raise PermissionDenied("Only Super Admins can officially issue certificates.")
    if not settings.CERT_PRODUCTION_ISSUANCE_ENABLED:
        raise GenerationError("Production issuance is disabled.")
    issuance = Issuance.objects.select_related("request", "template_version").get(pk=issuance_id)
    lock_internship(issuance.request.internship_id)
    issuance = Issuance.objects.select_for_update().select_related("request__internship", "template_version").get(
        pk=issuance_id)
    if issuance.status != Issuance.Status.NOT_ISSUED or issuance.pdf_status != Issuance.PdfStatus.GENERATED:
        raise GenerationError("Only a generated draft can be issued.")
    version = issuance.template_version
    if not (issuance.is_production and version.can_issue_production):
        raise GenerationError("Only drafts made from the active authentic template can be issued; "
                              "TEST templates can never be issued.")
    req = CertificateRequest.objects.select_for_update().get(pk=issuance.request_id)
    if req.approval_status != CertificateRequest.Approval.APPROVED or not approval_is_authorised(req):
        raise GenerationError("The certificate is no longer approved.")
    issuance.status = Issuance.Status.ISSUED
    issuance.issued_by, issuance.issued_at = actor, timezone.now()
    issuance.save(update_fields=["status", "issued_by", "issued_at"])
    audit.record("certificate.issued", actor=actor, entity=issuance, request=request,
                 metadata=_meta(issuance, sha256=issuance.file.sha256))
    return issuance


@transaction.atomic
def cancel_draft(issuance_id, *, actor, reason, request=None):
    _require(actor, "certificates.generate_certificate")
    reason = clean_reason(reason, required=True, message="Please give a reason for cancelling this draft.")
    issuance = Issuance.objects.select_related("request").get(pk=issuance_id)
    lock_internship(issuance.request.internship_id)
    issuance = Issuance.objects.select_for_update().select_related("request__internship").get(pk=issuance_id)
    if issuance.status != Issuance.Status.NOT_ISSUED:
        raise GenerationError("Only drafts that have not been issued can be cancelled here.")
    issuance.status = Issuance.Status.CANCELLED
    issuance.change_reason = reason
    issuance.save(update_fields=["status", "change_reason"])
    audit.record("certificate.draft_cancelled", actor=actor, entity=issuance, request=request,
                 metadata=_meta(issuance, reason=reason))
    return issuance


def proof_pdf(version_id, *, actor, pronouns="she", request=None):
    """Template proof filled with clearly labelled SAMPLE data, in the chosen pronoun set (to check grammar).
    Returned to the reviewer, never stored or attached to a student."""
    _require(actor, "doctemplates.map_template")
    if pronouns not in value_builder.PRONOUN_FORMS:
        raise GenerationError("Choose he, she or they.")
    version = TemplateVersion.objects.select_related("template").get(pk=version_id)
    cert_type = version.template.cert_type
    values = {**sample_values(cert_type), **value_builder.date_values(dt.date(2026, 1, 5)),
              **value_builder.PRONOUN_FORMS[pronouns]}
    placeholders = version.inspection.get("placeholders", [])
    try:
        pdf_bytes, info = _render(version, values, placeholders)
    except (docxfill.FillError, pdf.ConversionError, WorkflowError) as exc:
        raise GenerationError(f"Proof failed: {exc}") from None
    audit.record("template.proof_generated", actor=actor, entity=version, request=request,
                 metadata={"template_version_id": version.pk, "cert_type": cert_type, "version": version.version,
                           "pages": info.page_count, "pronouns": pronouns})
    return pdf_bytes, info
