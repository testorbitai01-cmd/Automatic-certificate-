"""Tests added by the Phase 4 security/workflow review (synthetic data only)."""
import datetime as dt
import io
import logging
import threading

import pytest
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.certificates import approvals
from apps.certificates.models import CertificateRequest
from apps.core.logging import redact
from apps.core.models import AuditLog
from apps.core.workflow import REASON_MAX_LENGTH, WorkflowError
from apps.internships import services
from apps.registry import review
from apps.registry.models import CorrectionRequest, DuplicateCandidate, Submission

from . import factories as f
from .phase4 import registration, verified_registration

pytestmark = pytest.mark.django_db
JUNE1, AUG31 = dt.date(2026, 6, 1), dt.date(2026, 8, 31)


def lor_ready(hr):
    """Verified student, completion verified, recommended: LOR eligible."""
    sub, i = verified_registration(hr)
    services.verify_completion(i.pk, actor=hr, decision="verified", start_date=JUNE1, end_date=AUG31)
    services.record_recommendation(i.pk, actor=hr, decision="recommended", mentor_name="Mentor Y",
                                   mentor_feedback="Private mentor feedback text",
                                   performance_evaluation="Private evaluation text")
    return sub, i


def lor_url(i):
    return reverse("internships:certificate_decision", kwargs={"pk": i.pk, "cert_type": "lor"})


# =========================== LOR: Super Admin only ===========================

def test_roles_hr_cannot_super_admin_can():
    assert not f.hr_admin().has_perm("certificates.approve_lor")
    assert f.super_admin().has_perm("certificates.approve_lor")


@pytest.mark.parametrize("decision,reason", [("approve", ""), ("reject", "No"), ("request_correction", "Fix"),
                                             ("reset", "Again")])
def test_hr_crafted_http_request_for_lor_is_forbidden(client, decision, reason):
    hr = f.hr_admin()
    _, i = lor_ready(hr)
    client.force_login(hr)
    resp = client.post(lor_url(i), {"decision": decision, "reason": reason, "expected_status": "pending"})
    assert resp.status_code == 403
    assert status_of(i) == "pending" and not CertificateRequest.objects.filter(cert_type="lor").exists()
    assert not AuditLog.objects.filter(action__startswith="certificate.").exists()


def test_hr_service_call_for_lor_is_refused(client):
    hr = f.hr_admin()
    _, i = lor_ready(hr)
    with pytest.raises(PermissionDenied):
        approvals.decide(i.pk, "lor", "approve", actor=hr)
    assert status_of(i) == "pending"


def test_super_admin_gives_final_lor_approval_over_http(client):
    hr, sa = f.hr_admin(), f.super_admin()
    _, i = lor_ready(hr)
    client.force_login(sa)
    resp = client.post(lor_url(i), {"decision": "approve", "expected_status": "pending"})
    assert resp.status_code == 302
    req = CertificateRequest.objects.get(internship=i, cert_type="lor")
    assert (req.approval_status, req.decided_by) == ("approved", sa)
    entry = AuditLog.objects.get(action="certificate.approve")
    assert entry.actor == sa and entry.metadata["cert_type"] == "lor"


def test_super_admin_still_needs_lor_prerequisites():
    hr, sa = f.hr_admin(), f.super_admin()
    _, i = verified_registration(hr)  # no completion, no recommendation
    with pytest.raises(WorkflowError, match="Cannot approve yet"):
        approvals.decide(i.pk, "lor", "approve", actor=sa)


def test_recommendation_stays_with_hr_and_other_types_unaffected(client):
    hr = f.hr_admin()
    _, i = lor_ready(hr)  # HR recorded the recommendation
    client.force_login(hr)
    for t in ["offer_letter", "experience"]:
        client.post(reverse("internships:certificate_decision", kwargs={"pk": i.pk, "cert_type": t}),
                    {"decision": "approve"})
    assert dict(CertificateRequest.objects.values_list("cert_type", "approval_status")) == {
        "offer_letter": "approved", "experience": "approved"}


