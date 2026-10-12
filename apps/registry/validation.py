"""Field validation for the student registration form.

Validators never silently change spelling. They only:
  * trim and collapse whitespace (not a spelling change), and
  * raise a FieldError with a friendly message and, where safe, a suggested
    correction that the student can choose to apply.

Values that are unusual but may be legitimate (an unrecognized degree, say) are
accepted and returned with a review note so HR can check them.
"""
import re
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator

from .models import collapse_whitespace

CONNECTING_WORDS = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of", "on", "or", "the",
                    "to", "with", "via", "using", "&"}

# Recognized degree abbreviations (illustrative, not exhaustive). Canonical spelling first.
KNOWN_DEGREES = [
    "B.E.", "B.Tech.", "M.E.", "M.Tech.", "B.Sc.", "M.Sc.", "BCA", "MCA", "BBA", "MBA", "B.Com.", "M.Com.",
    "B.A.", "M.A.", "B.Arch.", "M.Arch.", "B.Pharm.", "M.Pharm.", "Pharm.D.", "MBBS", "BDS", "BPT", "MPT",
    "B.Voc.", "B.Des.", "M.Des.", "LLB", "LLM", "B.Ed.", "M.Ed.", "M.Phil.", "Ph.D.", "PGDM", "Diploma",
]


def _degree_key(token):
    return re.sub(r"[.\s]", "", token).casefold()


_DEGREE_BY_KEY = {_degree_key(d): d for d in KNOWN_DEGREES}


class FieldError(ValidationError):
    def __init__(self, message, suggestion=None):
        super().__init__(message)
        self.suggestion = suggestion


@dataclass
class Checked:
    value: str
    review_notes: list = field(default_factory=list)


# --- Name -----------------------------------------------------------------

_NAME_ALLOWED = re.compile(r"^[^\W\d_]+(?:['.\-][^\W\d_]*)*\.?$")
_INITIALS = re.compile(r"^(?:[A-Z]\.?){1,3}$")


def _name_component_ok(part):
    if _INITIALS.match(part):
        return True
    letters = [c for c in part if c.isalpha()]
    if not letters or not letters[0].isupper():
        return False
    # Reject ALL-CAPS words longer than an initials group (e.g. "JANAVI").
    rest = letters[1:]
    return any(c.islower() for c in rest) or not rest


def _suggest_name_component(part):
    if _INITIALS.match(part.upper()) and sum(c.isalpha() for c in part) <= 2:
        return part.upper()
    # Capitalize after apostrophes and hyphens (D'Souza, Anne-Marie).
    return re.sub(r"(^|['\-])([^\W\d_])([^\W\d_]*)",
                  lambda m: m.group(1) + m.group(2).upper() + m.group(3).lower(), part)


def check_person_name(raw):
    value = collapse_whitespace(raw)
    if not value:
        raise FieldError("Please enter your name.")
    if len(value) > 100:
        raise FieldError("Name must be 100 characters or fewer.")
    parts = value.split(" ")
    if any(not _NAME_ALLOWED.match(p) for p in parts):
        raise FieldError("Name may contain only letters, spaces, initials with dots, apostrophes and hyphens.")
    if all(_name_component_ok(p) for p in parts):
        return Checked(value)
    suggestion = " ".join(p if _name_component_ok(p) else _suggest_name_component(p) for p in parts)
    raise FieldError("Please capitalize each part of your name correctly. Example: Amrita Zen A.",
                     suggestion=suggestion)


# --- Mobile ---------------------------------------------------------------

def check_indian_mobile(raw):
    value = (raw or "").strip()
    if not value:
        raise FieldError("Please enter your mobile number.")
    if not value.isdigit() or not value.isascii():
        raise FieldError("Mobile number must contain digits only (no spaces, +91 or other symbols).")
    if len(value) != 10:
        raise FieldError("Mobile number must contain exactly 10 digits.")
    if value[0] not in "6789":
        raise FieldError("Please enter a valid Indian mobile number (it should start with 6, 7, 8 or 9).")
    return Checked(value)


# --- Email ----------------------------------------------------------------

