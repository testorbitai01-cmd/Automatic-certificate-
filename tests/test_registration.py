"""Public registration flow through the QR link."""
import datetime as dt
import secrets
import threading

import pytest
from django.db import IntegrityError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from apps.certificates.models import CertificateRequest, Issuance
from apps.core.models import AuditLog, Setting
from apps.internships.models import Internship
from apps.registry import services
from apps.registry.models import Campaign, Domain, DuplicateCandidate, Student, Submission

from . import factories as f

pytestmark = pytest.mark.django_db

SUCCESS = "Thank you for your response. Your details have been submitted successfully."


@pytest.fixture
def campaign():
    return f.campaign(f.hr_admin(), name="Test Batch Phase 3")


def url(campaign):
    return reverse("registry:register", kwargs={"token": campaign.public_token})


def payload(**overrides):
    data = {
        "title": "Ms.", "full_name": "Test Learner A", "preferred_name": "", "pronouns": "she",
        "gender": "female", "gender_detail": "",
        "mobile": "9000000001", "reg_no": "TEST2026001", "email": "test.learner.a@gmail.com",
        "alternate_email": "", "degree": "B.E. (Computer Science and Engineering)",
        "college_name": "Example Institute of Engineering and Technology", "college_location": "Testville",
        # get_or_create: transactional tests flush the migration-seeded domains.
        "domain": str(Domain.objects.get_or_create(normalized_key="data science",
                                                   defaults={"name": "Data Science", "status": "active"})[0].pk),
        "requested_domain": "",
        "start_date": "2026-06-01", "end_date": "2026-08-31", "project_title": "Hotel Management System",
        "feedback": "", "form_token": secrets.token_urlsafe(32),
    }
    data.update(overrides)
    return data


def submit(client, campaign, **overrides):
    return client.post(url(campaign), payload(**overrides))


# --- Happy path -------------------------------------------------------------

def test_form_renders_with_initial_domains_and_token(client, campaign):
    resp = client.get(url(campaign))
    assert resp.status_code == 200
    html = resp.content.decode()
    for name in ["Data Science", "Data Analytics", "Machine Learning", "FSD Java", "FSD Python", "HRM",
                 "Digital Marketing", "Medical Coding"]:
        assert name in html
    assert "request a new domain" in html
    assert 'name="form_token"' in html
    assert resp["Cache-Control"].startswith("max-age=0")


def test_valid_submission_creates_pending_records_only(client, campaign):
    resp = submit(client, campaign)
    assert resp.status_code == 302
    assert resp["Location"] == reverse("registry:register_done", kwargs={"token": campaign.public_token})
    assert SUCCESS in client.get(resp["Location"]).content.decode()

    sub = Submission.objects.get()
    assert sub.status == Submission.Status.SUBMITTED  # pending HR review, not approved
    assert sub.campaign == campaign and sub.reg_no_key == "test2026001"
    s = sub.student
    assert (s.title, s.full_name, s.mobile, s.reg_no) == ("Ms.", "Test Learner A", "9000000001", "TEST2026001")
    assert s.emails.get(kind="primary").address == "test.learner.a@gmail.com"
    assert s.emails.get(kind="primary").verified_at is None  # format valid ≠ ownership proven

    internship = Internship.objects.get()
    assert internship.start_date == dt.date(2026, 6, 1) and internship.end_date == dt.date(2026, 8, 31)
    assert internship.completion_status == Internship.Completion.PENDING  # registration ≠ completion
    assert internship.project_status == Internship.ProjectStatus.PENDING
    # Nothing certificate-related happens on submission.
    assert not CertificateRequest.objects.exists()
    assert not Issuance.objects.exists()


def test_raw_payload_keeps_what_student_typed_without_csrf(client, campaign):
    submit(client, campaign, full_name="  Test   Learner A ")
    sub = Submission.objects.get()
    assert sub.raw_payload["full_name"] == "  Test   Learner A "
    assert sub.student.full_name == "Test Learner A"
    assert "csrfmiddlewaretoken" not in sub.raw_payload and "form_token" not in sub.raw_payload


def test_submission_is_audited_without_personal_data(client, campaign):
    submit(client, campaign)
    entry = AuditLog.objects.get(action="submission.created")
    assert entry.actor is None and entry.actor_label == "student (public form)"
    assert entry.metadata == {"campaign_id": campaign.pk}
    assert "9000000001" not in str(entry.metadata) and "gmail" not in str(entry.metadata)