def test_internship_page_shows_lor_controls_only_to_super_admin(client):
    hr, sa = f.hr_admin(), f.super_admin()
    _, i = lor_ready(hr)
    client.force_login(hr)
    rows = {r["type"]: r for r in client.get(reverse("internships:detail", kwargs={"pk": i.pk})).context["certificates"]}
    assert rows["lor"]["can_decide"] is False and rows["offer_letter"]["can_decide"] is True
    html = client.get(reverse("internships:detail", kwargs={"pk": i.pk})).content.decode()
    assert "Final decision by a Super Admin." in html
    client.force_login(sa)
    rows = {r["type"]: r for r in client.get(reverse("internships:detail", kwargs={"pk": i.pk})).context["certificates"]}
    assert rows["lor"]["can_decide"] is True


def test_legacy_lor_approval_is_reported_not_changed(client):
    """An LOR approved by HR before the rule existed is flagged and listed, never silently altered."""
    hr = f.hr_admin()
    _, i = lor_ready(hr)
    legacy = CertificateRequest.objects.create(internship=i, cert_type="lor", approval_status="approved",
                                               decided_by=hr, decided_at=timezone.now())
    assert approvals.approval_is_authorised(legacy) is False
    out = io.StringIO()
    call_command("check_lor_approvals", stdout=out)
    assert f"internship {i.pk}: approved by {hr.email}" in out.getvalue()
    assert "1 LOR approval(s) need Super Admin review" in out.getvalue()
    legacy.refresh_from_db()
    assert (legacy.approval_status, legacy.decided_by) == ("approved", hr)  # unchanged
    client.force_login(f.super_admin())
    html = client.get(reverse("internships:detail", kwargs={"pk": i.pk})).content.decode()
    assert "without final LOR authority" in html
    # A Super Admin can review it through the normal workflow.
    approvals.decide(i.pk, "lor", "reset", actor=f.super_admin(), reason="Legacy approval re-reviewed")
    approvals.decide(i.pk, "lor", "approve", actor=f.super_admin())
    assert approvals.approval_is_authorised(CertificateRequest.objects.get(pk=legacy.pk))


def test_hr_fact_changes_still_withdraw_a_super_admin_lor_approval():
    """Withdrawal is a safety mechanism, not a decision: HR changing the facts must still invalidate it."""
    hr, sa = f.hr_admin(), f.super_admin()
    sub, i = lor_ready(hr)
    approvals.decide(i.pk, "lor", "approve", actor=sa)
    services.record_recommendation(i.pk, actor=hr, decision="not_recommended")
    assert status_of(i) == "pending"
    entry = AuditLog.objects.get(action="certificate.approval_withdrawn")
    assert entry.actor == hr and entry.metadata["cert_type"] == "lor"


def status_of(i, cert_type="lor"):
    req = CertificateRequest.objects.filter(internship=i, cert_type=cert_type).first()
    return req.approval_status if req else "pending"


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="true concurrency needs PostgreSQL row locks")
def test_concurrent_lor_attempts_only_super_admin_succeeds(pg_transactional_db):
    hr, sa = f.hr_admin(), f.super_admin()
    _, i = lor_ready(hr)
    actors = [hr, hr, hr, sa]
    barrier = threading.Barrier(len(actors))
    outcomes = []

    def worker(actor):
        from django.db import connection as conn
        barrier.wait()
        try:
            approvals.decide(i.pk, "lor", "approve", actor=actor)
            outcomes.append(("ok", actor.pk))
        except PermissionDenied:
            outcomes.append(("forbidden", actor.pk))
        except WorkflowError:
            outcomes.append(("refused", actor.pk))
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(a,)) for a in actors]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == sorted([("forbidden", hr.pk)] * 3 + [("ok", sa.pk)])
    req = CertificateRequest.objects.get(internship=i, cert_type="lor")
    assert (req.approval_status, req.decided_by_id) == ("approved", sa.pk)


# =========================== Correction-link privacy ===========================

def test_redact_helper():
    assert redact("Gone: /correct/AbC-123_xyz/") == "Gone: /correct/[redacted]/"
    assert redact('"GET /correct/AbC?x=1 HTTP/1.1" 410') == '"GET /correct/[redacted]?x=1 HTTP/1.1" 410'
    assert redact("GET /correct/done/") == "GET /correct/done/"
    assert redact("Not Found: /register/campaign-token/") == "Not Found: /register/campaign-token/"


