"""Create the four certificate types (no template files; those are uploaded by staff)."""
from django.db import migrations

TYPES = ["offer_letter", "experience", "project", "lor"]


def seed(apps, schema_editor):
    CertificateTemplate = apps.get_model("doctemplates", "CertificateTemplate")
    for cert_type in TYPES:
        CertificateTemplate.objects.get_or_create(cert_type=cert_type)


class Migration(migrations.Migration):
    dependencies = [("doctemplates", "0002_template_review_workflow")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
