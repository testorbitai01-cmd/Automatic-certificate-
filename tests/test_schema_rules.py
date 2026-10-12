"""Integrity rules enforced by the database, not just the UI."""
import datetime as dt

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.certificates.models import EmailJob, GeneratedFile, Issuance
from apps.core.choices import CertificateType
from apps.doctemplates.models import TemplateVersion
from apps.registry.models import Domain, reg_no_key

from . import factories as f

pytestmark = pytest.mark.django_db


def raises_integrity():
    return pytest.raises(IntegrityError)


# --- registry -------------------------------------------------------------

def test_domain_names_unique_ignoring_case_and_spaces():
    # "Data Science" etc. are seeded by migration, so use a name that is not.
    Domain.objects.create(name="Test Robotics")
    with raises_integrity(), transaction.atomic():
        Domain.objects.create(name="  test   ROBOTICS ")
    with raises_integrity(), transaction.atomic():
        Domain.objects.create(name="data science")  # seeded domain, different case


def test_campaign_token_is_random_and_long():
    hr = f.hr_admin()
    a, b = f.campaign(hr), f.campaign(hr)
    assert a.public_token != b.public_token
    assert len(a.public_token) >= 32


def test_campaign_must_close_after_opening():
    now = timezone.now()
    with raises_integrity(), transaction.atomic():
        f.campaign(f.hr_admin(), opens_at=now, closes_at=now - dt.timedelta(hours=1))


def test_campaign_accepting_window_and_deactivation():
    camp = f.campaign(f.hr_admin())
    assert camp.is_accepting()
    assert not camp.is_accepting(camp.closes_at)
    camp.status = camp.Status.INACTIVE
    assert not camp.is_accepting()


def test_reg_no_key_flags_formatting_variants_but_is_not_unique():
    assert reg_no_key("219X1B 07-C4") == reg_no_key("219x1b07c4")
    f.student(reg_no="ABC-123")
    f.student(reg_no="abc123")  # allowed: different institutions may share formats


def test_one_submission_per_student_per_campaign():
    camp = f.campaign(f.hr_admin())
    stu = f.student()
    f.submission(camp, stu)
    with raises_integrity(), transaction.atomic():
        f.submission(camp, stu)


# --- internships ----------------------------------------------------------

def test_internship_end_cannot_precede_start():
    with raises_integrity(), transaction.atomic():
        f.internship(start_date=dt.date(2026, 8, 1), end_date=dt.date(2026, 7, 31))


def test_student_may_have_multiple_internships():
    stu = f.student()
    f.internship(stu)
    f.internship(stu, start_date=dt.date(2026, 9, 1), end_date=dt.date(2026, 11, 30))
    assert stu.internships.count() == 2


# --- templates ------------------------------------------------------------

def test_active_version_requires_approver():
    with raises_integrity(), transaction.atomic():
        f.test_template_version(f.hr_admin(), status=TemplateVersion.Status.ACTIVE)


def test_only_one_active_version_per_type():
    hr, sa = f.hr_admin(), f.super_admin()
    approved = dict(status=TemplateVersion.Status.ACTIVE, approved_by=sa, approved_at=timezone.now())
    f.test_template_version(hr, **approved)
    with raises_integrity(), transaction.atomic():
        f.test_template_version(hr, **approved)


def test_test_template_can_never_issue_production(settings):
    settings.CERT_PRODUCTION_ISSUANCE_ENABLED = True
    tv = f.test_template_version(f.hr_admin(), status=TemplateVersion.Status.ACTIVE,
                                 approved_by=f.super_admin(), approved_at=timezone.now())
    assert tv.is_production is False
    assert tv.can_issue_production is False


def test_production_template_blocked_while_master_switch_off(settings):
    settings.CERT_PRODUCTION_ISSUANCE_ENABLED = False
    tv = f.test_template_version(f.hr_admin(), is_production=True, status=TemplateVersion.Status.ACTIVE,
                                 approved_by=f.super_admin(), approved_at=timezone.now())
    assert tv.can_issue_production is False


# --- certificates ---------------------------------------------------------

@pytest.fixture
def setup():
    hr = f.hr_admin()
    intern = f.internship()
    return hr, intern, f.test_template_version(hr)


def test_each_certificate_type_is_a_separate_request(setup):
    hr, intern, _ = setup
    for t in CertificateType.values:
        f.cert_request(intern, t)
    with raises_integrity(), transaction.atomic():
        f.cert_request(intern, CertificateType.EXPERIENCE)


def test_only_one_live_issuance_per_request(setup):
    hr, intern, tv = setup
    req = f.cert_request(intern)
    f.issuance(req, tv, hr)
    with raises_integrity(), transaction.atomic():
        f.issuance(req, tv, hr, version_no=2)


def test_cannot_mark_issued_without_generated_pdf(setup):
    hr, intern, tv = setup
    req = f.cert_request(intern)
    with raises_integrity(), transaction.atomic():
        f.issuance(req, tv, hr, status=Issuance.Status.ISSUED, issue_date=dt.date(2026, 9, 1))


def test_reissue_requires_reason_and_supersedes(setup):
    hr, intern, tv = setup
    req = f.cert_request(intern)
    pdf = GeneratedFile.objects.create(file="issued/x.pdf", sha256="0" * 64, size_bytes=100, page_count=1)
    first = f.issuance(req, tv, hr, status=Issuance.Status.ISSUED, pdf_status=Issuance.PdfStatus.GENERATED,
                       file=pdf, issue_date=dt.date(2026, 9, 1))
    with raises_integrity(), transaction.atomic():
        Issuance.objects.filter(pk=first.pk).update(status=Issuance.Status.SUPERSEDED)
        f.issuance(req, tv, hr, version_no=2, replaces=first)  # no reason
    Issuance.objects.filter(pk=first.pk).update(status=Issuance.Status.SUPERSEDED)
    second = f.issuance(req, tv, hr, version_no=2, replaces=first, change_reason="Corrected project title")
    assert second.replaces == first


def test_one_inflight_email_per_recipient(setup):
    hr, intern, tv = setup
    iss = f.issuance(f.cert_request(intern), tv, hr)
    EmailJob.objects.create(issuance=iss, recipient="student@test.invalid", idempotency_key="k1", requested_by=hr)
    with raises_integrity(), transaction.atomic():
        EmailJob.objects.create(issuance=iss, recipient="student@test.invalid", idempotency_key="k2",
                                requested_by=hr)
    # Same idempotency key is rejected outright.
    with raises_integrity(), transaction.atomic():
        EmailJob.objects.create(issuance=iss, recipient="other@test.invalid", idempotency_key="k1",
                                requested_by=hr)


def test_issued_records_are_protected_from_cascade_delete(setup):
    from django.db.models import ProtectedError

    hr, intern, tv = setup
    f.issuance(f.cert_request(intern), tv, hr)
    with pytest.raises(ProtectedError):
        tv.delete()
    with pytest.raises(ProtectedError):
        intern.domain.delete()



@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(__import__("django.db").db.connection.vendor != "postgresql",
                    reason="database trigger is PostgreSQL-only")
def test_audit_trigger_still_enabled_after_transactional_tests(pg_transactional_db):
    # pytest-django runs transactional tests last; this one follows the transactional tests in
    # test_audit.py and test_registration.py, which disable the trigger only for their cleanup.
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute("SELECT tgenabled FROM pg_trigger WHERE tgname = 'core_auditlog_append_only'")
        assert cur.fetchone() == ("O",)