def test_done_page_shows_no_personal_details(client, campaign):
    submit(client, campaign)
    html = client.get(reverse("registry:register_done", kwargs={"token": campaign.public_token})).content.decode()
    assert SUCCESS in html
    assert "pending HR review" in html
    for private in ["Test Learner A", "9000000001", "TEST2026001", "gmail.com"]:
        assert private not in html


# --- Field validation through the form -----------------------------------------

@pytest.mark.parametrize("field,value,message", [
    ("full_name", "test learner a", "capitalize"),
    ("mobile", "12345", "exactly 10 digits"),
    ("mobile", "98765abcde", "digits only"),
    ("email", "learner@yahoo.com", "Gmail"),
    ("email", "not-an-email", "valid"),
    ("alternate_email", "bad@", "valid"),
    ("degree", "mca (general)", "capitalization"),
    ("college_name", "example institute of engineering", "capitalize"),
    ("college_location", "testville", "capitalize"),
    ("project_title", "hotel management system", "capitalize"),
    ("title", "Dr.", "Select a valid choice"),
    ("gender", "unknown", "Select a valid choice"),
    ("reg_no", "", "required"),
])
def test_invalid_field_is_rejected_with_message_and_values_kept(client, campaign, field, value, message):
    resp = submit(client, campaign, **{field: value})
    assert resp.status_code == 200
    errors = resp.context["form"].errors
    assert field in errors and message in " ".join(errors[field])
    assert not Submission.objects.exists()
    # Other entered values are preserved for correction.
    if field != "full_name":
        assert 'value="Test Learner A"' in resp.content.decode()


def test_suggestion_is_offered_not_applied(client, campaign):
    resp = submit(client, campaign, full_name="test learner a")
    html = resp.content.decode()
    assert resp.context["form"].suggestions["full_name"] == "Test Learner A"
    assert 'data-value="Test Learner A"' in html and "Use this" in html
    assert not Student.objects.exists()


def test_end_date_before_start_is_rejected(client, campaign):
    resp = submit(client, campaign, start_date="2026-08-01", end_date="2026-07-31")
    assert "The end date cannot be earlier than the start date." in resp.context["form"].errors["end_date"]
    assert not Submission.objects.exists()


@pytest.mark.parametrize("start,end", [("2026-06-01", "2026-06-30"), ("2026-06-01", "2026-07-31"),
                                       ("2026-06-01", "2026-09-30"), ("2026-06-15", "2026-10-14"),
                                       ("2026-06-01", "2026-06-01")])
def test_various_durations_accepted(client, campaign, start, end):
    assert submit(client, campaign, start_date=start, end_date=end).status_code == 302


def test_implausibly_long_range_rejected(client, campaign):
    resp = submit(client, campaign, start_date="2026-01-01", end_date="2027-06-30")
    assert "longer than one year" in " ".join(resp.context["form"].errors["end_date"])


def test_alternate_email_and_feedback_optional(client, campaign):
    assert submit(client, campaign, alternate_email="", feedback="").status_code == 302


def test_alternate_email_stored_unverified_and_any_provider(client, campaign):
    submit(client, campaign, alternate_email="learner@college.edu.in", feedback="Great mentors & projects <3")
    s = Student.objects.get()
    alt = s.emails.get(kind="alternate")
    assert alt.address == "learner@college.edu.in" and alt.verified_at is None
    assert Submission.objects.get().feedback == "Great mentors & projects <3"


def test_alternate_email_same_as_registered_rejected(client, campaign):
    resp = submit(client, campaign, alternate_email="TEST.LEARNER.A@gmail.com")
    assert "same as your registered email" in " ".join(resp.context["form"].errors["alternate_email"])


def test_feedback_length_limit(client, campaign):
    resp = submit(client, campaign, feedback="x" * 2001)
    assert resp.status_code == 200
    assert "2000 characters or fewer" in " ".join(resp.context["form"].errors["feedback"])
    assert not Submission.objects.exists()
    assert submit(client, campaign, feedback="x" * 2000).status_code == 302


def test_feedback_is_escaped_when_rendered_back(client, campaign):
    resp = submit(client, campaign, feedback="<script>alert(1)</script>", mobile="1")
    html = resp.content.decode()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_gmail_only_policy_can_be_turned_off(client, campaign):
    Setting.objects.create(key="registration.gmail_only", value=False)
    assert submit(client, campaign, email="learner@college.edu.in").status_code == 302


def test_gender_detail_only_kept_for_others(client, campaign):
    submit(client, campaign, gender="female", gender_detail="should be dropped")
    assert Student.objects.get().gender_detail == ""
    submit(client, campaign, gender="others", gender_detail="Non-binary", reg_no="TEST2026002",
           mobile="9000000002", email="learner2@gmail.com")
    assert Student.objects.get(reg_no="TEST2026002").gender_detail == "Non-binary"


