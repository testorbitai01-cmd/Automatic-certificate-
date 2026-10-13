"""HR review of student submissions (Phase 4).

All transitions lock the submission row and check the status the reviewer saw (expected_status),
so two reviewers acting at once cannot both succeed. Every decision is written to the audit log
with the submission id; personal details are not copied into audit metadata.
"""
import datetime as dt
import hashlib
import secrets

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.certificates import approvals
from apps.core import audit, policy
from apps.core.choices import CertificateType
from apps.core.workflow import STALE_MESSAGE, WorkflowError, clean_reason
from apps.internships.models import Internship

from .forms import NO_TITLE
from .models import (CorrectionRequest, DuplicateCandidate, StudentEmail, Submission, SubmissionNote,
                     collapse_whitespace, reg_no_key)

S = Submission.Status

# Groups HR can ask the student to correct -> form fields shown to the student.
CORRECTABLE = {
    "name": ("Title and name", ["title", "full_name"]),
    "preferred_name": ("Preferred name", ["preferred_name"]),
    "pronouns": ("Pronouns", ["pronouns"]),
    "gender": ("Gender", ["gender", "gender_detail"]),
    "mobile": ("Mobile number", ["mobile"]),
    "reg_no": ("University registration number", ["reg_no"]),
    "email": ("Registered Gmail address", ["email"]),
    "alternate_email": ("Alternative email address", ["alternate_email"]),
    "degree": ("Degree and department", ["degree"]),
    "college_name": ("College name", ["college_name"]),
    "college_location": ("College location", ["college_location"]),
    "domain": ("Internship domain", ["domain"]),
    "dates": ("Internship dates", ["start_date", "end_date"]),
    "project_title": ("Project title", ["project_title"]),
}


def hash_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _record(action, actor, submission, request=None, **meta):
    audit.record(action, actor=actor, entity=submission, request=request,
                 metadata={"submission_id": submission.pk, **meta})


def _lock(submission_id, expected_status):
    sub = Submission.objects.select_for_update().select_related("student", "campaign").get(pk=submission_id)
    if expected_status is not None and sub.status != expected_status:
        raise WorkflowError(STALE_MESSAGE)
    return sub


def _require(sub, allowed, action):
    if sub.status not in allowed:
        raise WorkflowError(f"Cannot {action} a submission that is “{sub.get_status_display()}”.")


def _withdraw_all_approvals(sub, actor, why, request):
    for internship in Internship.objects.select_for_update().filter(submission=sub):
        approvals.withdraw_approvals(internship, CertificateType.values, actor=actor, why=why, request=request)


def _set_status(sub, status, actor, reason=""):
    sub.status = status
    sub.status_reason = reason
    if status in (S.VERIFIED, S.REJECTED):
        sub.reviewed_by = actor
        sub.reviewed_at = timezone.now()
    sub.save(update_fields=["status", "status_reason", "reviewed_by", "reviewed_at"])


# --- Decisions ----------------------------------------------------------------

@transaction.atomic
def start_review(submission_id, *, actor, expected_status=None, request=None):
    sub = _lock(submission_id, expected_status)
    _require(sub, (S.SUBMITTED,), "start reviewing")
    _set_status(sub, S.UNDER_REVIEW, actor)
    _record("submission.review_started", actor, sub, request)
    return sub


@transaction.atomic
def approve(submission_id, *, actor, expected_status=None, note="", request=None):
    note = clean_reason(note, required=False)
    sub = _lock(submission_id, expected_status)
    _require(sub, (S.SUBMITTED, S.UNDER_REVIEW), "approve")
    if sub.duplicate_flags.filter(resolution=DuplicateCandidate.Resolution.DUPLICATE).exists():
        raise WorkflowError("This submission is marked as a confirmed duplicate. Reject it, or change the "
                            "duplicate decision if that was a mistake.")
    if sub.duplicate_flags.filter(resolution__in=DuplicateCandidate.OPEN_RESOLUTIONS).exists():
        raise WorkflowError("Resolve the possible-duplicate flags before approving this submission.")
    if sub.internships.filter(domain__status="pending").exists():
        raise WorkflowError("The requested domain is still pending. Approve or change it before approving.")
    _set_status(sub, S.VERIFIED, actor, note)
    _record("submission.approved", actor, sub, request, **({"note": note} if note else {}))
    return sub


