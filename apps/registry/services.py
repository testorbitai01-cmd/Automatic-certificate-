"""Registration workflow. Submitting never approves a student or creates a certificate."""
from django.db import IntegrityError, transaction

from apps.core import audit
from apps.internships.models import Internship

from .models import (Campaign, Domain, DuplicateCandidate, Student, StudentEmail, Submission, collapse_whitespace,
                     comparison_key, reg_no_key)

PUBLIC_ACTOR = "student (public form)"
MAX_FLAGS_PER_REASON = 20

# Fields copied as typed into Submission.raw_payload (never the CSRF token).
RAW_FIELDS = ["title", "full_name", "preferred_name", "pronouns", "gender", "gender_detail", "mobile", "reg_no", "email", "alternate_email",
              "degree", "college_name", "college_location", "domain", "requested_domain", "start_date",
              "end_date", "project_title", "feedback"]


class RegistrationError(Exception):
    """A submission was refused for a reason that can be shown to the student."""

    message = ""

    def __str__(self):
        return self.message


class CampaignClosed(RegistrationError):
    message = "This registration link is no longer accepting responses. Please contact HR."


class DuplicateInCampaign(RegistrationError):
    message = ("This university registration number has already been submitted for this registration. "
               "Please verify your details or contact HR.")


class DomainUnavailable(RegistrationError):
    message = "The domain you requested is not currently offered. Please choose a domain from the list."


class TokenMismatch(RegistrationError):
    message = "This form has expired. Please reload the page and try again."


def _resolve_domain(data):
    if data.get("domain_obj") is not None:
        return data["domain_obj"], False
    name = data["requested_domain"]
    existing = Domain.objects.filter(normalized_key=comparison_key(name)).first()
    created = False
    if existing is None:
        try:
            with transaction.atomic():
                existing = Domain.objects.create(name=name, status=Domain.Status.PENDING)
                created = True
        except IntegrityError:
            # Another student requested the same domain at the same moment.
            existing = Domain.objects.get(normalized_key=comparison_key(name))
    if existing.status == Domain.Status.INACTIVE:
        raise DomainUnavailable()
    return existing, created


def _existing_for_token(campaign, token):
    existing = Submission.objects.filter(idempotency_key=token).first()
    if existing is not None and existing.campaign_id != campaign.pk:
        raise TokenMismatch()
    return existing


def submit_registration(campaign, data, raw, request=None):
    """Create the student, submission and (unverified) internship records.

    Returns (submission, created). A repeated form token returns the original submission,
    so double clicks and browser retries never create a second record.
    """
    token = data["form_token"]
    existing = _existing_for_token(campaign, token)
    if existing is not None:
        return existing, False

    try:
        try:
            with transaction.atomic():
                # Lock the campaign row: serializes submissions per campaign and makes the
                # open/closed check and the duplicate check race-free on PostgreSQL.
                locked = Campaign.objects.select_for_update().get(pk=campaign.pk)
                if not locked.is_accepting():
                    raise CampaignClosed()
                return _create_records(locked, data, raw, request), True
        except IntegrityError:
            # Lost a race: either the same form token or the same registration number won first.
            existing = _existing_for_token(campaign, token)
            if existing is not None:
                return existing, False
            raise DuplicateInCampaign() from None
    except DuplicateInCampaign:
        audit.record("submission.blocked_duplicate", actor_label=PUBLIC_ACTOR, entity=campaign,
                     metadata={"reason": "reg_no_in_campaign"}, request=request)
        raise


def _create_records(campaign, data, raw, request):
    student = Student(
        title=data["title"], full_name=data["full_name"], short_name=data.get("preferred_name", ""),
        pronouns=data["pronouns"], gender=data["gender"],
        gender_detail=data["gender_detail"], mobile=data["mobile"], reg_no=data["reg_no"],
        degree=data["degree"], college_name=data["college_name"], college_location=data["college_location"],
    )
    student.reg_no_key = reg_no_key(collapse_whitespace(student.reg_no))
    if Submission.objects.filter(campaign=campaign, reg_no_key=student.reg_no_key).exists():
        raise DuplicateInCampaign()

    domain, domain_created = _resolve_domain(data)
    student.save()
    StudentEmail.objects.create(student=student, kind=StudentEmail.Kind.PRIMARY, address=data["email"])
    if data.get("alternate_email"):
        StudentEmail.objects.create(student=student, kind=StudentEmail.Kind.ALTERNATE,
                                    address=data["alternate_email"])

    submission = Submission.objects.create(
        campaign=campaign, student=student, idempotency_key=data["form_token"],
        raw_payload={k: raw.get(k, "") for k in RAW_FIELDS}, feedback=data.get("feedback", ""),
        review_notes=list(data.get("review_notes", [])),
    )
    Internship.objects.create(
        student=student, campaign=campaign, submission=submission, domain=domain,
        start_date=data["start_date"], end_date=data["end_date"],
        project_title=data["project_title"],
        project_status=Internship.ProjectStatus.PENDING if data["project_title"] else
        Internship.ProjectStatus.NOT_APPLICABLE,
    )

    # Audit metadata holds IDs only, no personal details.
    audit.record("submission.created", actor_label=PUBLIC_ACTOR, entity=submission,
                 metadata={"campaign_id": campaign.pk}, request=request)
    if domain_created:
        domain.requested_by_submission = submission
        domain.save(update_fields=["requested_by_submission", "updated_at"])
        audit.record("domain.requested", actor_label=PUBLIC_ACTOR, entity=domain,
                     metadata={"submission_id": submission.pk}, request=request)
    flag_duplicates(submission, request)
    return submission


def flag_duplicates(submission, request=None, actor_label=PUBLIC_ACTOR):
    """Flag other submissions sharing this student's registration number, email or mobile.

    Safe to run again (e.g. after a correction): existing flags are kept, only new matches are added.
    """
    student = submission.student
    others = Submission.objects.exclude(pk=submission.pk)
    emails = list(student.emails.values_list("address_key", flat=True))
    checks = [
        (DuplicateCandidate.Reason.REG_NO, others.filter(reg_no_key=student.reg_no_key)),
        (DuplicateCandidate.Reason.EMAIL, others.filter(student__emails__address_key__in=emails).distinct()),
        (DuplicateCandidate.Reason.MOBILE, others.filter(student__mobile=student.mobile)),
    ]
    flagged = {}
    for reason, qs in checks:
        existing = set(submission.duplicate_flags.filter(reason=reason).values_list("matches_id", flat=True))
        matches = [pk for pk in qs.order_by("-submitted_at").values_list("pk", flat=True)[:MAX_FLAGS_PER_REASON]
                   if pk not in existing]
        DuplicateCandidate.objects.bulk_create(
            [DuplicateCandidate(submission=submission, matches_id=pk, reason=reason) for pk in matches],
            ignore_conflicts=True,
        )
        if matches:
            flagged[reason] = len(matches)
    if flagged:
        audit.record("submission.duplicate_flagged", actor_label=actor_label, entity=submission,
                     metadata={"reasons": flagged, "submission_id": submission.pk}, request=request)
    return flagged