def test_unusual_degree_accepted_and_noted_for_hr(client, campaign):
    submit(client, campaign, degree="B.Lib.Sc. (Library Science)")
    notes = Submission.objects.get().review_notes
    assert any("not in the recognized list" in n for n in notes)


# --- Domains -------------------------------------------------------------

def test_request_new_domain_creates_pending_domain(client, campaign):
    resp = submit(client, campaign, domain="__request__", requested_domain="Cloud Computing")
    assert resp.status_code == 302
    d = Domain.objects.get(name="Cloud Computing")
    assert d.status == Domain.Status.PENDING
    assert d.requested_by_submission == Submission.objects.get()
    assert Internship.objects.get().domain == d
    assert AuditLog.objects.filter(action="domain.requested", entity_id=str(d.pk)).exists()
    # Pending domains are not offered to other students.
    assert "Cloud Computing" not in client.get(url(campaign)).content.decode()


def test_request_existing_domain_reuses_it(client, campaign):
    submit(client, campaign, domain="__request__", requested_domain="Data   Science")
    assert Internship.objects.get().domain.name == "Data Science"
    assert Domain.objects.filter(normalized_key="data science").count() == 1


def test_request_domain_requires_name_and_capitalization(client, campaign):
    resp = submit(client, campaign, domain="__request__", requested_domain="")
    assert "requested_domain" in resp.context["form"].errors
    resp = submit(client, campaign, domain="__request__", requested_domain="cloud computing")
    assert resp.context["form"].suggestions["requested_domain"] == "Cloud Computing"


def test_inactive_domain_cannot_be_chosen_or_requested(client, campaign):
    d = Domain.objects.get(name="HRM")
    d.status = Domain.Status.INACTIVE
    d.save()
    resp = submit(client, campaign, domain=str(d.pk))
    assert "domain" in resp.context["form"].errors
    resp = submit(client, campaign, domain="__request__", requested_domain="HRM")
    assert "not currently offered" in " ".join(resp.context["form"].non_field_errors())


# --- Campaign availability ---------------------------------------------------

def test_unknown_token_shows_friendly_404(client):
    resp = client.get(reverse("registry:register", kwargs={"token": "not-a-real-token-" + "x" * 20}))
    assert resp.status_code == 404
    assert "not valid" in resp.content.decode()


@pytest.mark.parametrize("state,status,text", [
    ("expired", 410, "Registration has closed"),
    ("scheduled", 403, "has not opened yet"),
    ("deactivated", 410, "no longer active"),
])
def test_closed_campaigns_refuse_get_and_post(client, state, status, text):
    hr = f.hr_admin()
    now = timezone.now()
    kw = {
        "expired": dict(opens_at=now - dt.timedelta(days=10), closes_at=now - dt.timedelta(seconds=1)),
        "scheduled": dict(opens_at=now + dt.timedelta(days=1), closes_at=now + dt.timedelta(days=10)),
        "deactivated": dict(status=Campaign.Status.INACTIVE),
    }[state]
    camp = f.campaign(hr, **kw)
    for resp in (client.get(url(camp)), submit(client, camp)):
        assert resp.status_code == status
        assert text in resp.content.decode()
    assert not Submission.objects.exists()


def test_campaign_closing_between_page_load_and_submit_is_refused(client, campaign):
    form_page = client.get(url(campaign))
    assert form_page.status_code == 200
    Campaign.objects.filter(pk=campaign.pk).update(closes_at=timezone.now() - dt.timedelta(seconds=1))
    assert submit(client, campaign).status_code == 410
    assert not Submission.objects.exists()


def test_service_rechecks_campaign_state_under_lock(campaign, rf):
    from apps.registry.forms import RegistrationForm

    form = RegistrationForm(payload(), campaign=campaign)
    assert form.is_valid(), form.errors
    Campaign.objects.filter(pk=campaign.pk).update(status=Campaign.Status.INACTIVE)
    with pytest.raises(services.CampaignClosed):
        services.submit_registration(campaign, form.cleaned_data, payload())


# --- Double clicks and duplicates ---------------------------------------------

def test_double_click_same_token_creates_one_submission(client, campaign):
    data = payload()
    first = client.post(url(campaign), data)
    second = client.post(url(campaign), data)
    assert first.status_code == second.status_code == 302
    assert first["Location"] == second["Location"]
    assert Submission.objects.count() == 1 and Student.objects.count() == 1
    assert AuditLog.objects.filter(action="submission.created").count() == 1