@transaction.atomic
def reject(submission_id, *, actor, reason, expected_status=None, request=None):
    reason = clean_reason(reason, required=True, message="Please give a reason for rejecting this submission.")
    sub = _lock(submission_id, expected_status)
    _require(sub, (S.SUBMITTED, S.UNDER_REVIEW), "reject")
    _set_status(sub, S.REJECTED, actor, reason)
    _record("submission.rejected", actor, sub, request, reason=reason)
    return sub


@transaction.atomic
def reopen(submission_id, *, actor, reason, expected_status=None, request=None):
    """Move a verified or rejected submission back to review. Withdraws certificate approvals."""
    reason = clean_reason(reason, required=True, message="Please give a reason for reopening this submission.")
    sub = _lock(submission_id, expected_status)
    _require(sub, (S.VERIFIED, S.REJECTED), "reopen")
    if approvals.any_live_issuance_for_submission(sub):
        raise WorkflowError("A certificate has already been created for this student; it cannot be reopened here.")
    _withdraw_all_approvals(sub, actor, "submission reopened for review", request)
    _set_status(sub, S.UNDER_REVIEW, actor)
    _record("submission.reopened", actor, sub, request, reason=reason)
    return sub


def _new_token():
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


@transaction.atomic
def request_correction(submission_id, *, actor, groups, reason, expected_status=None, request=None):
    """Ask the student to correct specific fields. Returns (correction, raw_token).

    The raw token is returned once for HR to pass on; only its hash is stored.
    """
    groups = [g for g in dict.fromkeys(groups or []) if g in CORRECTABLE]
    if not groups:
        raise WorkflowError("Select at least one field that needs correction.")
    reason = clean_reason(reason, required=True, message="Please explain what needs to be corrected.")
    sub = _lock(submission_id, expected_status)
    _require(sub, (S.SUBMITTED, S.UNDER_REVIEW, S.VERIFIED), "request corrections on")
    if approvals.any_live_issuance_for_submission(sub):
        raise WorkflowError("A certificate has already been created for this student; details cannot be changed here.")
    _withdraw_all_approvals(sub, actor, "student asked to correct their details", request)
    raw, digest = _new_token()
    correction = CorrectionRequest.objects.create(
        submission=sub, fields=groups, reason=reason, token_hash=digest, requested_by=actor,
        expires_at=timezone.now() + dt.timedelta(days=policy.get("registration.correction_link_days")),
    )
    _set_status(sub, S.CORRECTION_REQUESTED, actor, reason)
    _record("submission.correction_requested", actor, sub, request, fields=groups, reason=reason,
            correction_id=correction.pk)
    return correction, raw


@transaction.atomic
def regenerate_correction_link(submission_id, *, actor, expected_status=None, request=None):
    """Issue a new link (old one stops working) and restart the expiry period."""
    sub = _lock(submission_id, expected_status)
    _require(sub, (S.CORRECTION_REQUESTED,), "regenerate a correction link for")
    correction = CorrectionRequest.objects.select_for_update().get(submission=sub, status=CorrectionRequest.Status.OPEN)
    raw, digest = _new_token()
    correction.token_hash = digest
    correction.expires_at = timezone.now() + dt.timedelta(days=policy.get("registration.correction_link_days"))
    correction.save(update_fields=["token_hash", "expires_at"])
    _record("submission.correction_link_regenerated", actor, sub, request, correction_id=correction.pk)
    return correction, raw


@transaction.atomic
def cancel_correction(submission_id, *, actor, expected_status=None, request=None):
    sub = _lock(submission_id, expected_status)
    _require(sub, (S.CORRECTION_REQUESTED,), "cancel the correction request on")
    correction = CorrectionRequest.objects.select_for_update().get(submission=sub, status=CorrectionRequest.Status.OPEN)
    correction.status = CorrectionRequest.Status.CANCELLED
    correction.cancelled_by = actor
    correction.cancelled_at = timezone.now()
    correction.save(update_fields=["status", "cancelled_by", "cancelled_at"])
    _set_status(sub, S.UNDER_REVIEW, actor)
    _record("submission.correction_cancelled", actor, sub, request, correction_id=correction.pk)
    return sub


