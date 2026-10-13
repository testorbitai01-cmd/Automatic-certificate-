"""Phase 6: only Super Admins may officially issue certificates; drafts keep their own permission.

Some tests switch CERT_PRODUCTION_ISSUANCE_ENABLED on *inside the test only* (pytest `settings` fixture) to
prove the success path and that authority is still required when the gate is open. Real configuration keeps
production issuance disabled (see test_production_issuance_disabled_by_default)."""
import threading

import pytest
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.urls import reverse

from apps.accounts.roles import HR_ADMIN_PERMISSIONS, SUPER_ADMIN_PERMISSIONS
from apps.certificates import generation
from apps.certificates.models import Issuance
from apps.core.models import AuditLog

from . import factories as f
from .phase6 import active_template, approved_internship, fake_converter

pytestmark = pytest.mark.django_db


@pytest.fixture
def draft(monkeypatch):
    """A generated draft from an authentic (production) template, created with production enabled in-test."""
    def make(settings, cert_type="offer_letter"):
        fake_converter(monkeypatch)
        settings.CERT_PRODUCTION_ISSUANCE_ENABLED = True
        active_template(cert_type, is_production=True)
        i, hr, sa = approved_internship(cert_type)
        d = generation.generate_draft(i.pk, cert_type, actor=hr)
        return d, hr, sa
    return make


def status(d):
    return Issuance.objects.get(pk=d.pk).status


def issue_url(d):
    return reverse("certificates:action", kwargs={"pk": d.pk})


# --- Role policy ---------------------------------------------------------------------

def test_issue_permission_only_in_super_admin_role():
    assert "certificates.issue_certificate" in SUPER_ADMIN_PERMISSIONS
    assert "certificates.issue_certificate" not in HR_ADMIN_PERMISSIONS
    assert f.super_admin().has_perm("certificates.issue_certificate")
    assert not f.hr_admin().has_perm("certificates.issue_certificate")
    # HR keeps draft generation and downloads.
    hr = f.hr_admin()
    assert hr.has_perm("certificates.generate_certificate") and hr.has_perm("certificates.download_certificate")


def test_production_issuance_disabled_by_default(settings):
    assert settings.CERT_PRODUCTION_ISSUANCE_ENABLED is False


# --- Service boundary --------------------------------------------------------------------

@pytest.mark.parametrize("who", ["hr", "roleless", "generate_only", "inactive_super_admin", "anonymous"])
def test_non_super_admins_cannot_issue_even_with_gate_open(settings, draft, who):
    from django.contrib.auth.models import AnonymousUser

    d, hr, _ = draft(settings)
    actor = {"hr": hr, "roleless": f.user(role=None), "anonymous": AnonymousUser()}.get(who)
    if who == "generate_only":
        actor = f.user(role=None)
        actor.user_permissions.add(Permission.objects.get(codename="generate_certificate"))
    if who == "inactive_super_admin":
        actor = f.super_admin()
        actor.is_active = False
        actor.save()
    with pytest.raises(PermissionDenied):
        generation.issue(d.pk, actor=actor)
    assert status(d) == "not_issued"
    assert not AuditLog.objects.filter(action="certificate.issued").exists()


def test_authority_is_checked_before_anything_else(settings, monkeypatch):
    """A non-Super-Admin gets PermissionDenied even for a nonexistent id or with production disabled."""
    hr = f.hr_admin()
    settings.CERT_PRODUCTION_ISSUANCE_ENABLED = False
    with pytest.raises(PermissionDenied):
        generation.issue(999999, actor=hr)


def test_super_admin_blocked_while_production_disabled(settings, draft):
    d, _, sa = draft(settings)
    settings.CERT_PRODUCTION_ISSUANCE_ENABLED = False
    with pytest.raises(generation.GenerationError, match="Production issuance is disabled"):
        generation.issue(d.pk, actor=sa)
    assert status(d) == "not_issued"


