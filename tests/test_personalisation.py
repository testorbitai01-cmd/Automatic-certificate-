"""Phase 6: titles (incl. Mx. and no title), preferred name and pronouns on the form and via corrections."""
import pytest
from django.urls import reverse

from apps.core.models import Setting
from apps.registry import review
from apps.registry.models import CorrectionRequest, Student

from . import factories as f
from .phase4 import registration
from .test_registration import payload, url

pytestmark = pytest.mark.django_db


@pytest.fixture
def campaign():
    return f.campaign(f.hr_admin())


def test_form_offers_titles_mx_and_no_title_and_pronoun_choices(client, campaign):
    form = client.get(url(campaign)).context["form"]
    assert [c[0] for c in form.fields["title"].choices] == ["", "Mr.", "Ms.", "Mrs.", "Mx.", "__none__"]
    assert [c[0] for c in form.fields["pronouns"].choices] == ["he", "she", "they"]


@pytest.mark.parametrize("posted,stored", [("Mx.", "Mx."), ("__none__", ""), ("Mrs.", "Mrs.")])
def test_title_choices_are_stored(client, campaign, posted, stored):
    assert client.post(url(campaign), payload(title=posted)).status_code == 302
    assert Student.objects.get().title == stored


def test_no_title_can_be_disabled_by_policy(client, campaign):
    Setting.objects.create(key="registration.allow_no_title", value=False)
    resp = client.post(url(campaign), payload(title="__none__"))
    assert "title" in resp.context["form"].errors


@pytest.mark.parametrize("value", ["", "Dr.", "Prof."])
def test_unconfigured_titles_rejected(client, campaign, value):
    resp = client.post(url(campaign), payload(title=value))
    assert "title" in resp.context["form"].errors


def test_pronouns_are_required_and_never_inferred(client, campaign):
    resp = client.post(url(campaign), payload(pronouns=""))
    assert "pronouns" in resp.context["form"].errors
    for gender in ("male", "female", "others"):
        resp = client.post(url(campaign), payload(gender=gender, pronouns=""))
        assert "pronouns" in resp.context["form"].errors
    assert not Student.objects.exists()


@pytest.mark.parametrize("pronouns", ["he", "she", "they"])
def test_pronouns_stored_independently_of_gender(client, campaign, pronouns):
    client.post(url(campaign), payload(gender="others", gender_detail="", pronouns=pronouns))
    assert Student.objects.get().pronouns == pronouns


def test_invalid_pronoun_value_rejected(client, campaign):
    resp = client.post(url(campaign), payload(pronouns="it"))
    assert "pronouns" in resp.context["form"].errors


def test_preferred_name_optional_validated_and_stored(client, campaign):
    client.post(url(campaign), payload(preferred_name=""))
    assert Student.objects.get().short_name == ""
    resp = client.post(url(campaign), payload(preferred_name="sam", reg_no="OTHER-9", mobile="9333333333",
                                              email="other9@gmail.com"))
    assert resp.context["form"].suggestions["preferred_name"] == "Sam"
    client.post(url(campaign), payload(preferred_name="Sam", reg_no="OTHER-9", mobile="9333333333",
                                       email="other9@gmail.com"))
    assert Student.objects.get(reg_no="OTHER-9").short_name == "Sam"


def test_preferred_name_and_pronouns_correctable_by_student_link(client):
    hr = f.hr_admin()
    sub, _ = registration()
    _, raw = review.request_correction(sub.pk, actor=hr, groups=["preferred_name", "pronouns"],
                                       reason="Please confirm your preferred name and pronouns")
    form = client.get(f"/correct/{raw}/").context["form"]
    assert set(form.fields) == {"preferred_name", "pronouns"}
    client.post(f"/correct/{raw}/", {"preferred_name": "Ash", "pronouns": "they"})
    s = Student.objects.get(pk=sub.student_id)
    assert (s.short_name, s.pronouns) == ("Ash", "they")
    assert CorrectionRequest.objects.get().changes["pronouns"] == {"old": "", "new": "they"}


def test_resubmitting_no_title_unchanged_records_no_change(client):
    hr = f.hr_admin()
    sub, _ = registration(title="")
    _, raw = review.request_correction(sub.pk, actor=hr, groups=["name"], reason="Please confirm")
    student = Student.objects.get(pk=sub.student_id)
    client.post(f"/correct/{raw}/", {"title": "__none__", "full_name": student.full_name})
    assert CorrectionRequest.objects.get().changes == {}
    assert Student.objects.get(pk=sub.student_id).title == ""


def test_hr_sees_missing_pronouns_flag(client):
    hr = f.hr_admin()
    sub, _ = registration()  # factory students have no pronouns (registered before collection)
    client.force_login(hr)
    html = client.get(reverse("registry:submission_detail", kwargs={"pk": sub.pk})).content.decode()
    assert "Not collected — request a correction" in html
