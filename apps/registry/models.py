import re
import secrets

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

_WS = re.compile(r"\s+")


def collapse_whitespace(value):
    return _WS.sub(" ", (value or "").strip())


def comparison_key(value):
    """Case- and whitespace-insensitive key for duplicate comparison only."""
    return collapse_whitespace(value).casefold()


def reg_no_key(value):
    """Registration numbers compared without case, spaces, or separators.

    Used only to *flag* possible duplicates for review, never as a unique key.
    """
    return re.sub(r"[\s\-/._]", "", (value or "")).casefold()


def _with_derived(kwargs, source, derived):
    """When saving with update_fields, also save a key derived from a field being saved."""
    fields = kwargs.get("update_fields")
    if fields is not None and source in fields and derived not in fields:
        kwargs["update_fields"] = [*fields, derived]
    return kwargs


def new_public_token():
    return secrets.token_urlsafe(24)


class Domain(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending approval"
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    name = models.CharField(max_length=100)
    normalized_key = models.CharField(max_length=100, unique=True, editable=False)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    requested_by_submission = models.ForeignKey(
        "Submission", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        permissions = [("manage_domain", "Can approve, rename, activate and deactivate domains")]

    def save(self, *args, **kwargs):
        self.name = collapse_whitespace(self.name)
        self.normalized_key = comparison_key(self.name)
        super().save(*args, **_with_derived(kwargs, "name", "normalized_key"))

    def __str__(self):
        return self.name


class Campaign(models.Model):
    """A registration batch. Its QR code encodes only the public registration URL."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Deactivated"

    name = models.CharField(max_length=150)
    domain = models.ForeignKey(Domain, null=True, blank=True, on_delete=models.PROTECT, related_name="campaigns")
    opens_at = models.DateTimeField()
    closes_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    public_token = models.CharField(max_length=64, unique=True, default=new_public_token, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-opens_at"]
        constraints = [
            models.CheckConstraint(condition=Q(closes_at__gt=F("opens_at")), name="campaign_closes_after_opens"),
        ]
        permissions = [("manage_campaign", "Can create, deactivate and reopen registration campaigns")]

    def is_accepting(self, at=None):
        at = at or timezone.now()
        return self.status == self.Status.ACTIVE and self.opens_at <= at < self.closes_at

    def availability(self, at=None):
        """One of: open, scheduled, expired, deactivated."""
        at = at or timezone.now()
        if self.status != self.Status.ACTIVE:
            return "deactivated"
        if at < self.opens_at:
            return "scheduled"
        if at >= self.closes_at:
            return "expired"
        return "open"

    def __str__(self):
        return self.name


class Student(models.Model):
    class Gender(models.TextChoices):
        MALE = "male", "Male"
        FEMALE = "female", "Female"
        OTHERS = "others", "Others"

    class Pronouns(models.TextChoices):
        # Chosen by the student; never inferred from gender.
        HE = "he", "He / him"
        SHE = "she", "She / her"
        THEY = "they", "They / them"

    # Validated against the configured title list; empty = "No title" (if the office allows it).
    title = models.CharField(max_length=10, blank=True)
    full_name = models.CharField(max_length=150)
    # Optional preferred name entered by the student (HR may correct it via a correction request). Used only in
    # the salutation/LOR short-name slots; the full name is used when it is empty. Never auto-derived.
    short_name = models.CharField(max_length=150, blank=True)
    # Blank only for students registered before pronouns were collected; generation then asks HR to collect them.
    pronouns = models.CharField(max_length=10, choices=Pronouns.choices, blank=True)
    gender = models.CharField(max_length=10, choices=Gender.choices)
    gender_detail = models.CharField(max_length=100, blank=True)
    mobile = models.CharField(max_length=10)
    reg_no = models.CharField(max_length=40)
    reg_no_key = models.CharField(max_length=40, db_index=True, editable=False)
    degree = models.CharField(max_length=150)
    college_name = models.CharField(max_length=200)
    college_location = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["full_name"]

    def save(self, *args, **kwargs):
        self.reg_no = collapse_whitespace(self.reg_no)
        self.reg_no_key = reg_no_key(self.reg_no)
        super().save(*args, **_with_derived(kwargs, "reg_no", "reg_no_key"))

    def __str__(self):
        return f"{self.title} {self.full_name}"


class StudentEmail(models.Model):
    class Kind(models.TextChoices):
        PRIMARY = "primary", "Registered"
        ALTERNATE = "alternate", "Alternative"

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="emails")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    address = models.EmailField()
    # Lowercased only; no Gmail dot/plus folding (those can be different mailboxes in policy terms).
    address_key = models.CharField(max_length=254, db_index=True, editable=False)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["student", "kind"], name="one_email_per_kind")]

    def save(self, *args, **kwargs):
        self.address = self.address.strip()
        self.address_key = self.address.lower()
        super().save(*args, **_with_derived(kwargs, "address", "address_key"))

    @property
    def is_verified(self):
        return self.verified_at is not None

    def __str__(self):
        return self.address


class Submission(models.Model):
    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        UNDER_REVIEW = "under_review", "Under review"
        CORRECTION_REQUESTED = "correction_requested", "Correction requested"
        VERIFIED = "verified", "Verified"
        REJECTED = "rejected", "Rejected"

    campaign = models.ForeignKey(Campaign, on_delete=models.PROTECT, related_name="submissions")
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="submissions")
    # Exactly what the student typed, before any normalization, for audit.
    raw_payload = models.JSONField()
    # Copied from the student at submission time so the database can block a repeated
    # registration number within one campaign, even under concurrent requests.
    reg_no_key = models.CharField(max_length=40, editable=False)
    # Validator notes for HR (e.g. unrecognized degree format). Never shown publicly.
    review_notes = models.JSONField(default=list, blank=True)
    feedback = models.TextField(blank=True)
    status = models.CharField(max_length=25, choices=Status.choices, default=Status.SUBMITTED)
    status_reason = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    # Form token: a resubmitted/double-clicked form maps to the same submission.
    idempotency_key = models.CharField(max_length=64, unique=True)
    submitted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-submitted_at"]
        constraints = [
            models.UniqueConstraint(fields=["campaign", "student"], name="one_submission_per_student_per_campaign"),
            models.UniqueConstraint(fields=["campaign", "reg_no_key"], name="one_submission_per_reg_no_per_campaign"),
            models.CheckConstraint(
                condition=~Q(status__in=["rejected", "correction_requested"]) | ~Q(status_reason=""),
                name="rejection_or_correction_requires_reason",
            ),
            models.CheckConstraint(
                condition=~Q(status__in=["verified", "rejected"]) | Q(reviewed_by__isnull=False),
                name="decision_requires_reviewer",
            ),
        ]
        permissions = [("review_submission", "Can approve, reject or request corrections on submissions")]

    def save(self, *args, **kwargs):
        if not self.reg_no_key:
            self.reg_no_key = self.student.reg_no_key
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.student} / {self.campaign}"


class DuplicateCandidate(models.Model):
    """A possible duplicate flagged for HR review. Nothing is merged or deleted automatically."""

    class Reason(models.TextChoices):
        REG_NO = "reg_no", "Same registration number"
        EMAIL = "email", "Same email address"
        MOBILE = "mobile", "Same mobile number"

    class Resolution(models.TextChoices):
        PENDING = "pending", "Pending review"
        NOT_DUPLICATE = "not_duplicate", "Not a duplicate"
        DUPLICATE = "duplicate", "Confirmed duplicate"
        FURTHER_REVIEW = "further_review", "Requires further review"

    # Flags in these states block approval of the submission.
    OPEN_RESOLUTIONS = (Resolution.PENDING, Resolution.FURTHER_REVIEW)
    BLOCKING_RESOLUTIONS = (*OPEN_RESOLUTIONS, Resolution.DUPLICATE)

    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="duplicate_flags")
    matches = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="+")
    reason = models.CharField(max_length=10, choices=Reason.choices)
    resolution = models.CharField(max_length=15, choices=Resolution.choices, default=Resolution.PENDING)
    resolution_reason = models.TextField(blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["submission", "matches", "reason"], name="unique_duplicate_flag"),
            models.CheckConstraint(condition=~Q(submission=F("matches")), name="duplicate_not_self"),
            models.CheckConstraint(
                condition=Q(resolution="pending") | (Q(resolved_by__isnull=False) & ~Q(resolution_reason="")),
                name="duplicate_decision_has_reviewer_and_reason",
            ),
        ]
        permissions = [("resolve_duplicate", "Can resolve possible duplicate submissions")]


class SubmissionNote(models.Model):
    """Internal HR note on a submission. Never shown to students. Notes are not edited or deleted."""

    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="notes")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class CorrectionRequest(models.Model):
    """HR request for the student to correct specific fields through a private link.

    Only a SHA-256 hash of the link token is stored; the raw token is shown to HR once.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Awaiting student"
        COMPLETED = "completed", "Corrected by student"
        CANCELLED = "cancelled", "Cancelled"

    submission = models.ForeignKey(Submission, on_delete=models.PROTECT, related_name="correction_requests")
    fields = models.JSONField()  # list of correctable field names
    reason = models.TextField()
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    requested_at = models.DateTimeField(auto_now_add=True)
    # {field: {"old": ..., "new": ...}} for fields the student actually changed. Kept here, not in the audit log.
    changes = models.JSONField(default=dict, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+")
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-requested_at"]
        constraints = [
            models.UniqueConstraint(fields=["submission"], condition=Q(status="open"),
                                    name="one_open_correction_per_submission"),
            models.CheckConstraint(condition=~Q(reason=""), name="correction_requires_reason"),
            models.CheckConstraint(condition=~Q(status="completed") | Q(completed_at__isnull=False),
                                   name="completed_correction_has_time"),
            models.CheckConstraint(
                condition=~Q(status="cancelled") | Q(cancelled_by__isnull=False, cancelled_at__isnull=False),
                name="cancelled_correction_has_actor"),
        ]

    def is_usable(self, at=None):
        return self.status == self.Status.OPEN and (at or timezone.now()) < self.expires_at
