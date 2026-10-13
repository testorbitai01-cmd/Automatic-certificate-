"""Phase 6: values, safe filling, PDF checks, generation gates, issuance, downloads and permissions."""
import datetime as dt
import io
import threading
import zipfile

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.core.files.storage import default_storage
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from apps.certificates import docxfill, generation, pdf, values as vb
from apps.certificates.models import CertificateRequest, GeneratedFile, Issuance
from apps.core.models import AuditLog
from apps.doctemplates.management.commands.make_test_template import build_docx
from apps.doctemplates.models import TemplateVersion

from . import factories as f
from .phase6 import active_template, approved_internship, docx_lines, fake_converter, make_pdf

pytestmark = pytest.mark.django_db
TYPES = ["offer_letter", "experience", "project", "lor"]


# =========================== Values: titles, names, pronouns, dates ===========================

@pytest.mark.parametrize("day,suffix", [(1, "st"), (2, "nd"), (3, "rd"), (4, "th"), (11, "th"), (12, "th"),
                                        (13, "th"), (21, "st"), (22, "nd"), (23, "rd"), (24, "th"), (31, "st")])
def test_ordinal_suffixes(day, suffix):
    assert vb.ordinal_suffix(day) == suffix


def test_date_values_use_two_digit_day_like_the_office_documents():
    assert vb.date_values(dt.date(2026, 10, 5)) == {"issue_day": "05", "issue_day_suffix": "th",
                                                     "issue_month_year": "October 2026",
                                                     "issue_date": "05th October 2026"}


@pytest.mark.parametrize("start,end,text", [((2026, 10, 1), (2026, 12, 31), "3 months"),
                                            ((2026, 6, 1), (2026, 6, 30), "1 month"),
                                            ((2026, 11, 15), (2027, 2, 14), "4 months")])
def test_duration_text_from_dates(start, end, text):
    i, _, _ = approved_internship("offer_letter")
    i.start_date, i.end_date = dt.date(*start), dt.date(*end)
    values, _ = vb.build_values(i, "offer_letter", dt.date(2026, 1, 5))
    assert values["duration_text"] == text


@pytest.mark.parametrize("pronouns,expected", [
    ("he", ("he", "He", "him", "his", "has", "is", "goes")),
    ("she", ("she", "She", "her", "her", "has", "is", "goes")),
    ("they", ("they", "They", "them", "their", "have", "are", "go")),
])
def test_pronoun_forms_and_verb_agreement(pronouns, expected):
    i, _, _ = approved_internship("lor", pronouns=pronouns)
    v, problems = vb.build_values(i, "lor", dt.date(2026, 1, 5))
    got = tuple(v[k] for k in ("pronoun_subject", "pronoun_subject_cap", "pronoun_object", "pronoun_possessive",
                               "verb_has", "verb_is", "verb_goes"))
    assert got == expected and not any(k.startswith(("pronoun", "verb")) for k in problems)


def test_missing_pronouns_are_reported_never_inferred():
    i, _, _ = approved_internship("experience", pronouns="")
    assert i.student.gender == "female"
    values, problems = vb.build_values(i, "experience", dt.date(2026, 1, 5))
    assert "pronoun_object" in problems and "pronoun_object" not in values
    assert "never inferred" in problems["pronoun_object"]


@pytest.mark.parametrize("title,expected", [("Ms.", "Ms. "), ("Mx.", "Mx. "), ("", "")])
def test_title_and_name_including_no_title(title, expected):
    i, _, _ = approved_internship("offer_letter", title=title)
    v, _ = vb.build_values(i, "offer_letter", dt.date(2026, 1, 5))
    assert v["title_and_name"] == expected + i.student.full_name
    assert not v["title_and_name"].startswith(" ")