@pytest.mark.parametrize("make_unusable", ["used", "expired", "cancelled", "replaced", "unknown"])
def test_token_never_reaches_django_logs(client, caplog, make_unusable):
    hr = f.hr_admin()
    sub, _ = registration()
    corr, raw = review.request_correction(sub.pk, actor=hr, groups=["name"], reason="Fix name")
    if make_unusable == "used":
        client.post(f"/correct/{raw}/", {"title": "Ms.", "full_name": "Learner Fixed A"})
    elif make_unusable == "expired":
        CorrectionRequest.objects.filter(pk=corr.pk).update(expires_at=timezone.now() - dt.timedelta(seconds=1))
    elif make_unusable == "cancelled":
        review.cancel_correction(sub.pk, actor=hr)
    elif make_unusable == "replaced":
        review.regenerate_correction_link(sub.pk, actor=hr)
    else:
        raw = "x" * 43
    with caplog.at_level(logging.DEBUG):
        resp = client.get(f"/correct/{raw}/")
        client.post(f"/correct/{raw}/", {"title": "Ms.", "full_name": "Learner Again B"})
    assert resp.status_code in (404, 410)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "/correct/[redacted]/" in logged
    assert raw not in logged


def test_correction_pages_send_privacy_headers_and_load_nothing_external(client):
    hr = f.hr_admin()
    sub, _ = registration()
    _, raw = review.request_correction(sub.pk, actor=hr, groups=["name"], reason="Fix name")
    pages = [client.get(f"/correct/{raw}/"), client.get("/correct/done/"), client.get("/correct/" + "y" * 43 + "/")]
    client.force_login(hr)
    pages.append(client.post(reverse("registry:submission_action", kwargs={"pk": sub.pk}),
                             {"action": "regenerate_link", "expected_status": "correction_requested"}))
    for resp in pages:
        assert resp["Referrer-Policy"] == "strict-origin"
        assert resp["X-Robots-Tag"] == "noindex, nofollow"
        assert "no-store" in resp["Cache-Control"]
        html = resp.content.decode()
        for marker in ['src="http', "src='http", 'href="http', "@import", "//cdn", "fonts.googleapis"]:
            assert marker not in html.replace(resp.wsgi_request.build_absolute_uri("/"), ""), marker


def test_correction_post_requires_csrf():
    hr = f.hr_admin()
    sub, _ = registration()
    _, raw = review.request_correction(sub.pk, actor=hr, groups=["name"], reason="Fix name")
    resp = Client(enforce_csrf_checks=True).post(f"/correct/{raw}/", {"title": "Ms.", "full_name": "Learner Csrf A"})
    assert resp.status_code == 403
    assert Submission.objects.get().status == "correction_requested"


def test_tokens_use_secrets_and_differ():
    hr = f.hr_admin()
    tokens = set()
    for n in range(5):
        sub, _ = registration(email=f"t{n}@gmail.com")
        tokens.add(review.request_correction(sub.pk, actor=hr, groups=["name"], reason="r")[1])
    assert len(tokens) == 5 and all(len(t) >= 43 for t in tokens)


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="true concurrency needs PostgreSQL row locks")
def test_concurrent_double_submission_of_one_link_applies_once(pg_transactional_db):
    hr = f.hr_admin()
    sub, _ = registration()
    _, raw = review.request_correction(sub.pk, actor=hr, groups=["name"], reason="Fix name")
    barrier = threading.Barrier(4)
    outcomes = []

    def worker(n):
        from django.db import connection as conn
        barrier.wait()
        try:
            review.apply_correction(raw, {"title": "Ms.", "full_name": f"Learner Race {'ABCD'[n]}"})
            outcomes.append("ok")
        except WorkflowError:
            outcomes.append("refused")
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["ok", "refused", "refused", "refused"]
    assert AuditLog.objects.filter(action="submission.corrected").count() == 1


def test_database_rules_for_correction_status():
    hr = f.hr_admin()
    sub, _ = registration()
    corr, _ = review.request_correction(sub.pk, actor=hr, groups=["name"], reason="r")
    for update in [dict(status="completed"), dict(status="cancelled"), dict(reason="")]:
        with pytest.raises(IntegrityError), transaction.atomic():
            CorrectionRequest.objects.filter(pk=corr.pk).update(**update)