@transaction.atomic
def add_note(submission_id, *, actor, text, request=None):
    text = (text or "").strip()
    if not text:
        raise WorkflowError("The note is empty.")
    if len(text) > 4000:
        raise WorkflowError("Notes must be 4000 characters or fewer.")
    sub = Submission.objects.get(pk=submission_id)
    note = SubmissionNote.objects.create(submission=sub, author=actor, text=text)
    _record("submission.note_added", actor, sub, request, note_id=note.pk)
    return note


# --- Duplicates -----------------------------------------------------------------

DUPLICATE_DECISIONS = (DuplicateCandidate.Resolution.DUPLICATE, DuplicateCandidate.Resolution.NOT_DUPLICATE,
                       DuplicateCandidate.Resolution.FURTHER_REVIEW)


@transaction.atomic
def resolve_duplicate(flag_id, *, actor, resolution, reason, expected_resolution=None, request=None):
    """Record HR's decision on a possible duplicate. Records are never merged or deleted."""
    if resolution not in DUPLICATE_DECISIONS:
        raise WorkflowError("Choose a valid decision.")
    reason = clean_reason(reason, required=True, message="Please give a reason for this decision.")
    # Lock the flagged submission first (same order as approve()), so a decision cannot race an approval.
    submission_id = DuplicateCandidate.objects.values_list("submission_id", flat=True).get(pk=flag_id)
    sub = Submission.objects.select_for_update().get(pk=submission_id)
    flag = DuplicateCandidate.objects.select_for_update().get(pk=flag_id)
    flag.submission = sub
    if expected_resolution is not None and flag.resolution != expected_resolution:
        raise WorkflowError(STALE_MESSAGE)
    if sub.status == S.VERIFIED and resolution != DuplicateCandidate.Resolution.NOT_DUPLICATE:
        raise WorkflowError("This submission is already verified. Reopen it before marking it as a duplicate "
                            "or for further review.")
    previous = flag.resolution
    flag.resolution = resolution
    flag.resolution_reason = reason
    flag.resolved_by = actor
    flag.resolved_at = timezone.now()
    flag.save(update_fields=["resolution", "resolution_reason", "resolved_by", "resolved_at"])
    _record("duplicate.resolved", actor, flag.submission, request, flag_id=flag.pk, matches_id=flag.matches_id,
            reason_code=flag.reason, previous=previous, resolution=resolution, reason=reason)
    return flag


# --- Student correction (public, token-based) -----------------------------------------

def find_correction(raw_token):
    if not raw_token or len(raw_token) > 100:
        return None
    return (CorrectionRequest.objects.select_related("submission__student", "submission__campaign")
            .filter(token_hash=hash_token(raw_token)).first())


def fields_for(correction):
    names = []
    for group in correction.fields:
        names.extend(CORRECTABLE[group][1])
    return names


def current_values(submission):
    student = submission.student
    internship = submission.internships.select_related("domain").first()
    emails = {e.kind: e.address for e in student.emails.all()}
    return {
        "title": student.title or NO_TITLE, "full_name": student.full_name, "gender": student.gender,
        "preferred_name": student.short_name, "pronouns": student.pronouns,
        "gender_detail": student.gender_detail, "mobile": student.mobile, "reg_no": student.reg_no,
        "email": emails.get(StudentEmail.Kind.PRIMARY, ""), "alternate_email": emails.get(StudentEmail.Kind.ALTERNATE, ""),
        "degree": student.degree, "college_name": student.college_name, "college_location": student.college_location,
        "domain": str(internship.domain_id) if internship else "",
        "start_date": internship.start_date if internship else None,
        "end_date": internship.end_date if internship else None,
        "project_title": internship.project_title if internship else "",
    }


