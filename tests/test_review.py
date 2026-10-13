"""Phase 4: HR submission review, duplicate resolution, notes, list/search and permissions."""
import threading

import pytest
from django.db import IntegrityError, connection, transaction
from django.urls import reverse

from apps.core.models import AuditLog
from apps.core.workflow import WorkflowError
from apps.registry import review
from apps.registry.models import DuplicateCandidate, Submission, SubmissionNote

from . import factories as f
from .phase4 import registration, verified_registration

pytestmark = pytest.mark.django_db
S = Submission.Status


@pytest.fixture
def hr():
    return f.hr_admin()


def act(client, sub, action, **data):
    return client.post(reverse("registry:submission_action", kwargs={"pk": sub.pk}),
                       {"action": action, **data})


# --- Transitions ---------------------------------------------------------------

def test_full_happy_path_records_reviewer_and_audit(hr):
    sub, _ = registration()
    review.start_review(sub.pk, actor=hr, expected_status=S.SUBMITTED)
    review.approve(sub.pk, actor=hr, expected_status=S.UNDER_REVIEW, note="Checked against college ID")
    sub.refresh_from_db()
    assert sub.status == S.VERIFIED and sub.reviewed_by == hr and sub.reviewed_at
    assert sub.status_reason == "Checked against college ID"
    actions = list(AuditLog.objects.filter(metadata__submission_id=sub.pk).order_by("created_at", "id")
                   .values_list("action", flat=True))
    assert actions[-2:] == ["submission.review_started", "submission.approved"]


def test_approve_directly_from_submitted(hr):
    sub, _ = registration()
    review.approve(sub.pk, actor=hr)
    sub.refresh_from_db()
    assert sub.status == S.VERIFIED


@pytest.mark.parametrize("reason", ["", "   "])
def test_reject_requires_reason(hr, reason):
    sub, _ = registration()
    with pytest.raises(WorkflowError, match="reason"):
        review.reject(sub.pk, actor=hr, reason=reason)
    sub.refresh_from_db()
    assert sub.status == S.SUBMITTED


def test_reject_records_reason(hr):
    sub, _ = registration()
    review.reject(sub.pk, actor=hr, reason="Not part of this batch")
    sub.refresh_from_db()
    assert (sub.status, sub.status_reason, sub.reviewed_by) == (S.REJECTED, "Not part of this batch", hr)
    assert AuditLog.objects.get(action="submission.rejected").metadata["reason"] == "Not part of this batch"


def test_database_requires_reason_for_rejection(hr):
    sub, _ = registration()
    with pytest.raises(IntegrityError), transaction.atomic():
        Submission.objects.filter(pk=sub.pk).update(status=S.REJECTED, status_reason="", reviewed_by=hr)


def test_database_requires_reviewer_for_verification():
    sub, _ = registration()
    with pytest.raises(IntegrityError), transaction.atomic():
        Submission.objects.filter(pk=sub.pk).update(status=S.VERIFIED)


@pytest.mark.parametrize("start,action", [
    (S.VERIFIED, "approve"), (S.REJECTED, "approve"), (S.REJECTED, "reject"), (S.VERIFIED, "reject"),
    (S.CORRECTION_REQUESTED, "approve"), (S.UNDER_REVIEW, "start_review"), (S.SUBMITTED, "reopen"),
])
def test_invalid_transitions_are_refused(hr, start, action):
    sub, _ = registration()
    Submission.objects.filter(pk=sub.pk).update(status=start, status_reason="x", reviewed_by=hr)
    fn = {"approve": lambda: review.approve(sub.pk, actor=hr),
          "reject": lambda: review.reject(sub.pk, actor=hr, reason="r"),
          "start_review": lambda: review.start_review(sub.pk, actor=hr),
          "reopen": lambda: review.reopen(sub.pk, actor=hr, reason="r")}[action]
    with pytest.raises(WorkflowError):
        fn()
    sub.refresh_from_db()
    assert sub.status == start


def test_stale_page_is_refused(hr):
    sub, _ = registration()
    review.reject(sub.pk, actor=f.hr_admin(), reason="Duplicate entry")
    with pytest.raises(WorkflowError, match="changed by someone else"):
        review.approve(sub.pk, actor=hr, expected_status=S.SUBMITTED)
    sub.refresh_from_db()
    assert sub.status == S.REJECTED


def test_reopen_requires_reason_and_returns_to_review(hr):
    sub, _ = verified_registration(hr)
    with pytest.raises(WorkflowError):
        review.reopen(sub.pk, actor=hr, reason="")
    review.reopen(sub.pk, actor=hr, reason="College name needs checking")
    sub.refresh_from_db()
    assert sub.status == S.UNDER_REVIEW
    assert AuditLog.objects.get(action="submission.reopened").metadata["reason"] == "College name needs checking"


