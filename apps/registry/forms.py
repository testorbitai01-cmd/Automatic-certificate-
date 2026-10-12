import datetime as dt
import secrets

from django import forms
from django.utils import timezone

from apps.core import policy

from . import validation as v
from .models import Campaign, Domain, Student, comparison_key

REQUEST_NEW_DOMAIN = "__request__"


def format_period(start, end):
    """'June 2026 to August 2026' — month and year, as used on certificates."""
    return f"{start:%B %Y} to {end:%B %Y}"


class RegistrationForm(forms.Form):
    """Public student registration form. All checks also run server-side here."""

    title = forms.ChoiceField(label="Title")
    full_name = forms.CharField(label="Full name", max_length=100,
                                help_text="As it should appear on your certificate, e.g. Amrita Zen A.")
    gender = forms.ChoiceField(label="Gender", choices=Student.Gender.choices, widget=forms.RadioSelect)
    gender_detail = forms.CharField(label="Please specify (optional)", max_length=100, required=False)
    mobile = forms.CharField(
        label="Mobile number", max_length=15,
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "tel-national",
                                      "pattern": "[6-9][0-9]{9}", "maxlength": "10"}),
        help_text="10 digits, without +91 or 0.")
    reg_no = forms.CharField(label="University registration number", max_length=40)
    email = forms.CharField(label="Registered Gmail address", max_length=254,
                            widget=forms.EmailInput(attrs={"autocomplete": "email"}))
    alternate_email = forms.CharField(label="Alternative email address", max_length=254, required=False,
                                      widget=forms.EmailInput())
    degree = forms.CharField(
        label="Degree and department / specialization", max_length=150,
        help_text="Examples: B.E. (Computer Science and Engineering), MCA (General), B.Sc., Information Technology.")
    college_name = forms.CharField(label="College name", max_length=200,
                                   help_text="Example: Dr. Mahalingam College of Engineering and Technology")
    college_location = forms.CharField(label="College location (town or city)", max_length=100,
                                       help_text="Example: Pollachi")
    domain = forms.ChoiceField(label="Internship domain")
    requested_domain = forms.CharField(label="Domain you would like to request", max_length=60, required=False)
    start_date = forms.DateField(label="From date", widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(label="To date", widget=forms.DateInput(attrs={"type": "date"}))
    project_title = forms.CharField(label="Project title", max_length=200,
                                    help_text="Example: Hotel Management System")
    feedback = forms.CharField(label="Internship feedback (optional)", required=False,
                               widget=forms.Textarea(attrs={"rows": 4}))
    form_token = forms.CharField(widget=forms.HiddenInput, max_length=64)

    def __init__(self, *args, campaign=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.suggestions = {}
        self.review_notes = []
        self.gmail_only = policy.get("registration.gmail_only")
        titles = policy.get("registration.titles")
        self.fields["title"].choices = [("", "Select")] + [(t, t) for t in titles]
        domains = Domain.objects.filter(status=Domain.Status.ACTIVE).order_by("name")
        self.fields["domain"].choices = (
            [("", "Select a domain")] + [(str(d.pk), d.name) for d in domains]
            + [(REQUEST_NEW_DOMAIN, "My domain is not listed – request a new domain")]
        )
        if campaign is not None and campaign.domain_id and campaign.domain.status == Domain.Status.ACTIVE:
            self.fields["domain"].initial = str(campaign.domain_id)
        if self.gmail_only:
            self.fields["email"].help_text = "Must be a Gmail address (ending in @gmail.com)."
        self.fields["alternate_email"].required = policy.get("registration.alternate_email_required")
        self.fields["feedback"].required = policy.get("registration.feedback_required")
        self.feedback_max = policy.get("registration.feedback_max_length")
        self.fields["feedback"].max_length = self.feedback_max
        self.fields["feedback"].widget.attrs["maxlength"] = str(self.feedback_max)
        if not self.is_bound:
            self.fields["form_token"].initial = secrets.token_urlsafe(32)

    # Helpers -------------------------------------------------------------

    def _run(self, name, check, *args, **kwargs):
        try:
            result = check(self.cleaned_data.get(name, ""), *args, **kwargs)
        except v.FieldError as exc:
            if exc.suggestion and exc.suggestion != self.cleaned_data.get(name):
                self.suggestions[name] = exc.suggestion
            raise forms.ValidationError(exc.messages[0]) from None
        self.review_notes.extend(f"{self.fields[name].label}: {note}" for note in result.review_notes)
        return result.value

    # Field checks --------------------------------------------------------

    def clean_full_name(self):
        return self._run("full_name", v.check_person_name)

    def clean_mobile(self):
        return self._run("mobile", v.check_indian_mobile)

    def clean_reg_no(self):
        return self._run("reg_no", v.check_reg_no)

    def clean_email(self):
        return self._run("email", v.check_email, gmail_only=self.gmail_only, label="Gmail address")

    def clean_alternate_email(self):
        if not self.cleaned_data.get("alternate_email", "").strip():
            return ""
        return self._run("alternate_email", v.check_email, gmail_only=False, label="alternative email address")

    def clean_degree(self):
        return self._run("degree", v.check_degree)

    def clean_college_name(self):
        return self._run("college_name", v.check_college_name)

    def clean_college_location(self):
        return self._run("college_location", v.check_location)

    def clean_project_title(self):
        return self._run("project_title", v.check_project_title)

    def clean_gender_detail(self):
        return v.collapse_whitespace(self.cleaned_data.get("gender_detail", ""))

    def clean_feedback(self):
        # Kept exactly as written (only outer whitespace trimmed); escaped on display.
        feedback = self.cleaned_data.get("feedback", "").strip()
        if len(feedback) > self.feedback_max:
            raise forms.ValidationError(f"Feedback must be {self.feedback_max} characters or fewer.")
        return feedback

    def clean_form_token(self):
        token = self.cleaned_data["form_token"]
        if len(token) < 32:
            raise forms.ValidationError("This form has expired. Please reload the page.")
        return token

    def clean(self):
        data = super().clean()
        if data.get("gender") != Student.Gender.OTHERS:
            data["gender_detail"] = ""

        choice = data.get("domain")
        if choice == REQUEST_NEW_DOMAIN:
            data["domain_obj"] = None
            try:
                data["requested_domain"] = v.check_domain_request(data.get("requested_domain", "")).value
            except v.FieldError as exc:
                if exc.suggestion:
                    self.suggestions["requested_domain"] = exc.suggestion
                self.add_error("requested_domain", exc.messages[0])
        elif choice:
            data["domain_obj"] = Domain.objects.get(pk=int(choice), status=Domain.Status.ACTIVE)
            data["requested_domain"] = ""

        start, end = data.get("start_date"), data.get("end_date")
        if start and end:
            if end < start:
                self.add_error("end_date", "The end date cannot be earlier than the start date.")
            elif (end - start).days > policy.get("registration.max_internship_days"):
                self.add_error("end_date", "Please check the dates: the internship period is longer than one year.")
            elif abs(start.year - timezone.localdate().year) > 2:
                self.add_error("start_date", "Please check the year of the start date.")
            else:
                data["period_display"] = format_period(start, end)

        email, alt = data.get("email"), data.get("alternate_email")
        if email and alt and email == alt:
            self.add_error("alternate_email",
                           "This is the same as your registered email. Enter a different address or leave it blank.")
        return data


class CampaignForm(forms.ModelForm):
    opens_at = forms.DateTimeField(label="Registration opens",
                                   widget=forms.DateTimeInput(attrs={"type": "datetime-local"},
                                                              format="%Y-%m-%dT%H:%M"))
    closes_at = forms.DateTimeField(label="Registration closes",
                                    widget=forms.DateTimeInput(attrs={"type": "datetime-local"},
                                                               format="%Y-%m-%dT%H:%M"))

    class Meta:
        model = Campaign
        fields = ["name", "domain", "opens_at", "closes_at"]
        labels = {"name": "Batch / campaign name", "domain": "Default domain (optional)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["domain"].queryset = Domain.objects.filter(status=Domain.Status.ACTIVE)
        self.fields["domain"].required = False
        if not self.instance.pk and not self.is_bound:
            now = timezone.localtime().replace(second=0, microsecond=0)
            self.initial.setdefault("opens_at", now)
            self.initial.setdefault("closes_at", now + dt.timedelta(days=30))

    def clean_name(self):
        name = v.collapse_whitespace(self.cleaned_data["name"])
        if not name:
            raise forms.ValidationError("Please enter a name.")
        return name

    def clean(self):
        data = super().clean()
        if data.get("opens_at") and data.get("closes_at") and data["closes_at"] <= data["opens_at"]:
            self.add_error("closes_at", "Closing time must be after the opening time.")
        return data


class DomainRenameForm(forms.Form):
    name = forms.CharField(max_length=100)

    def __init__(self, *args, domain, **kwargs):
        super().__init__(*args, **kwargs)
        self.domain = domain

    def clean_name(self):
        try:
            name = v.check_domain_request(self.cleaned_data["name"]).value
        except v.FieldError as exc:
            raise forms.ValidationError(exc.messages[0]) from None
        clash = Domain.objects.filter(normalized_key=comparison_key(name)).exclude(pk=self.domain.pk)
        if clash.exists():
            raise forms.ValidationError("A domain with this name already exists.")
        return name
