"""Phase 4: completion verification, LOR recommendation and per-certificate approval."""
import datetime as dt
import threading

import pytest
from django.db import IntegrityError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from apps.certificates import approvals
from apps.certificates.models import CertificateRequest, Issuance
from apps.core.models import AuditLog
from apps.core.workflow import WorkflowError
from apps.internships import services
from apps.internships.models import AttendancePeriod, Internship, Recommendation
from apps.registry import review

from . import factories as f
from .phase4 import registration, verified_registration

pytestmark = pytest.mark.django_db
TYPES = ["offer_letter", "experience", "project", "lor"]
JUNE1, AUG31 = dt.date(2026, 6, 1), dt.date(2026, 8, 31)


@pytest.fixture
def hr():
    return f.hr_admin()


def complete(hr, i, **kw):
    return services.verify_completion(i.pk, actor=hr, decision="verified", start_date=kw.get("start", JUNE1),
                                      end_date=kw.get("end", AUG31), note=kw.get("note", ""))


def recommend(hr, i):
    return services.record_recommendation(i.pk, actor=hr, decision="recommended", mentor_name="Mentor X",
                                          mentor_feedback="Consistently strong work",
                                          performance_evaluation="Excellent")


def status(i, cert_type):
    req = CertificateRequest.objects.filter(internship=i, cert_type=cert_type).first()
    return req.approval_status if req else "pending"


# --- Completion verification ---------------------------------------------------------

def test_registration_alone_is_not_completion(hr):
    _, i = verified_registration(hr)
    assert i.completion_status == "pending"
    assert "Internship completion has not been verified." in approvals.eligibility(i, "experience")


def test_completion_requires_verified_student(hr):
    _, i = registration()
    with pytest.raises(WorkflowError, match="Verify the student's registration"):
        complete(hr, i)


def test_completion_verified_with_actual_dates(hr):
    _, i = verified_registration(hr)
    complete(hr, i, start=dt.date(2026, 6, 3), end=dt.date(2026, 8, 29), note="Attendance sheet checked")
    i.refresh_from_db()
    assert (i.completion_status, i.completion_verified_by) == ("verified", hr)
    assert (i.start_date, i.end_date) == (dt.date(2026, 6, 3), dt.date(2026, 8, 29))
    entry = AuditLog.objects.get(action="internship.completion_decided")
    assert entry.metadata["previous"]["start_date"] == "2026-06-01" and entry.metadata["start_date"] == "2026-06-03"


@pytest.mark.parametrize("kw,message", [
    (dict(decision="not_completed", note=""), "note"),
    (dict(decision="maybe"), "valid"),
    (dict(start_date=AUG31, end_date=JUNE1), "earlier"),
    (dict(start_date=None), "dates"),
])
def test_completion_validation(hr, kw, message):
    _, i = verified_registration(hr)
    args = dict(decision="verified", start_date=JUNE1, end_date=AUG31, note="")
    args.update(kw)
    with pytest.raises(WorkflowError, match=message):
        services.verify_completion(i.pk, actor=hr, **args)
    i.refresh_from_db()
    assert i.completion_status == "pending"


def test_database_rules_for_completion_and_project():
    _, i = registration()
    for update in [dict(completion_status="verified"), dict(completion_status="not_completed"),
                   dict(project_status="verified"), dict(project_status="rejected")]:
        with pytest.raises(IntegrityError), transaction.atomic():
            Internship.objects.filter(pk=i.pk).update(**update)


def test_project_verification(hr):
    _, i = verified_registration(hr)
    with pytest.raises(WorkflowError, match="note"):
        services.verify_project(i.pk, actor=hr, decision="rejected")
    services.verify_project(i.pk, actor=hr, decision="verified")
    i.refresh_from_db()
    assert (i.project_status, i.project_verified_by) == ("verified", hr)


def test_project_without_title_cannot_be_verified(hr):
    _, i = verified_registration(hr, project_title="")
    with pytest.raises(WorkflowError, match="no project title"):
        services.verify_project(i.pk, actor=hr, decision="verified")


# --- Attendance (history only) ------------------------------------------------------