_email_validator = EmailValidator()


def normalize_email(raw):
    """Trim and lowercase. No Gmail dot/plus folding: we never assume two addresses are the same mailbox."""
    return (raw or "").strip().lower()


def check_email(raw, *, gmail_only, label="email address"):
    value = normalize_email(raw)
    if not value:
        raise FieldError(f"Please enter your {label}.")
    try:
        _email_validator(value)
    except ValidationError:
        raise FieldError(f"Please enter a valid {label}, for example name@gmail.com.") from None
    if gmail_only and value.rsplit("@", 1)[1] != "gmail.com":
        raise FieldError("Please use a Gmail address (ending in @gmail.com).")
    return Checked(value)


# --- Registration number -------------------------------------------------

_REG_NO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ./\-]*$")


def check_reg_no(raw):
    value = collapse_whitespace(raw)
    if not value:
        raise FieldError("Please enter your university registration number.")
    if len(value) > 40:
        raise FieldError("Registration number must be 40 characters or fewer.")
    if not _REG_NO.match(value):
        raise FieldError("Registration number may contain letters, numbers, spaces, '-', '/' and '.' only.")
    return Checked(value)


# --- Shared "title-like text" capitalization -----------------------------

_WORD = re.compile(r"[^\W_][\w.'’&+#\-]*")


def _significant_word_ok(word):
    """A meaningful word is fine if it starts uppercase or contains uppercase/digits anywhere
    (preserves acronyms and technical terms: AI, IoT, iOS, YOLOv8, 5G, eCommerce)."""
    if word[0].isupper() or word[0].isdigit():
        return True
    return any(c.isupper() or c.isdigit() for c in word)


def _capitalization_problems(text, *, allow_lower_connectors=True):
    """Return (has_problem, suggestion)."""
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 10 and all(c.isupper() for c in letters) and " " in text:
        # Whole text typed in capitals: suggest normal case, keeping short tokens (likely acronyms).
        def recase(match):
            word = match.group(0)
            if match.start() and word.lower() in CONNECTING_WORDS:
                return word.lower()
            return word if len(word) <= 3 else word[0] + word[1:].lower()
        return True, _WORD.sub(recase, text)

    problem = False

    def fix(match):
        nonlocal problem
        word = match.group(0)
        is_first = match.start() == 0
        if allow_lower_connectors and not is_first and word.lower() in CONNECTING_WORDS:
            return word
        if _significant_word_ok(word) and not _shouting(word):
            return word
        problem = True
        if _shouting(word):
            return word[0] + word[1:].lower()
        return word[0].upper() + word[1:]

    suggestion = _WORD.sub(fix, text)
    return problem, suggestion


def _shouting(word):
    """An ALL-CAPS plain word of 6+ letters (likely caps-lock). Short acronyms and technical
    tokens with dots, digits or symbols (ASP.NET, COVID19, C++) are not treated as shouting."""
    return len(word) >= 6 and word.isalpha() and word.isupper()


# --- Degree ---------------------------------------------------------------

_DEGREE_ALLOWED = re.compile(r"^[A-Za-z0-9 .,()&/\-']+$")
_DEGREE_HEAD = re.compile(r"^[A-Za-z.]+")


