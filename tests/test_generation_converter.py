"""Integration with the real DOCX->PDF converter (Gotenberg). Runs only when GRADTWIN_TEST_PDF_CONVERTER_URL
is set, e.g.  docker run -d --rm -p 127.0.0.1:3000:3000 gotenberg/gotenberg:8  and
$env:GRADTWIN_TEST_PDF_CONVERTER_URL = "http://127.0.0.1:3000". Uses synthetic templates only."""
import os

import pytest

from apps.certificates import docxfill, generation, pdf

from .phase6 import active_template, approved_internship, template_docx

CONVERTER = os.environ.get("GRADTWIN_TEST_PDF_CONVERTER_URL", "")
pytestmark = [pytest.mark.django_db,
              pytest.mark.skipif(not CONVERTER, reason="set GRADTWIN_TEST_PDF_CONVERTER_URL to test the real converter")]


@pytest.fixture(autouse=True)
def _converter(settings):
    settings.PDF_CONVERTER_URL = CONVERTER


@pytest.mark.parametrize("cert_type", ["offer_letter", "experience", "project", "lor"])
def test_real_conversion_produces_checked_a4_pdf(cert_type):
    active_template(cert_type)
    i, hr, _ = approved_internship(cert_type, pronouns="they", title="Mx.")
    issuance = generation.generate_draft(i.pk, cert_type, actor=hr)
    info = pdf.inspect_pdf(issuance.file.file.open("rb").read())
    assert info.page_count == 1 and abs(info.page_sizes[0][0] - 595.3) < 2
    assert f"Mx. {i.student.full_name}" in " ".join(info.text.split())
    assert "{{" not in info.text


def test_real_converter_rejects_garbage():
    with pytest.raises(pdf.ConversionError):
        pdf.convert_docx_to_pdf(b"this is not a docx file")


def test_injection_text_survives_as_literal_text():
    filled, _ = docxfill.fill_docx(template_docx("offer_letter"), {
        **{p: "x" for p in ["issue_day", "issue_day_suffix", "issue_month_year", "reg_no", "college_name",
                            "college_location", "short_name", "duration_text", "start_month_year", "end_month_year"]},
        "title_and_name": "<b>Bold?</b> & ]]> ‮evil"})
    text = pdf.inspect_pdf(pdf.convert_docx_to_pdf(filled)).text
    assert "<b>Bold?</b> &" in " ".join(text.split())