def test_attendance_periods_are_history_only(hr):
    _, i = verified_registration(hr)
    services.add_attendance_period(i.pk, actor=hr, from_date=JUNE1, to_date=dt.date(2026, 6, 30), mode="offline")
    services.add_attendance_period(i.pk, actor=hr, from_date=dt.date(2026, 7, 1), to_date=AUG31, mode="online")
    assert AttendancePeriod.objects.filter(internship=i).count() == 2
    # Attendance does not make anything eligible.
    assert "Internship completion has not been verified." in approvals.eligibility(i, "experience")


@pytest.mark.parametrize("start,end,message", [
    (dt.date(2026, 5, 1), dt.date(2026, 6, 10), "within the internship"),
    (dt.date(2026, 6, 20), dt.date(2026, 6, 10), "valid date range"),
    (dt.date(2026, 6, 15), dt.date(2026, 7, 15), "overlaps"),
])
def test_attendance_validation(hr, start, end, message):
    _, i = verified_registration(hr)
    services.add_attendance_period(i.pk, actor=hr, from_date=JUNE1, to_date=dt.date(2026, 6, 30), mode="offline")
    with pytest.raises(WorkflowError, match=message):
        services.add_attendance_period(i.pk, actor=hr, from_date=start, to_date=end, mode="online")


# --- Recommendation --------------------------------------------------------------

def test_recommendation_needs_feedback_and_evaluation(hr):
    _, i = verified_registration(hr)
    with pytest.raises(WorkflowError, match="mentor feedback and a performance evaluation"):
        services.record_recommendation(i.pk, actor=hr, decision="recommended", mentor_feedback="Good")
    services.record_recommendation(i.pk, actor=hr, decision="not_recommended")
    rec = Recommendation.objects.get(internship=i)
    assert (rec.decision, rec.decided_by) == ("not_recommended", hr)


def test_recommendation_feedback_stays_out_of_audit_log(hr):
    _, i = verified_registration(hr)
    recommend(hr, i)
    entry = AuditLog.objects.get(action="internship.recommendation_decided")
    assert "Consistently strong work" not in str(entry.metadata)


def test_database_rule_for_recommendation(hr):
    _, i = verified_registration(hr)
    rec = Recommendation.objects.create(internship=i)
    with pytest.raises(IntegrityError), transaction.atomic():
        Recommendation.objects.filter(pk=rec.pk).update(decision="recommended", decided_by=hr,
                                                        decided_at=timezone.now())


# --- Per-certificate prerequisites ------------------------------------------------------

def test_nothing_is_approvable_before_student_verification(hr):
    _, i = registration()
    sa = f.super_admin()  # may decide on every type, including the LOR
    for t in TYPES:
        assert "The student's registration details have not been verified by HR." in approvals.eligibility(i, t)
        with pytest.raises(WorkflowError, match="Cannot approve yet"):
            approvals.decide(i.pk, t, "approve", actor=sa)


def test_offer_letter_can_be_approved_before_internship_starts(hr):
    _, i = verified_registration(hr)
    Internship.objects.filter(pk=i.pk).update(start_date=timezone.localdate() + dt.timedelta(days=30),
                                              end_date=timezone.localdate() + dt.timedelta(days=120))
    approvals.decide(i.pk, "offer_letter", "approve", actor=hr)
    assert status(i, "offer_letter") == "approved"
    # …and approving it approves nothing else.
    assert [status(i, t) for t in ["experience", "project", "lor"]] == ["pending"] * 3


def test_experience_requires_completion(hr):
    _, i = verified_registration(hr)
    with pytest.raises(WorkflowError, match="completion"):
        approvals.decide(i.pk, "experience", "approve", actor=hr)
    complete(hr, i)
    approvals.decide(i.pk, "experience", "approve", actor=hr)
    assert status(i, "experience") == "approved"
    assert status(i, "project") == "pending"


def test_project_requires_project_verification_not_completion(hr):
    _, i = verified_registration(hr)
    complete(hr, i)
    with pytest.raises(WorkflowError, match="Project completion"):
        approvals.decide(i.pk, "project", "approve", actor=hr)
    services.verify_project(i.pk, actor=hr, decision="verified")
    approvals.decide(i.pk, "project", "approve", actor=hr)
    assert status(i, "project") == "approved"


