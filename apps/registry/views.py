import io

import segno
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from apps.core import audit, ratelimit
from apps.core.audit import client_ip

from .forms import CampaignForm, DomainRenameForm, RegistrationForm
from .models import Campaign, Domain, DuplicateCandidate, Submission
from .services import RegistrationError, submit_registration

# --- Public registration ---------------------------------------------------


def _campaign_for_token(token):
    return Campaign.objects.select_related("domain").filter(public_token=token).first()


def _over_post_limit(request):
    return ratelimit.hit("registration-posts", client_ip(request), 3600) > settings.REGISTRATION_POSTS_PER_HOUR


@never_cache
@require_http_methods(["GET", "POST"])
def register(request, token):
    campaign = _campaign_for_token(token)
    if campaign is None:
        return render(request, "registry/public/unavailable.html", {"reason": "invalid"}, status=404)
    state = campaign.availability()
    if state != "open":
        return render(request, "registry/public/unavailable.html",
                      {"reason": state, "campaign": campaign}, status=403 if state == "scheduled" else 410)

    if request.method == "POST":
        if _over_post_limit(request):
            return render(request, "registry/public/unavailable.html", {"reason": "rate_limited"}, status=429)
        form = RegistrationForm(request.POST, campaign=campaign)
        if form.is_valid():
            data = {**form.cleaned_data, "review_notes": form.review_notes}
            try:
                submit_registration(campaign, data, request.POST, request=request)
            except RegistrationError as exc:
                form.add_error(None, str(exc))
            else:
                return redirect("registry:register_done", token=token)
    else:
        form = RegistrationForm(campaign=campaign)
    return render(request, "registry/public/form.html", {"form": form, "campaign": campaign})


@never_cache
def register_done(request, token):
    # Shows a fixed message only; never any submitted personal details.
    campaign = _campaign_for_token(token)
    if campaign is None:
        return render(request, "registry/public/unavailable.html", {"reason": "invalid"}, status=404)
    return render(request, "registry/public/done.html", {"campaign": campaign})


# --- HR: campaigns ----------------------------------------------------------


def public_registration_url(request, campaign):
    path = reverse("registry:register", kwargs={"token": campaign.public_token})
    if settings.PUBLIC_BASE_URL:
        return settings.PUBLIC_BASE_URL + path
    return request.build_absolute_uri(path)


@login_required
@permission_required("registry.view_campaign", raise_exception=True)
def campaign_list(request):
    campaigns = Campaign.objects.select_related("domain").annotate(n_submissions=Count("submissions"))
    return render(request, "registry/hr/campaign_list.html", {"campaigns": campaigns, "now": timezone.now()})


@login_required
@permission_required("registry.manage_campaign", raise_exception=True)
@require_http_methods(["GET", "POST"])
def campaign_create(request):
    form = CampaignForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        campaign = form.save(commit=False)
        campaign.created_by = request.user
        campaign.save()
        audit.record("campaign.created", actor=request.user, entity=campaign, request=request,
                     metadata={"opens_at": campaign.opens_at.isoformat(), "closes_at": campaign.closes_at.isoformat()})
        messages.success(request, "Campaign created. Its QR code is ready below.")
        return redirect("registry:campaign_detail", pk=campaign.pk)
    return render(request, "registry/hr/campaign_form.html", {"form": form})


