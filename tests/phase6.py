"""Phase 6 test helpers: synthetic templates, a fake converter producing real (minimal) PDFs, and setup."""
import datetime as dt
import io
import re
import zipfile

from apps.certificates import approvals, pdf
from apps.doctemplates import services as template_services
from apps.doctemplates.management.commands.make_test_template import build_docx
from apps.internships import services as internship_services
from apps.registry.models import Student

from . import factories as f
from .phase4 import verified_registration

DATE_LINE = "{{ issue_day }}{{ issue_day_suffix }} {{ issue_month_year }}"
COMMON = "This is to certify {{ title_and_name }} (Reg.No: {{ reg_no }}), {{ degree }} of {{ college_name }}, " \
         "{{ college_location }}, from {{ start_month_year }} to {{ end_month_year }} in {{ domain }}."
PRONOUN_LINE = "We found {{ pronoun_object }} diligent. {{ pronoun_subject_cap }} {{ verb_has }} done well and " \
               "{{ pronoun_subject }} {{ verb_is }} able; {{ pronoun_subject }} often {{ verb_goes }} further " \
               "with {{ pronoun_possessive }} work."
PARAGRAPHS = {
    "experience": ["TEST TEMPLATE - NOT FOR ISSUE", DATE_LINE, COMMON, PRONOUN_LINE],
    "project": ["TEST TEMPLATE - NOT FOR ISSUE", DATE_LINE, COMMON, "Project: {{ project_title }}.", PRONOUN_LINE],
    "lor": ["TEST TEMPLATE - NOT FOR ISSUE", DATE_LINE, "About {{ title_and_name }}.",
            "{{ short_name }} worked with us from {{ start_month_year }} to {{ end_month_year }}.", PRONOUN_LINE],
    "offer_letter": ["TEST TEMPLATE - NOT FOR ISSUE", DATE_LINE, "{{ title_and_name }}", "Reg. No: {{ reg_no }}",
                     "{{ college_name }}, {{ college_location }}.", "Dear {{ short_name }},",
                     "Duration: {{ duration_text }} from {{ start_month_year }} to {{ end_month_year }}."],
}


def template_docx(cert_type, extra=None):
    return build_docx(PARAGRAPHS[cert_type] + (extra or []))


# ----------------------------------------------------------------- minimal real PDFs

def make_pdf(lines, *, pages=1, size=(595.28, 841.89), images=2):
    def esc(s):
        return s.encode("latin-1", "replace").replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")

    objects = []

    def add(body):
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    image = add(b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray "
                b"/BitsPerComponent 8 /Length 1 >>\nstream\n\x00\nendstream")
    pages_id = len(objects) + 1 + 2 * pages
    page_ids = []
    for n in range(pages):
        content = b"BT /F1 9 Tf 40 800 Td 11 TL " + b" ".join(b"(" + esc(l) + b") '" for l in lines) + b" ET"
        content += b"".join(b" q 10 0 0 10 %d 40 cm /Im%d Do Q" % (40 + 20 * k, k) for k in range(images))
        stream = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        xobjects = b" ".join(b"/Im%d %d 0 R" % (k, image) for k in range(images))
        page_ids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Contents %d 0 R "
                            b"/Resources << /Font << /F1 %d 0 R >> /XObject << %s >> >> >>"
                            % (pages_id, size[0], size[1], stream, font, xobjects)))
    assert add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % p for p in page_ids), pages)) \
        == pages_id
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    out.write(b"".join(b"%010d 00000 n \n" % o for o in offsets))
    out.write(b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog, xref))
    return out.getvalue()


def docx_lines(docx_bytes):
    xml = zipfile.ZipFile(io.BytesIO(docx_bytes)).read("word/document.xml").decode()
    return ["".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p)) for p in re.findall(r"<w:p\b.*?</w:p>", xml, re.S)]


def fake_converter(monkeypatch, **pdf_kwargs):
    """Replace the network converter with one that turns the filled .docx text into a real PDF."""
    calls = []

    def convert(docx_bytes):
        calls.append(docx_bytes)
        import html
        return make_pdf([html.unescape(l) for l in docx_lines(docx_bytes)], **pdf_kwargs)

    monkeypatch.setattr(pdf, "convert_docx_to_pdf", convert)
    return calls


# ----------------------------------------------------------------- setup

def active_template(cert_type, *, is_production=False, extra=None):
    """Upload (HR), submit, and activate (a different Super Admin) a synthetic template."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    uploader, approver = f.hr_admin(), f.super_admin()
    v = template_services.upload_version(
        cert_type, SimpleUploadedFile("t.docx", template_docx(cert_type, extra)), actor=uploader,
        is_production=is_production)
    template_services.submit_for_review(v.pk, actor=uploader)
    template_services.approve_and_activate(v.pk, actor=approver, confirmed_compared=True)
    v.refresh_from_db()
    return v


def approved_internship(cert_type, *, pronouns="she", title="Ms.", short_name=""):
    """A verified student with everything needed for `cert_type`, and that certificate approved."""
    hr, sa = f.hr_admin(), f.super_admin()
    sub, i = verified_registration(hr)
    Student.objects.filter(pk=i.student_id).update(pronouns=pronouns, title=title, short_name=short_name)
    if cert_type in ("experience", "lor"):
        internship_services.verify_completion(i.pk, actor=hr, decision="verified", start_date=i.start_date,
                                              end_date=i.end_date)
    if cert_type == "project":
        internship_services.verify_project(i.pk, actor=hr, decision="verified")
    if cert_type == "lor":
        internship_services.record_recommendation(i.pk, actor=hr, decision="recommended", mentor_name="Mentor Z",
                                                  mentor_feedback="Strong", performance_evaluation="Excellent")
    approvals.decide(i.pk, cert_type, "approve", actor=sa if cert_type == "lor" else hr)
    i.refresh_from_db()
    return i, hr, sa


TODAY = dt.date.today
