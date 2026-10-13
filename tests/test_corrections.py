"""Phase 4: HR correction requests and the student's private correction link."""
import datetime as dt
import hashlib

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.certificates import approvals
from apps.certificates.models import CertificateRequest, Issuance
from apps.core.models import AuditLog
from apps.core.workflow import WorkflowError
from apps.registry import review
from apps.registry.models import CorrectionRequest, DuplicateCandidate, StudentEmail, Submission

from . import factories as f
from .phase4 import active_domain, registration, verified_registration

pytestmark = pytest.mark.django_db
S = Submission.Status


@pytest.fixture
def hr():
    return f.hr_admin()


def link(raw):
    return reverse("registry:correction", kwargs={"token": raw})


def ask(hr, sub, groups=("name",), reason="Please fix the capitalisation of your name"):
    return review.request_correction(sub.pk, actor=hr, groups=list(groups), reason=reason)


# --- Requesting ----------------------------------------------------------------

def test_request_creates_hashed_single_link_and_moves_status(hr):
    sub, _ = registration()
    correction, raw = ask(hr, sub, groups=["name", "dates", "bogus"])
    sub.refresh_from_db()
    assert sub.status == S.CORRECTION_REQUESTED and sub.status_reason.startswith("Please fix")
    assert correction.fields == ["name", "dates"]  # unknown groups ignored
    assert len(raw) >= 40
    assert correction.token_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in str(CorrectionRequest.objects.values())  # raw token never stored
    entry = AuditLog.objects.get(action="submission.correction_requested")
    assert raw not in str(entry.metadata) and entry.metadata["fields"] == ["name", "dates"]
    assert correction.expires_at - timezone.now() > dt.timedelta(days=6)


@pytest.mark.parametrize("groups,reason", [([], "x"), (["name"], ""), (["unknown"], "x")])
def test_request_requires_fields_and_reason(hr, groups, reason):
    sub, _ = registration()
    with pytest.raises(WorkflowError):
        review.request_correction(sub.pk, actor=hr, groups=groups, reason=reason)
    assert not CorrectionRequest.objects.exists()


def test_only_one_open_correction_per_submission(hr):
    sub, _ = registration()
    ask(hr, sub)
    with pytest.raises(WorkflowError):
        ask(hr, sub)  # status is now correction_requested
    assert CorrectionRequest.objects.count() == 1


def test_http_request_shows_link_once_and_does_not_store_it(client, hr):
    sub, _ = registration()
    client.force_login(hr)
    resp = client.post(reverse("registry:submission_action", kwargs={"pk": sub.pk}),
                       {"action": "request_correction", "fields": ["name"], "reason": "Fix your name",
                        "expected_status": "submitted"})
    assert resp.status_code == 200 and resp["Cache-Control"] == "no-store"
    shown = resp.context["link"]
    assert "/correct/" in shown
    raw = shown.rstrip("/").rsplit("/", 1)[1]
    assert CorrectionRequest.objects.get().token_hash == hashlib.sha256(raw.encode()).hexdigest()
    detail = client.get(reverse("registry:submission_detail", kwargs={"pk": sub.pk})).content.decode()
    assert raw not in detail


# --- Student side ------------------------------------------------------------------

def test_student_sees_only_requested_fields(client, hr):
    sub, _ = registration(full_name="Learner Name A")
    _, raw = ask(hr, sub, groups=["name", "dates"])
    resp = client.get(link(raw))
    assert resp.status_code == 200
    fields = set(resp.context["form"].fields)
    assert fields == {"title", "full_name", "start_date", "end_date"}
    html = resp.content.decode()
    assert "Please fix the capitalisation" in html and 'value="Learner Name A"' in html
    assert "learner@gmail.com" not in html and sub.student.mobile not in html  # other details not exposed


