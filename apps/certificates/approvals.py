"""Per-certificate approval workflow (Phase 4). No PDFs are generated and nothing is sent here.

Each certificate type is approved independently, with its own prerequisites. Approving one type
never approves another. Approvals are withdrawn automatically when the facts they relied on change
(e.g. the student's details are reopened, or completion is no longer verified).
"""
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from apps.core import audit
from apps.core.choices import CertificateType
from apps.core.workflow import STALE_MESSAGE, WorkflowError, clean_reason
from apps.internships.models import Internship, Recommendation, lock_internship
from apps.registry.models import Domain, DuplicateCandidate, Submission

from .models import CertificateRequest, Issuance

LIVE_ISSUANCE = (Issuance.Status.NOT_ISSUED, Issuance.Status.ISSUED)

# Which approvals depend on which verification (used to withdraw approvals when it changes).
DEPENDS_ON_COMPLETION = (CertificateType.EXPERIENCE, CertificateType.LOR)
DEPENDS_ON_PROJECT = (CertificateType.PROJECT,)
DEPENDS_ON_RECOMMENDATION = (CertificateType.LOR,)


def _common_requirements(internship):
    unmet = []
    submission = internship.submission
    if submission is not None:
        if submission.status != Submission.Status.VERIFIED:
            unmet.append("The student's registration details have not been verified by HR.")
        if submission.duplicate_flags.filter(resolution__in=DuplicateCandidate.OPEN_RESOLUTIONS).exists():
            unmet.append("Possible duplicate flags on the submission are not resolved.")
        if submission.duplicate_flags.filter(resolution=DuplicateCandidate.Resolution.DUPLICATE).exists():
            unmet.append("The submission is marked as a confirmed duplicate.")
    if internship.domain.status != Domain.Status.ACTIVE:
        unmet.append(f"The domain “{internship.domain.name}” is not approved/active.")
    return unmet


def eligibility(internship, cert_type):
    """Return a list of unmet requirements (empty list = eligible for approval)."""
    unmet = _common_requirements(internship)
    if cert_type == CertificateType.OFFER_LETTER:
        pass  # Selection = verified registration; may be approved before the internship starts.
    elif cert_type == CertificateType.EXPERIENCE:
        if internship.completion_status != Internship.Completion.VERIFIED:
            unmet.append("Internship completion has not been verified.")
    elif cert_type == CertificateType.PROJECT:
        if internship.project_status != Internship.ProjectStatus.VERIFIED:
            unmet.append("Project completion has not been verified.")
    elif cert_type == CertificateType.LOR:
        if internship.completion_status != Internship.Completion.VERIFIED:
            unmet.append("Internship completion has not been verified.")
        rec = Recommendation.objects.filter(internship=internship).first()
        if rec is None or rec.decision != Recommendation.Decision.RECOMMENDED:
            unmet.append("No positive recommendation decision has been recorded.")
    else:
        raise ValueError(cert_type)
    return unmet


def has_live_issuance(request_obj):
    return request_obj.issuances.filter(status__in=LIVE_ISSUANCE).exists()


def _meta(internship, cert_type, **extra):
    return {"internship_id": internship.pk, "submission_id": internship.submission_id, "cert_type": cert_type,
            **extra}


DECISIONS = {"approve", "reject", "request_correction", "reset"}

# Certificate types whose final decision needs an extra permission (office rule: LOR = Super Admin only).
FINAL_AUTHORITY = {CertificateType.LOR: "certificates.approve_lor"}


def may_decide(user, cert_type):
    """True if the user may record decisions for this certificate type."""
    if not user.has_perm("certificates.approve_certificate"):
        return False
    extra = FINAL_AUTHORITY.get(cert_type)
    return extra is None or user.has_perm(extra)