def test_token_from_another_campaign_is_refused(client, campaign):
    other = f.campaign(f.hr_admin())
    data = payload()
    client.post(url(other), data)
    resp = client.post(url(campaign), data)
    assert "form has expired" in " ".join(resp.context["form"].non_field_errors())
    assert Submission.objects.count() == 1


def test_same_reg_no_in_same_campaign_is_blocked(client, campaign):
    submit(client, campaign, reg_no="ABC-123")
    resp = submit(client, campaign, reg_no="abc 123", full_name="Other Person B", email="other.b@gmail.com",
                  mobile="9000000009")
    errors = " ".join(resp.context["form"].non_field_errors())
    assert "already been submitted" in errors
    # The message must not reveal who registered first.
    assert "Test Learner A" not in resp.content.decode()
    assert Submission.objects.count() == 1
    assert AuditLog.objects.filter(action="submission.blocked_duplicate").count() == 1


def test_database_blocks_same_reg_no_even_if_precheck_is_bypassed(campaign):
    a = f.student(reg_no="RACE-1")
    b = f.student(reg_no="race1")
    f.submission(campaign, a)
    with pytest.raises(IntegrityError), transaction.atomic():
        f.submission(campaign, b)


def test_integrity_race_is_reported_as_duplicate(campaign, monkeypatch):
    from apps.registry.forms import RegistrationForm

    form = RegistrationForm(payload(), campaign=campaign)
    assert form.is_valid(), form.errors

    def lose_race(*args, **kwargs):
        raise IntegrityError("simulated concurrent insert")

    monkeypatch.setattr(services, "_create_records", lose_race)
    with pytest.raises(services.DuplicateInCampaign):
        services.submit_registration(campaign, form.cleaned_data, payload())
    assert AuditLog.objects.filter(action="submission.blocked_duplicate").exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="true concurrency needs PostgreSQL row locks")
def test_concurrent_submissions_same_reg_no_create_exactly_one(pg_transactional_db):
    from django.test import Client

    camp = f.campaign(f.hr_admin())
    barrier = threading.Barrier(5)
    results = []

    def worker(i):
        from django.db import connection as conn
        barrier.wait()
        try:
            results.append(Client().post(url(camp), payload(email=f"race{i}@gmail.com",
                                                            mobile=f"900000010{i}")).status_code)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert Submission.objects.filter(campaign=camp).count() == 1
    assert sorted(results) == [200, 200, 200, 200, 302]


def test_matches_in_other_campaigns_are_flagged_not_blocked(client, campaign):
    other = f.campaign(f.hr_admin())
    submit(client, other)
    resp = submit(client, campaign)  # same reg no, email and mobile, different campaign
    assert resp.status_code == 302
    new = Submission.objects.get(campaign=campaign)
    reasons = set(DuplicateCandidate.objects.filter(submission=new).values_list("reason", flat=True))
    assert reasons == {"reg_no", "email", "mobile"}
    assert all(d.resolution == "pending" for d in new.duplicate_flags.all())
    entry = AuditLog.objects.get(action="submission.duplicate_flagged")
    assert entry.metadata == {"reasons": {"reg_no": 1, "email": 1, "mobile": 1}, "submission_id": new.pk}


def test_email_match_is_case_insensitive_and_checks_alternate(client, campaign):
    other = f.campaign(f.hr_admin())
    submit(client, other, alternate_email="shared@college.edu.in")
    submit(client, campaign, reg_no="DIFF-1", mobile="9111111111", email="new.one@gmail.com",
           alternate_email="SHARED@college.edu.in")
    new = Submission.objects.get(campaign=campaign)
    assert list(new.duplicate_flags.values_list("reason", flat=True)) == ["email"]


def test_no_flags_for_distinct_students(client, campaign):
    submit(client, campaign)
    submit(client, campaign, reg_no="OTHER-2", mobile="9222222222", email="other.two@gmail.com")
    assert not DuplicateCandidate.objects.exists()
    assert not AuditLog.objects.filter(action="submission.duplicate_flagged").exists()


def test_public_post_rate_limit(client, campaign, settings):
    settings.REGISTRATION_POSTS_PER_HOUR = 2
    submit(client, campaign, mobile="1")
    submit(client, campaign, mobile="1")
    assert submit(client, campaign).status_code == 429
    assert not Submission.objects.exists()


def test_csrf_is_enforced_on_public_form(campaign):
    from django.test import Client

    resp = Client(enforce_csrf_checks=True).post(url(campaign), payload())
    assert resp.status_code == 403
    assert not Submission.objects.exists()