def test_super_admin_issues_only_when_every_condition_holds(settings, draft):
    d, _, sa = draft(settings)
    issued = generation.issue(d.pk, actor=sa)
    assert (issued.status, issued.issued_by) == ("issued", sa)
    entry = AuditLog.objects.get(action="certificate.issued")
    assert entry.actor == sa and entry.metadata["sha256"] == d.file.sha256
    # Retry / double submit: refused, still exactly one issuance event.
    with pytest.raises(generation.GenerationError):
        generation.issue(d.pk, actor=sa)
    assert AuditLog.objects.filter(action="certificate.issued").count() == 1


def test_cancelled_draft_cannot_be_issued(settings, draft):
    d, hr, sa = draft(settings)
    generation.cancel_draft(d.pk, actor=hr, reason="Wrong date")
    with pytest.raises(generation.GenerationError, match="Only a generated draft"):
        generation.issue(d.pk, actor=sa)


def test_hr_can_still_generate_and_cancel_drafts(settings, draft):
    d, hr, _ = draft(settings)
    generation.cancel_draft(d.pk, actor=hr, reason="Regenerate")
    assert status(d) == "cancelled"


# --- HTTP boundary -------------------------------------------------------------------------

def test_hr_crafted_issue_request_is_forbidden(client, settings, draft):
    d, hr, _ = draft(settings)
    client.force_login(hr)
    assert client.post(issue_url(d), {"action": "issue"}).status_code == 403
    assert status(d) == "not_issued"


def test_issue_route_requires_login_and_post(client, settings, draft):
    d, _, sa = draft(settings)
    assert client.post(issue_url(d), {"action": "issue"}).status_code == 302  # to login
    client.force_login(sa)
    assert client.get(issue_url(d), {"action": "issue"}).status_code == 405
    assert status(d) == "not_issued"


def test_super_admin_http_issue(client, settings, draft):
    d, _, sa = draft(settings)
    client.force_login(sa)
    assert client.post(issue_url(d), {"action": "issue"}).status_code == 302
    assert status(d) == "issued"


def test_no_other_route_issues(client, settings, draft):
    """Generation, download and template routes never change issuance state."""
    d, hr, sa = draft(settings)
    client.force_login(sa)
    client.get(reverse("certificates:download", kwargs={"pk": d.pk}))
    client.post(reverse("internships:certificate_generate",
                        kwargs={"pk": d.request.internship_id, "cert_type": "offer_letter"}), {"action": "issue"})
    client.post(reverse("internships:certificate_decision",
                        kwargs={"pk": d.request.internship_id, "cert_type": "offer_letter"}), {"decision": "issue"})
    assert status(d) == "not_issued"


def test_issue_button_only_shown_to_super_admin(client, settings, draft):
    d, hr, sa = draft(settings)
    page = reverse("internships:detail", kwargs={"pk": d.request.internship_id})
    client.force_login(hr)
    html = client.get(page).content.decode()
    assert 'value="issue"' not in html and "Issued by a Super Admin." in html
    client.force_login(sa)
    assert 'value="issue"' in client.get(page).content.decode()


# --- Concurrency (PostgreSQL) -----------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="true concurrency needs PostgreSQL row locks")
def test_concurrent_issue_attempts_issue_once(pg_transactional_db, settings, monkeypatch):
    fake_converter(monkeypatch)
    settings.CERT_PRODUCTION_ISSUANCE_ENABLED = True
    active_template("offer_letter", is_production=True)
    i, hr, sa = approved_internship("offer_letter")
    d = generation.generate_draft(i.pk, "offer_letter", actor=hr)
    actors = [sa, f.super_admin(), hr, hr]
    barrier = threading.Barrier(len(actors))
    outcomes = []

    def worker(actor):
        from django.db import connection as conn
        barrier.wait()
        try:
            generation.issue(d.pk, actor=actor)
            outcomes.append("issued")
        except PermissionDenied:
            outcomes.append("forbidden")
        except generation.GenerationError:
            outcomes.append("refused")
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(a,)) for a in actors]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["forbidden", "forbidden", "issued", "refused"]
    assert Issuance.objects.filter(status="issued").count() == 1
    assert AuditLog.objects.filter(action="certificate.issued").count() == 1