def test_lor_requires_its_own_recommendation(hr):
    _, i = verified_registration(hr)
    complete(hr, i)
    approvals.decide(i.pk, "experience", "approve", actor=hr)
    sa = f.super_admin()
    with pytest.raises(WorkflowError, match="recommendation"):
        approvals.decide(i.pk, "lor", "approve", actor=sa)
    services.record_recommendation(i.pk, actor=hr, decision="not_recommended")
    with pytest.raises(WorkflowError, match="recommendation"):
        approvals.decide(i.pk, "lor", "approve", actor=sa)
    recommend(hr, i)
    approvals.decide(i.pk, "lor", "approve", actor=f.super_admin())  # final LOR authority
    assert status(i, "lor") == "approved"


def test_common_requirements_duplicates_and_domain(hr):
    sub, i = verified_registration(hr)
    other, _ = registration(email="other@gmail.com")
    from apps.registry.models import DuplicateCandidate
    DuplicateCandidate.objects.create(submission=sub, matches=other, reason="mobile")
    assert any("duplicate" in u for u in approvals.eligibility(i, "offer_letter"))
    i.domain.status = "inactive"
    i.domain.save()
    assert any("not approved/active" in u for u in approvals.eligibility(i, "offer_letter"))


# --- Decisions -----------------------------------------------------------------------

@pytest.mark.parametrize("decision", ["reject", "request_correction"])
def test_negative_certificate_decisions_require_reason(hr, decision):
    _, i = verified_registration(hr)
    with pytest.raises(WorkflowError, match="reason"):
        approvals.decide(i.pk, "offer_letter", decision, actor=hr, reason=" ")
    req = approvals.decide(i.pk, "offer_letter", decision, actor=hr, reason="Offer details unclear")
    assert req.approval_status == {"reject": "rejected", "request_correction": "correction_requested"}[decision]
    assert req.decided_by == hr


def test_double_approval_and_stale_page_refused(hr):
    _, i = verified_registration(hr)
    approvals.decide(i.pk, "offer_letter", "approve", actor=hr, expected_status="pending")
    with pytest.raises(WorkflowError, match="already approved"):
        approvals.decide(i.pk, "offer_letter", "approve", actor=hr)
    with pytest.raises(WorkflowError, match="changed by someone else"):
        approvals.decide(i.pk, "offer_letter", "reject", actor=hr, reason="x", expected_status="pending")
    assert AuditLog.objects.filter(action="certificate.approve").count() == 1


def test_reset_to_pending_requires_reason(hr):
    _, i = verified_registration(hr)
    approvals.decide(i.pk, "offer_letter", "approve", actor=hr)
    with pytest.raises(WorkflowError):
        approvals.decide(i.pk, "offer_letter", "reset", actor=hr)
    approvals.decide(i.pk, "offer_letter", "reset", actor=hr, reason="Batch postponed")
    assert status(i, "offer_letter") == "pending"


def test_database_rules_for_certificate_decisions(hr):
    _, i = verified_registration(hr)
    req = CertificateRequest.objects.create(internship=i, cert_type="offer_letter")
    with pytest.raises(IntegrityError), transaction.atomic():
        CertificateRequest.objects.filter(pk=req.pk).update(approval_status="approved")
    with pytest.raises(IntegrityError), transaction.atomic():
        CertificateRequest.objects.filter(pk=req.pk).update(approval_status="rejected", decided_by=hr,
                                                            decided_at=timezone.now(), reason="")


def test_unknown_type_or_decision_refused(hr):
    _, i = verified_registration(hr)
    with pytest.raises(WorkflowError):
        approvals.decide(i.pk, "diploma", "approve", actor=hr)
    with pytest.raises(WorkflowError):
        approvals.decide(i.pk, "offer_letter", "issue", actor=hr)


# --- Automatic withdrawal when facts change ---------------------------------------------