def test_approval_blocked_by_open_duplicate_flags_and_pending_domain(hr):
    sub, internship = registration()
    other, _ = registration(email="other@gmail.com")
    flag = DuplicateCandidate.objects.create(submission=sub, matches=other, reason="mobile")
    with pytest.raises(WorkflowError, match="duplicate"):
        review.approve(sub.pk, actor=hr)
    review.resolve_duplicate(flag.pk, actor=hr, resolution="further_review", reason="Ask the college")
    with pytest.raises(WorkflowError, match="duplicate"):
        review.approve(sub.pk, actor=hr)
    review.resolve_duplicate(flag.pk, actor=hr, resolution="not_duplicate", reason="Siblings sharing a phone")

    internship.domain.status = "pending"
    internship.domain.save()
    with pytest.raises(WorkflowError, match="domain"):
        review.approve(sub.pk, actor=hr)
    internship.domain.status = "active"
    internship.domain.save()
    review.approve(sub.pk, actor=hr)


# --- Duplicates --------------------------------------------------------------

@pytest.mark.parametrize("resolution", ["duplicate", "not_duplicate", "further_review"])
def test_duplicate_decisions_record_reviewer_reason_and_never_delete(hr, resolution):
    sub, _ = registration()
    other, _ = registration(email="other@gmail.com")
    flag = DuplicateCandidate.objects.create(submission=sub, matches=other, reason="reg_no")
    review.resolve_duplicate(flag.pk, actor=hr, resolution=resolution, reason="Checked with the college")
    flag.refresh_from_db()
    assert (flag.resolution, flag.resolved_by, flag.resolution_reason) == (resolution, hr, "Checked with the college")
    assert flag.resolved_at is not None
    assert Submission.objects.filter(pk__in=[sub.pk, other.pk]).count() == 2
    entry = AuditLog.objects.get(action="duplicate.resolved")
    assert entry.metadata["resolution"] == resolution and entry.metadata["submission_id"] == sub.pk


@pytest.mark.parametrize("resolution,reason", [("duplicate", ""), ("pending", "x"), ("merge", "x")])
def test_duplicate_decision_validation(hr, resolution, reason):
    sub, _ = registration()
    other, _ = registration(email="other@gmail.com")
    flag = DuplicateCandidate.objects.create(submission=sub, matches=other, reason="reg_no")
    with pytest.raises(WorkflowError):
        review.resolve_duplicate(flag.pk, actor=hr, resolution=resolution, reason=reason)
    flag.refresh_from_db()
    assert flag.resolution == "pending"


def test_database_requires_reviewer_and_reason_for_duplicate_decision():
    sub, _ = registration()
    other, _ = registration(email="other@gmail.com")
    flag = DuplicateCandidate.objects.create(submission=sub, matches=other, reason="reg_no")
    with pytest.raises(IntegrityError), transaction.atomic():
        DuplicateCandidate.objects.filter(pk=flag.pk).update(resolution="duplicate")


def test_duplicate_stale_decision_refused(hr):
    sub, _ = registration()
    other, _ = registration(email="other@gmail.com")
    flag = DuplicateCandidate.objects.create(submission=sub, matches=other, reason="reg_no")
    review.resolve_duplicate(flag.pk, actor=hr, resolution="duplicate", reason="Same person")
    with pytest.raises(WorkflowError, match="changed by someone else"):
        review.resolve_duplicate(flag.pk, actor=hr, resolution="not_duplicate", reason="x",
                                 expected_resolution="pending")


# --- Notes -------------------------------------------------------------------

def test_notes_are_internal_and_audited_without_text(client, hr):
    sub, _ = registration()
    client.force_login(hr)
    client.post(reverse("registry:submission_add_note", kwargs={"pk": sub.pk}), {"text": "Called the student"})
    note = SubmissionNote.objects.get()
    assert (note.author, note.text) == (hr, "Called the student")
    entry = AuditLog.objects.get(action="submission.note_added")
    assert "Called the student" not in str(entry.metadata)
    assert client.post(reverse("registry:submission_add_note", kwargs={"pk": sub.pk}), {"text": "  "}).status_code == 302
    assert SubmissionNote.objects.count() == 1


# --- HTTP: list, detail, actions -------------------------------------------------------

def test_list_search_and_filters(client, hr):
    camp_a, camp_b = f.campaign(hr), f.campaign(hr)
    a, _ = registration(camp_a, full_name="Alpha Learner", email="alpha@gmail.com")
    b, _ = registration(camp_b, full_name="Beta Learner", email="beta@gmail.com", reg_no="BETA-77")
    review.reject(b.pk, actor=hr, reason="Wrong batch")
    DuplicateCandidate.objects.create(submission=a, matches=b, reason="mobile")
    client.force_login(hr)
    url = reverse("registry:submission_list")

    def names(**params):
        return [s.student.full_name for s in client.get(url, params).context["page"]]

    assert set(names()) == {"Alpha Learner", "Beta Learner"}
    assert names(q="alpha") == ["Alpha Learner"]
    assert names(q="BETA-77") == ["Beta Learner"]
    assert names(q="beta@gmail") == ["Beta Learner"]
    assert names(campaign=camp_b.pk) == ["Beta Learner"]
    assert names(status="rejected") == ["Beta Learner"]
    assert names(duplicates="open") == ["Alpha Learner"]
    assert names(duplicates="none") == ["Beta Learner"]
    assert names(status="bogus") and names(campaign="x")  # invalid filters are ignored, not errors


