"""HR pages for completion verification, LOR recommendation and per-certificate approval (Phase 4)."""
import datetime as dt

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.certificates import approvals
from apps.core.models import AuditLog
from apps.core.workflow import WorkflowError

from . import services
from .models import AttendancePeriod, Internship, Recommendation


def _date(value):
    try:
        return dt.date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def _back(internship_id, anchor=""):
    return redirect(reverse("internships:detail", kwargs={"pk": internship_id}) + anchor)


@login_required
@permission_required("internships.view_internship", raise_exception=True)
@require_GET
def detail(request, pk):
    internship = get_object_or_404(
        Internship.objects.select_related("student", "domain", "submission", "campaign",
                                          "completion_verified_by", "project_verified_by"), pk=pk)
    history = (AuditLog.objects.select_related("actor")
               .filter(Q(entity_type="internships.Internship", entity_id=str(pk)) | Q(metadata__internship_id=pk))
               .order_by("created_at", "id"))
    return render(request, "internships/detail.html", {
        "i": internship,
        "recommendation": Recommendation.objects.filter(internship=internship).select_related("decided_by").first(),
        "attendance": internship.attendance_periods.all(),
        "certificates": approvals.certificate_overview(internship, request.user),
        "history": history,
        "C": Internship.Completion, "P": Internship.ProjectStatus, "R": Recommendation.Decision,
        "modes": AttendancePeriod.Mode.choices,
        "production_enabled": settings.CERT_PRODUCTION_ISSUANCE_ENABLED,
        "may_issue": request.user.has_perm("certificates.issue_certificate"),
        "today": timezone.localdate(),
    })


def _run(request, fn, success):
    try:
        fn()
        messages.success(request, success)
    except WorkflowError as exc:
        messages.error(request, str(exc))


@login_required
@permission_required("internships.verify_completion", raise_exception=True)
@require_POST
def completion(request, pk):
    p = request.POST
    _run(request, lambda: services.verify_completion(
        pk, actor=request.user, decision=p.get("decision"), start_date=_date(p.get("start_date")),
        end_date=_date(p.get("end_date")), note=p.get("note", ""),
        expected_status=p.get("expected_status") or None, request=request), "Completion decision recorded.")
    return _back(pk, "#completion")


@login_required
@permission_required("internships.verify_project", raise_exception=True)
@require_POST
def project(request, pk):
    p = request.POST
    _run(request, lambda: services.verify_project(
        pk, actor=request.user, decision=p.get("decision"), note=p.get("note", ""),
        expected_status=p.get("expected_status") or None, request=request), "Project decision recorded.")
    return _back(pk, "#project")


@login_required
@permission_required("internships.verify_completion", raise_exception=True)
@require_POST
def attendance(request, pk):
    p = request.POST
    _run(request, lambda: services.add_attendance_period(
        pk, actor=request.user, from_date=_date(p.get("from_date")), to_date=_date(p.get("to_date")),
        mode=p.get("mode"), request=request), "Attendance period added.")
    return _back(pk, "#attendance")


@login_required
@permission_required("internships.decide_recommendation", raise_exception=True)
@require_POST
def recommendation(request, pk):
    p = request.POST
    _run(request, lambda: services.record_recommendation(
        pk, actor=request.user, decision=p.get("decision"), mentor_name=p.get("mentor_name", ""),
        mentor_feedback=p.get("mentor_feedback", ""), performance_evaluation=p.get("performance_evaluation", ""),
        expected_decision=p.get("expected_decision") or None, request=request), "Recommendation decision recorded.")
    return _back(pk, "#recommendation")


@login_required
@permission_required("certificates.approve_certificate", raise_exception=True)
@require_POST
def certificate_decision(request, pk, cert_type):
    get_object_or_404(Internship, pk=pk)
    if cert_type in approvals.FINAL_AUTHORITY and not approvals.may_decide(request.user, cert_type):
        raise PermissionDenied
    p = request.POST
    _run(request, lambda: approvals.decide(
        pk, cert_type, p.get("decision"), actor=request.user, reason=p.get("reason", ""),
        expected_status=p.get("expected_status") or None, request=request), "Certificate decision recorded.")
    return _back(pk, "#certificates")


@login_required
@permission_required("certificates.generate_certificate", raise_exception=True)
@require_POST
def certificate_generate(request, pk, cert_type):
    from apps.certificates import generation

    get_object_or_404(Internship, pk=pk)
    raw_date = request.POST.get("issue_date")
    issue_date = _date(raw_date) if raw_date else None
    if raw_date and issue_date is None:
        messages.error(request, "Enter a valid issue date.")
        return _back(pk, "#certificates")
    _run(request, lambda: generation.generate_draft(pk, cert_type, actor=request.user, issue_date=issue_date,
                                                     request=request),
         "Draft PDF generated. It has not been issued or sent.")
    return _back(pk, "#certificates")