def fully_approved(hr):
    sub, i = verified_registration(hr)
    complete(hr, i)
    services.verify_project(i.pk, actor=hr, decision="verified")
    recommend(hr, i)
    for t in TYPES:
        # LOR: final authority is Super Admin only.
        approvals.decide(i.pk, t, "approve", actor=f.super_admin() if t == "lor" else hr)
    return sub, i


def test_completion_change_withdraws_only_dependent_approvals(hr):
    _, i = fully_approved(hr)
    services.verify_completion(i.pk, actor=hr, decision="not_completed", start_date=JUNE1, end_date=AUG31,
                               note="Left in July")
    assert {t: status(i, t) for t in TYPES} == {"offer_letter": "approved", "experience": "pending",
                                                "project": "approved", "lor": "pending"}


def test_completion_date_change_withdraws_experience_and_lor(hr):
    _, i = fully_approved(hr)
    complete(hr, i, end=dt.date(2026, 8, 15))
    assert status(i, "experience") == "pending" and status(i, "lor") == "pending"
    assert status(i, "offer_letter") == "approved"


def test_project_change_withdraws_project_approval_only(hr):
    _, i = fully_approved(hr)
    services.verify_project(i.pk, actor=hr, decision="rejected", note="Not the submitted project")
    assert {t: status(i, t) for t in TYPES} == {"offer_letter": "approved", "experience": "approved",
                                                "project": "pending", "lor": "approved"}


def test_recommendation_change_withdraws_lor_only(hr):
    _, i = fully_approved(hr)
    services.record_recommendation(i.pk, actor=hr, decision="not_recommended")
    assert status(i, "lor") == "pending"
    assert status(i, "experience") == "approved"


def test_reopening_submission_withdraws_all_approvals(hr):
    sub, i = fully_approved(hr)
    review.reopen(sub.pk, actor=hr, reason="Recheck college")
    assert [status(i, t) for t in TYPES] == ["pending"] * 4
    assert AuditLog.objects.filter(action="certificate.approval_withdrawn").count() == 4


def test_decisions_locked_once_a_certificate_exists(hr):
    _, i = fully_approved(hr)
    req = CertificateRequest.objects.get(internship=i, cert_type="experience")
    Issuance.objects.create(request=req, template_version=f.test_template_version(hr), created_by=hr)
    with pytest.raises(WorkflowError, match="already been created"):
        approvals.decide(i.pk, "experience", "reset", actor=hr, reason="x")
    with pytest.raises(WorkflowError, match="already been created"):
        services.verify_completion(i.pk, actor=hr, decision="not_completed", start_date=JUNE1, end_date=AUG31,
                                   note="x")
    assert status(i, "experience") == "approved"


# --- HTTP and permissions ------------------------------------------------------------

def test_internship_page_lists_all_four_certificates_with_requirements(client, hr):
    _, i = verified_registration(hr)
    client.force_login(hr)
    resp = client.get(reverse("internships:detail", kwargs={"pk": i.pk}))
    rows = {r["type"]: r for r in resp.context["certificates"]}
    assert set(rows) == set(TYPES)
    assert rows["offer_letter"]["unmet"] == []
    assert "Internship completion has not been verified." in rows["experience"]["unmet"]
    assert "Registration is not proof of completion" in resp.content.decode()


def test_http_decisions(client, hr):
    _, i = verified_registration(hr)
    client.force_login(hr)
    client.post(reverse("internships:completion", kwargs={"pk": i.pk}),
                {"decision": "verified", "start_date": "2026-06-01", "end_date": "2026-08-31",
                 "expected_status": "pending"})
    client.post(reverse("internships:certificate_decision", kwargs={"pk": i.pk, "cert_type": "experience"}),
                {"decision": "approve", "expected_status": "pending"})
    assert status(i, "experience") == "approved"
    resp = client.post(reverse("internships:completion", kwargs={"pk": i.pk}),
                       {"decision": "verified", "start_date": "not-a-date", "end_date": "2026-08-31"})
    assert resp.status_code == 302  # refused with a message, nothing changed
    i.refresh_from_db()
    assert i.start_date == JUNE1