def test_detail_shows_details_flags_notes_and_history(client, hr):
    sub, _ = registration(full_name="Detail Learner")
    other, _ = registration(email="other@gmail.com", full_name="Other Learner")
    DuplicateCandidate.objects.create(submission=sub, matches=other, reason="reg_no")
    review.add_note(sub.pk, actor=hr, text="First call done")
    client.force_login(hr)
    html = client.get(reverse("registry:submission_detail", kwargs={"pk": sub.pk})).content.decode()
    for text in ["Detail Learner", "learner@gmail.com", "Other Learner", "Same registration number",
                 "First call done", "submission.note_added", "Request correction"]:
        assert text in html, text


def test_actions_over_http(client, hr):
    sub, _ = registration()
    client.force_login(hr)
    act(client, sub, "start_review", expected_status="submitted")
    resp = act(client, sub, "reject", expected_status="under_review", reason="")
    assert resp.status_code == 302
    sub.refresh_from_db()
    assert sub.status == S.UNDER_REVIEW  # refused: no reason
    act(client, sub, "reject", expected_status="under_review", reason="Incomplete internship")
    sub.refresh_from_db()
    assert sub.status == S.REJECTED


def test_actions_require_post(client, hr):
    sub, _ = registration()
    client.force_login(hr)
    assert client.get(reverse("registry:submission_action", kwargs={"pk": sub.pk})).status_code == 405


# --- Permissions --------------------------------------------------------------

def _phase4_post_urls(sub, flag):
    return [
        (reverse("registry:submission_action", kwargs={"pk": sub.pk}), {"action": "approve"}),
        (reverse("registry:submission_add_note", kwargs={"pk": sub.pk}), {"text": "x"}),
        (reverse("registry:duplicate_resolve", kwargs={"pk": flag.pk}), {"resolution": "duplicate", "reason": "x"}),
    ]


def test_anonymous_and_roleless_users_cannot_review(client):
    sub, _ = registration()
    other, _ = registration(email="other@gmail.com")
    flag = DuplicateCandidate.objects.create(submission=sub, matches=other, reason="reg_no")
    gets = [reverse("registry:submission_list"), reverse("registry:submission_detail", kwargs={"pk": sub.pk})]
    for url in gets:
        assert client.get(url).status_code == 302
    for url, data in _phase4_post_urls(sub, flag):
        assert client.post(url, data).status_code == 302
    client.force_login(f.user(role=None))
    for url in gets:
        assert client.get(url).status_code == 403
    for url, data in _phase4_post_urls(sub, flag):
        assert client.post(url, data).status_code == 403
    sub.refresh_from_db()
    flag.refresh_from_db()
    assert sub.status == S.SUBMITTED and flag.resolution == "pending" and not SubmissionNote.objects.exists()


def test_view_only_user_cannot_decide(client):
    from django.contrib.auth.models import Permission

    viewer = f.user(role=None)
    viewer.user_permissions.add(Permission.objects.get(codename="view_submission"))
    sub, _ = registration()
    other, _ = registration(email="other@gmail.com")
    flag = DuplicateCandidate.objects.create(submission=sub, matches=other, reason="reg_no")
    client.force_login(viewer)
    assert client.get(reverse("registry:submission_detail", kwargs={"pk": sub.pk})).status_code == 200
    html = client.get(reverse("registry:submission_detail", kwargs={"pk": sub.pk})).content.decode()
    assert "Approve details" not in html  # controls hidden…
    for url, data in _phase4_post_urls(sub, flag):
        assert client.post(url, data).status_code == 403  # …and refused on the server


# --- Concurrency (PostgreSQL) ------------------------------------------------------

@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="true concurrency needs PostgreSQL row locks")
def test_concurrent_conflicting_decisions_only_one_wins(pg_transactional_db):
    sub, _ = registration()
    reviewers = [f.hr_admin() for _ in range(4)]
    barrier = threading.Barrier(4)
    outcomes = []

    def worker(i):
        from django.db import connection as conn
        barrier.wait()
        try:
            if i % 2:
                review.approve(sub.pk, actor=reviewers[i], expected_status=S.SUBMITTED)
            else:
                review.reject(sub.pk, actor=reviewers[i], reason="r", expected_status=S.SUBMITTED)
            outcomes.append("ok")
        except WorkflowError:
            outcomes.append("refused")
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["ok", "refused", "refused", "refused"]
    decisions = AuditLog.objects.filter(action__in=["submission.approved", "submission.rejected"],
                                        metadata__submission_id=sub.pk)
    assert decisions.count() == 1