@login_required
@permission_required("registry.manage_campaign", raise_exception=True)
@require_http_methods(["GET", "POST"])
def campaign_edit(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    form = CampaignForm(request.POST or None, instance=campaign)
    if request.method == "POST" and form.is_valid():
        changed = form.changed_data
        form.save()
        audit.record("campaign.updated", actor=request.user, entity=campaign, request=request,
                     metadata={"fields": changed})
        messages.success(request, "Campaign updated.")
        return redirect("registry:campaign_detail", pk=campaign.pk)
    return render(request, "registry/hr/campaign_form.html", {"form": form, "campaign": campaign})


@login_required
@permission_required("registry.view_campaign", raise_exception=True)
def campaign_detail(request, pk):
    campaign = get_object_or_404(Campaign.objects.select_related("domain", "created_by"), pk=pk)
    url = public_registration_url(request, campaign)
    context = {
        "campaign": campaign,
        "registration_url": url,
        "qr_svg": segno.make(url, error="m").svg_inline(scale=5, title="Registration QR code"),
        "state": campaign.availability(),
    }
    if request.user.has_perm("registry.view_submission"):
        context["submissions"] = (
            campaign.submissions.select_related("student")
            .prefetch_related("internships__domain")
            .annotate(open_flags=Count("duplicate_flags",
                                       filter=Q(duplicate_flags__resolution=DuplicateCandidate.Resolution.PENDING)))
        )
    return render(request, "registry/hr/campaign_detail.html", context)


@login_required
@permission_required("registry.manage_campaign", raise_exception=True)
@require_POST
def campaign_set_status(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    target = request.POST.get("status")
    if target not in Campaign.Status.values:
        return HttpResponse("Invalid status.", status=400)
    if campaign.status != target:
        campaign.status = target
        campaign.save(update_fields=["status"])
        action = "campaign.activated" if target == Campaign.Status.ACTIVE else "campaign.deactivated"
        audit.record(action, actor=request.user, entity=campaign, request=request)
        messages.success(request, "Registration link activated." if target == Campaign.Status.ACTIVE
                         else "Registration link deactivated. It no longer accepts submissions.")
    return redirect("registry:campaign_detail", pk=campaign.pk)


@login_required
@permission_required("registry.view_campaign", raise_exception=True)
def campaign_qr(request, pk, fmt):
    if fmt not in ("svg", "png"):
        raise Http404
    campaign = get_object_or_404(Campaign, pk=pk)
    qr = segno.make(public_registration_url(request, campaign), error="m")
    buffer = io.BytesIO()
    if fmt == "svg":
        qr.save(buffer, kind="svg", scale=10, border=4)
        content_type = "image/svg+xml"
    else:
        qr.save(buffer, kind="png", scale=12, border=4)
        content_type = "image/png"
    audit.record("campaign.qr_downloaded", actor=request.user, entity=campaign, request=request,
                 metadata={"format": fmt})
    response = HttpResponse(buffer.getvalue(), content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="registration-qr-{campaign.pk}.{fmt}"'
    return response


# --- HR: domains ------------------------------------------------------------


@login_required
@permission_required("registry.view_domain", raise_exception=True)
def domain_list(request):
    domains = (Domain.objects.select_related("requested_by_submission")
               .annotate(n_internships=Count("internships")).order_by("status", "name"))
    return render(request, "registry/hr/domain_list.html", {"domains": domains})


@login_required
@permission_required("registry.manage_domain", raise_exception=True)
@require_POST
def domain_set_status(request, pk):
    domain = get_object_or_404(Domain, pk=pk)
    target = request.POST.get("status")
    if target not in (Domain.Status.ACTIVE, Domain.Status.INACTIVE):
        return HttpResponse("Invalid status.", status=400)
    previous = domain.status
    if previous != target:
        domain.status = target
        if target == Domain.Status.ACTIVE and domain.approved_by_id is None:
            domain.approved_by = request.user
        domain.save(update_fields=["status", "approved_by", "updated_at"])
        action = {
            (Domain.Status.PENDING, Domain.Status.ACTIVE): "domain.approved",
            (Domain.Status.PENDING, Domain.Status.INACTIVE): "domain.rejected",
        }.get((previous, target), "domain.activated" if target == Domain.Status.ACTIVE else "domain.deactivated")
        audit.record(action, actor=request.user, entity=domain, request=request)
        messages.success(request, f"“{domain.name}” is now {domain.get_status_display().lower()}.")
    return redirect("registry:domain_list")


@login_required
@permission_required("registry.manage_domain", raise_exception=True)
@require_http_methods(["GET", "POST"])
def domain_rename(request, pk):
    domain = get_object_or_404(Domain, pk=pk)
    form = DomainRenameForm(request.POST or None, domain=domain, initial={"name": domain.name})
    if request.method == "POST" and form.is_valid():
        old = domain.name
        domain.name = form.cleaned_data["name"]
        domain.save()
        audit.record("domain.renamed", actor=request.user, entity=domain, request=request,
                     metadata={"from": old, "to": domain.name})
        messages.success(request, "Domain renamed. Existing records keep their link to this domain.")
        return redirect("registry:domain_list")
    return render(request, "registry/hr/domain_rename.html", {"form": form, "domain": domain})
