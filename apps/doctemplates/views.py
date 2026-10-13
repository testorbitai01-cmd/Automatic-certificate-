"""HR/Super Admin template management pages (Phase 5). Preview is text-only with synthetic data."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.core import audit
from apps.core.choices import CertificateType
from apps.core.workflow import WorkflowError

from . import docx, services
from .models import CertificateTemplate, TemplateVersion
from .placeholders import REGISTRY, allowed_for, sample_values


@login_required
@permission_required("doctemplates.view_templateversion", raise_exception=True)
@require_GET
def template_list(request):
    templates = CertificateTemplate.objects.prefetch_related("versions__uploaded_by").order_by("id")
    return render(request, "doctemplates/list.html", {"templates": templates})


@login_required
@permission_required("doctemplates.upload_template", raise_exception=True)
@require_http_methods(["GET", "POST"])
def upload(request):
    errors = None
    if request.method == "POST" and _declared_too_large(request):
        # Refuse before Django parses (and spools to disk) the multipart body.
        return render(request, "doctemplates/upload.html", {
            "types": CertificateType.choices, "posted": {}, "registry": REGISTRY.values(),
            "errors": f"The upload is larger than {settings.TEMPLATE_UPLOAD_MAX_BYTES // (1024 * 1024)} MB.",
        }, status=413)
    if request.method == "POST":
        try:
            version = services.upload_version(
                request.POST.get("cert_type"), request.FILES.get("file") or _NoFile(), actor=request.user,
                is_production=request.POST.get("is_production") == "on", notes=request.POST.get("notes", ""),
                request=request)
        except WorkflowError as exc:
            errors = str(exc)
        else:
            messages.success(request, f"Uploaded as version {version.version} (draft). Check the preview, then "
                                      "submit it for Super Admin review.")
            return redirect("doctemplates:version", pk=version.pk)
    return render(request, "doctemplates/upload.html", {
        "types": CertificateType.choices, "errors": errors, "posted": request.POST,
        "registry": REGISTRY.values(),
    }, status=400 if errors else 200)


MULTIPART_OVERHEAD = 64 * 1024


def _declared_too_large(request):
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return True
    return length > settings.TEMPLATE_UPLOAD_MAX_BYTES + MULTIPART_OVERHEAD


class _NoFile:
    name, size = "", 0

    def read(self, *_):
        return b""


@login_required
@permission_required("doctemplates.view_templateversion", raise_exception=True)
@require_GET
def version_detail(request, pk):
    version = get_object_or_404(TemplateVersion.objects.select_related(
        "template", "uploaded_by", "submitted_by", "approved_by", "closed_by"), pk=pk)
    cert_type = version.template.cert_type
    preview, preview_error = None, None
    try:
        preview = docx.preview(services.inspect_stored(version), sample_values(cert_type))
    except (WorkflowError, docx.TemplateFileError) as exc:
        preview_error = str(exc)
    allowed = allowed_for(cert_type)
    used = set(version.inspection.get("placeholders", []))
    return render(request, "doctemplates/version.html", {
        "v": version, "preview": preview, "preview_error": preview_error,
        "used": [allowed[n] for n in sorted(used) if n in allowed],
        "unused": [p for n, p in sorted(allowed.items()) if n not in used],
        "S": TemplateVersion.Status,
        "active": version.template.active_version(),
        "may_approve": services.may_approve(request.user, version),
    })


@login_required
@permission_required("doctemplates.view_templateversion", raise_exception=True)
@require_GET
def download(request, pk):
    version = get_object_or_404(TemplateVersion.objects.select_related("template"), pk=pk)
    try:
        handle = version.source_file.open("rb")
    except FileNotFoundError:
        raise Http404("The stored template file is missing.") from None
    audit.record("template.downloaded", actor=request.user, entity=version, request=request,
                 metadata={"template_version_id": version.pk, "cert_type": version.template.cert_type,
                           "version": version.version})
    response = FileResponse(handle, as_attachment=True,
                            filename=f"{version.template.cert_type}-template-v{version.version}.docx",
                            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    response["Cache-Control"] = "no-store, private"
    return response


@login_required
@require_GET
def proof(request, pk):
    """PDF proof filled with labelled SAMPLE data — never a real student's certificate; nothing is stored."""
    from apps.certificates import generation

    if not request.user.has_perm("doctemplates.map_template"):
        raise PermissionDenied
    get_object_or_404(TemplateVersion, pk=pk)
    try:
        data, _ = generation.proof_pdf(pk, actor=request.user, pronouns=request.GET.get("pronouns", "she"),
                                       request=request)
    except WorkflowError as exc:
        messages.error(request, str(exc))
        return redirect("doctemplates:version", pk=pk)
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="PROOF-SAMPLE-DATA-template-v{pk}.pdf"'
    response["Cache-Control"] = "no-store, private"
    return response


@login_required
@require_POST
def action(request, pk):
    # Permissions are enforced per action in the service layer (PermissionDenied -> 403).
    get_object_or_404(TemplateVersion, pk=pk)
    act = request.POST.get("action")
    common = {"actor": request.user, "expected_status": request.POST.get("expected_status") or None,
              "request": request}
    try:
        if act == "submit":
            services.submit_for_review(pk, **common)
            messages.success(request, "Submitted for Super Admin review.")
        elif act == "activate":
            services.approve_and_activate(pk, confirmed_compared=request.POST.get("confirm_compared") == "on",
                                          note=request.POST.get("note", ""), **common)
            messages.success(request, "Template approved and activated. Any previously active version was retired.")
        elif act == "reject":
            services.reject(pk, reason=request.POST.get("reason", ""), **common)
            messages.success(request, "Version rejected.")
        elif act == "deactivate":
            services.deactivate(pk, reason=request.POST.get("reason", ""), **common)
            messages.success(request, "Version deactivated. This certificate type has no active template now.")
        else:
            messages.error(request, "Unknown action.")
    except WorkflowError as exc:
        messages.error(request, str(exc))
    return redirect("doctemplates:version", pk=pk)