def _internship_posts(i):
    return [
        (reverse("internships:completion", kwargs={"pk": i.pk}),
         {"decision": "verified", "start_date": "2026-06-01", "end_date": "2026-08-31"}),
        (reverse("internships:project", kwargs={"pk": i.pk}), {"decision": "verified"}),
        (reverse("internships:attendance", kwargs={"pk": i.pk}),
         {"from_date": "2026-06-01", "to_date": "2026-06-30", "mode": "online"}),
        (reverse("internships:recommendation", kwargs={"pk": i.pk}), {"decision": "not_recommended"}),
        (reverse("internships:certificate_decision", kwargs={"pk": i.pk, "cert_type": "offer_letter"}),
         {"decision": "approve"}),
    ]


def test_anonymous_and_roleless_users_refused_everywhere(client, hr):
    _, i = verified_registration(hr)
    detail = reverse("internships:detail", kwargs={"pk": i.pk})
    assert client.get(detail).status_code == 302
    for url, data in _internship_posts(i):
        assert client.post(url, data).status_code == 302
    client.force_login(f.user(role=None))
    assert client.get(detail).status_code == 403
    for url, data in _internship_posts(i):
        assert client.post(url, data).status_code == 403
    i.refresh_from_db()
    assert i.completion_status == "pending" and not CertificateRequest.objects.exists()
    assert not AttendancePeriod.objects.exists() and not Recommendation.objects.exists()


def test_each_action_needs_its_own_permission(client, hr):
    from django.contrib.auth.models import Permission

    _, i = verified_registration(hr)
    user = f.user(role=None)
    user.user_permissions.add(Permission.objects.get(codename="view_internship"),
                              Permission.objects.get(codename="verify_completion"))
    client.force_login(user)
    posts = _internship_posts(i)
    assert client.post(*posts[0]).status_code == 302  # completion: allowed
    assert client.post(*posts[1]).status_code == 403  # project: needs verify_project
    assert client.post(*posts[3]).status_code == 403  # recommendation: needs decide_recommendation
    assert client.post(*posts[4]).status_code == 403  # certificate: needs approve_certificate


def test_hr_admin_role_has_phase4_permissions_but_not_super_admin_ones():
    hr = f.hr_admin()
    for perm in ["registry.review_submission", "registry.resolve_duplicate", "internships.verify_completion",
                 "internships.verify_project", "internships.decide_recommendation",
                 "certificates.approve_certificate"]:
        assert hr.has_perm(perm), perm
    for perm in ["certificates.authorize_reissue", "certificates.revoke_certificate", "doctemplates.approve_template"]:
        assert not hr.has_perm(perm), perm


# --- Concurrency (PostgreSQL) ------------------------------------------------------

@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="true concurrency needs PostgreSQL row locks")
def test_concurrent_approvals_record_one_decision(pg_transactional_db):
    hr = f.hr_admin()
    _, i = verified_registration(hr)
    barrier = threading.Barrier(5)
    outcomes = []

    def worker():
        from django.db import connection as conn
        barrier.wait()
        try:
            approvals.decide(i.pk, "offer_letter", "approve", actor=hr, expected_status="pending")
            outcomes.append("ok")
        except WorkflowError:
            outcomes.append("refused")
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["ok"] + ["refused"] * 4
    assert CertificateRequest.objects.filter(internship=i).count() == 1
    assert AuditLog.objects.filter(action="certificate.approve").count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="true concurrency needs PostgreSQL row locks")
def test_approval_racing_with_reopen_never_leaves_stale_approval(pg_transactional_db):
    hr = f.hr_admin()
    sub, i = verified_registration(hr)
    barrier = threading.Barrier(2)

    def approve():
        from django.db import connection as conn
        barrier.wait()
        try:
            approvals.decide(i.pk, "offer_letter", "approve", actor=hr)
        except WorkflowError:
            pass
        finally:
            conn.close()

    def reopen():
        from django.db import connection as conn
        barrier.wait()
        try:
            review.reopen(sub.pk, actor=hr, reason="Recheck")
        finally:
            conn.close()

    threads = [threading.Thread(target=approve), threading.Thread(target=reopen)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    sub.refresh_from_db()
    assert sub.status == "under_review"
    # Whichever ran first, an approval must not survive the reopen.
    assert status(i, "offer_letter") == "pending"
