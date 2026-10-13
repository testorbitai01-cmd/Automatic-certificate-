"""Browser checks for the public registration form and HR access (synthetic data only)."""
import datetime as dt
import re

import pytest
from django.utils import timezone
from playwright.sync_api import expect

from apps.registry.models import Campaign, Domain, Submission
from tests import factories as f

from .conftest import DESKTOP, MOBILE

SUCCESS = "Thank you for your response. Your details have been submitted successfully."

VALID = {
    "full_name": "Browser Tester A",
    "mobile": "9000000101",
    "reg_no": "BROWSER-001",
    "email": "browser.tester.a@gmail.com",
    "degree": "B.E. (Computer Science and Engineering)",
    "college_name": "Example Institute of Engineering and Technology",
    "college_location": "Testville",
    "start_date": "2026-06-01",
    "end_date": "2026-08-31",
    "project_title": "Hotel Management System",
}


@pytest.fixture
def campaign(site):
    return f.campaign(f.hr_admin(), name="Browser Test Batch")


def form_url(site, campaign):
    return f"{site.url}/register/{campaign.public_token}/"


def fill(page, **overrides):
    data = {**VALID, **overrides}
    page.select_option("#id_title", "Ms.")
    page.check("input[name=gender][value=female]")
    page.check("input[name=pronouns][value=she]")
    page.select_option("#id_domain", label="Data Science")
    for name, value in data.items():
        page.fill(f"#id_{name}", value)


# --- Layout and field behaviour -------------------------------------------------

@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
def test_form_loads_and_fits_viewport(page, site, campaign, viewport):
    page.set_viewport_size(viewport)
    page.goto(form_url(site, campaign))
    expect(page.get_by_role("heading", name="Browser Test Batch")).to_be_visible()
    for name in ["title", "full_name", "mobile", "reg_no", "email", "alternate_email", "degree", "college_name",
                 "college_location", "domain", "start_date", "end_date", "project_title", "feedback"]:
        expect(page.locator(f"#id_{name}")).to_be_visible()
        expect(page.locator(f"label[for=id_{name}]")).to_be_visible()
    expect(page.locator("input[name=gender]")).to_have_count(3)
    # Required fields are marked; optional ones are not.
    expect(page.locator("label[for=id_full_name] .required-mark")).to_have_count(1)
    expect(page.locator("label[for=id_alternate_email] .required-mark")).to_have_count(0)
    expect(page.locator("label[for=id_feedback] .required-mark")).to_have_count(0)
    # No horizontal scrolling on either viewport.
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    # Hidden-until-needed fields start hidden.
    expect(page.locator("#field-gender_detail")).to_be_hidden()
    expect(page.locator("#field-requested_domain")).to_be_hidden()


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
def test_successful_submission_shows_confirmation(page, site, campaign, viewport):
    page.set_viewport_size(viewport)
    page.goto(form_url(site, campaign))
    fill(page, reg_no=f"BROWSER-{viewport['width']}")
    page.click("#submit-button")
    expect(page.get_by_text(SUCCESS)).to_be_visible()
    expect(page.get_by_text("pending HR review")).to_be_visible()
    assert page.url == f"{site.url}/register/{campaign.public_token}/submitted/"
    html = page.content()
    for private in [VALID["full_name"], VALID["mobile"], f"BROWSER-{viewport['width']}", VALID["email"]]:
        assert private not in html and private not in page.url
    sub = Submission.objects.get(campaign=campaign)
    assert sub.status == Submission.Status.SUBMITTED


def test_required_fields_block_submit_and_optional_fields_may_be_blank(page, site, campaign):
    page.goto(form_url(site, campaign))
    page.click("#submit-button")
    # The browser stops the submission at the first empty required field.
    assert page.url == form_url(site, campaign)
    assert page.evaluate("document.activeElement.id") == "id_title"
    assert not Submission.objects.exists()
    # Alternative email and feedback left blank: accepted.
    fill(page)
    expect(page.locator("#id_alternate_email")).to_have_value("")
    expect(page.locator("#id_feedback")).to_have_value("")
    page.click("#submit-button")
    expect(page.get_by_text(SUCCESS)).to_be_visible()


