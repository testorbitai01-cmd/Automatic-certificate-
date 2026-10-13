"""Builds the placeholder values for one student's certificate from verified records.

Only approved placeholders (apps/doctemplates/placeholders.py) are produced. Pronouns come only from the
student's own choice; nothing is inferred from gender. Missing data is reported, never guessed.
"""
from apps.core.choices import CertificateType
from apps.internships.models import Recommendation

PRONOUN_FORMS = {
    "he": {"pronoun_subject": "he", "pronoun_subject_cap": "He", "pronoun_object": "him",
           "pronoun_possessive": "his", "pronoun_possessive_cap": "His",
           "verb_has": "has", "verb_is": "is", "verb_goes": "goes"},
    "she": {"pronoun_subject": "she", "pronoun_subject_cap": "She", "pronoun_object": "her",
            "pronoun_possessive": "her", "pronoun_possessive_cap": "Her",
            "verb_has": "has", "verb_is": "is", "verb_goes": "goes"},
    "they": {"pronoun_subject": "they", "pronoun_subject_cap": "They", "pronoun_object": "them",
             "pronoun_possessive": "their", "pronoun_possessive_cap": "Their",
             "verb_has": "have", "verb_is": "are", "verb_goes": "go"},
}
PRONOUN_FIELDS = set(PRONOUN_FORMS["he"])


def ordinal_suffix(day):
    if 11 <= day % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def month_count(start, end):
    """Calendar months covered, inclusive (October–December = 3)."""
    return (end.year - start.year) * 12 + end.month - start.month + 1


def date_values(issue_date):
    day = f"{issue_date.day:02d}"  # the office documents use a two-digit day ("05th")
    suffix = ordinal_suffix(issue_date.day)
    month_year = issue_date.strftime("%B %Y")
    return {"issue_day": day, "issue_day_suffix": suffix, "issue_month_year": month_year,
            "issue_date": f"{day}{suffix} {month_year}"}


def build_values(internship, cert_type, issue_date):
    """Return (values, problems). `values` maps placeholder -> text; `problems` maps placeholder -> reason."""
    student = internship.student
    values, problems = {}, {}
    full_name = student.full_name.strip()
    values["title"] = student.title
    values["full_name"] = full_name
    values["title_and_name"] = f"{student.title} {full_name}" if student.title else full_name
    values["short_name"] = student.short_name.strip() or full_name
    for name in ("reg_no", "degree", "college_name", "college_location"):
        values[name] = getattr(student, name).strip()
    if student.pronouns in PRONOUN_FORMS:
        values.update(PRONOUN_FORMS[student.pronouns])
    else:
        for name in PRONOUN_FIELDS:
            problems[name] = ("The student has not chosen pronouns. Send a correction request for "
                              "“Pronouns” — pronouns are never inferred.")
    values["domain"] = internship.domain.name
    values["start_month_year"] = internship.start_date.strftime("%B %Y")
    values["end_month_year"] = internship.end_date.strftime("%B %Y")
    months = month_count(internship.start_date, internship.end_date)
    values["duration_months"] = str(months)
    values["duration_text"] = f"{months} month" if months == 1 else f"{months} months"
    if cert_type == CertificateType.PROJECT:
        values["project_title"] = internship.project_title.strip()
    if cert_type == CertificateType.LOR:
        rec = Recommendation.objects.filter(internship=internship).first()
        values["mentor_name"] = (rec.mentor_name.strip() if rec else "")
    values.update(date_values(issue_date))
    problems["reference_no"] = "Gradtwin certificates print no reference number; no numbering policy is approved."
    for name, value in list(values.items()):
        if not value and name not in ("title",):
            problems.setdefault(name, "No value recorded.")
    return values, problems


def missing_for(placeholders, problems):
    """Problems that matter for a template using `placeholders`."""
    return {name: problems[name] for name in placeholders if name in problems}