# =========================== Audit-log privacy ===========================

def test_full_workflow_audit_contains_no_tokens_values_or_private_text(client):
    hr, sa = f.hr_admin(), f.super_admin()
    sub, i = registration(full_name="Private Learner A", email="private.learner@gmail.com", mobile="9123456780",
                          reg_no="PRIV-001")
    corr, raw = review.request_correction(sub.pk, actor=hr, groups=["name", "mobile", "email"], reason="Check")
    client.post(f"/correct/{raw}/", {"title": "Ms.", "full_name": "Private Learner B", "mobile": "9876501234",
                                     "email": "private.new@gmail.com"})
    review.add_note(sub.pk, actor=hr, text="Private note text")
    review.approve(sub.pk, actor=hr)
    services.verify_completion(i.pk, actor=hr, decision="verified", start_date=JUNE1, end_date=AUG31)
    services.record_recommendation(i.pk, actor=hr, decision="recommended", mentor_name="Private Mentor",
                                   mentor_feedback="Private mentor feedback text",
                                   performance_evaluation="Private evaluation text")
    approvals.decide(i.pk, "lor", "approve", actor=sa)
    dump = str(list(AuditLog.objects.values_list("metadata", flat=True)))
    for secret in [raw, corr.token_hash, "Private Learner", "private.learner@gmail.com", "private.new@gmail.com",
                   "9123456780", "9876501234", "PRIV-001", "Private note text", "Private Mentor",
                   "Private mentor feedback text", "Private evaluation text"]:
        assert secret not in dump, secret
    assert AuditLog.objects.get(action="submission.corrected").metadata["changed_fields"] == [
        "email", "full_name", "mobile"]


def test_approval_note_is_kept_in_history():
    hr = f.hr_admin()
    sub, _ = registration()
    review.approve(sub.pk, actor=hr, note="Checked against college ID card")
    assert AuditLog.objects.get(action="submission.approved").metadata["note"] == "Checked against college ID card"


@pytest.mark.parametrize("call", ["reject", "correction", "duplicate", "completion", "certificate"])
def test_reasons_are_length_limited_on_the_server(call):
    hr = f.hr_admin()
    long = "x" * (REASON_MAX_LENGTH + 1)
    sub, i = registration()
    other, _ = registration(email="other@gmail.com")
    flag = DuplicateCandidate.objects.create(submission=sub, matches=other, reason="reg_no")
    fn = {
        "reject": lambda: review.reject(sub.pk, actor=hr, reason=long),
        "correction": lambda: review.request_correction(sub.pk, actor=hr, groups=["name"], reason=long),
        "duplicate": lambda: review.resolve_duplicate(flag.pk, actor=hr, resolution="not_duplicate", reason=long),
        "completion": lambda: services.verify_completion(i.pk, actor=hr, decision="not_completed",
                                                         start_date=JUNE1, end_date=AUG31, note=long),
        "certificate": lambda: approvals.decide(i.pk, "offer_letter", "reject", actor=hr, reason=long),
    }[call]
    with pytest.raises(WorkflowError, match="characters or fewer"):
        fn()


# =========================== Workflow correctness ===========================

def test_corrected_dates_reset_completion_verification():
    hr = f.hr_admin()
    sub, i = verified_registration(hr)
    services.verify_completion(i.pk, actor=hr, decision="verified", start_date=JUNE1, end_date=AUG31)
    _, raw = review.request_correction(sub.pk, actor=hr, groups=["dates"], reason="Wrong end date")
    review.apply_correction(raw, {"start_date": JUNE1, "end_date": dt.date(2026, 9, 30)})
    i.refresh_from_db()
    assert (i.completion_status, i.completion_verified_by) == ("pending", None)
    assert AuditLog.objects.get(action="internship.verification_reset").metadata["reset"] == ["completion"]
    assert "Internship completion has not been verified." in approvals.eligibility(i, "experience")