def test_others_reveals_gender_detail_field(page, site, campaign):
    page.goto(form_url(site, campaign))
    detail = page.locator("#field-gender_detail")
    expect(detail).to_be_hidden()
    page.check("input[name=gender][value=others]")
    expect(detail).to_be_visible()
    page.fill("#id_gender_detail", "Non-binary")
    page.check("input[name=gender][value=male]")
    expect(detail).to_be_hidden()
    expect(page.locator("#id_gender_detail")).to_have_value("")


def test_name_suggestion_applies_only_when_chosen(page, site, campaign):
    page.goto(form_url(site, campaign))
    fill(page, full_name="browser tester a")
    page.click("#submit-button")
    suggestion = page.locator(".suggestion", has_text="Browser Tester A")
    expect(suggestion).to_be_visible()
    expect(page.get_by_text("Please capitalize each part of your name correctly")).to_be_visible()
    # Not applied automatically: the field still holds what the student typed.
    expect(page.locator("#id_full_name")).to_have_value("browser tester a")
    assert not Submission.objects.exists()
    # Other values were preserved.
    expect(page.locator("#id_reg_no")).to_have_value(VALID["reg_no"])
    suggestion.get_by_role("button", name="Use this").click()
    expect(page.locator("#id_full_name")).to_have_value("Browser Tester A")
    page.click("#submit-button")
    expect(page.get_by_text(SUCCESS)).to_be_visible()
    assert Submission.objects.get().student.full_name == "Browser Tester A"


def test_invalid_dates_are_explained_and_not_submitted(page, site, campaign):
    page.goto(form_url(site, campaign))
    fill(page, start_date="2026-08-01", end_date="2026-07-15")
    expect(page.locator("#period-preview")).to_have_text("The end date cannot be earlier than the start date.")
    page.click("#submit-button")
    assert page.url == form_url(site, campaign)
    assert not Submission.objects.exists()
    page.fill("#id_end_date", "2026-10-31")
    expect(page.locator("#period-preview")).to_have_text("Internship period: August 2026 to October 2026")


def test_server_side_errors_show_next_to_fields(page, site, campaign):
    page.goto(form_url(site, campaign))
    fill(page, email="browser.tester@yahoo.com", degree="mca (general)")
    page.click("#submit-button")
    expect(page.locator("#field-email .field-error")).to_have_text("Please use a Gmail address (ending in @gmail.com).")
    expect(page.locator("#field-degree .suggestion")).to_contain_text("MCA (General)")
    expect(page.locator("#id_email")).to_have_attribute("aria-invalid", "true")
    assert not Submission.objects.exists()


def test_mobile_number_checked_in_browser(page, site, campaign):
    page.goto(form_url(site, campaign))
    fill(page, mobile="12345")
    page.click("#submit-button")
    assert page.url == form_url(site, campaign)
    assert "exactly 10 digits" in page.evaluate("document.getElementById('id_mobile').validationMessage")
    assert not Submission.objects.exists()


def test_domain_request_stays_pending_until_hr_approves(page, site, campaign):
    page.goto(form_url(site, campaign))
    fill(page)
    page.select_option("#id_domain", value="__request__")
    expect(page.locator("#field-requested_domain")).to_be_visible()
    expect(page.get_by_text("New domains are reviewed by HR")).to_be_visible()
    page.fill("#id_requested_domain", "Browser Test Robotics")
    page.click("#submit-button")
    expect(page.get_by_text(SUCCESS)).to_be_visible()
    domain = Domain.objects.get(name="Browser Test Robotics")
    assert domain.status == Domain.Status.PENDING

    # Not offered to other students while pending.
    page.goto(form_url(site, campaign))
    assert page.locator("#id_domain option", has_text="Browser Test Robotics").count() == 0

    # HR approves it through the UI.
    hr = f.hr_admin()
    page.goto(f"{site.url}/accounts/login/")
    page.fill("#id_username", hr.email)
    page.fill("#id_password", f.PASSWORD)
    page.click("button[type=submit]")
    page.goto(f"{site.url}/hr/domains/")
    row = page.locator("tr", has_text="Browser Test Robotics")
    expect(row.get_by_text("Pending approval")).to_be_visible()
    row.get_by_role("button", name="Approve").click()
    expect(page.locator("tr", has_text="Browser Test Robotics").get_by_text("Active")).to_be_visible()
    domain.refresh_from_db()
    assert domain.status == Domain.Status.ACTIVE and domain.approved_by == hr

    page.goto(form_url(site, campaign))
    assert page.locator("#id_domain option", has_text="Browser Test Robotics").count() == 1