def test_preferred_name_with_full_name_fallback():
    i, _, _ = approved_internship("offer_letter", short_name="Sam")
    assert vb.build_values(i, "offer_letter", dt.date(2026, 1, 5))[0]["short_name"] == "Sam"
    i2, _, _ = approved_internship("offer_letter", short_name="")
    assert vb.build_values(i2, "offer_letter", dt.date(2026, 1, 5))[0]["short_name"] == i2.student.full_name


def test_reference_number_is_never_available():
    i, _, _ = approved_internship("offer_letter")
    _, problems = vb.build_values(i, "offer_letter", dt.date(2026, 1, 5))
    assert "reference_no" in problems


# =========================== Safe filling ===========================

def doc_with_runs(*runs, extra_paragraph=""):
    body = "<w:p>" + "".join(f'<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">{r}</w:t></w:r>' if i == 0 else
                             f'<w:r><w:t xml:space="preserve">{r}</w:t></w:r>' for i, r in enumerate(runs)) + "</w:p>"
    from .test_templates import raw_docx
    return raw_docx(body + extra_paragraph)


def text(docx_bytes):
    return "|".join(docx_lines(docx_bytes))


def test_fill_handles_placeholders_split_across_runs_keeping_first_run_format():
    filled, used = docxfill.fill_docx(doc_with_runs("Name: {", "{ full_", "name }", "}!"), {"full_name": "Asha K"})
    xml = zipfile.ZipFile(io.BytesIO(filled)).read("word/document.xml").decode()
    assert "Asha K" in text(filled) and used == {"full_name"}
    assert '<w:b/></w:rPr><w:t xml:space="preserve">Name: Asha K</w:t>' in xml