def decide(internship_id, cert_type, decision, *, actor, reason="", expected_status=None, request=None):
    """Record a decision for one certificate type on one internship."""
    if cert_type not in CertificateType.values:
        raise WorkflowError("Unknown certificate type.")
    if decision not in DECISIONS:
        raise WorkflowError("Unknown decision.")
    if not may_decide(actor, cert_type):
        # Enforced here as well as in the view, so no code path can bypass it.
        raise PermissionDenied(f"You are not authorised to decide on the {CertificateType(cert_type).label}.")
    reason = clean_reason(reason, required=False)
    with transaction.atomic():
        # Lock submission then internship (same order as the review workflow); serializes all
        # certificate decisions and verifications for this internship. Status reads happen after locking.
        internship = lock_internship(internship_id)
        req, _ = CertificateRequest.objects.get_or_create(internship=internship, cert_type=cert_type)
        req = CertificateRequest.objects.select_for_update().get(pk=req.pk)
        if expected_status is not None and req.approval_status != expected_status:
            raise WorkflowError(STALE_MESSAGE)
        if has_live_issuance(req):
            raise WorkflowError("A certificate has already been created for this approval. "
                                "Review its issuance history before changing the decision.")
        A = CertificateRequest.Approval
        if decision == "approve":
            if req.approval_status == A.APPROVED:
                raise WorkflowError("This certificate is already approved.")
            unmet = eligibility(internship, cert_type)
            if unmet:
                raise WorkflowError("Cannot approve yet: " + " ".join(unmet))
            new_status = A.APPROVED
        elif decision in ("reject", "request_correction"):
            if not reason:
                raise WorkflowError("Please give a reason.")
            new_status = A.REJECTED if decision == "reject" else A.CORRECTION_REQUESTED
        else:  # reset to pending
            if req.approval_status == A.PENDING:
                raise WorkflowError("This certificate is already pending.")
            if not reason:
                raise WorkflowError("Please give a reason for reopening this decision.")
            new_status = A.PENDING
        previous = req.approval_status
        req.approval_status = new_status
        req.reason = reason
        req.decided_by = actor if new_status != A.PENDING else None
        req.decided_at = timezone.now() if new_status != A.PENDING else None
        req.save()
        audit.record(f"certificate.{decision}", actor=actor, entity=req, request=request,
                     metadata=_meta(internship, cert_type, previous=previous, reason=reason))
    return req


def withdraw_approvals(internship, cert_types, *, actor, why, request=None):
    """Return approved (not yet issued) requests of the given types to pending.

    Must be called inside the caller's transaction, after locking the internship row.
    Raises WorkflowError if any affected certificate already has a live issuance.
    """
    reqs = CertificateRequest.objects.select_for_update().filter(
        internship=internship, cert_type__in=cert_types,
        approval_status=CertificateRequest.Approval.APPROVED)
    withdrawn = []
    for req in reqs:
        if has_live_issuance(req):
            raise WorkflowError(f"The {req.get_cert_type_display()} has already been created. "
                                "This change needs the reissue process (not available yet).")
        req.approval_status = CertificateRequest.Approval.PENDING
        req.decided_by = None
        req.decided_at = None
        req.reason = f"Approval withdrawn automatically: {why}"
        req.save()
        withdrawn.append(req.cert_type)
        audit.record("certificate.approval_withdrawn", actor=actor, entity=req, request=request,
                     metadata=_meta(internship, req.cert_type, why=why))
    return withdrawn


def any_live_issuance_for_submission(submission):
    return Issuance.objects.filter(request__internship__submission=submission, status__in=LIVE_ISSUANCE).exists()


def approval_is_authorised(req):
    """False for an approval recorded by someone who lacks the type's final authority.

    Used to surface LOR approvals made before the Super-Admin-only rule; such records are
    reported, never changed automatically. Later phases must check this before issuing.
    """
    if req.approval_status != CertificateRequest.Approval.APPROVED or req.decided_by is None:
        return True
    extra = FINAL_AUTHORITY.get(req.cert_type)
    return extra is None or req.decided_by.has_perm(extra)


def certificate_overview(internship, user=None):
    """Rows for the HR page: one per certificate type with status and unmet requirements."""
    existing = {r.cert_type: r for r in internship.certificate_requests.select_related("decided_by")}
    rows = []
    for value, label in CertificateType.choices:
        req = existing.get(value)
        rows.append({
            "type": value, "label": label, "request": req,
            "status": req.approval_status if req else CertificateRequest.Approval.PENDING,
            "status_label": req.get_approval_status_display() if req else "Pending",
            "unmet": eligibility(internship, value),
            "locked": bool(req and has_live_issuance(req)),
            "can_decide": user is not None and may_decide(user, value),
            "final_authority": value in FINAL_AUTHORITY,
            "unauthorised_approval": bool(req) and not approval_is_authorised(req),
        })
    return rows
