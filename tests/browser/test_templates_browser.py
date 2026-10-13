"""Browser check of the Phase 5 template upload → preview → Super Admin activation flow (synthetic test file)."""
import pytest
from playwright.sync_api import expect

from apps.doctemplates.management.commands.make_test_template import build_docx, demo_paragraphs
from apps.doctemplates.models import TemplateVersion
from tests import factories as f


def login(page, site, user):
    page.goto(f"{site.url}/accounts/login/")
    page.fill("#id_username", user.email)
    page.fill("#id_password", f.PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(f"{site.url}/")


@pytest.fixture
def docx_file(tmp_path):
    path = tmp_path / "offer-test-template.docx"
    path.write_bytes(build_docx(demo_paragraphs("offer_letter")))
    return path


def test_upload_preview_and_super_admin_activation(page, site, browser, docx_file):
    hr, sa = f.hr_admin(), f.super_admin()
    login(page, site, hr)
    page.goto(f"{site.url}/hr/templates/upload/")
    page.select_option("#cert_type", "offer_letter")
    page.set_input_files("#file", str(docx_file))
    page.get_by_role("button", name="Upload as draft").click()
    expect(page.get_by_text("Uploaded as version 1 (draft)")).to_be_visible()
    expect(page.get_by_text("TEST TEMPLATE – NOT FOR ISSUE.")).to_be_visible()
    expect(page.get_by_text("PREVIEW — SYNTHETIC SAMPLE DATA — NOT A CERTIFICATE")).to_be_visible()
    expect(page.locator("mark[title=full_name]").first).to_have_text("Sample Student A")
    # HR has no activation controls, only submission.
    assert page.get_by_role("button", name="Approve and activate").count() == 0
    page.get_by_role("button", name="Submit for Super Admin review").click()
    expect(page.get_by_text("Waiting for a Super Admin")).to_be_visible()

    admin = browser.new_context()
    ap = admin.new_page()
    login(ap, site, sa)
    ap.goto(page.url)
    ap.get_by_role("button", name="Approve and activate").click()  # checkbox not ticked: browser blocks it
    assert TemplateVersion.objects.get().status == "pending_approval"
    ap.get_by_label("I compared this version and its preview").check()
    ap.get_by_role("button", name="Approve and activate").click()
    expect(ap.get_by_text("Template approved and activated.")).to_be_visible()
    admin.close()

    v = TemplateVersion.objects.get()
    assert (v.status, v.approved_by, v.is_production) == ("active", sa, False)
    assert not v.can_issue_production


def test_invalid_upload_shows_reasons(page, site, tmp_path):
    bad = tmp_path / "bad.docx"
    bad.write_bytes(build_docx(["{% if x %}{{ secret_value }}{% endif %}"]))
    login(page, site, f.hr_admin())
    page.goto(f"{site.url}/hr/templates/upload/")
    page.select_option("#cert_type", "experience")
    page.set_input_files("#file", str(bad))
    page.get_by_role("button", name="Upload as draft").click()
    expect(page.locator(".alert-error")).to_contain_text("template logic")
    expect(page.locator(".alert-error")).to_contain_text("not an approved placeholder")
    assert not TemplateVersion.objects.exists()
