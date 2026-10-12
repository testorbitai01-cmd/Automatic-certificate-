import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts.roles import HR_ADMIN, ROLES, SUPER_ADMIN
from apps.core.models import AuditLog

from . import factories as f

pytestmark = pytest.mark.django_db


def login(client, email, password=f.PASSWORD):
    return client.post(reverse("accounts:login"), {"username": email, "password": password})


def test_passwords_are_hashed_with_argon2():
    u = f.hr_admin()
    assert u.password.startswith("argon2$")


def test_email_is_case_insensitive_login(client):
    u = f.user(HR_ADMIN, email="hr.person@test.invalid")
    resp = login(client, "HR.Person@Test.Invalid")
    assert resp.status_code == 302
    assert client.session["_auth_user_id"] == str(u.pk)


def test_anonymous_is_redirected_to_login(client):
    resp = client.get(reverse("core:dashboard"))
    assert resp.status_code == 302
    assert reverse("accounts:login") in resp["Location"]


def test_staff_without_role_is_forbidden(client):
    u = f.user(role=None)
    client.force_login(u)
    assert client.get(reverse("core:dashboard")).status_code == 403


def test_hr_admin_can_open_dashboard(client):
    client.force_login(f.hr_admin())
    assert client.get(reverse("core:dashboard")).status_code == 200


def test_roles_are_synced_exactly():
    for name, perms in ROLES.items():
        group = Group.objects.get(name=name)
        actual = {f"{p.content_type.app_label}.{p.codename}" for p in group.permissions.all()}
        assert actual == set(perms)


@pytest.mark.parametrize("perm", [
    "doctemplates.approve_template",
    "accounts.manage_users",
    "core.manage_settings",
    "certificates.authorize_reissue",
    "certificates.revoke_certificate",
])
def test_hr_admin_lacks_super_admin_only_permissions(perm):
    assert not f.hr_admin().has_perm(perm)
    assert f.user(SUPER_ADMIN).has_perm(perm)


def test_login_and_failure_are_audited_without_password(client):
    u = f.hr_admin()
    login(client, u.email, "wrong-password")
    login(client, u.email)
    failed = AuditLog.objects.get(action="auth.login_failed")
    assert failed.metadata == {"attempted_email": u.email}
    assert "wrong-password" not in str(failed.metadata)
    assert AuditLog.objects.filter(action="auth.login", actor=u).exists()


def test_login_lockout_after_repeated_failures(client, settings):
    settings.LOGIN_MAX_FAILURES = 3
    u = f.hr_admin()
    for _ in range(3):
        assert login(client, u.email, "wrong").status_code == 200
    # Even the correct password is refused while locked out.
    resp = login(client, u.email)
    assert resp.status_code == 429
    assert "_auth_user_id" not in client.session


def test_logout_requires_post(client):
    client.force_login(f.hr_admin())
    assert client.get(reverse("accounts:logout")).status_code == 405
