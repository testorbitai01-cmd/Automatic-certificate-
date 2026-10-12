from django.db import migrations, models


def copy_reg_no_key(apps, schema_editor):
    Submission = apps.get_model("registry", "Submission")
    for submission in Submission.objects.select_related("student").iterator():
        submission.reg_no_key = submission.student.reg_no_key
        submission.save(update_fields=["reg_no_key"])


class Migration(migrations.Migration):
    dependencies = [("registry", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="submission",
            name="reg_no_key",
            field=models.CharField(default="", editable=False, max_length=40),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="submission",
            name="review_notes",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(copy_reg_no_key, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="submission",
            constraint=models.UniqueConstraint(fields=("campaign", "reg_no_key"),
                                               name="one_submission_per_reg_no_per_campaign"),
        ),
    ]