def test_correction_applies_requested_fields_and_returns_to_hr(client, hr):
    sub, internship = registration(full_name="Learner Name A")
    _, raw = ask(hr, sub, groups=["name", "dates"])
    resp = client.post(link(raw), {"title": "Mr.", "full_name": "Learner Name B",
                                   "start_date": "2026-06-15", "end_date": "2026-09-14",
                                   # Tampering: fields not requested must be ignored.
                                   "mobile": "9999999999", "reg_no": "HACKED-1", "email": "x@gmail.com"})
    assert resp.status_code == 302 and resp["Location"] == reverse("registry:correction_done")
    sub.refresh_from_db()
    internship.refresh_from_db()
    s = sub.student
    assert sub.status == S.SUBMITTED
    assert (s.title, s.full_name) == ("Mr.", "Learner Name B")
    assert (internship.start_date, internship.end_date) == (dt.date(2026, 6, 15), dt.date(2026, 9, 14))
    assert s.mobile != "9999999999" and s.reg_no != "HACKED-1"
    assert s.emails.get(kind="primary").address == "learner@gmail.com"
    c = CorrectionRequest.objects.get()
    assert c.status == "completed" and set(c.changes) == {"title", "full_name", "start_date", "end_date"}
    assert c.changes["full_name"] == {"old": "Learner Name A", "new": "Learner Name B"}
    entry = AuditLog.objects.get(action="submission.corrected")
    assert entry.metadata["changed_fields"] == ["end_date", "full_name", "start_date", "title"]
    assert "Learner Name B" not in str(entry.metadata)  # values stay out of the audit log


def test_corrected_values_are_validated(client, hr):
    sub, _ = registration()
    _, raw = ask(hr, sub, groups=["name", "mobile", "dates"])
    resp = client.post(link(raw), {"title": "Mr.", "full_name": "learner lower", "mobile": "12",
                                   "start_date": "2026-09-01", "end_date": "2026-08-01"})
    assert resp.status_code == 200
    errors = resp.context["form"].errors
    assert {"full_name", "mobile", "end_date"} <= set(errors)
    assert resp.context["form"].suggestions["full_name"] == "Learner Lower"
    sub.refresh_from_db()
    assert sub.status == S.CORRECTION_REQUESTED
    assert CorrectionRequest.objects.get().status == "open"


def test_link_is_single_use(client, hr):
    sub, _ = registration()
    _, raw = ask(hr, sub)
    client.post(link(raw), {"title": "Ms.", "full_name": "Learner Fixed A"})
    resp = client.get(link(raw))
    assert resp.status_code == 410 and "not available" in resp.content.decode()
    resp = client.post(link(raw), {"title": "Ms.", "full_name": "Learner Again B"})
    assert resp.status_code == 410
    assert Submission.objects.get().student.full_name == "Learner Fixed A"


def test_expired_link_refused(client, hr):
    sub, _ = registration()
    correction, raw = ask(hr, sub)
    CorrectionRequest.objects.filter(pk=correction.pk).update(expires_at=timezone.now() - dt.timedelta(seconds=1))
    assert client.get(link(raw)).status_code == 410
    assert client.post(link(raw), {"title": "Ms.", "full_name": "Learner Late A"}).status_code == 410
    with pytest.raises(WorkflowError, match="expired"):
        review.apply_correction(raw, {"title": "Ms.", "full_name": "Learner Late A"})


def test_unknown_or_malformed_tokens_reveal_nothing(client):
    for token in ["nope", "x" * 43, "a" * 150]:
        resp = client.get(link(token))
        assert resp.status_code == 404 and "not available" in resp.content.decode()


def test_regenerated_link_invalidates_old_one(client, hr):
    sub, _ = registration()
    _, old = ask(hr, sub)
    _, new = review.regenerate_correction_link(sub.pk, actor=hr)
    assert old != new
    assert client.get(link(old)).status_code == 404
    assert client.get(link(new)).status_code == 200
    assert CorrectionRequest.objects.count() == 1


def test_cancelled_correction_link_stops_working(client, hr):
    sub, _ = registration()
    _, raw = ask(hr, sub)
    review.cancel_correction(sub.pk, actor=hr)
    sub.refresh_from_db()
    assert sub.status == S.UNDER_REVIEW
    assert client.get(link(raw)).status_code == 410


def test_link_cannot_touch_another_submission(client, hr):
    a, _ = registration(full_name="Learner Alpha A", email="alpha@gmail.com")
    b, _ = registration(full_name="Learner Beta B", email="beta@gmail.com")
    _, raw = ask(hr, a)
    client.post(link(raw), {"title": "Ms.", "full_name": "Learner Alpha Fixed"})
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.student.full_name == "Learner Alpha Fixed"
    assert b.student.full_name == "Learner Beta B" and b.status == S.SUBMITTED


