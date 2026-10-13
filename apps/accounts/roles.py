"""Role definitions. Roles are Django groups; the backend checks permissions, never role names.

Run `python manage.py sync_roles` after migrations to create or update groups.
"""

HR_ADMIN = "HR Admin"
SUPER_ADMIN = "Super Admin"

HR_ADMIN_PERMISSIONS = [
    "registry.manage_campaign",
    "registry.view_campaign",
    "registry.manage_domain",
    "registry.view_domain",
    "registry.view_student",
    "registry.change_student",
    "registry.view_submission",
    "registry.review_submission",
    "registry.resolve_duplicate",
    "internships.view_internship",
    "internships.verify_completion",
    "internships.verify_project",
    "internships.decide_recommendation",
    "doctemplates.view_templateversion",
    "doctemplates.upload_template",
    "doctemplates.map_template",
    "certificates.view_certificaterequest",
    "certificates.approve_certificate",
    "certificates.view_issuance",
    "certificates.generate_certificate",
    "certificates.download_certificate",
    "certificates.send_certificate_email",
    "certificates.record_print",
    "certificates.record_handover",
    "certificates.request_reissue",
    "certificates.view_emailjob",
    "core.view_auditlog",
    "core.export_data",
]

# Super Admin additionally controls accounts, settings, template activation,
# reissue/revocation authorization and sensitive audit events.
SUPER_ADMIN_PERMISSIONS = HR_ADMIN_PERMISSIONS + [
    "accounts.manage_users",
    "core.manage_settings",
    "core.view_sensitive_audit",
    "doctemplates.approve_template",
    "certificates.approve_lor",
    "certificates.authorize_reissue",
    "certificates.revoke_certificate",
]

ROLES = {
    HR_ADMIN: HR_ADMIN_PERMISSIONS,
    SUPER_ADMIN: SUPER_ADMIN_PERMISSIONS,
}
