from django.core.management.base import BaseCommand

from apps.certificates.approvals import approval_is_authorised
from apps.certificates.models import CertificateRequest
from apps.core.choices import CertificateType


class Command(BaseCommand):
    help = ("List LOR approvals recorded by accounts without final LOR authority (Super Admin only). "
            "Read-only: nothing is changed. A Super Admin should review each one on the internship page.")

    def handle(self, *args, **options):
        approved = (CertificateRequest.objects.select_related("decided_by", "internship__student")
                    .filter(cert_type=CertificateType.LOR, approval_status=CertificateRequest.Approval.APPROVED))
        flagged = [r for r in approved if not approval_is_authorised(r)]
        for r in flagged:
            self.stdout.write(f"internship {r.internship_id}: approved by {r.decided_by} on {r.decided_at:%Y-%m-%d %H:%M}")
        self.stdout.write(f"{len(flagged)} LOR approval(s) need Super Admin review; {approved.count()} approved in total.")
