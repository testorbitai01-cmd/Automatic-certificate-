from django.urls import path

from . import views

app_name = "internships"

urlpatterns = [
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/completion/", views.completion, name="completion"),
    path("<int:pk>/project/", views.project, name="project"),
    path("<int:pk>/attendance/", views.attendance, name="attendance"),
    path("<int:pk>/recommendation/", views.recommendation, name="recommendation"),
    path("<int:pk>/certificates/<str:cert_type>/", views.certificate_decision, name="certificate_decision"),
]
