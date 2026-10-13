from django.urls import path

from . import views

app_name = "certificates"

urlpatterns = [
    path("<int:pk>/download/", views.download, name="download"),
    path("<int:pk>/action/", views.action, name="action"),
]
