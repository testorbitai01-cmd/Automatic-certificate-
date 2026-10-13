"""Browser checks for the Phase 4 HR review, correction link and approval screens (synthetic data only)."""
import pytest
from playwright.sync_api import expect

from apps.certificates.models import CertificateRequest
from apps.registry.models import CorrectionRequest, Submission
from tests import factories as f
from tests.phase4 import registration

from .conftest import MOBILE


def login(page, site, user, expect_dashboard=True):
    page.goto(f"{site.url}/accounts/login/")
    page.fill("#id_username", user.email)
    page.fill("#id_password", f.PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(f"{site.url}/")
    if expect_dashboard:
        expect(page.get_by_role("heading", name="Dashboard")).to_be_visible()


@pytest.fixture
def hr(site):
    return f.hr_admin()


def test_review_correction_and_approval_round_trip(page, site, hr, browser):
    sub, _ = registration(full_name="Browser Review A", email="browser.review@gmail.com")
    registration(full_name="Unrelated Learner B", email="unrelated@gmail.com", reg_no="UNREL-1")
    login(page, site, hr)

    # List, search and open.
    page.goto(f"{site.url}/hr/submissions/")
    expect(page.get_by_text("2 submissions")).to_be_visible()
    page.fill("#q", "Browser Review")
    page.get_by_role("button", name="Filter").click()
    expect(page.get_by_text("1 submission")).to_be_visible()
    page.get_by_role("link", name="Ms. Browser Review A").click()
    expect(page.get_by_role("heading", name="Ms. Browser Review A")).to_be_visible()

    # Rejecting without a reason is stopped in the browser.
    page.get_by_role("button", name="Reject").click()
    assert Submission.objects.get(pk=sub.pk).status == "submitted"

    # Request a name correction and receive the one-time link.
    form = page.locator("#correction-form")
    form.get_by_label("Title and name").check()
    form.locator("textarea[name=reason]").fill("Please write your name as on your college ID")
    form.get_by_role("button", name="Request correction and create link").click()
    expect(page.get_by_text("It is shown only once")).to_be_visible()
    correction_link = page.locator("#correction-link").inner_text()

    # Student (signed out, phone-sized) opens the link and sees only the requested fields.
    student = browser.new_context(viewport=MOBILE)
    sp = student.new_page()
    sp.goto(correction_link)
    expect(sp.get_by_text("Please write your name as on your college ID")).to_be_visible()
    expect(sp.locator("#id_full_name")).to_have_value("Browser Review A")
    assert sp.locator("#id_mobile").count() == 0 and sp.locator("#id_email").count() == 0
    assert sp.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    sp.fill("#id_full_name", "browser review corrected")
    sp.click("#submit-button")
    expect(sp.locator(".suggestion")).to_contain_text("Browser Review Corrected")
    sp.locator(".suggestion").get_by_role("button", name="Use this").click()
    sp.click("#submit-button")
    expect(sp.get_by_text("Your corrected details have been submitted")).to_be_visible()
    sp.goto(correction_link)  # single use
    expect(sp.get_by_role("heading", name="This correction link is not available")).to_be_visible()
    student.close()

    # HR sees the change and approves.
    page.goto(f"{site.url}/hr/submissions/{sub.pk}/")
    expect(page.get_by_role("cell", name="Browser Review Corrected")).to_be_visible()
    expect(page.get_by_text("Corrected by student")).to_be_visible()
    # The correction re-ran duplicate detection (both synthetic learners share a mobile number),
    # so approval is blocked until HR records a duplicate decision with a reason.
    page.get_by_role("button", name="Approve details").click()
    expect(page.get_by_text("Resolve the possible-duplicate flags before approving")).to_be_visible()
    row = page.locator("#duplicates + .table-wrap tr", has_text="Unrelated Learner B")
    row.locator("select[name=resolution]").select_option("not_duplicate")
    row.locator("input[name=reason]").fill("Different students; shared family phone")
    row.get_by_role("button", name="Save").click()
    expect(page.get_by_text("Duplicate decision recorded.")).to_be_visible()
    page.get_by_role("button", name="Approve details").click()
    expect(page.get_by_text("Registration details verified.")).to_be_visible()
    assert Submission.objects.get(pk=sub.pk).status == "verified"
    assert CorrectionRequest.objects.get().status == "completed"

    # Internship page: offer letter approvable now; experience/LOR blocked until verified.
    page.get_by_role("link", name="Completion, recommendation and certificates →").click()
    offer = page.locator("#cert-offer_letter")
    expect(offer.get_by_text("All requirements met")).to_be_visible()
    expect(page.locator("#cert-experience").get_by_role("button", name="Approve")).to_be_disabled()
    expect(page.locator("#cert-lor").get_by_text("No positive recommendation decision")).to_be_visible()
    offer.get_by_role("button", name="Approve").click()
    expect(page.get_by_text("Certificate decision recorded.")).to_be_visible()

    page.locator("#completion").locator("xpath=following-sibling::div[1]").get_by_role(
        "button", name="Completion verified").click()
    expect(page.locator("#cert-experience").get_by_role("button", name="Approve")).to_be_enabled()
    page.locator("#cert-experience").get_by_role("button", name="Approve").click()
    expect(page.locator("#cert-experience").get_by_text("Approved")).to_be_visible()

    statuses = dict(CertificateRequest.objects.values_list("cert_type", "approval_status"))
    assert statuses == {"offer_letter": "approved", "experience": "approved"}


def test_roleless_staff_cannot_open_review_pages(page, site):
    sub, internship = registration()
    login(page, site, f.user(role=None), expect_dashboard=False)
    for path in ["/hr/submissions/", f"/hr/submissions/{sub.pk}/", f"/hr/internships/{internship.pk}/"]:
        assert page.goto(site.url + path).status == 403, path
