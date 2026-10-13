"""The approved placeholder registry.

Templates may contain only `{{ name }}` placeholders listed here; anything else (unknown names,
Jinja logic such as {% %}, filters, attribute access) is rejected at upload. These are field names
only — no certificate wording is defined here; all wording comes from the uploaded Gradtwin template.

`sample` values are synthetic and used only for the labelled preview.
"""
from dataclasses import dataclass

from apps.core.choices import CertificateType

ALL = frozenset(CertificateType.values)


@dataclass(frozen=True)
class Placeholder:
    name: str
    label: str
    source: str          # where the value will come from when certificates are generated (Phase 6)
    sample: str          # synthetic preview value
    types: frozenset = ALL


_ITEMS = [
    # Student
    Placeholder("title", "Title (Mr./Ms./Mrs.)", "student.title", "Ms."),
    Placeholder("full_name", "Full name", "student.full_name", "Sample Student A"),
    Placeholder("title_and_name", "Title and full name (name only if “No title”)", "student.title + full_name",
                "Ms. Sample Student A"),
    Placeholder("short_name", "Preferred name for salutations / LOR (full name if none)",
                "student.short_name or student.full_name", "Sample Student"),
    Placeholder("reg_no", "University registration number", "student.reg_no", "SAMPLE-0001"),
    Placeholder("degree", "Degree and department", "student.degree", "B.E. (Sample Engineering)"),
    Placeholder("college_name", "College name", "student.college_name", "Sample Institute of Technology"),
    Placeholder("college_location", "College location", "student.college_location", "Sampleton"),
    # Pronouns: from the student's own choice (student.pronouns), never inferred from gender.
    Placeholder("pronoun_subject", "he / she / they", "student.pronouns", "she"),
    Placeholder("pronoun_subject_cap", "He / She / They", "student.pronouns", "She"),
    Placeholder("pronoun_object", "him / her / them", "student.pronouns", "her"),
    Placeholder("pronoun_possessive", "his / her / their", "student.pronouns", "her"),
    Placeholder("pronoun_possessive_cap", "His / Her / Their", "student.pronouns", "Her"),
    # Verbs that agree with the pronoun (they have / are / go).
    Placeholder("verb_has", "has / have (agrees with the pronoun)", "student.pronouns", "has"),
    Placeholder("verb_is", "is / are (agrees with the pronoun)", "student.pronouns", "is"),
    Placeholder("verb_goes", "goes / go (agrees with the pronoun)", "student.pronouns", "goes"),
    # Internship
    Placeholder("domain", "Internship domain", "internship.domain.name", "Sample Domain"),
    Placeholder("start_month_year", "Start month and year", "internship.start_date (verified)", "June 2026"),
    Placeholder("end_month_year", "End month and year", "internship.end_date (verified)", "August 2026"),
    Placeholder("duration_months", "Duration in months (number)", "internship dates", "3",
                frozenset({CertificateType.OFFER_LETTER})),
    Placeholder("duration_text", "Duration, e.g. “3 months” / “1 month”", "internship dates", "3 months",
                frozenset({CertificateType.OFFER_LETTER})),
    Placeholder("project_title", "Project title", "internship.project_title (verified)", "Sample Project Title",
                frozenset({CertificateType.PROJECT})),
    Placeholder("mentor_name", "Mentor / supervisor name", "recommendation.mentor_name", "Sample Mentor",
                frozenset({CertificateType.LOR})),
    # Issue date, split so the template can superscript the ordinal suffix as the office documents do.
    Placeholder("issue_date", "Issue date (e.g. 24th September 2026)", "issuance.issue_date", "24th September 2026"),
    Placeholder("issue_day", "Issue day number", "issuance.issue_date", "24"),
    Placeholder("issue_day_suffix", "Issue day suffix (st/nd/rd/th)", "issuance.issue_date", "th"),
    Placeholder("issue_month_year", "Issue month and year", "issuance.issue_date", "September 2026"),
    # Internal reference — reserved. Gradtwin certificates print no reference number, so generation refuses
    # any template that uses it until a numbering policy is approved.
    Placeholder("reference_no", "Certificate reference number (not used — no approved policy)",
                "not available", "SAMPLE-REF-0001"),
]

REGISTRY = {p.name: p for p in _ITEMS}


def allowed_for(cert_type):
    return {name: p for name, p in REGISTRY.items() if cert_type in p.types}


def sample_values(cert_type):
    return {name: p.sample for name, p in allowed_for(cert_type).items()}