@pytest.mark.parametrize("value", ['<w:t>x</w:t>', "A & B <script>", "{{ reg_no }}", "{% if 1 %}", "]]>&amp;"])
def test_values_are_inserted_as_text_never_markup_or_code(value):
    try:
        filled, _ = docxfill.fill_docx(doc_with_runs("Hi {{ full_name }}"), {"full_name": value, "reg_no": "SECRET"})
    except docxfill.FillError:
        assert "{{" in value or "{%" in value  # template-like input is refused outright
        return
    from lxml import etree
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(filled)).read("word/document.xml"))
    texts = [t.text for t in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")]
    assert texts == [f"Hi {value}"] and "SECRET" not in text(filled)


def test_control_characters_are_stripped():
    filled, _ = docxfill.fill_docx(doc_with_runs("{{ full_name }}"), {"full_name": "A\x00B\x07C"})
    assert text(filled) == "ABC"


def test_unknown_or_unfilled_placeholders_fail():
    with pytest.raises(docxfill.FillError, match="No value"):
        docxfill.fill_docx(doc_with_runs("{{ full_name }}"), {})


def test_other_parts_copied_byte_for_byte():
    from .test_templates import raw_docx
    src = raw_docx("<w:p><w:r><w:t>{{ full_name }}</w:t></w:r></w:p>", extra={"word/media/image1.png": b"\x89PNGx"})
    filled, _ = docxfill.fill_docx(src, {"full_name": "Asha K"})
    a, b = zipfile.ZipFile(io.BytesIO(src)), zipfile.ZipFile(io.BytesIO(filled))
    assert a.namelist() == b.namelist()
    assert [n for n in a.namelist() if a.read(n) != b.read(n)] == ["word/document.xml"]


def test_corrupt_template_fails_cleanly():
    with pytest.raises(docxfill.FillError):
        docxfill.fill_docx(b"not a zip", {})


# =========================== PDF checks ===========================

def test_pdf_checks_accept_good_and_reject_bad_output():
    good = pdf.inspect_pdf(make_pdf(["Ms. Asha K"]))
    assert pdf.check_pdf(good, expected_values=["Ms. Asha K"], min_images=2) == []
    two = pdf.inspect_pdf(make_pdf(["x"], pages=2))
    assert "Expected 1 page" in " ".join(pdf.check_pdf(two, expected_values=[], min_images=0))
    letter = pdf.inspect_pdf(make_pdf(["x"], size=(612, 792)))
    assert "not A4" in " ".join(pdf.check_pdf(letter, expected_values=[], min_images=0))
    no_img = pdf.inspect_pdf(make_pdf(["x"], images=0))
    assert "image" in " ".join(pdf.check_pdf(no_img, expected_values=[], min_images=2))
    leftover = pdf.inspect_pdf(make_pdf(["Dear {{ short_name }}"]))
    assert "Unfilled placeholder" in " ".join(pdf.check_pdf(leftover, expected_values=[], min_images=0))
    missing = pdf.check_pdf(good, expected_values=["Somebody Else"], min_images=0)
    assert "missing from the PDF" in " ".join(missing)


def test_unreadable_pdf_raises_conversion_error():
    with pytest.raises(pdf.ConversionError):
        pdf.inspect_pdf(b"%PDF-1.4 garbage")


def test_converter_not_configured(settings):
    settings.PDF_CONVERTER_URL = ""
    with pytest.raises(pdf.ConversionError, match="not configured"):
        pdf.convert_docx_to_pdf(build_docx(["x"]))


def test_converter_unreachable(settings):
    settings.PDF_CONVERTER_URL = "http://127.0.0.1:9"  # nothing listens here
    settings.PDF_CONVERTER_TIMEOUT = 2
    with pytest.raises(pdf.ConversionError, match="unavailable"):
        pdf.convert_docx_to_pdf(build_docx(["x"]))


@pytest.mark.parametrize("data", [b"plain text", b"%PDF-1.4", b"PKbroken", b""])
def test_only_docx_is_sent_to_the_converter(settings, data):
    settings.PDF_CONVERTER_URL = "http://127.0.0.1:9"
    with pytest.raises(pdf.ConversionError, match="Only Word"):
        pdf.convert_docx_to_pdf(data)


# =========================== Generation gates ===========================

@pytest.mark.parametrize("cert_type", TYPES)
def test_each_type_generates_a_private_checked_draft_from_a_test_template(monkeypatch, cert_type):
    calls = fake_converter(monkeypatch)
    version = active_template(cert_type)
    i, hr, _ = approved_internship(cert_type, pronouns="they")
    issuance = generation.generate_draft(i.pk, cert_type, actor=hr, issue_date=dt.date(2026, 1, 5))
    assert (issuance.status, issuance.pdf_status, issuance.is_production) == ("not_issued", "generated", False)
    assert issuance.template_version == version and issuance.issue_date == dt.date(2026, 1, 5)
    data = issuance.file.file.open("rb").read()
    import hashlib
    assert hashlib.sha256(data).hexdigest() == issuance.file.sha256 and data.startswith(b"%PDF-")
    with pytest.raises(ValueError):
        issuance.file.file.url  # private storage: no URL
    sent = text(calls[0])
    assert "{{" not in sent
    if cert_type != "offer_letter":  # the Offer Letter has no pronouns
        assert "They have done well and they are able; they often go further with their work." in sent
    assert set(issuance.data_snapshot) == set(version.inspection["placeholders"])
    assert mail.outbox == []


def test_generation_audit_has_ids_and_hash_only(monkeypatch):
    fake_converter(monkeypatch)
    active_template("offer_letter")
    i, hr, _ = approved_internship("offer_letter")
    issuance = generation.generate_draft(i.pk, "offer_letter", actor=hr)
    entry = AuditLog.objects.get(action="certificate.generated")
    assert entry.metadata["sha256"] == issuance.file.sha256 and entry.metadata["template_version_id"]
    dump = str(entry.metadata)
    for private in [i.student.full_name, i.student.reg_no, i.student.college_name, "learner@gmail.com"]:
        assert private not in dump


def test_authentic_template_blocked_while_production_disabled(monkeypatch, settings):
    calls = fake_converter(monkeypatch)
    assert settings.CERT_PRODUCTION_ISSUANCE_ENABLED is False
    active_template("offer_letter", is_production=True)
    i, hr, _ = approved_internship("offer_letter")
    with pytest.raises(generation.GenerationError, match="Production issuance is disabled"):
        generation.generate_draft(i.pk, "offer_letter", actor=hr)
    assert calls == [] and not Issuance.objects.exists() and not GeneratedFile.objects.exists()


@pytest.mark.parametrize("problem", ["not_approved", "rejected", "no_template", "missing_pronouns", "future_date",
                                     "reference_no", "legacy_lor_approval"])
def test_generation_refused_without_prerequisites(monkeypatch, problem):
    calls = fake_converter(monkeypatch)
    cert_type = {"legacy_lor_approval": "lor", "missing_pronouns": "experience"}.get(problem, "offer_letter")
    if problem != "no_template":
        active_template(cert_type, extra=["Ref {{ reference_no }}"] if problem == "reference_no" else None)
    i, hr, sa = approved_internship(cert_type, pronouns="" if problem == "missing_pronouns" else "she")
    req = CertificateRequest.objects.get(internship=i, cert_type=cert_type)
    kwargs = {}
    if problem == "not_approved":
        from apps.certificates import approvals
        approvals.decide(i.pk, cert_type, "reset", actor=hr, reason="Recheck")
    elif problem == "rejected":
        from apps.certificates import approvals
        approvals.decide(i.pk, cert_type, "reject", actor=hr, reason="No")
    elif problem == "future_date":
        kwargs["issue_date"] = timezone.localdate() + dt.timedelta(days=1)
    elif problem == "legacy_lor_approval":
        CertificateRequest.objects.filter(pk=req.pk).update(decided_by=hr)  # approved by someone without authority
    with pytest.raises(generation.GenerationError):
        generation.generate_draft(i.pk, cert_type, actor=hr, **kwargs)
    assert not Issuance.objects.exists() and calls == []


def test_missing_data_message_names_the_field(monkeypatch):
    fake_converter(monkeypatch)
    active_template("experience")
    i, hr, _ = approved_internship("experience", pronouns="")
    with pytest.raises(generation.GenerationError) as exc:
        generation.generate_draft(i.pk, "experience", actor=hr)
    assert "him / her / them" in str(exc.value) and "correction request" in str(exc.value)


def test_permission_required_for_generation(monkeypatch):
    fake_converter(monkeypatch)
    active_template("offer_letter")
    i, _, _ = approved_internship("offer_letter")
    with pytest.raises(PermissionDenied):
        generation.generate_draft(i.pk, "offer_letter", actor=f.user(role=None))


def test_bad_pdf_output_is_refused_and_nothing_stored(monkeypatch):
    fake_converter(monkeypatch, pages=2)
    active_template("offer_letter")
    i, hr, _ = approved_internship("offer_letter")
    with pytest.raises(generation.GenerationError, match="failed its checks"):
        generation.generate_draft(i.pk, "offer_letter", actor=hr)
    assert not Issuance.objects.exists() and not GeneratedFile.objects.exists()


def test_converter_failure_is_reported(monkeypatch):
    def broken(_):
        raise pdf.ConversionError("The PDF converter is unavailable. Try again later.")
    monkeypatch.setattr(pdf, "convert_docx_to_pdf", broken)
    active_template("offer_letter")
    i, hr, _ = approved_internship("offer_letter")
    with pytest.raises(generation.GenerationError, match="unavailable"):
        generation.generate_draft(i.pk, "offer_letter", actor=hr)


def test_tampered_template_file_is_refused(monkeypatch):
    fake_converter(monkeypatch)
    version = active_template("offer_letter")
    with default_storage.open(version.source_file.name, "wb") as fh:
        fh.write(build_docx(["{{ title_and_name }} tampered"]))
    i, hr, _ = approved_internship("offer_letter")
    with pytest.raises(generation.GenerationError, match="checksum"):
        generation.generate_draft(i.pk, "offer_letter", actor=hr)


def test_duplicate_generation_refused_until_draft_cancelled(monkeypatch):
    fake_converter(monkeypatch)
    active_template("offer_letter")
    i, hr, _ = approved_internship("offer_letter")
    first = generation.generate_draft(i.pk, "offer_letter", actor=hr)
    with pytest.raises(generation.GenerationError, match="already been generated"):
        generation.generate_draft(i.pk, "offer_letter", actor=hr)
    from apps.core.workflow import WorkflowError
    with pytest.raises(WorkflowError, match="reason"):
        generation.cancel_draft(first.pk, actor=hr, reason="")
    generation.cancel_draft(first.pk, actor=hr, reason="Wrong issue date")
    second = generation.generate_draft(i.pk, "offer_letter", actor=hr)
    assert (first.version_no, second.version_no) == (1, 2)
    assert Issuance.objects.get(pk=first.pk).status == "cancelled"


# =========================== Issuance stays locked ===========================

def test_issue_refused_while_production_disabled(monkeypatch):
    fake_converter(monkeypatch)
    active_template("offer_letter")
    i, hr, sa = approved_internship("offer_letter")
    draft = generation.generate_draft(i.pk, "offer_letter", actor=hr)
    with pytest.raises(generation.GenerationError, match="Production issuance is disabled"):
        generation.issue(draft.pk, actor=sa)  # even a Super Admin, while production is disabled
    assert Issuance.objects.get(pk=draft.pk).status == "not_issued"


def test_test_template_drafts_can_never_be_issued_even_with_production_on(monkeypatch, settings):
    fake_converter(monkeypatch)
    active_template("offer_letter", is_production=False)
    i, hr, sa = approved_internship("offer_letter")
    draft = generation.generate_draft(i.pk, "offer_letter", actor=hr)
    settings.CERT_PRODUCTION_ISSUANCE_ENABLED = True
    with pytest.raises(generation.GenerationError, match="TEST templates can never be issued"):
        generation.issue(draft.pk, actor=sa)


def test_authentic_flow_when_production_is_enabled_in_a_test(monkeypatch, settings):
    """Exercises the gate logic only; production stays disabled in real configuration."""
    fake_converter(monkeypatch)
    settings.CERT_PRODUCTION_ISSUANCE_ENABLED = True
    active_template("offer_letter", is_production=True)
    i, hr, sa = approved_internship("offer_letter")
    draft = generation.generate_draft(i.pk, "offer_letter", actor=hr)
    issued = generation.issue(draft.pk, actor=sa)
    assert issued.status == "issued" and issued.issued_by == sa
    with pytest.raises(generation.GenerationError):
        generation.issue(draft.pk, actor=sa)  # cannot issue twice
    assert AuditLog.objects.filter(action="certificate.issued").count() == 1


def test_issue_refused_if_approval_withdrawn(monkeypatch, settings):
    fake_converter(monkeypatch)
    settings.CERT_PRODUCTION_ISSUANCE_ENABLED = True
    active_template("experience", is_production=True)
    i, hr, sa = approved_internship("experience")
    draft = generation.generate_draft(i.pk, "experience", actor=hr)
    CertificateRequest.objects.filter(internship=i, cert_type="experience").update(approval_status="pending",
                                                                                  decided_by=None, decided_at=None)
    with pytest.raises(generation.GenerationError, match="no longer approved"):
        generation.issue(draft.pk, actor=sa)


def test_lor_final_authority_unchanged():
    hr, sa = f.hr_admin(), f.super_admin()
    assert not hr.has_perm("certificates.approve_lor") and sa.has_perm("certificates.approve_lor")


# =========================== HTTP: generate, download, proof ===========================

def test_http_generate_and_private_download(client, monkeypatch):
    fake_converter(monkeypatch)
    active_template("offer_letter")
    i, hr, _ = approved_internship("offer_letter")
    client.force_login(hr)
    resp = client.post(reverse("internships:certificate_generate", kwargs={"pk": i.pk, "cert_type": "offer_letter"}),
                       {"issue_date": "2026-01-05"})
    assert resp.status_code == 302
    issuance = Issuance.objects.get()
    d = client.get(reverse("certificates:download", kwargs={"pk": issuance.pk}))
    assert d.status_code == 200 and d["Content-Type"] == "application/pdf"
    assert "no-store" in d["Cache-Control"]
    assert d["Content-Disposition"] == f'attachment; filename="offer_letter-{issuance.pk}-v1-DRAFT-not-issued.pdf"'
    assert i.student.full_name not in d["Content-Disposition"]
    assert AuditLog.objects.filter(action="certificate.downloaded").exists()
    page = client.get(reverse("internships:detail", kwargs={"pk": i.pk})).content.decode()
    assert "TEST template" in page and "Issued by a Super Admin." in page and 'value="issue"' not in page
    client.force_login(f.super_admin())  # Super Admin sees the Issue button, disabled while production is off
    page = client.get(reverse("internships:detail", kwargs={"pk": i.pk})).content.decode()
    assert 'value="issue" disabled title="Production issuance is disabled"' in page


def test_http_permissions(client, monkeypatch):
    fake_converter(monkeypatch)
    active_template("offer_letter")
    i, hr, _ = approved_internship("offer_letter")
    draft = generation.generate_draft(i.pk, "offer_letter", actor=hr)
    urls = [("post", reverse("internships:certificate_generate", kwargs={"pk": i.pk, "cert_type": "offer_letter"})),
            ("get", reverse("certificates:download", kwargs={"pk": draft.pk})),
            ("post", reverse("certificates:action", kwargs={"pk": draft.pk}))]
    for method, url in urls:
        assert getattr(client, method)(url, {"action": "cancel", "reason": "x"}).status_code == 302  # login
    client.force_login(f.user(role=None))
    for method, url in urls:
        assert getattr(client, method)(url, {"action": "cancel", "reason": "x"}).status_code == 403
    assert Issuance.objects.get(pk=draft.pk).status == "not_issued"


def test_http_proof_pdf_uses_sample_data_and_is_not_stored(client, monkeypatch):
    calls = fake_converter(monkeypatch)
    version = active_template("lor")
    client.force_login(f.hr_admin())
    resp = client.get(reverse("doctemplates:proof", kwargs={"pk": version.pk}) + "?pronouns=they")
    assert resp.status_code == 200 and resp.content.startswith(b"%PDF-")
    assert "PROOF-SAMPLE-DATA" in resp["Content-Disposition"]
    sent = text(calls[0])
    assert "Sample Student" in sent and "they have done well" in sent.lower()
    assert not Issuance.objects.exists() and not GeneratedFile.objects.exists()
    assert AuditLog.objects.get(action="template.proof_generated").metadata["pronouns"] == "they"
    client.force_login(f.user(role=None))
    assert client.get(reverse("doctemplates:proof", kwargs={"pk": version.pk})).status_code == 403


# =========================== Concurrency (PostgreSQL) ===========================

@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="true concurrency needs PostgreSQL row locks")
def test_concurrent_generation_creates_one_draft(pg_transactional_db, monkeypatch):
    fake_converter(monkeypatch)
    active_template("offer_letter")
    i, hr, _ = approved_internship("offer_letter")
    barrier = threading.Barrier(4)
    outcomes = []

    def worker():
        from django.db import connection as conn
        barrier.wait()
        try:
            generation.generate_draft(i.pk, "offer_letter", actor=hr)
            outcomes.append("ok")
        except generation.GenerationError:
            outcomes.append("refused")
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["ok", "refused", "refused", "refused"]
    assert Issuance.objects.count() == 1 and GeneratedFile.objects.count() == 1
