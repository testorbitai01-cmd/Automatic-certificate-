import pytest
from django.db import connection

from apps.core import audit
from apps.core.models import AuditLog, AuditLogImmutableError

from . import factories as f

pytestmark = pytest.mark.django_db


def test_record_strips_secret_like_keys():
    entry = audit.record("test.event", metadata={"smtp_password": "x", "api_token": "y", "count": 3})
    assert entry.metadata == {"count": 3}


def test_entries_cannot_be_updated_or_deleted():
    entry = audit.record("test.event", actor=f.hr_admin())
    entry.action = "tampered"
    with pytest.raises(AuditLogImmutableError):
        entry.save()
    with pytest.raises(AuditLogImmutableError):
        entry.delete()
    with pytest.raises(AuditLogImmutableError):
        AuditLog.objects.filter(pk=entry.pk).update(action="tampered")
    with pytest.raises(AuditLogImmutableError):
        AuditLog.objects.all().delete()
    assert AuditLog.objects.get(pk=entry.pk).action == "test.event"


@pytest.mark.skipif(connection.vendor != "postgresql", reason="database trigger is PostgreSQL-only")
@pytest.mark.parametrize("sql", [
    "UPDATE core_auditlog SET action = 'x' WHERE id = %s",
    "DELETE FROM core_auditlog WHERE id = %s",
])
def test_raw_sql_changes_are_blocked_by_trigger(sql):
    from django.db import DatabaseError, transaction

    entry = audit.record("test.event")
    with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(sql, [entry.pk])
    assert AuditLog.objects.get(pk=entry.pk).action == "test.event"


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="database trigger is PostgreSQL-only")
def test_raw_sql_truncate_is_blocked_by_trigger(pg_transactional_db):
    # Committed row, outside a wrapping test transaction, so only the trigger can stop TRUNCATE.
    from django.db import DatabaseError

    entry = audit.record("test.event")
    with pytest.raises(DatabaseError, match="append-only"):
        with connection.cursor() as cur:
            cur.execute("TRUNCATE core_auditlog")
    assert AuditLog.objects.filter(pk=entry.pk).exists()


@pytest.mark.skipif(connection.vendor != "postgresql", reason="database trigger is PostgreSQL-only")
def test_trigger_is_enabled():
    # Guards against the test-only DISABLE in conftest leaking into later tests.
    with connection.cursor() as cur:
        cur.execute("SELECT tgenabled FROM pg_trigger WHERE tgname = 'core_auditlog_append_only'")
        assert cur.fetchone() == ("O",)
