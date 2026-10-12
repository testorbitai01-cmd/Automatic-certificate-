from django.urls import path

from . import views

app_name = "registry"

urlpatterns = [
    # Public (QR code target)
    path("register/<str:token>/", views.register, name="register"),
    path("register/<str:token>/submitted/", views.register_done, name="register_done"),
    # HR
    path("hr/campaigns/", views.campaign_list, name="campaign_list"),
    path("hr/campaigns/new/", views.campaign_create, name="campaign_create"),
    path("hr/campaigns/<int:pk>/", views.campaign_detail, name="campaign_detail"),
    path("hr/campaigns/<int:pk>/edit/", views.campaign_edit, name="campaign_edit"),
    path("hr/campaigns/<int:pk>/status/", views.campaign_set_status, name="campaign_set_status"),
    path("hr/campaigns/<int:pk>/qr.<str:fmt>", views.campaign_qr, name="campaign_qr"),
    path("hr/domains/", views.domain_list, name="domain_list"),
    path("hr/domains/<int:pk>/status/", views.domain_set_status, name="domain_set_status"),
    path("hr/domains/<int:pk>/rename/", views.domain_rename, name="domain_rename"),
]
