"""HR submission review and the public student-correction pages (Phase 4)."""
import functools

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.core import ratelimit
from apps.core.audit import client_ip
from apps.core.models import AuditLog
from apps.core.workflow import WorkflowError

from . import review
from .forms import CorrectionForm
from .models import Campaign, CorrectionRequest, Domain, DuplicateCandidate, Submission

OPEN_FLAGS = Q(duplicate_flags__resolution__in=DuplicateCandidate.OPEN_RESOLUTIONS)


def secret_page(view):
    """Headers for pages whose URL or body carries a correction token."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        response = view(*args, **kwargs)
        # strict-origin: the Referer carries only the site origin, never the token-bearing path.
        # ("no-referrer" would make browsers send "Origin: null" on the form POST and fail CSRF.)
        response["Referrer-Policy"] = "strict-origin"
        response["X-Robots-Tag"] = "noindex, nofollow"
        response["Cache-Control"] = "no-store"
        return response
    return wrapped


def _absolute(request, path):
    return (settings.PUBLIC_BASE_URL + path) if settings.PUBLIC_BASE_URL else request.build_absolute_uri(path)


# --- List and detail -------------------------------------------------------------

@login_required
@permission_required("registry.view_submission", raise_exception=True)
@require_GET
def submission_list(request):
    qs = (Submission.objects.select_related("student", "campaign")
          .prefetch_related("internships__domain")
          .annotate(open_flags=Count("duplicate_flags", filter=OPEN_FLAGS, distinct=True))
          .order_by("-submitted_at"))
    f = request.GET
    q = (f.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(student__full_name__icontains=q) | Q(student__reg_no__icontains=q)
                       | Q(student__emails__address_key__icontains=q.lower()) | Q(student__mobile__icontains=q)
                       | Q(student__college_name__icontains=q)).distinct()
    if f.get("campaign", "").isdigit():
        qs = qs.filter(campaign_id=int(f["campaign"]))
    if f.get("domain", "").isdigit():
        qs = qs.filter(internships__domain_id=int(f["domain"]))
    if f.get("status") in Submission.Status.values:
        qs = qs.filter(status=f["status"])
    dup = f.get("duplicates")
    if dup == "open":
        qs = qs.filter(open_flags__gt=0)
    elif dup == "further_review":
        qs = qs.filter(duplicate_flags__resolution=DuplicateCandidate.Resolution.FURTHER_REVIEW).distinct()
    elif dup == "none":
        qs = qs.filter(open_flags=0)
    page = Paginator(qs, 25).get_page(f.get("page"))
    params = f.copy()
    params.pop("page", None)
    return render(request, "registry/hr/submission_list.html", {
        "page": page, "filters": f, "querystring": params.urlencode(),
        "campaigns": Campaign.objects.order_by("-opens_at"), "domains": Domain.objects.order_by("name"),
        "statuses": Submission.Status.choices,
    })


def submission_history(submission):
    return (AuditLog.objects.select_related("actor")
            .filter(Q(entity_type="registry.Submission", entity_id=str(submission.pk))
                    | Q(metadata__submission_id=submission.pk))
            .order_by("created_at", "id"))


@login_required
@permission_required("registry.view_submission", raise_exception=True)
@require_GET
def submission_detail(request, pk):
    sub = get_object_or_404(Submission.objects.select_related("student", "campaign", "reviewed_by"), pk=pk)
    flags = (sub.duplicate_flags.select_related("matches__student", "matches__campaign", "resolved_by")
             .order_by("resolution", "reason"))
    return render(request, "registry/hr/submission_detail.html", {
        "sub": sub, "student": sub.student,
        "emails": sub.student.emails.order_by("kind"),
        "internships": sub.internships.select_related("domain"),
        "flags": flags,
        # Read-only: newer submissions that were flagged as possibly duplicating this one.
        "flagged_by": (DuplicateCandidate.objects.filter(matches=sub)
                       .select_related("submission__student", "submission__campaign")),
        "notes": sub.notes.select_related("author"),
        "corrections": sub.correction_requests.select_related("requested_by", "cancelled_by"),
        "history": submission_history(sub),
        "correctable": review.CORRECTABLE,
        "duplicate_choices": [(v, DuplicateCandidate.Resolution(v).label) for v in review.DUPLICATE_DECISIONS],
        "S": Submission.Status,
    })


# --- Actions -------------------------------------------------------------------------

def _correction_link_page(request, sub, correction, raw):
    link = _absolute(request, reverse("registry:correction", kwargs={"token": raw}))
    return secret_page(render)(request, "registry/hr/correction_link.html",
                               {"sub": sub, "correction": correction, "link": link})


@login_required
@permission_required("registry.review_submission", raise_exception=True)
@require_POST
def submission_action(request, pk):
    sub = get_object_or_404(Submission, pk=pk)
    action = request.POST.get("action")
    expected = request.POST.get("expected_status") or None
    reason = request.POST.get("reason", "")
    common = {"actor": request.user, "expected_status": expected, "request": request}
    try:
        if action == "start_review":
            review.start_review(sub.pk, **common)
            messages.success(request, "Marked as under review.")
        elif action == "approve":
            review.approve(sub.pk, note=reason, **common)
            messages.success(request, "Registration details verified.")
        elif action == "reject":
            review.reject(sub.pk, reason=reason, **common)
            messages.success(request, "Submission rejected.")
        elif action == "reopen":
            review.reopen(sub.pk, reason=reason, **common)
            messages.success(request, "Submission reopened for review.")
        elif action == "request_correction":
            correction, raw = review.request_correction(sub.pk, groups=request.POST.getlist("fields"),
                                                        reason=reason, **common)
            return _correction_link_page(request, sub, correction, raw)
        elif action == "regenerate_link":
            correction, raw = review.regenerate_correction_link(sub.pk, **common)
            return _correction_link_page(request, sub, correction, raw)
        elif action == "cancel_correction":
            review.cancel_correction(sub.pk, **common)
            messages.success(request, "Correction request cancelled.")
        else:
            messages.error(request, "Unknown action.")
    except WorkflowError as exc:
        messages.error(request, str(exc))
    return redirect("registry:submission_detail", pk=sub.pk)


@login_required
@permission_required("registry.review_submission", raise_exception=True)
@require_POST
def submission_add_note(request, pk):
    sub = get_object_or_404(Submission, pk=pk)
    try:
        review.add_note(sub.pk, actor=request.user, text=request.POST.get("text", ""), request=request)
        messages.success(request, "Note added.")
    except WorkflowError as exc:
        messages.error(request, str(exc))
    return redirect(reverse("registry:submission_detail", kwargs={"pk": sub.pk}) + "#notes")


@login_required
@permission_required("registry.resolve_duplicate", raise_exception=True)
@require_POST
def duplicate_resolve(request, pk):
    flag = get_object_or_404(DuplicateCandidate, pk=pk)
    try:
        review.resolve_duplicate(flag.pk, actor=request.user, resolution=request.POST.get("resolution"),
                                 reason=request.POST.get("reason", ""),
                                 expected_resolution=request.POST.get("expected_resolution") or None,
                                 request=request)
        messages.success(request, "Duplicate decision recorded.")
    except WorkflowError as exc:
        messages.error(request, str(exc))
    return redirect(reverse("registry:submission_detail", kwargs={"pk": flag.submission_id}) + "#duplicates")


# --- Public correction pages ------------------------------------------------------------

def _unavailable(request, status=410):
    return render(request, "registry/public/correction_unavailable.html", status=status)


@secret_page
@never_cache
@require_http_methods(["GET", "POST"])
def correction(request, token):
    corr = review.find_correction(token)
    if corr is None:
        return _unavailable(request, 404)
    if not corr.is_usable() or corr.submission.status != Submission.Status.CORRECTION_REQUESTED:
        return _unavailable(request)
    sub = corr.submission
    current = review.current_values(sub)
    allowed = review.fields_for(corr)
    if request.method == "POST":
        if ratelimit.hit("correction-posts", client_ip(request), 3600) > settings.REGISTRATION_POSTS_PER_HOUR:
            return render(request, "registry/public/unavailable.html", {"reason": "rate_limited"}, status=429)
        form = CorrectionForm(request.POST, allowed_fields=allowed, current=current, campaign=sub.campaign)
        if form.is_valid():
            try:
                review.apply_correction(token, form.cleaned_data, request=request)
            except WorkflowError as exc:
                form.add_error(None, str(exc))
            else:
                return redirect("registry:correction_done")
    else:
        form = CorrectionForm(allowed_fields=allowed, current=current, campaign=sub.campaign)
    groups = [review.CORRECTABLE[g][0] for g in corr.fields]
    return render(request, "registry/public/correction_form.html",
                  {"form": form, "correction": corr, "groups": groups, "campaign": sub.campaign})


@secret_page
@never_cache
def correction_done(request):
    return render(request, "registry/public/correction_done.html")
