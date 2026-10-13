"""Shared helpers for Phase 4 tests (synthetic data only)."""
import datetime as dt

from apps.registry import review
from apps.registry.models import Domain, StudentEmail, Submission

from . import factories as f


def active_domain(name="Data Science"):
    domain, _ = Domain.objects.get_or_create(normalized_key=name.casefold(),
                                             defaults={"name": name, "status": Domain.Status.ACTIVE})
    return domain


def registration(campaign=None, *, email="learner@gmail.com", project_title="Hotel Management System", **student_kw):
    """A submission with student, primary email and internship, as the public form would create."""
    campaign = campaign or f.campaign(f.hr_admin())
    student = f.student(**student_kw)
    StudentEmail.objects.create(student=student, kind=StudentEmail.Kind.PRIMARY, address=email)
    sub = f.submission(campaign, student)
    internship = f.internship(student, active_domain(), campaign=campaign, submission=sub,
                              project_title=project_title,
                              project_status="pending" if project_title else "not_applicable",
                              start_date=dt.date(2026, 6, 1), end_date=dt.date(2026, 8, 31))
    return sub, internship


def verified_registration(hr=None, **kw):
    sub, internship = registration(**kw)
    review.approve(sub.pk, actor=hr or f.hr_admin())
    sub.refresh_from_db()
    internship.refresh_from_db()
    assert sub.status == Submission.Status.VERIFIED
    return sub, internship
