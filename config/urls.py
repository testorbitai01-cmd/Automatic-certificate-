from django.urls import include, path

urlpatterns = [
    path("accounts/", include("apps.accounts.urls")),
    path("", include("apps.registry.urls")),
    path("hr/internships/", include("apps.internships.urls")),
    path("", include("apps.core.urls")),
]
