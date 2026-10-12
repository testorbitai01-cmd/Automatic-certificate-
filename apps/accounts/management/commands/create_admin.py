import getpass

from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.roles import ROLES
from apps.core import audit


class Command(BaseCommand):
    help = "Create a staff account with a role. Prompts for the password (never pass it as an argument)."

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("--role", choices=list(ROLES), required=True)
        parser.add_argument("--name", default="")

    def handle(self, *args, email, role, name, **options):
        User = get_user_model()
        if User.objects.filter(email=email.strip().lower()).exists():
            raise CommandError("An account with this email already exists.")
        try:
            group = Group.objects.get(name=role)
        except Group.DoesNotExist as exc:
            raise CommandError("Run `manage.py sync_roles` first.") from exc
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat password: "):
            raise CommandError("Passwords do not match.")
        user = User(email=email, first_name=name, is_staff=True)
        password_validation.validate_password(password, user)
        user.set_password(password)
        user.save()
        user.groups.add(group)
        audit.record("user.created", entity=user, metadata={"role": role, "via": "cli"})
        self.stdout.write(self.style.SUCCESS(f"Created {user.email} ({role})."))
