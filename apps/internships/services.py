"""Internship completion, project verification, attendance history and LOR recommendation (Phase 4).

Registration is not proof of completion: these are separate HR decisions, each recorded with the
reviewer, time and note. Changing a decision withdraws the certificate approvals that relied on it.
Attendance mode is history only; it never affects eligibility or how certificates are delivered.
"""
from django.db import transaction
from django.utils import timezone

from apps.certificates import approvals
from apps.core import audit
from apps.core.workflow import STALE_MESSAGE, WorkflowError, clean_reason
from apps.registry.models import Submission

from .models import AttendancePeriod, Internship, Recommendation, lock_internship

C = Internship.Completion
P = Internship.ProjectStatus


def _lock(internship_id):
    return lock_internship(internship_id)


def _require_verified_student(internship):
    sub = internship.submission
    if sub is not None and sub.status != Submission.Status.VERIFIED:
        raise WorkflowError("Verify the student's registration details before recording this decision.")


def _record(action, actor, internship, request=None, **meta):
    audit.record(action, actor=actor, entity=internship, request=request,
                 metadata={"internship_id": internship.pk, "submission_id": internship.submission_id, **meta})


@transaction.atomic
def verify_completion(internship_id, *, actor, decision, start_date, end_date, note="",
                      expected_status=None, request=None):
    """Record whether the internship was completed, with the verified actual dates."""
    if decision not in (C.VERIFIED, C.NOT_COMPLETED):
        raise WorkflowError("Choose a valid completion decision.")
    note = clean_reason(note, required=decision == C.NOT_COMPLETED,
                        message="Please add a note explaining why the internship is not completed.")
    if not start_date or not end_date:
        raise WorkflowError("Enter the verified start and end dates.")
    if end_date < start_date:
        raise WorkflowError("The end date cannot be earlier than the start date.")
    internship = _lock(internship_id)
    if expected_status is not None and internship.completion_status != expected_status:
        raise WorkflowError(STALE_MESSAGE)
    _require_verified_student(internship)
    dates_changed = (internship.start_date, internship.end_date) != (start_date, end_date)
    if internship.completion_status == C.VERIFIED and (decision != C.VERIFIED or dates_changed):
        approvals.withdraw_approvals(internship, approvals.DEPENDS_ON_COMPLETION, actor=actor, request=request,
                                     why="internship completion decision or dates changed")
    previous = {"status": internship.completion_status, "start_date": internship.start_date.isoformat(),
                "end_date": internship.end_date.isoformat()}
    internship.start_date, internship.end_date = start_date, end_date
    internship.completion_status = decision
    internship.completion_note = note
    internship.completion_verified_by = actor
    internship.completion_verified_at = timezone.now()
    internship.save()
    _record("internship.completion_decided", actor, internship, request, decision=decision, previous=previous,
            start_date=start_date.isoformat(), end_date=end_date.isoformat(), note=note)
    return internship


@transaction.atomic
def verify_project(internship_id, *, actor, decision, note="", expected_status=None, request=None):
    if decision not in (P.VERIFIED, P.REJECTED):
        raise WorkflowError("Choose a valid project decision.")
    note = clean_reason(note, required=decision == P.REJECTED,
                        message="Please add a note explaining why the project was not accepted.")
    internship = _lock(internship_id)
    if expected_status is not None and internship.project_status != expected_status:
        raise WorkflowError(STALE_MESSAGE)
    _require_verified_student(internship)
    if decision == P.VERIFIED and not internship.project_title:
        raise WorkflowError("There is no project title. Request a correction from the student first.")
    if internship.project_status == P.VERIFIED and decision != P.VERIFIED:
        approvals.withdraw_approvals(internship, approvals.DEPENDS_ON_PROJECT, actor=actor, request=request,
                                     why="project verification changed")
    previous = internship.project_status
    internship.project_status = decision
    internship.project_note = note
    internship.project_verified_by = actor
    internship.project_verified_at = timezone.now()
    internship.save()
    _record("internship.project_decided", actor, internship, request, decision=decision, previous=previous, note=note)
    return internship


@transaction.atomic
def add_attendance_period(internship_id, *, actor, from_date, to_date, mode, request=None):
    if mode not in AttendancePeriod.Mode.values:
        raise WorkflowError("Choose online or offline.")
    if not from_date or not to_date or to_date < from_date:
        raise WorkflowError("Enter a valid date range (the end cannot be before the start).")
    internship = _lock(internship_id)
    if from_date < internship.start_date or to_date > internship.end_date:
        raise WorkflowError("The period must fall within the internship dates.")
    if internship.attendance_periods.filter(from_date__lte=to_date, to_date__gte=from_date).exists():
        raise WorkflowError("This period overlaps an existing attendance period.")
    period = AttendancePeriod.objects.create(internship=internship, from_date=from_date, to_date=to_date, mode=mode)
    _record("internship.attendance_added", actor, internship, request, period_id=period.pk, mode=mode,
            from_date=from_date.isoformat(), to_date=to_date.isoformat())
    return period


@transaction.atomic
def record_recommendation(internship_id, *, actor, decision, mentor_name="", mentor_feedback="",
                          performance_evaluation="", expected_decision=None, request=None):
    """The LOR recommendation decision. Separate from (and required before) LOR approval."""
    D = Recommendation.Decision
    if decision not in (D.RECOMMENDED, D.NOT_RECOMMENDED):
        raise WorkflowError("Choose a valid recommendation decision.")
    mentor_feedback = clean_reason(mentor_feedback, required=False)
    performance_evaluation = clean_reason(performance_evaluation, required=False)
    if decision == D.RECOMMENDED and not (mentor_feedback and performance_evaluation):
        raise WorkflowError("A recommendation needs both mentor feedback and a performance evaluation.")
    mentor_name = (mentor_name or "").strip()
    if len(mentor_name) > 150:
        raise WorkflowError("The mentor name must be 150 characters or fewer.")
    internship = _lock(internship_id)
    _require_verified_student(internship)
    rec, _ = Recommendation.objects.get_or_create(internship=internship)
    rec = Recommendation.objects.select_for_update().get(pk=rec.pk)
    if expected_decision is not None and rec.decision != expected_decision:
        raise WorkflowError(STALE_MESSAGE)
    if rec.decision == D.RECOMMENDED:
        approvals.withdraw_approvals(internship, approvals.DEPENDS_ON_RECOMMENDATION, actor=actor, request=request,
                                     why="recommendation decision changed")
    previous = rec.decision
    rec.decision = decision
    rec.mentor_name = mentor_name
    rec.mentor_feedback = mentor_feedback
    rec.performance_evaluation = performance_evaluation
    rec.decided_by = actor
    rec.decided_at = timezone.now()
    rec.save()
    # Feedback text stays on the record; the audit log holds the decision only.
    _record("internship.recommendation_decided", actor, internship, request, decision=decision, previous=previous)
    return rec
