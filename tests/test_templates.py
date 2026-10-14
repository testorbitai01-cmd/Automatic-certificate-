"""Phase 5: template upload, validation, preview, versioning, activation and permissions.

All templates here are synthetic .docx files built in code. No Gradtwin or third-party document is used.
"""
import io
import threading
import zipfile

import pytest
from django.contrib.auth.models import Permission
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from apps.certificates.models import GeneratedFile, Issuance
from apps.core.models import AuditLog
from apps.core.workflow import WorkflowError
from apps.doctemplates import docx, services
from apps.doctemplates.management.commands.make_test_template import (CONTENT_TYPES, ROOT_RELS, build_docx,
                                                                      demo_paragraphs)
from apps.doctemplates.models import CertificateTemplate, TemplateField, TemplateVersion

from . import factories as f

pytestmark = pytest.mark.django_db
V = TemplateVersion.Status
W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def para(*runs):
    return "<w:p>" + "".join(f'<w:r><w:t xml:space="preserve">{r}</w:t></w:r>' for r in runs) + "</w:p>"


def raw_docx(body_xml, extra=None, content_types=CONTENT_TYPES, rels=None, prolog=""):
    """Build a .docx from raw WordprocessingML body XML (for tricky/malicious cases)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", ROOT_RELS)
        zf.writestr("word/document.xml",
                    f'<?xml version="1.0" encoding="UTF-8"?>{prolog}<w:document {W_NS}><w:body>{body_xml}</w:body>'
                    '</w:document>')
        if rels:
            zf.writestr("word/_rels/document.xml.rels", rels)
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def good_docx(cert_type="experience", text="Certificate for {{ title }} {{ full_name }} in {{ domain }}"):
    return build_docx(["TEST TEMPLATE – NOT FOR ISSUE", text])


def upload_file(data, name="test-template.docx"):
    return SimpleUploadedFile(name, data, content_type="application/octet-stream")


def upload(actor, data=None, cert_type="experience", **kw):
    return services.upload_version(cert_type, upload_file(data or good_docx(cert_type), kw.pop("name", "t.docx")),
                                   actor=actor, **kw)


@pytest.fixture
def hr():
    return f.hr_admin()


@pytest.fixture
def sa():
    return f.super_admin()


def pending(hr, **kw):
    v = upload(hr, **kw)
    return services.submit_for_review(v.pk, actor=hr)


# =========================== Inspection / placeholder validation ===========================

def inspect(data, cert_type="experience"):
    return docx.inspect_docx(io.BytesIO(data), cert_type)


def test_four_certificate_types_exist():
    assert sorted(CertificateTemplate.objects.values_list("cert_type", flat=True)) == \
        ["experience", "lor", "offer_letter", "project"]


def test_placeholders_found_even_when_word_splits_runs():
    data = raw_docx(para("Dear {", "{ short_", "name }", "},") + para("Domain: {{domain}}"))
    result = inspect(data)
    assert result.placeholders == ["domain", "short_name"]


def test_headers_footers_and_text_boxes_are_read_once():
    body = ('<w:p><w:r><w:t>Outer {{ full_name }}</w:t></w:r><w:r><w:pict><w:txbxContent>'
            '<w:p><w:r><w:t>Inner {{ reg_no }}</w:t></w:r></w:p></w:txbxContent></w:pict></w:r></w:p>')
    header = f'<?xml version="1.0"?><w:hdr {W_NS}>{para("Header {{ issue_date }}")}</w:hdr>'
    result = inspect(raw_docx(body, extra={"word/header1.xml": header}))
    assert result.placeholders == ["full_name", "issue_date", "reg_no"]
    assert [label for label, _ in result.parts] == ["Header", "Body"]
    assert dict(result.parts)["Body"] == ["Outer {{ full_name }}", "Inner {{ reg_no }}"]


@pytest.mark.parametrize("text,fragment", [
    ("Hello {{ student_password }}", "not an approved placeholder"),
    ("{% if gender %}x{% endif %} {{ full_name }}", "template logic"),
    ("{# comment #} {{ full_name }}", "template logic"),
    ("{{ full_name|upper }}", "malformed placeholder"),
    ("{{ student.full_name }}", "malformed placeholder"),
    ("{{ full_name", "malformed placeholder"),
    ("{{ full_name }} }}", "closing"),
    ("{{ FULL_NAME }}", "malformed placeholder"),
    ("{{ __class__ }}", "malformed placeholder"),
    ("No placeholders here at all", "No placeholders were found"),
])
def test_invalid_placeholder_usage_rejected(text, fragment):
    with pytest.raises(docx.TemplateFileError) as exc:
        inspect(build_docx([text]))
    assert any(fragment in e for e in exc.value.errors), exc.value.errors


@pytest.mark.parametrize("name,cert_type,ok", [
    ("project_title", "project", True), ("project_title", "experience", False),
    ("mentor_name", "lor", True), ("mentor_name", "offer_letter", False),
    ("duration_months", "offer_letter", True), ("duration_months", "lor", False),
])
def test_type_specific_placeholders(name, cert_type, ok):
    data = build_docx([f"{{{{ full_name }}}} {{{{ {name} }}}}"])
    if ok:
        assert name in inspect(data, cert_type).placeholders
    else:
        with pytest.raises(docx.TemplateFileError, match="cannot be used in this certificate type"):
            inspect(data, cert_type)


# =========================== File safety ===========================

@pytest.mark.parametrize("make,fragment", [
    (lambda: b"%PDF-1.7 not a docx", "not a valid Word"),
    (lambda: b"PK\x03\x04garbage", "not a valid Word"),
    (lambda: raw_docx(para("{{ full_name }}"), content_types=CONTENT_TYPES.replace(
        "document.main+xml", "document.macroEnabled.main+xml")), "Macro-enabled"),
    (lambda: raw_docx(para("{{ full_name }}"), extra={"word/vbaProject.bin": b"x"}), "Macro-enabled"),
    (lambda: raw_docx(para("{{ full_name }}"), extra={"word/embeddings/oleObject1.bin": b"x"}), "Embedded objects"),
    (lambda: raw_docx(para("{{ full_name }}"), rels=(
        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="https://tracker.example/pixel.png" TargetMode="External"/></Relationships>')), "external content"),
    (lambda: raw_docx(para("{{ full_name }}"), prolog='<!DOCTYPE x [<!ENTITY a "b">]>'), "DOCTYPE"),
    (lambda: raw_docx(para("{{ full_name }}"), prolog='<!DOCTYPE x SYSTEM "file:///etc/passwd">'), "DOCTYPE"),
])
def test_unsafe_or_invalid_files_rejected(make, fragment):
    with pytest.raises(docx.TemplateFileError) as exc:
        inspect(make())
    assert any(fragment in e for e in exc.value.errors), exc.value.errors


def test_external_hyperlinks_are_allowed():
    rels = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
            'Target="https://www.example.org" TargetMode="External"/></Relationships>')
    assert inspect(raw_docx(para("{{ full_name }}"), rels=rels)).placeholders == ["full_name"]


def test_zip_bomb_rejected():
    bomb = raw_docx(para("{{ full_name }}"), extra={"word/media/huge.bin": b"\0" * (15 * 1024 * 1024)})
    assert len(bomb) < 100_000
    with pytest.raises(docx.TemplateFileError, match="zip bomb"):
        inspect(bomb)


def test_images_are_counted():
    assert inspect(raw_docx(para("{{ full_name }}"), extra={"word/media/image1.png": b"\x89PNG"})).image_count == 1


# =========================== Upload service ===========================

def test_upload_creates_private_draft_with_fields_and_audit(hr):
    v = upload(hr, name="Ms. Sample Person - EC.docx")
    assert (v.status, v.version, v.is_production, v.source_format) == (V.DRAFT, 1, False, "docx")
    assert v.inspection["placeholders"] == ["domain", "full_name", "title"]
    assert set(TemplateField.objects.filter(version=v).values_list("placeholder", flat=True)) == {
        "domain", "full_name", "title"}
    # Stored under a random name in private storage (no public URL).
    assert "Sample Person" not in v.source_file.name and v.source_file.name.startswith("templates/experience/")
    with pytest.raises(ValueError):
        default_storage.url(v.source_file.name)
    with pytest.raises(ValueError):
        v.source_file.url
    entry = AuditLog.objects.get(action="template.uploaded")
    assert entry.actor == hr and entry.metadata["sha256"] == v.source_sha256
    assert "Sample Person" not in str(entry.metadata) and "docx" not in str(entry.metadata)


def test_versions_increment_and_duplicates_rejected(hr):
    v1 = upload(hr)
    v2 = upload(hr, data=good_docx(text="Other wording {{ full_name }}"))
    assert (v1.version, v2.version) == (1, 2)
    with pytest.raises(WorkflowError, match="already version 1"):
        upload(hr)
    assert TemplateVersion.objects.count() == 2


@pytest.mark.parametrize("status", ["draft", "pending_approval", "active", "retired"])
def test_identical_file_refused_while_an_unrejected_version_exists(hr, sa, status):
    v = upload(hr)
    if status != "draft":
        services.submit_for_review(v.pk, actor=hr)
    if status in ("active", "retired"):
        services.approve_and_activate(v.pk, actor=sa, confirmed_compared=True)
    if status == "retired":
        services.deactivate(v.pk, actor=sa, reason="Out of use")
    assert TemplateVersion.objects.get(pk=v.pk).status == status
    with pytest.raises(WorkflowError, match="already version 1"):
        upload(hr, is_production=True)
    assert TemplateVersion.objects.count() == 1


def test_identical_file_allowed_again_after_rejection(hr, sa):
    """A file rejected because it was uploaded as TEST by mistake can be re-uploaded as authentic."""
    wrong = upload(hr, is_production=False)
    services.submit_for_review(wrong.pk, actor=hr)
    services.reject(wrong.pk, actor=sa, reason="Uploaded as TEST by mistake")
    fixed = upload(hr, is_production=True)
    assert (fixed.version, fixed.is_production, fixed.status) == (2, True, V.DRAFT)
    assert fixed.source_sha256 == wrong.source_sha256
    assert TemplateVersion.objects.get(pk=wrong.pk).status == V.REJECTED  # history kept, never reused
    # Only one non-rejected copy at a time: a third identical upload is refused again.
    with pytest.raises(WorkflowError, match="already version 2"):
        upload(hr, is_production=True)
    # The re-uploaded version goes through the normal review: activated by a different Super Admin.
    services.submit_for_review(fixed.pk, actor=hr)
    services.approve_and_activate(fixed.pk, actor=sa, confirmed_compared=True)
    assert TemplateVersion.objects.get(pk=fixed.pk).status == V.ACTIVE


@pytest.mark.parametrize("name", ["template.pdf", "template.doc", "template.docm", "template.png", "template"])
def test_only_docx_extension_accepted(hr, name):
    with pytest.raises(WorkflowError):
        services.upload_version("experience", upload_file(good_docx(), name), actor=hr)
    assert not TemplateVersion.objects.exists()


def test_size_limit(hr, settings):
    settings.TEMPLATE_UPLOAD_MAX_BYTES = 500
    with pytest.raises(WorkflowError, match="larger than"):
        upload(hr)


def test_invalid_template_not_stored(hr):
    before = set(_stored_files())
    with pytest.raises(WorkflowError, match="not an approved placeholder"):
        upload(hr, data=build_docx(["{{ secret_field }}"]))
    assert not TemplateVersion.objects.exists() and set(_stored_files()) == before


def _stored_files():
    try:
        return default_storage.listdir("templates/experience")[1]
    except FileNotFoundError:
        return []


def test_unknown_type_rejected(hr):
    with pytest.raises(WorkflowError):
        services.upload_version("diploma", upload_file(good_docx()), actor=hr)


# =========================== Review and activation ===========================

def test_full_lifecycle_and_supersession(hr, sa):
    v1 = pending(hr)
    services.approve_and_activate(v1.pk, actor=sa, confirmed_compared=True, note="Matches original")
    v2 = pending(hr, data=good_docx(text="Revised {{ full_name }}"))
    services.approve_and_activate(v2.pk, actor=sa, confirmed_compared=True)
    v1.refresh_from_db()
    v2.refresh_from_db()
    assert (v1.status, v2.status) == (V.RETIRED, V.ACTIVE)
    assert v1.close_reason == "Superseded by version 2." and v1.closed_by == sa
    assert TemplateVersion.objects.filter(status=V.ACTIVE).count() == 1
    actions = list(AuditLog.objects.filter(action__startswith="template.").order_by("id").values_list("action", flat=True))
    assert actions == ["template.uploaded", "template.submitted", "template.activated", "template.uploaded",
                       "template.submitted", "template.retired", "template.activated"]


def test_retired_version_can_be_reactivated(hr, sa):
    v1 = pending(hr)
    services.approve_and_activate(v1.pk, actor=sa, confirmed_compared=True)
    services.deactivate(v1.pk, actor=sa, reason="Wording under review")
    v1.refresh_from_db()
    assert v1.status == V.RETIRED and CertificateTemplate.objects.get(cert_type="experience").active_version() is None
    services.approve_and_activate(v1.pk, actor=sa, confirmed_compared=True)
    assert AuditLog.objects.filter(action="template.activated").order_by("id").last().metadata["reactivated"] is True


def test_activation_rules(hr, sa):
    draft = upload(hr)
    with pytest.raises(WorkflowError, match="Only versions pending approval"):
        services.approve_and_activate(draft.pk, actor=sa, confirmed_compared=True)
    services.submit_for_review(draft.pk, actor=hr)
    with pytest.raises(WorkflowError, match="Confirm that you compared"):
        services.approve_and_activate(draft.pk, actor=sa, confirmed_compared=False)
    with pytest.raises(WorkflowError, match="changed by someone else"):
        services.approve_and_activate(draft.pk, actor=sa, confirmed_compared=True, expected_status=V.DRAFT)
    assert TemplateVersion.objects.get(pk=draft.pk).status == V.PENDING_APPROVAL


def test_reject_and_deactivate_need_reasons(hr, sa):
    v = pending(hr)
    with pytest.raises(WorkflowError, match="reason"):
        services.reject(v.pk, actor=sa, reason=" ")
    services.reject(v.pk, actor=sa, reason="Signature block moved")
    v.refresh_from_db()
    assert (v.status, v.close_reason, v.closed_by) == (V.REJECTED, "Signature block moved", sa)
    with pytest.raises(WorkflowError):
        services.approve_and_activate(v.pk, actor=sa, confirmed_compared=True)  # rejected is final
    v2 = pending(hr, data=good_docx(text="New {{ full_name }}"))
    services.approve_and_activate(v2.pk, actor=sa, confirmed_compared=True)
    with pytest.raises(WorkflowError, match="reason"):
        services.deactivate(v2.pk, actor=sa, reason="")


def test_new_unreviewed_and_superseded_versions_are_never_production_usable(hr, sa, settings):
    settings.CERT_PRODUCTION_ISSUANCE_ENABLED = True
    draft = upload(hr, is_production=True)
    assert not draft.can_issue_production
    services.submit_for_review(draft.pk, actor=hr)
    draft.refresh_from_db()
    assert not draft.can_issue_production
    services.approve_and_activate(draft.pk, actor=sa, confirmed_compared=True)
    draft.refresh_from_db()
    assert draft.can_issue_production  # only now, and only because the switch is on in this test
    newer = pending(hr, data=good_docx(text="Newer {{ full_name }}"), is_production=True)
    services.approve_and_activate(newer.pk, actor=sa, confirmed_compared=True)
    draft.refresh_from_db()
    assert not draft.can_issue_production  # superseded
    test_tpl = pending(hr, data=good_docx(text="Test {{ full_name }}"), is_production=False)
    # Test templates never reach production: with the switch on they cannot even be activated.
    with pytest.raises(WorkflowError, match="production issuance is enabled"):
        services.approve_and_activate(test_tpl.pk, actor=sa, confirmed_compared=True)
    test_tpl.refresh_from_db()
    assert test_tpl.status == V.PENDING_APPROVAL and not test_tpl.can_issue_production


def test_production_switch_still_off_by_default(hr, sa, settings):
    assert settings.CERT_PRODUCTION_ISSUANCE_ENABLED is False
    v = pending(hr, is_production=True)
    services.approve_and_activate(v.pk, actor=sa, confirmed_compared=True)
    v.refresh_from_db()
    assert v.status == V.ACTIVE and not v.can_issue_production


def test_database_rules(hr):
    v = upload(hr)
    for update in [dict(status="pending_approval"), dict(status="rejected"), dict(status="retired"),
                   dict(status="active"), dict(source_sha256="")]:
        with pytest.raises(IntegrityError), transaction.atomic():
            TemplateVersion.objects.filter(pk=v.pk).update(**update)


# =========================== Permissions ===========================

@pytest.mark.parametrize("call", ["activate", "reject", "deactivate"])
def test_hr_admin_cannot_approve_reject_or_deactivate(hr, sa, call):
    v = pending(hr)
    if call == "deactivate":
        services.approve_and_activate(v.pk, actor=sa, confirmed_compared=True)
    status_before = TemplateVersion.objects.get(pk=v.pk).status
    fn = {"activate": lambda: services.approve_and_activate(v.pk, actor=hr, confirmed_compared=True),
          "reject": lambda: services.reject(v.pk, actor=hr, reason="x"),
          "deactivate": lambda: services.deactivate(v.pk, actor=hr, reason="x")}[call]
    with pytest.raises(PermissionDenied):
        fn()
    assert TemplateVersion.objects.get(pk=v.pk).status == status_before


@pytest.mark.parametrize("act,extra", [("activate", {"confirm_compared": "on"}), ("reject", {"reason": "x"}),
                                       ("deactivate", {"reason": "x"})])
def test_hr_crafted_http_requests_are_forbidden(client, hr, sa, act, extra):
    v = pending(hr)
    if act == "deactivate":
        services.approve_and_activate(v.pk, actor=sa, confirmed_compared=True)
    before = TemplateVersion.objects.get(pk=v.pk).status
    client.force_login(hr)
    resp = client.post(reverse("doctemplates:action", kwargs={"pk": v.pk}), {"action": act, **extra})
    assert resp.status_code == 403
    assert TemplateVersion.objects.get(pk=v.pk).status == before


def test_role_policy_unchanged():
    hr, sa = f.hr_admin(), f.super_admin()
    assert hr.has_perm("doctemplates.upload_template") and not hr.has_perm("doctemplates.approve_template")
    assert sa.has_perm("doctemplates.approve_template")
    # Phase 4 rule preserved.
    assert not hr.has_perm("certificates.approve_lor") and sa.has_perm("certificates.approve_lor")


def test_anonymous_roleless_and_view_only_access(client, hr):
    v = upload(hr)
    pages = [reverse("doctemplates:list"), reverse("doctemplates:upload"),
             reverse("doctemplates:version", kwargs={"pk": v.pk}), reverse("doctemplates:download", kwargs={"pk": v.pk})]
    for url in pages:
        assert client.get(url).status_code == 302
    assert client.post(reverse("doctemplates:action", kwargs={"pk": v.pk}), {"action": "submit"}).status_code == 302
    client.force_login(f.user(role=None))
    for url in pages:
        assert client.get(url).status_code == 403
    assert client.post(reverse("doctemplates:action", kwargs={"pk": v.pk}), {"action": "submit"}).status_code == 403
    viewer = f.user(role=None)
    viewer.user_permissions.add(Permission.objects.get(codename="view_templateversion"))
    client.force_login(viewer)
    assert client.get(pages[2]).status_code == 200 and client.get(pages[3]).status_code == 200
    assert client.get(pages[1]).status_code == 403
    assert client.post(pages[1], {"cert_type": "experience", "file": upload_file(good_docx())}).status_code == 403
    assert TemplateVersion.objects.get(pk=v.pk).status == V.DRAFT


# =========================== HTTP upload, preview, download ===========================

def test_http_upload_shows_all_errors(client, hr):
    client.force_login(hr)
    resp = client.post(reverse("doctemplates:upload"),
                       {"cert_type": "experience", "file": upload_file(build_docx(["{% if x %}{{ secret }}"]))})
    assert resp.status_code == 400
    html = resp.content.decode()
    assert "template logic" in html and "not an approved placeholder" in html


def test_preview_uses_labelled_synthetic_data_and_creates_nothing(client, hr):
    v = upload(hr, data=build_docx(["To {{ title }} {{ full_name }}", "Reg {{ reg_no }}"]))
    files_before = set(_stored_files())
    client.force_login(hr)
    html = client.get(reverse("doctemplates:version", kwargs={"pk": v.pk})).content.decode()
    assert "PREVIEW — SYNTHETIC SAMPLE DATA — NOT A CERTIFICATE" in html
    assert '<mark title="full_name">Sample Student A</mark>' in html and "SAMPLE-0001" in html
    assert "TEST TEMPLATE – NOT FOR ISSUE" in html
    assert not Issuance.objects.exists() and not GeneratedFile.objects.exists()
    assert mail.outbox == [] and set(_stored_files()) == files_before


def test_preview_escapes_template_text_and_values(client, hr, monkeypatch):
    from apps.doctemplates import placeholders as ph

    v = upload(hr, data=build_docx(['<script>alert("t")</script> {{ full_name }}']))
    original = ph.sample_values
    monkeypatch.setattr("apps.doctemplates.views.sample_values",
                        lambda t: {**original(t), "full_name": "<img src=x onerror=alert(1)>"})
    client.force_login(hr)
    html = client.get(reverse("doctemplates:version", kwargs={"pk": v.pk})).content.decode()
    assert "<script>alert" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in html and "&lt;img src=x onerror=alert(1)&gt;" in html


def test_download_is_private_audited_and_safely_named(client, hr):
    v = upload(hr, name="Ms. Sample Person - EC.docx")
    client.force_login(hr)
    resp = client.get(reverse("doctemplates:download", kwargs={"pk": v.pk}))
    assert resp.status_code == 200
    assert resp["Content-Disposition"] == 'attachment; filename="experience-template-v1.docx"'
    assert "no-store" in resp["Cache-Control"]
    assert b"".join(resp.streaming_content).startswith(b"PK")
    assert AuditLog.objects.filter(action="template.downloaded", actor=hr).exists()


def test_tampered_stored_file_is_detected(client, hr):
    v = upload(hr)
    with default_storage.open(v.source_file.name, "wb") as fh:
        fh.write(good_docx(text="Tampered {{ full_name }}"))
    client.force_login(hr)
    resp = client.get(reverse("doctemplates:version", kwargs={"pk": v.pk}))
    assert "does not match its recorded checksum" in resp.content.decode()
    assert resp.context["preview"] is None


# =========================== Audit privacy ===========================

def test_audit_entries_hold_no_filenames_content_or_personal_data(hr, sa):
    v = pending(hr, name="Ms. Sample Person - EC.docx", notes="Uploaded from the office PC")
    services.approve_and_activate(v.pk, actor=sa, confirmed_compared=True, note="Matches original")
    dump = str(list(AuditLog.objects.filter(action__startswith="template.").values_list("metadata", flat=True)))
    for leak in ["Sample Person", ".docx", "TEST TEMPLATE", "Certificate for", "Uploaded from the office PC"]:
        assert leak not in dump, leak


# =========================== Test-template command ===========================

def test_make_test_template_produces_an_acceptable_labelled_file(tmp_path, hr):
    out = tmp_path / "lor-test.docx"
    call_command("make_test_template", "lor", str(out), stdout=io.StringIO())
    data = out.read_bytes()
    result = inspect(data, "lor")
    assert "mentor_name" in result.placeholders and "project_title" not in result.placeholders
    assert dict(result.parts)["Body"][0] == "TEST TEMPLATE – NOT FOR ISSUE"
    assert "Gradtwin" not in " ".join(demo_paragraphs("lor")).replace("not a Gradtwin document", "")
    upload(hr, data=data, cert_type="lor")


# =========================== Concurrency (PostgreSQL) ===========================

@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="true concurrency needs PostgreSQL row locks")
def test_concurrent_activations_leave_exactly_one_active(pg_transactional_db):
    hr = f.hr_admin()
    admins = [f.super_admin() for _ in range(3)]
    versions = [pending(hr, data=good_docx(text=f"Variant {n} {{{{ full_name }}}}")) for n in range(3)]
    barrier = threading.Barrier(3)
    errors = []

    def worker(n):
        from django.db import connection as conn
        barrier.wait()
        try:
            services.approve_and_activate(versions[n].pk, actor=admins[n], confirmed_compared=True)
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion below
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert TemplateVersion.objects.filter(status=V.ACTIVE).count() == 1
    assert TemplateVersion.objects.filter(status=V.RETIRED).count() == 2
    assert AuditLog.objects.filter(action="template.activated").count() == 3


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="true concurrency needs PostgreSQL row locks")
def test_concurrent_uploads_get_distinct_version_numbers(pg_transactional_db):
    hr = f.hr_admin()
    barrier = threading.Barrier(4)
    errors = []

    def worker(n):
        from django.db import connection as conn
        barrier.wait()
        try:
            upload(hr, data=good_docx(text=f"Parallel {n} {{{{ full_name }}}}"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(TemplateVersion.objects.values_list("version", flat=True)) == [1, 2, 3, 4]
