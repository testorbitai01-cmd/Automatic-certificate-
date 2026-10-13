from django.urls import path

from . import views

app_name = "doctemplates"

urlpatterns = [
    path("", views.template_list, name="list"),
    path("upload/", views.upload, name="upload"),
    path("versions/<int:pk>/", views.version_detail, name="version"),
    path("versions/<int:pk>/download/", views.download, name="download"),
    path("versions/<int:pk>/action/", views.action, name="action"),
]
