"""Make core_auditlog append-only at the database level (PostgreSQL only).

The model already blocks updates/deletes through the ORM; this trigger also blocks raw SQL.
"""
from django.db import migrations

CREATE = """
CREATE OR REPLACE FUNCTION core_auditlog_block_changes() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'core_auditlog is append-only (% blocked)', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER core_auditlog_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON core_auditlog
    FOR EACH STATEMENT EXECUTE FUNCTION core_auditlog_block_changes();
"""

DROP = """
DROP TRIGGER IF EXISTS core_auditlog_append_only ON core_auditlog;
DROP FUNCTION IF EXISTS core_auditlog_block_changes();
"""


# params=None: the SQL contains a literal '%' (plpgsql RAISE format), which psycopg would
# otherwise treat as a query placeholder.
def forwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(CREATE, params=None)


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(DROP, params=None)


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]
    operations = [migrations.RunPython(forwards, backwards)]