def _reset_verifications_after_correction(internship, changes, request):
    """A verification only covers the values HR checked: student-corrected dates/domain or project
    title send the matching verification back to pending (certificate approvals were already withdrawn
    when the correction was requested)."""
    reset = []
    if changes.keys() & {"start_date", "end_date", "domain"} and internship.completion_status != Internship.Completion.PENDING:
        internship.completion_status = Internship.Completion.PENDING
        internship.completion_verified_by = None
        internship.completion_verified_at = None
        internship.completion_note = ""
        reset.append("completion")
    if "project_title" in changes and internship.project_status in (Internship.ProjectStatus.VERIFIED,
                                                                     Internship.ProjectStatus.REJECTED):
        internship.project_status = Internship.ProjectStatus.PENDING
        internship.project_verified_by = None
        internship.project_verified_at = None
        internship.project_note = ""
        reset.append("project")
    if internship.project_status == Internship.ProjectStatus.NOT_APPLICABLE and "project_title" in changes:
        internship.project_status = Internship.ProjectStatus.PENDING
    if reset:
        audit.record("internship.verification_reset", actor_label="student (correction link)", entity=internship,
                     request=request, metadata={"internship_id": internship.pk,
                                                "submission_id": internship.submission_id, "reset": reset})


def _jsonable(value):
    return value.isoformat() if isinstance(value, dt.date) else value


@transaction.atomic
def apply_correction(raw_token, cleaned, *, request=None):
    """Apply the student's corrected values for the requested fields only."""
    correction = find_correction(raw_token)
    if correction is None:
        raise WorkflowError("This correction link is not valid.")
    correction = CorrectionRequest.objects.select_for_update().get(pk=correction.pk)
    if not correction.is_usable():
        raise WorkflowError("This correction link has expired or has already been used. Please contact HR.")
    sub = Submission.objects.select_for_update().select_related("student").get(pk=correction.submission_id)
    if sub.status != S.CORRECTION_REQUESTED:
        raise WorkflowError("This submission is not waiting for corrections. Please contact HR.")
    if approvals.any_live_issuance_for_submission(sub):
        raise WorkflowError("This record can no longer be changed online. Please contact HR.")

    before = current_values(sub)
    allowed = fields_for(correction)
    student = sub.student
    internship = Internship.objects.select_for_update().filter(submission=sub).first()
    changes = {}
    for name in allowed:
        if name not in cleaned:
            continue
        new = cleaned["domain_obj"].pk if name == "domain" else cleaned[name]
        old = before[name]
        if name == "title":
            old = student.title  # stored value ("" = no title), not the form's "No title" sentinel
        if name == "domain":
            old = int(old) if old else None
        if new == old:
            continue
        changes[name] = {"old": _jsonable(old), "new": _jsonable(new)}
        if name in ("title", "full_name", "gender", "gender_detail", "mobile", "reg_no", "degree",
                    "college_name", "college_location", "pronouns"):
            setattr(student, name, new)
        elif name == "preferred_name":
            student.short_name = new
        elif name in ("email", "alternate_email"):
            kind = StudentEmail.Kind.PRIMARY if name == "email" else StudentEmail.Kind.ALTERNATE
            if new:
                # A changed address is unverified again.
                StudentEmail.objects.update_or_create(student=student, kind=kind,
                                                      defaults={"address": new, "verified_at": None})
            else:
                StudentEmail.objects.filter(student=student, kind=kind).delete()
        elif name == "domain":
            internship.domain = cleaned["domain_obj"]
        elif name in ("start_date", "end_date", "project_title"):
            setattr(internship, name, new)

    student.save()
    if internship is not None:
        _reset_verifications_after_correction(internship, changes, request)
        internship.save()
    new_key = reg_no_key(collapse_whitespace(student.reg_no))
    if new_key != sub.reg_no_key:
        sub.reg_no_key = new_key
        try:
            with transaction.atomic():
                sub.save(update_fields=["reg_no_key"])
        except IntegrityError:
            raise WorkflowError("This university registration number has already been submitted for this "
                                "registration. Please verify your details or contact HR.") from None

    correction.status = CorrectionRequest.Status.COMPLETED
    correction.changes = changes
    correction.completed_at = timezone.now()
    correction.save(update_fields=["status", "changes", "completed_at"])
    sub.status = S.SUBMITTED
    sub.status_reason = ""
    sub.save(update_fields=["status", "status_reason"])
    # Field names only in the audit log; old/new values stay on the correction record.
    audit.record("submission.corrected", actor_label="student (correction link)", entity=sub, request=request,
                 metadata={"submission_id": sub.pk, "correction_id": correction.pk,
                           "changed_fields": sorted(changes)})
    from .services import flag_duplicates
    flag_duplicates(sub, request, actor_label="student (correction link)")
    return sub, changes