def test_repeated_clicks_create_one_submission(page, site, campaign):
    page.goto(form_url(site, campaign))
    fill(page)
    posts = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
    # Click five times in the same tick; the page disables the button after the first click.
    page.evaluate("""() => { const b = document.getElementById('submit-button');
                             for (let i = 0; i < 5; i++) b.click(); }""")
    expect(page.get_by_text(SUCCESS)).to_be_visible()
    assert posts == [form_url(site, campaign)]  # exactly one POST left the browser
    assert Submission.objects.filter(campaign=campaign).count() == 1
    # Server-side de-duplication of a repeated POST with the same form token is covered in
    # tests/test_registration.py::test_double_click_same_token_creates_one_submission.


# --- Campaign states and access control -------------------------------------------

@pytest.mark.parametrize("state,text", [
    ("expired", "Registration has closed"),
    ("inactive", "This registration link is no longer active"),
    ("scheduled", "Registration has not opened yet"),
])
def test_closed_campaign_messages(page, site, state, text):
    now = timezone.now()
    kw = {"expired": dict(opens_at=now - dt.timedelta(days=5), closes_at=now - dt.timedelta(minutes=1)),
          "inactive": dict(status=Campaign.Status.INACTIVE),
          "scheduled": dict(opens_at=now + dt.timedelta(days=1), closes_at=now + dt.timedelta(days=5))}[state]
    camp = f.campaign(f.hr_admin(), **kw)
    page.goto(form_url(site, camp))
    expect(page.get_by_role("heading", name=text)).to_be_visible()
    assert page.locator("#registration-form").count() == 0


def test_invalid_link_message(page, site):
    page.goto(f"{site.url}/register/this-is-not-a-real-registration-token/")
    expect(page.get_by_role("heading", name="This registration link is not valid")).to_be_visible()


def test_unauthorized_users_cannot_open_hr_pages(page, site):
    camp = f.campaign(f.hr_admin())
    for path in ["/hr/campaigns/", f"/hr/campaigns/{camp.pk}/", "/hr/domains/", "/hr/campaigns/new/"]:
        page.goto(site.url + path)
        assert re.search(r"/accounts/login/\?next=", page.url), path
        expect(page.get_by_role("heading", name="Staff sign in")).to_be_visible()

    # Signed in but without an HR role: forbidden.
    nobody = f.user(role=None)
    page.goto(f"{site.url}/accounts/login/")
    page.fill("#id_username", nobody.email)
    page.fill("#id_password", f.PASSWORD)
    page.click("button[type=submit]")
    response = page.goto(f"{site.url}/hr/campaigns/")
    assert response.status == 403
    assert page.locator("#registration-url").count() == 0


def test_hr_sees_qr_link_containing_only_the_token(page, site):
    hr = f.hr_admin()
    camp = f.campaign(hr)
    page.goto(f"{site.url}/accounts/login/")
    page.fill("#id_username", hr.email)
    page.fill("#id_password", f.PASSWORD)
    page.click("button[type=submit]")
    page.goto(f"{site.url}/hr/campaigns/{camp.pk}/")
    link = page.locator("#registration-url").inner_text()
    assert link == f"{site.url}/register/{camp.public_token}/"
    expect(page.locator("svg").first).to_be_visible()
    # The QR link opens the student form in a fresh, signed-out context.
    other = page.context.browser.new_context()
    student_page = other.new_page()
    student_page.goto(link)
    expect(student_page.locator("#registration-form")).to_be_visible()
    other.close()
