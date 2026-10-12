"""HR campaign management, QR codes, domain approval and access control."""
import io

import pytest
import segno
from django.urls import reverse

from apps.core.models import AuditLog
from apps.registry.models import Campaign, Domain

from . import factories as f

pytestmark = pytest.mark.django_db


@pytest.fixture
def hr(client):
    user = f.hr_admin()
    client.force_login(user)
    return user


def create(client, **overrides):
    data = {"name": "Test Batch October", "domain": "", "opens_at": "2026-10-01T09:00",
            "closes_at": "2026-12-31T18:00"}
    data.update(overrides)
    return client.post(reverse("registry:campaign_create"), data)


# --- Campaign CRUD -----------------------------------------------------------

def test_hr_creates_campaign_with_random_token(client, hr):
    resp = create(client)
    camp = Campaign.objects.get()
    assert resp["Location"] == reverse("registry:campaign_detail", kwargs={"pk": camp.pk})
    assert camp.created_by == hr and camp.status == Campaign.Status.ACTIVE
    assert len(camp.public_token) >= 32 and camp.public_token != str(camp.pk)
    assert AuditLog.objects.filter(action="campaign.created", actor=hr, entity_id=str(camp.pk)).exists()


def test_campaign_times_interpreted_in_ist(client, hr):
    create(client)
    camp = Campaign.objects.get()
    assert camp.opens_at.isoformat() == "2026-10-01T03:30:00+00:00"  # 09:00 IST


def test_campaign_close_before_open_rejected(client, hr):
    resp = create(client, closes_at="2026-09-01T09:00")
    assert resp.status_code == 200 and "closes_at" in resp.context["form"].errors
    assert not Campaign.objects.exists()


def test_deactivate_and_reactivate_are_audited(client, hr):
    camp = f.campaign(hr)
    status_url = reverse("registry:campaign_set_status", kwargs={"pk": camp.pk})
    client.post(status_url, {"status": "inactive"})
    camp.refresh_from_db()
    assert camp.status == Campaign.Status.INACTIVE and not camp.is_accepting()
    client.post(status_url, {"status": "active"})
    camp.refresh_from_db()
    assert camp.is_accepting()
    actions = list(AuditLog.objects.filter(entity_id=str(camp.pk)).values_list("action", flat=True))
    assert "campaign.deactivated" in actions and "campaign.activated" in actions


def test_status_change_requires_post_and_valid_value(client, hr):
    camp = f.campaign(hr)
    status_url = reverse("registry:campaign_set_status", kwargs={"pk": camp.pk})
    assert client.get(status_url).status_code == 405
    assert client.post(status_url, {"status": "deleted"}).status_code == 400


def test_edit_extends_closing_date_and_keeps_token(client, hr):
    camp = f.campaign(hr)
    token = camp.public_token
    client.post(reverse("registry:campaign_edit", kwargs={"pk": camp.pk}),
                {"name": camp.name, "domain": "", "opens_at": "2026-10-01T09:00", "closes_at": "2027-01-31T18:00"})
    camp.refresh_from_db()
    assert camp.public_token == token and camp.closes_at.year == 2027
    assert AuditLog.objects.get(action="campaign.updated").metadata["fields"] == ["opens_at", "closes_at"]


# --- QR codes -------------------------------------------------------------

def test_detail_shows_registration_url_and_inline_qr(client, hr, settings):
    settings.PUBLIC_BASE_URL = "https://certificates.example.org"
    camp = f.campaign(hr)
    html = client.get(reverse("registry:campaign_detail", kwargs={"pk": camp.pk})).content.decode()
    expected = f"https://certificates.example.org/register/{camp.public_token}/"
    assert expected in html and "<svg" in html


