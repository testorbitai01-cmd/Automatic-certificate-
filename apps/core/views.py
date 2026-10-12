from django.contrib.auth.decorators import login_required, permission_required
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.certificates.models import Issuance
from apps.registry.models import Submission


@require_GET
def health(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"status": "ok"})


@login_required
@permission_required("registry.view_submission", raise_exception=True)
def dashboard(request):
    # Placeholder counts from real records; the full dashboard is built in Phase 9.
    context = {
        "pending_submissions": Submission.objects.filter(status=Submission.Status.SUBMITTED).count(),
        "issued_certificates": Issuance.objects.filter(status=Issuance.Status.ISSUED).count(),
    }
    return render(request, "core/dashboard.html", context)