def test_reg_no_correction_respects_campaign_uniqueness(client, hr):
    camp = f.campaign(hr)
    a, _ = registration(camp, reg_no="CAMP-001", email="a@gmail.com")
    registration(camp, reg_no="CAMP-002", email="b@gmail.com")
    _, raw = ask(hr, a, groups=["reg_no"])
    resp = client.post(link(raw), {"reg_no": "camp 002"})
    assert "already been submitted" in " ".join(resp.context["form"].non_field_errors())
    a.refresh_from_db()
    assert a.student.reg_no == "CAMP-001" and a.status == S.CORRECTION_REQUESTED
    resp = client.post(link(raw), {"reg_no": "CAMP-003"})
    assert resp.status_code == 302
    a.refresh_from_db()
    assert a.reg_no_key == "camp003"


def test_email_correction_resets_verification_and_reflags(client, hr):
    other, _ = registration(email="shared@gmail.com", reg_no="OTHER-1", mobile="9111111111")
    sub, _ = registration(email="old@gmail.com", reg_no="MINE-1", mobile="9222222222")
    StudentEmail.objects.filter(student=sub.student).update(verified_at=timezone.now())
    _, raw = ask(hr, sub, groups=["email"])
    client.post(link(raw), {"email": "shared@gmail.com"})
    primary = sub.student.emails.get(kind="primary")
    assert primary.address == "shared@gmail.com" and primary.verified_at is None
    assert DuplicateCandidate.objects.filter(submission=sub, matches=other, reason="email").exists()


def test_domain_correction_offers_only_active_domains(client, hr):
    sub, internship = registration()
    _, raw = ask(hr, sub, groups=["domain"])
    form = client.get(link(raw)).context["form"]
    assert "__request__" not in [c[0] for c in form.fields["domain"].choices]
    target = active_domain("Machine Learning")
    client.post(link(raw), {"domain": str(target.pk)})
    internship.refresh_from_db()
    assert internship.domain == target


# --- Interaction with approvals and issued certificates ---------------------------------

def test_correction_after_verification_withdraws_certificate_approvals(hr):
    sub, internship = verified_registration(hr)
    approvals.decide(internship.pk, "offer_letter", "approve", actor=hr)
    ask(hr, sub)
    req = CertificateRequest.objects.get()
    assert req.approval_status == "pending" and "Approval withdrawn automatically" in req.reason
    assert AuditLog.objects.filter(action="certificate.approval_withdrawn").exists()


def _live_issuance(hr, internship):
    req = CertificateRequest.objects.create(internship=internship, cert_type="offer_letter",
                                            approval_status="approved", decided_by=hr, decided_at=timezone.now())
    tv = f.test_template_version(hr, cert_type="offer_letter")
    return Issuance.objects.create(request=req, template_version=tv, created_by=hr)


def test_cannot_request_correction_once_a_certificate_exists(hr):
    sub, internship = verified_registration(hr)
    _live_issuance(hr, internship)
    with pytest.raises(WorkflowError, match="already been created"):
        ask(hr, sub)
    with pytest.raises(WorkflowError, match="already been created"):
        review.reopen(sub.pk, actor=hr, reason="x")


def test_open_link_stops_working_if_a_certificate_appears(client, hr):
    sub, internship = verified_registration(hr)
    _, raw = ask(hr, sub)
    _live_issuance(hr, internship)
    resp = client.post(link(raw), {"title": "Ms.", "full_name": "Learner Changed A"})
    assert "can no longer be changed" in " ".join(resp.context["form"].non_field_errors())
    sub.refresh_from_db()
    assert sub.student.full_name != "Learner Changed A"


def test_correction_post_rate_limited(client, hr, settings):
    settings.REGISTRATION_POSTS_PER_HOUR = 1
    sub, _ = registration()
    _, raw = ask(hr, sub)
    client.post(link(raw), {"title": "Ms.", "full_name": "bad name"})
    assert client.post(link(raw), {"title": "Ms.", "full_name": "Learner Ok A"}).status_code == 429
