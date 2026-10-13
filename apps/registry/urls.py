from django.urls import path

from . import views, views_review

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
    # HR review (Phase 4)
    path("hr/submissions/", views_review.submission_list, name="submission_list"),
    path("hr/submissions/<int:pk>/", views_review.submission_detail, name="submission_detail"),
    path("hr/submissions/<int:pk>/action/", views_review.submission_action, name="submission_action"),
    path("hr/submissions/<int:pk>/notes/", views_review.submission_add_note, name="submission_add_note"),
    path("hr/duplicates/<int:pk>/resolve/", views_review.duplicate_resolve, name="duplicate_resolve"),
    # Public correction link (token is random and stored only as a hash)
    path("correct/done/", views_review.correction_done, name="correction_done"),
    path("correct/<str:token>/", views_review.correction, name="correction"),
]
