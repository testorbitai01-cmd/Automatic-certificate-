"""Draft/issued certificate downloads and issuance actions (Phase 6). Nothing here sends email."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.core import audit
from apps.core.workflow import WorkflowError

from . import generation
from .models import Issuance


def _back(issuance):
    return redirect(reverse("internships:detail", kwargs={"pk": issuance.request.internship_id}) + "#certificates")


@login_required
@permission_required("certificates.download_certificate", raise_exception=True)
@require_GET
def download(request, pk):
    issuance = get_object_or_404(Issuance.objects.select_related("request", "file"), pk=pk)
    if issuance.file is None:
        raise Http404("No PDF has been generated.")
    try:
        handle = issuance.file.file.open("rb")
    except FileNotFoundError:
        raise Http404("The stored PDF is missing.") from None
    audit.record("certificate.downloaded", actor=request.user, entity=issuance, request=request,
                 metadata={"issuance_id": issuance.pk, "cert_type": issuance.request.cert_type,
                           "internship_id": issuance.request.internship_id, "sha256": issuance.file.sha256})
    status = "issued" if issuance.status == Issuance.Status.ISSUED else "DRAFT-not-issued"
    response = FileResponse(handle, as_attachment=True, content_type="application/pdf",
                            filename=f"{issuance.request.cert_type}-{issuance.pk}-v{issuance.version_no}-{status}.pdf")
    response["Cache-Control"] = "no-store, private"
    return response


@login_required
@require_POST
def action(request, pk):
    # Permissions are enforced here and again in the generation service (PermissionDenied -> 403).
    act = request.POST.get("action")
    if act == "issue" and not generation.may_issue(request.user):
        raise PermissionDenied
    if act == "cancel" and not request.user.has_perm("certificates.generate_certificate"):
        raise PermissionDenied
    issuance = get_object_or_404(Issuance.objects.select_related("request"), pk=pk)
    try:
        if act == "issue":
            generation.issue(pk, actor=request.user, request=request)
            messages.success(request, "Certificate marked as issued.")
        elif act == "cancel":
            generation.cancel_draft(pk, actor=request.user, reason=request.POST.get("reason", ""), request=request)
            messages.success(request, "Draft cancelled. A new draft can be generated.")
        else:
            messages.error(request, "Unknown action.")
    except WorkflowError as exc:
        messages.error(request, str(exc))
    return _back(issuance)