def check_degree(raw):
    """Degree abbreviation followed by department/specialization, e.g. "B.E. (Computer Science and Engineering)".

    Dots in abbreviations are not forced (both "B.Tech" and "B.Tech." are accepted); only letter case is checked.
    """
    value = collapse_whitespace(raw)
    if not value:
        raise FieldError("Please enter your degree and department, for example B.E. (Computer Science and Engineering).")
    if len(value) > 150:
        raise FieldError("Degree must be 150 characters or fewer.")
    if not _DEGREE_ALLOWED.match(value):
        raise FieldError("Degree may contain letters, numbers, spaces and . , ( ) & / - only.")
    if value.count("(") != value.count(")"):
        raise FieldError("Please check the brackets in your degree, for example MCA (General).")

    notes = []
    match = _DEGREE_HEAD.match(value)
    degree = match.group(0) if match else ""
    rest = value[len(degree):]
    separator = rest[: len(rest) - len(rest.lstrip(" ,"))]
    specialization = rest[len(separator):]

    canonical = _DEGREE_BY_KEY.get(_degree_key(degree))
    degree_suggestion = degree
    if canonical is None:
        notes.append("Degree abbreviation not in the recognized list; please confirm with the student.")
    elif degree.replace(".", "") != canonical.replace(".", ""):
        degree_suggestion = canonical  # same letters, wrong case (e.g. "mca", "b.e.")

    spec_problem, spec_suggestion = (
        _capitalization_problems(specialization) if specialization else (False, "")
    )
    if degree_suggestion != degree or spec_problem:
        suggestion = f"{degree_suggestion}{separator}{spec_suggestion}".strip()
        raise FieldError(
            "Please check the capitalization of your degree and department. "
            "Examples: MCA (General), B.E. (Computer Science and Engineering), B.Sc., Information Technology.",
            suggestion=suggestion,
        )
    if not rest.strip():
        notes.append("No department or specialization given.")
    return Checked(value, notes)


# --- College name and location -------------------------------------------

_INSTITUTION_ALLOWED = re.compile(r"^[^\W_][\w .,'’&()\-]*$")


def check_college_name(raw):
    value = collapse_whitespace(raw)
    if not value:
        raise FieldError("Please enter your college name.")
    if len(value) > 200:
        raise FieldError("College name must be 200 characters or fewer.")
    if not _INSTITUTION_ALLOWED.match(value):
        raise FieldError("College name contains characters that are not allowed.")
    problem, suggestion = _capitalization_problems(value)
    if problem:
        raise FieldError("Please capitalize the college name correctly. "
                         "Example: Dr. Mahalingam College of Engineering and Technology.", suggestion=suggestion)
    return Checked(value)


_LOCATION_ALLOWED = re.compile(r"^[^\W\d_][^\W\d_ .'\-]*(?:[ .'\-]+[^\W\d_]+)*\.?$")


def check_location(raw):
    value = collapse_whitespace(raw)
    if not value:
        raise FieldError("Please enter the college location (town or city).")
    if len(value) > 100:
        raise FieldError("Location must be 100 characters or fewer.")
    if not _LOCATION_ALLOWED.match(value):
        raise FieldError("Location may contain letters, spaces, '.', apostrophes and hyphens only.")
    problem, suggestion = _capitalization_problems(value)
    if problem:
        raise FieldError("Please capitalize the location correctly. Example: Pollachi.", suggestion=suggestion)
    return Checked(value)


# --- Project title -------------------------------------------------------

_PROJECT_ALLOWED = re.compile(r"^[\w .,:;'’\"“”&()/+#\-?!]+$")


def check_project_title(raw):
    value = collapse_whitespace(raw)
    if not value:
        raise FieldError("Please enter your project title.")
    if len(value) < 3:
        raise FieldError("Project title is too short.")
    if len(value) > 200:
        raise FieldError("Project title must be 200 characters or fewer.")
    if not _PROJECT_ALLOWED.match(value):
        raise FieldError("Project title contains characters that are not allowed.")
    problem, suggestion = _capitalization_problems(value)
    if problem:
        raise FieldError("Please capitalize the main words of your project title. Example: Hotel Management System.",
                         suggestion=suggestion)
    return Checked(value)


# --- Domain request ------------------------------------------------------

_DOMAIN_ALLOWED = re.compile(r"^[^\W_][\w .&/+#\-]*$")


def check_domain_request(raw):
    value = collapse_whitespace(raw)
    if not value:
        raise FieldError("Please enter the domain you want to request.")
    if len(value) > 60:
        raise FieldError("Domain name must be 60 characters or fewer.")
    if not _DOMAIN_ALLOWED.match(value):
        raise FieldError("Domain name contains characters that are not allowed.")
    problem, suggestion = _capitalization_problems(value)
    if problem:
        raise FieldError("Please capitalize the domain name. Example: Cloud Computing.", suggestion=suggestion)
    return Checked(value)
