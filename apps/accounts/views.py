from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views

from apps.core import ratelimit
from apps.core.audit import client_ip

LOGIN_SCOPE = "login-failures"


def _ident(request, email):
    return f"{(email or '').strip().lower()}|{client_ip(request)}"


class LoginView(auth_views.LoginView):
    """Login with per-(email, IP) lockout after repeated failures, shared across all processes."""

    template_name = "accounts/login.html"
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        ident = _ident(request, request.POST.get("username"))
        window = settings.LOGIN_LOCKOUT_SECONDS
        if ratelimit.count(LOGIN_SCOPE, ident, window) >= settings.LOGIN_MAX_FAILURES:
            messages.error(request, "Too many failed sign-in attempts. Please try again later.")
            return self.render_to_response(self.get_context_data(form=self.get_form()), status=429)
        response = super().post(request, *args, **kwargs)
        if request.user.is_authenticated:
            ratelimit.reset(LOGIN_SCOPE, ident)
        else:
            ratelimit.hit(LOGIN_SCOPE, ident, window)
        return response