def test_qr_png_decodes_to_registration_url_only(client, hr, settings):
    settings.PUBLIC_BASE_URL = "https://certificates.example.org"
    camp = f.campaign(hr)
    resp = client.get(reverse("registry:campaign_qr", kwargs={"pk": camp.pk, "fmt": "png"}))
    assert resp.status_code == 200 and resp["Content-Type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert "attachment" in resp["Content-Disposition"]
    # The QR payload is exactly the public URL: no IDs, personal data or secrets.
    expected = f"https://certificates.example.org/register/{camp.public_token}/"
    reference = io.BytesIO()
    segno.make(expected, error="m").save(reference, kind="png", scale=12, border=4)
    assert resp.content == reference.getvalue()
    assert AuditLog.objects.filter(action="campaign.qr_downloaded", entity_id=str(camp.pk)).exists()


def test_qr_svg_and_unknown_format(client, hr):
    camp = f.campaign(hr)
    svg = client.get(reverse("registry:campaign_qr", kwargs={"pk": camp.pk, "fmt": "svg"}))
    assert svg["Content-Type"] == "image/svg+xml" and b"<svg" in svg.content
    assert client.get(reverse("registry:campaign_qr", kwargs={"pk": camp.pk, "fmt": "exe"})).status_code == 404


def test_qr_link_opens_the_form(client, hr, settings):
    settings.PUBLIC_BASE_URL = ""
    camp = f.campaign(hr)
    detail = client.get(reverse("registry:campaign_detail", kwargs={"pk": camp.pk}))
    link = detail.context["registration_url"]
    assert link == f"http://testserver/register/{camp.public_token}/"
    client.logout()
    assert client.get(link.replace("http://testserver", "")).status_code == 200


def test_detail_lists_submissions_with_flags(client, hr):
    camp = f.campaign(hr)
    other = f.campaign(hr)
    stu = f.student(full_name="Flagged Learner")
    f.submission(other, f.student(reg_no=stu.reg_no))
    sub = f.submission(camp, stu)
    from apps.registry.models import DuplicateCandidate
    DuplicateCandidate.objects.create(submission=sub, matches=other.submissions.get(), reason="reg_no")
    html = client.get(reverse("registry:campaign_detail", kwargs={"pk": camp.pk})).content.decode()
    assert "Flagged Learner" in html and "Possible duplicate ×1" in html


# --- Domains -----------------------------------------------------------------

def test_hr_approves_pending_domain(client, hr):
    d = Domain.objects.create(name="Cloud Computing", status=Domain.Status.PENDING)
    client.post(reverse("registry:domain_set_status", kwargs={"pk": d.pk}), {"status": "active"})
    d.refresh_from_db()
    assert d.status == Domain.Status.ACTIVE and d.approved_by == hr
    assert AuditLog.objects.filter(action="domain.approved", actor=hr).exists()


def test_domain_cannot_be_set_back_to_pending(client, hr):
    d = Domain.objects.get(name="HRM")
    resp = client.post(reverse("registry:domain_set_status", kwargs={"pk": d.pk}), {"status": "pending"})
    assert resp.status_code == 400


def test_deactivated_domain_keeps_history(client, hr):
    d = Domain.objects.get(name="Medical Coding")
    f.internship(dom=d)
    client.post(reverse("registry:domain_set_status", kwargs={"pk": d.pk}), {"status": "inactive"})
    d.refresh_from_db()
    assert d.status == Domain.Status.INACTIVE and d.internships.count() == 1


def test_rename_domain_rejects_duplicates(client, hr):
    d = Domain.objects.get(name="FSD Java")
    resp = client.post(reverse("registry:domain_rename", kwargs={"pk": d.pk}), {"name": "FSD   Python"})
    assert "already exists" in " ".join(resp.context["form"].errors["name"])
    client.post(reverse("registry:domain_rename", kwargs={"pk": d.pk}), {"name": "Full Stack Java"})
    d.refresh_from_db()
    assert d.name == "Full Stack Java"
    assert AuditLog.objects.get(action="domain.renamed").metadata == {"from": "FSD Java", "to": "Full Stack Java"}


# --- Access control ------------------------------------------------------------

def hr_urls(camp, dom):
    return [
        ("get", reverse("registry:campaign_list")),
        ("get", reverse("registry:campaign_create")),
        ("get", reverse("registry:campaign_detail", kwargs={"pk": camp.pk})),
        ("get", reverse("registry:campaign_edit", kwargs={"pk": camp.pk})),
        ("post", reverse("registry:campaign_set_status", kwargs={"pk": camp.pk})),
        ("get", reverse("registry:campaign_qr", kwargs={"pk": camp.pk, "fmt": "png"})),
        ("get", reverse("registry:domain_list")),
        ("post", reverse("registry:domain_set_status", kwargs={"pk": dom.pk})),
        ("get", reverse("registry:domain_rename", kwargs={"pk": dom.pk})),
    ]


def test_anonymous_cannot_reach_any_hr_page(client):
    camp = f.campaign(f.hr_admin())
    for method, path in hr_urls(camp, Domain.objects.first()):
        resp = getattr(client, method)(path, {"status": "inactive"} if method == "post" else None)
        assert resp.status_code == 302 and "/accounts/login/" in resp["Location"], path
    camp.refresh_from_db()
    assert camp.status == Campaign.Status.ACTIVE


def test_staff_without_role_is_forbidden_everywhere(client):
    camp = f.campaign(f.hr_admin())
    client.force_login(f.user(role=None))
    for method, path in hr_urls(camp, Domain.objects.first()):
        resp = getattr(client, method)(path, {"status": "inactive"} if method == "post" else None)
        assert resp.status_code == 403, path


def test_view_only_user_cannot_manage(client):
    from django.contrib.auth.models import Permission

    viewer = f.user(role=None)
    viewer.user_permissions.add(Permission.objects.get(codename="view_campaign"),
                                Permission.objects.get(codename="view_domain"))
    camp = f.campaign(f.hr_admin())
    client.force_login(viewer)
    assert client.get(reverse("registry:campaign_detail", kwargs={"pk": camp.pk})).status_code == 200
    detail = client.get(reverse("registry:campaign_detail", kwargs={"pk": camp.pk}))
    assert detail.context.get("submissions") is None  # no view_submission permission
    assert client.post(reverse("registry:campaign_set_status", kwargs={"pk": camp.pk}),
                       {"status": "inactive"}).status_code == 403
    assert client.get(reverse("registry:campaign_create")).status_code == 403


def test_public_cannot_list_or_enumerate_submissions(client):
    camp = f.campaign(f.hr_admin())
    f.submission(camp, f.student(full_name="Private Person"))
    # The public routes are only the form and the fixed confirmation page.
    done = client.get(reverse("registry:register_done", kwargs={"token": camp.public_token}))
    assert "Private Person" not in done.content.decode()
    assert client.get(f"/register/{camp.public_token}/submissions/").status_code == 404
    assert client.get(f"/register/{camp.pk}/").status_code == 404
