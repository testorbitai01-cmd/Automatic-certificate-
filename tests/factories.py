"""Clearly fictional test data. Never use real student or office records in tests."""
import datetime as dt
import hashlib
import itertools

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.base import ContentFile
from django.utils import timezone

from apps.accounts.roles import HR_ADMIN, SUPER_ADMIN
from apps.certificates.models import CertificateRequest, Issuance
from apps.core.choices import CertificateType
from apps.doctemplates.models import CertificateTemplate, TemplateVersion
from apps.internships.models import Internship
from apps.registry.models import Campaign, Domain, Student, Submission

_seq = itertools.count(1)
PASSWORD = "Test-only-password-123"


def user(role=None, email=None):
    u = get_user_model().objects.create_user(email or f"staff{next(_seq)}@test.invalid", PASSWORD, is_staff=True)
    if role:
        u.groups.add(Group.objects.get(name=role))
    return u


def hr_admin():
    return user(HR_ADMIN)


def super_admin():
    return user(SUPER_ADMIN)


def domain(name="Test Domain", status=Domain.Status.ACTIVE):
    return Domain.objects.create(name=name, status=status)


def campaign(created_by, **kw):
    now = timezone.now()
    kw.setdefault("name", f"Test Batch {next(_seq)}")
    kw.setdefault("opens_at", now - dt.timedelta(days=1))
    kw.setdefault("closes_at", now + dt.timedelta(days=30))
    return Campaign.objects.create(created_by=created_by, **kw)


def student(**kw):
    n = next(_seq)
    data = dict(title="Ms.", full_name=f"Test Student {n}", gender="female", mobile="9000000000",
                reg_no=f"TEST-{n:05d}", degree="B.E. (Test Engineering)",
                college_name="Example Institute of Testing", college_location="Testville")
    data.update(kw)
    return Student.objects.create(**data)


def submission(camp, stu=None):
    stu = stu or student()
    return Submission.objects.create(campaign=camp, student=stu, raw_payload={"test": True},
                                     idempotency_key=f"idem-{next(_seq)}")


def internship(stu=None, dom=None, **kw):
    kw.setdefault("start_date", dt.date(2026, 6, 1))
    kw.setdefault("end_date", dt.date(2026, 8, 31))
    return Internship.objects.create(student=stu or student(), domain=dom or domain(f"Dom {next(_seq)}"), **kw)


def test_template_version(uploader, cert_type=CertificateType.EXPERIENCE, **kw):
    tpl, _ = CertificateTemplate.objects.get_or_create(cert_type=cert_type)
    content = b"TEST TEMPLATE - NOT FOR ISSUE"
    tv = TemplateVersion(template=tpl, version=kw.pop("version", tpl.versions.count() + 1),
                         source_filename="test-template.docx",
                         source_sha256=hashlib.sha256(content).hexdigest(),
                         source_format=TemplateVersion.SourceFormat.DOCX, uploaded_by=uploader, **kw)
    tv.source_file.save("test-template.docx", ContentFile(content), save=False)
    tv.save()
    return tv


def cert_request(intern, cert_type=CertificateType.EXPERIENCE):
    return CertificateRequest.objects.create(internship=intern, cert_type=cert_type)


def issuance(req, tv, by, **kw):
    return Issuance.objects.create(request=req, template_version=tv, created_by=by, **kw)
