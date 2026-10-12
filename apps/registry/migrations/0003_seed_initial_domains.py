"""Seed the initial internship domains agreed with the office. Existing rows are left untouched."""
import re

from django.db import migrations

INITIAL_DOMAINS = [
    "Data Science",
    "Data Analytics",
    "Machine Learning",
    "FSD Java",
    "FSD Python",
    "HRM",
    "Digital Marketing",
    "Medical Coding",
]


def key(name):
    return re.sub(r"\s+", " ", name.strip()).casefold()


def seed(apps, schema_editor):
    Domain = apps.get_model("registry", "Domain")
    for name in INITIAL_DOMAINS:
        Domain.objects.get_or_create(normalized_key=key(name), defaults={"name": name, "status": "active"})


class Migration(migrations.Migration):
    dependencies = [("registry", "0002_submission_reg_no_key_review_notes")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