def test_corrected_project_title_resets_project_verification():
    hr = f.hr_admin()
    sub, i = verified_registration(hr)
    services.verify_project(i.pk, actor=hr, decision="verified")
    _, raw = review.request_correction(sub.pk, actor=hr, groups=["project_title"], reason="Typo in title")
    review.apply_correction(raw, {"project_title": "Hotel Booking System"})
    i.refresh_from_db()
    assert (i.project_title, i.project_status) == ("Hotel Booking System", "pending")


def test_unchanged_resubmission_keeps_verification():
    hr = f.hr_admin()
    sub, i = verified_registration(hr)
    services.verify_completion(i.pk, actor=hr, decision="verified", start_date=JUNE1, end_date=AUG31)
    _, raw = review.request_correction(sub.pk, actor=hr, groups=["dates"], reason="Please confirm dates")
    review.apply_correction(raw, {"start_date": JUNE1, "end_date": AUG31})
    i.refresh_from_db()
    assert i.completion_status == "verified"


def test_confirmed_duplicate_blocks_approval_and_eligibility():
    hr = f.hr_admin()
    sub, i = registration()
    other, _ = registration(email="other@gmail.com")
    flag = DuplicateCandidate.objects.create(submission=sub, matches=other, reason="reg_no")
    review.resolve_duplicate(flag.pk, actor=hr, resolution="duplicate", reason="Same student registered twice")
    with pytest.raises(WorkflowError, match="confirmed duplicate"):
        review.approve(sub.pk, actor=hr)
    review.reject(sub.pk, actor=hr, reason="Duplicate registration")
    assert Submission.objects.filter(pk=sub.pk).exists()  # rejected, never deleted


def test_flag_on_verified_submission_cannot_be_reopened_without_reopening_submission():
    hr = f.hr_admin()
    sub, _ = registration()
    other, _ = registration(email="other@gmail.com")
    flag = DuplicateCandidate.objects.create(submission=sub, matches=other, reason="reg_no")
    review.resolve_duplicate(flag.pk, actor=hr, resolution="not_duplicate", reason="Different people")
    review.approve(sub.pk, actor=hr)
    for resolution in ["duplicate", "further_review"]:
        with pytest.raises(WorkflowError, match="Reopen it"):
            review.resolve_duplicate(flag.pk, actor=hr, resolution=resolution, reason="Second thoughts")
    review.reopen(sub.pk, actor=hr, reason="New information")
    review.resolve_duplicate(flag.pk, actor=hr, resolution="further_review", reason="Second thoughts")


def test_reverse_flags_visible_on_older_submission(client):
    hr = f.hr_admin()
    older, _ = registration(full_name="Older Learner A")
    newer, _ = registration(full_name="Newer Learner B", email="newer@gmail.com")
    DuplicateCandidate.objects.create(submission=newer, matches=older, reason="mobile")
    client.force_login(hr)
    html = client.get(reverse("registry:submission_detail", kwargs={"pk": older.pk})).content.decode()
    assert "Later submissions flagged against this one" in html and "Newer Learner B" in html


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="true concurrency needs PostgreSQL row locks")
def test_approval_racing_duplicate_reopen_is_serialized(pg_transactional_db):
    hr = f.hr_admin()
    sub, _ = registration()
    other, _ = registration(email="other@gmail.com")
    flag = DuplicateCandidate.objects.create(submission=sub, matches=other, reason="reg_no")
    review.resolve_duplicate(flag.pk, actor=hr, resolution="not_duplicate", reason="Different people")
    barrier = threading.Barrier(2)
    results = {}

    def approve():
        from django.db import connection as conn
        barrier.wait()
        try:
            review.approve(sub.pk, actor=hr)
            results["approve"] = "ok"
        except WorkflowError:
            results["approve"] = "refused"
        finally:
            conn.close()

    def reflag():
        from django.db import connection as conn
        barrier.wait()
        try:
            review.resolve_duplicate(flag.pk, actor=hr, resolution="further_review", reason="Check again")
            results["reflag"] = "ok"
        except WorkflowError:
            results["reflag"] = "refused"
        finally:
            conn.close()

    threads = [threading.Thread(target=approve), threading.Thread(target=reflag)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    sub.refresh_from_db()
    flag.refresh_from_db()
    # Never both: a verified submission with an open duplicate flag.
    assert not (sub.status == "verified" and flag.resolution == "further_review")
    assert sorted(results.values()) == ["ok", "refused"]
