"""Phase 5: separation of duties — nobody approves or activates a template version they uploaded."""
import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.core.models import AuditLog
from apps.doctemplates import services
from apps.doctemplates.models import TemplateVersion

from . import factories as f
from .test_templates import good_docx, upload

pytestmark = pytest.mark.django_db
V = TemplateVersion.Status


def own_pending(sa, **kw):
    """A Super Admin uploads (allowed: they hold upload_template too) and submits a version."""
    v = upload(sa, **kw)
    return services.submit_for_review(v.pk, actor=sa)


def test_uploader_cannot_activate_own_version_via_service():
    sa = f.super_admin()
    v = own_pending(sa)
    with pytest.raises(PermissionDenied, match="another Super Admin"):
        services.approve_and_activate(v.pk, actor=sa, confirmed_compared=True)
    v.refresh_from_db()
    assert (v.status, v.approved_by) == (V.PENDING_APPROVAL, None)
    assert not AuditLog.objects.filter(action="template.activated").exists()


def test_uploader_crafted_http_activation_is_forbidden(client):
    sa = f.super_admin()
    v = own_pending(sa)
    client.force_login(sa)
    resp = client.post(reverse("doctemplates:action", kwargs={"pk": v.pk}),
                       {"action": "activate", "confirm_compared": "on", "expected_status": "pending_approval"})
    assert resp.status_code == 403
    assert TemplateVersion.objects.get(pk=v.pk).status == V.PENDING_APPROVAL


def test_different_super_admin_can_activate():
    uploader, approver = f.super_admin(), f.super_admin()
    v = own_pending(uploader)
    services.approve_and_activate(v.pk, actor=approver, confirmed_compared=True)
    v.refresh_from_db()
    assert (v.status, v.uploaded_by, v.approved_by) == (V.ACTIVE, uploader, approver)


def test_hr_upload_approved_by_super_admin_still_works():
    hr, sa = f.hr_admin(), f.super_admin()
    v = upload(hr)
    services.submit_for_review(v.pk, actor=hr)
    services.approve_and_activate(v.pk, actor=sa, confirmed_compared=True)
    assert TemplateVersion.objects.get(pk=v.pk).status == V.ACTIVE


def test_uploader_cannot_reactivate_own_retired_version():
    uploader, other = f.super_admin(), f.super_admin()
    v = own_pending(uploader)
    services.approve_and_activate(v.pk, actor=other, confirmed_compared=True)
    services.deactivate(v.pk, actor=other, reason="Checking wording")
    with pytest.raises(PermissionDenied):
        services.approve_and_activate(v.pk, actor=uploader, confirmed_compared=True)
    services.approve_and_activate(v.pk, actor=other, confirmed_compared=True)
    assert TemplateVersion.objects.get(pk=v.pk).status == V.ACTIVE


def test_check_runs_before_any_state_change_even_with_bad_input():
    """Separation of duties is reported even when the request is also otherwise invalid (no info leak order)."""
    sa = f.super_admin()
    v = upload(sa)  # draft, not pending
    with pytest.raises(PermissionDenied):
        services.approve_and_activate(v.pk, actor=sa, confirmed_compared=True)
    assert TemplateVersion.objects.get(pk=v.pk).status == V.DRAFT


def test_uploader_may_still_reject_or_deactivate_own_version():
    """Withdrawing one's own upload or taking it out of use is not self-approval."""
    uploader, other = f.super_admin(), f.super_admin()
    rejected = own_pending(uploader)
    services.reject(rejected.pk, actor=uploader, reason="Uploaded the wrong file")
    active = own_pending(uploader, data=good_docx(text="Second {{ full_name }}"))
    services.approve_and_activate(active.pk, actor=other, confirmed_compared=True)
    services.deactivate(active.pk, actor=uploader, reason="Found a typo")
    assert TemplateVersion.objects.get(pk=rejected.pk).status == V.REJECTED
    assert TemplateVersion.objects.get(pk=active.pk).status == V.RETIRED


def test_hr_admin_still_cannot_approve_anything():
    hr, sa = f.hr_admin(), f.super_admin()
    v = own_pending(sa)  # uploaded by someone else, so only the role check applies
    with pytest.raises(PermissionDenied):
        services.approve_and_activate(v.pk, actor=hr, confirmed_compared=True)
    assert services.may_approve(hr, v) is False


def test_database_rejects_self_approval_even_bypassing_the_service():
    sa = f.super_admin()
    v = own_pending(sa)
    with pytest.raises(IntegrityError), transaction.atomic():
        TemplateVersion.objects.filter(pk=v.pk).update(status=V.ACTIVE, approved_by=sa, approved_at=timezone.now())


def test_ui_hides_activation_from_uploader_and_shows_it_to_other_super_admin(client):
    uploader, other = f.super_admin(), f.super_admin()
    v = own_pending(uploader)
    url = reverse("doctemplates:version", kwargs={"pk": v.pk})
    client.force_login(uploader)
    html = client.get(url).content.decode()
    assert "another Super Admin must approve" in html and "Approve and activate" not in html
    assert "Reject version" in html  # rejecting own upload remains possible
    client.force_login(other)
    assert "Approve and activate" in client.get(url).content.decode()


def test_existing_protections_unchanged(settings):
    uploader, other, hr = f.super_admin(), f.super_admin(), f.hr_admin()
    # TEST templates never production-usable; switch still off by default.
    assert settings.CERT_PRODUCTION_ISSUANCE_ENABLED is False
    v = own_pending(uploader, is_production=False)
    services.approve_and_activate(v.pk, actor=other, confirmed_compared=True)
    v.refresh_from_db()
    assert v.status == V.ACTIVE and not v.can_issue_production
    # Phase 4 final LOR authority unchanged.
    assert other.has_perm("certificates.approve_lor") and not hr.has_perm("certificates.approve_lor")
    # Private storage still has no URL.
    with pytest.raises(ValueError):
        v.source_file.url
