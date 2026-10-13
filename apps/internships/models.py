from django.conf import settings
from django.db import models
from django.db.models import F, Q


class Internship(models.Model):
    """One internship engagement. A student may have more than one (different batches/domains)."""

    class Completion(models.TextChoices):
        PENDING = "pending", "Pending verification"
        VERIFIED = "verified", "Completion verified"
        NOT_COMPLETED = "not_completed", "Not completed"

    class ProjectStatus(models.TextChoices):
        NOT_APPLICABLE = "not_applicable", "No project"
        PENDING = "pending", "Pending verification"
        VERIFIED = "verified", "Project verified"
        REJECTED = "rejected", "Project not accepted"

    student = models.ForeignKey("registry.Student", on_delete=models.PROTECT, related_name="internships")
    campaign = models.ForeignKey("registry.Campaign", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="internships")
    submission = models.ForeignKey("registry.Submission", null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="internships")
    domain = models.ForeignKey("registry.Domain", on_delete=models.PROTECT, related_name="internships")
    start_date = models.DateField()
    end_date = models.DateField()

    completion_status = models.CharField(max_length=15, choices=Completion.choices, default=Completion.PENDING)
    completion_verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    completion_verified_at = models.DateTimeField(null=True, blank=True)
    completion_note = models.TextField(blank=True)

    project_title = models.CharField(max_length=200, blank=True)
    project_status = models.CharField(max_length=15, choices=ProjectStatus.choices,
                                      default=ProjectStatus.NOT_APPLICABLE)
    project_verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    project_verified_at = models.DateTimeField(null=True, blank=True)
    project_note = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date"]
        constraints = [
            models.CheckConstraint(condition=Q(end_date__gte=F("start_date")), name="internship_end_after_start"),
            models.CheckConstraint(
                condition=Q(completion_status="pending")
                | Q(completion_verified_by__isnull=False, completion_verified_at__isnull=False),
                name="completion_decision_has_reviewer",
            ),
            models.CheckConstraint(
                condition=~Q(completion_status="not_completed") | ~Q(completion_note=""),
                name="not_completed_requires_note",
            ),
            models.CheckConstraint(
                condition=~Q(project_status__in=["verified", "rejected"])
                | Q(project_verified_by__isnull=False, project_verified_at__isnull=False),
                name="project_decision_has_reviewer",
            ),
            models.CheckConstraint(
                condition=~Q(project_status="verified") | ~Q(project_title=""),
                name="verified_project_has_title",
            ),
            models.CheckConstraint(
                condition=~Q(project_status="rejected") | ~Q(project_note=""),
                name="rejected_project_requires_note",
            ),
        ]
        permissions = [
            ("verify_completion", "Can verify internship completion"),
            ("verify_project", "Can verify project completion"),
        ]

    def __str__(self):
        return f"{self.student} – {self.domain} ({self.start_date:%b %Y}–{self.end_date:%b %Y})"


class AttendancePeriod(models.Model):
    """Optional attendance-mode history. Never used for eligibility or certificate text."""

    class Mode(models.TextChoices):
        ONLINE = "online", "Online"
        OFFLINE = "offline", "Offline"

    internship = models.ForeignKey(Internship, on_delete=models.CASCADE, related_name="attendance_periods")
    from_date = models.DateField()
    to_date = models.DateField()
    mode = models.CharField(max_length=10, choices=Mode.choices)

    class Meta:
        ordering = ["from_date"]
        constraints = [
            models.CheckConstraint(condition=Q(to_date__gte=F("from_date")), name="attendance_to_after_from"),
        ]


class Recommendation(models.Model):
    """The LOR decision. LORs are never issued automatically."""

    class Decision(models.TextChoices):
        PENDING = "pending", "Pending"
        RECOMMENDED = "recommended", "Recommended"
        NOT_RECOMMENDED = "not_recommended", "Not recommended"

    internship = models.OneToOneField(Internship, on_delete=models.PROTECT, related_name="recommendation")
    mentor_name = models.CharField(max_length=150, blank=True)
    mentor_feedback = models.TextField(blank=True)
    performance_evaluation = models.TextField(blank=True)
    decision = models.CharField(max_length=20, choices=Decision.choices, default=Decision.PENDING)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(decision="pending") | Q(decided_by__isnull=False, decided_at__isnull=False),
                name="recommendation_decision_has_reviewer",
            ),
            models.CheckConstraint(
                condition=~Q(decision="recommended") | (~Q(mentor_feedback="") & ~Q(performance_evaluation="")),
                name="recommended_requires_feedback_and_evaluation",
            ),
        ]
        permissions = [("decide_recommendation", "Can record the recommendation decision")]


def lock_internship(internship_id):
    """Lock an internship for a decision, in the same order as the submission workflow.

    Order: the student's submission row first, then the internship row, each in its own statement.
    This matches review.reopen()/request_correction() (submission -> internship), so the two cannot
    deadlock, and every status read happens after the locks are held. (Reading the submission through
    a join in the locking query would use the statement's starting snapshot, so a concurrent reopen
    could be missed.)
    """
    from apps.registry.models import Submission

    submission_id = Internship.objects.values_list("submission_id", flat=True).get(pk=internship_id)
    if submission_id is not None:
        Submission.objects.select_for_update().filter(pk=submission_id).first()
    internship = Internship.objects.select_for_update().get(pk=internship_id)
    # Fresh reads, after both locks are held.
    internship = Internship.objects.select_related("submission", "student", "domain").get(pk=internship_id)
    return internship
