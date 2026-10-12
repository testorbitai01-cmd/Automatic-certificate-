from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from apps.core import audit


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    audit.record("auth.login", actor=user, request=request)


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    if user is not None:
        audit.record("auth.logout", actor=user, request=request)


@receiver(user_login_failed)
def on_login_failed(sender, credentials, request=None, **kwargs):
    # Record the attempted email only; Django already strips the password.
    audit.record(
        "auth.login_failed",
        actor_label="anonymous",
        metadata={"attempted_email": str(credentials.get("username", ""))[:254]},
        request=request,
    )
