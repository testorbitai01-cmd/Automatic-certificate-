"""Office policy values, overridable through the Setting table (managed by Super Admins)."""
from .models import Setting

DEFAULTS = {
    "registration.titles": ["Mr.", "Ms.", "Mrs."],
    "registration.gmail_only": True,
    "registration.alternate_email_required": False,
    "registration.feedback_required": False,
    "registration.feedback_max_length": 2000,
    # Longest internship accepted on the form; longer ranges are almost always typing mistakes.
    "registration.max_internship_days": 366,
    # How long a student correction link stays valid.
    "registration.correction_link_days": 7,
}


def get(key):
    row = Setting.objects.filter(key=key).only("value").first()
    return row.value if row is not None else DEFAULTS[key]
