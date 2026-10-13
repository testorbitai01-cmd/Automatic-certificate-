"""Regression tests for issues found in the Phase 5 pre-commit review (synthetic files only)."""
import io

import pytest
from django.core.files.storage import default_storage
from django.urls import reverse

from apps.core.workflow import WorkflowError
from apps.doctemplates import docx, services
from apps.doctemplates.models import TemplateVersion

from . import factories as f
from .test_templates import good_docx, para, pending, raw_docx, upload, upload_file

pytestmark = pytest.mark.django_db
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
WP = 'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'


def inspect(data, cert_type="experience"):
    return docx.inspect_docx(io.BytesIO(data), cert_type)


# --- Damaged files must be refused, not crash ---------------------------------------

def corrupt_entry(data, name=b"word/document.xml"):
    buf = bytearray(data)
    start = buf.find(name) + len(name) + 10  # inside the compressed data of the local entry
    buf[start:start + 20] = b"\xff" * 20
    return bytes(buf)


def test_corrupted_entry_is_refused_cleanly():
    with pytest.raises(docx.TemplateFileError, match="damaged"):
        inspect(corrupt_entry(raw_docx(para("{{ full_name }}"))))


def test_corrupted_upload_over_http_gives_400_not_500(client):
    client.force_login(f.hr_admin())
    resp = client.post(reverse("doctemplates:upload"),
                       {"cert_type": "experience", "file": upload_file(corrupt_entry(good_docx()))})
    assert resp.status_code == 400 and "damaged" in resp.content.decode()
    assert not TemplateVersion.objects.exists()


# --- Template syntax hidden outside validated text (would be executed by docxtpl in Phase 6) -----

HIDDEN = {
    "footnotes": {"word/footnotes.xml": f'<?xml version="1.0"?><w:footnotes {W}>'
                                        f'{para("{% for x in y %}{{ x }}{% endfor %}")}</w:footnotes>'},
    "endnotes": {"word/endnotes.xml": f'<?xml version="1.0"?><w:endnotes {W}>{para("{{ reg_no }}")}</w:endnotes>'},
    "comments": {"word/comments.xml": f'<?xml version="1.0"?><w:comments {W}>{para("{{ secret }}")}</w:comments>'},
    "settings": {"word/settings.xml": f'<?xml version="1.0"?><w:settings {W}><w:docVar w:name="v" '
                                      'w:val="{{ secret }}"/></w:settings>'},
}


@pytest.mark.parametrize("where", sorted(HIDDEN))
def test_template_syntax_in_other_parts_is_refused(where):
    with pytest.raises(docx.TemplateFileError, match="outside the main text"):
        inspect(raw_docx(para("{{ full_name }}"), extra=HIDDEN[where]))


@pytest.mark.parametrize("body", [
    '<w:p><w:r><w:instrText xml:space="preserve"> MERGEFIELD {{ request }} </w:instrText></w:r></w:p>',
    '<w:p><w:fldSimple w:instr="{{ secret }}"><w:r><w:t>x</w:t></w:r></w:fldSimple></w:p>',
    '<w:p><w:del w:id="1" w:author="a"><w:r><w:delText>{% raw %}</w:delText></w:r></w:del></w:p>',
    f'<w:p><w:r><w:drawing><wp:inline {WP}><wp:docPr id="1" name="Logo" descr="{{{{ secret }}}}"/></wp:inline>'
    '</w:drawing></w:r></w:p>',
    '<w:p><w:r><w:instrText>{</w:instrText></w:r><w:r><w:instrText>% if 1 %</w:instrText></w:r></w:p>',
])
def test_template_syntax_in_field_codes_deletions_or_attributes_is_refused(body):
    with pytest.raises(docx.TemplateFileError, match="outside the main text"):
        inspect(raw_docx(para("{{ full_name }}") + body))


def test_ordinary_documents_with_fields_and_images_still_pass():
    body = (para("{{ full_name }}")
            + '<w:p><w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r></w:p>'
            + f'<w:p><w:r><w:drawing><wp:inline {WP}><wp:docPr id="1" name="Logo" descr="Company logo"/>'
              '</wp:inline></w:drawing></w:r></w:p>')
    assert inspect(raw_docx(body)).placeholders == ["full_name"]


# --- TEST templates must not displace the authentic template ------------------------------

def test_test_template_cannot_replace_active_authentic_template():
    hr, sa = f.hr_admin(), f.super_admin()
    real = pending(hr, is_production=True)
    services.approve_and_activate(real.pk, actor=sa, confirmed_compared=True)
    test = pending(hr, data=good_docx(text="Test {{ full_name }}"), is_production=False)
    with pytest.raises(WorkflowError, match="cannot replace the active authentic template"):
        services.approve_and_activate(test.pk, actor=sa, confirmed_compared=True)
    real.refresh_from_db()
    test.refresh_from_db()
    assert (real.status, test.status) == ("active", "pending_approval")
    # Deliberate path: deactivate the authentic version (with a reason) first.
    services.deactivate(real.pk, actor=sa, reason="Testing layout changes on staging")
    services.approve_and_activate(test.pk, actor=sa, confirmed_compared=True)
    assert TemplateVersion.objects.get(pk=test.pk).status == "active"


def test_authentic_template_may_replace_a_test_template():
    hr, sa = f.hr_admin(), f.super_admin()
    test = pending(hr, is_production=False)
    services.approve_and_activate(test.pk, actor=sa, confirmed_compared=True)
    real = pending(hr, data=good_docx(text="Authentic {{ full_name }}"), is_production=True)
    services.approve_and_activate(real.pk, actor=sa, confirmed_compared=True)
    assert TemplateVersion.objects.get(pk=test.pk).status == "retired"


# --- Request size and missing files -----------------------------------------------------

def test_oversized_request_refused_before_parsing(client, settings):
    settings.TEMPLATE_UPLOAD_MAX_BYTES = 1024
    client.force_login(f.hr_admin())
    big = upload_file(b"PK" + b"x" * (200 * 1024))
    resp = client.post(reverse("doctemplates:upload"), {"cert_type": "experience", "file": big})
    assert resp.status_code == 413 and "larger than" in resp.content.decode()
    assert not TemplateVersion.objects.exists()


def test_missing_stored_file_gives_404_on_download(client):
    hr = f.hr_admin()
    v = upload(hr)
    default_storage.delete(v.source_file.name)
    client.force_login(hr)
    assert client.get(reverse("doctemplates:download", kwargs={"pk": v.pk})).status_code == 404
    resp = client.get(reverse("doctemplates:version", kwargs={"pk": v.pk}))
    assert resp.status_code == 200  # page still renders; preview reports the problem
    assert "stored template file is missing" in resp.content.decode()
