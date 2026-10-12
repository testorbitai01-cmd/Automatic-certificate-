from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.roles import ROLES
from apps.core import audit


def resolve(perm):
    app_label, codename = perm.split(".", 1)
    try:
        return Permission.objects.get(content_type__app_label=app_label, codename=codename)
    except Permission.DoesNotExist as exc:
        raise CommandError(f"Unknown permission: {perm}") from exc


class Command(BaseCommand):
    help = "Create or update role groups so they exactly match apps/accounts/roles.py."

    @transaction.atomic
    def handle(self, *args, **options):
        for name, perms in ROLES.items():
            group, _ = Group.objects.get_or_create(name=name)
            group.permissions.set([resolve(p) for p in perms])
            self.stdout.write(f"{name}: {len(perms)} permissions")
        audit.record("roles.synced", metadata={"roles": list(ROLES)})
