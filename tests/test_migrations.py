"""Migration behaviour, including the rebuild that re-keys the audit table."""

from __future__ import annotations

import sqlite3

from telemetry_gateway.migrations import (
    MIGRATIONS,
    apply_migrations,
    migration_001,
)


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _database_at_version_001() -> sqlite3.Connection:
    """Build the schema exactly as a gateway installed before the fix had it."""
    connection = _connect()
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    connection.execute("BEGIN IMMEDIATE")
    migration_001(connection)
    connection.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (1, datetime('now'))"
    )
    connection.commit()
    return connection


def test_migrations_are_ordered_and_uniquely_versioned() -> None:
    versions = [version for version, _ in MIGRATIONS]

    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))


def test_applying_migrations_is_idempotent() -> None:
    connection = _connect()
    try:
        apply_migrations(connection)
        applied_once = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()

        apply_migrations(connection)
        applied_twice = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()

        assert [row["version"] for row in applied_once] == [
            version for version, _ in MIGRATIONS
        ]
        assert [row["version"] for row in applied_twice] == [
            row["version"] for row in applied_once
        ]
    finally:
        connection.close()


def test_migration_restores_foreign_key_enforcement() -> None:
    connection = _connect()
    try:
        apply_migrations(connection)

        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()


def test_rekeying_preserves_existing_raw_audit_history() -> None:
    """Re-keying the audit table must not delete or renumber recorded events."""
    connection = _database_at_version_001()
    try:
        connection.execute(
            """
            INSERT INTO device_boots (device_id, boot_id, generation, registered_at)
            VALUES ('device-01', 'boot-a', 1, datetime('now'))
            """
        )
        for sequence in (1, 2):
            connection.execute(
                """
                INSERT INTO telemetry_events
                    (device_id, boot_id, generation, sequence, device_time,
                     received_at, metric, value)
                VALUES ('device-01', 'boot-a', 1, ?, '2026-08-12T09:00:00+00:00',
                        '2026-08-12T09:00:01+00:00', 'temperature', 21.4)
                """,
                (sequence,),
            )
        before = connection.execute(
            "SELECT id, device_id, boot_id, sequence FROM telemetry_events ORDER BY id"
        ).fetchall()

        apply_migrations(connection)

        after = connection.execute(
            "SELECT id, device_id, boot_id, sequence FROM telemetry_events ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in after] == [tuple(row) for row in before]
    finally:
        connection.close()


def test_rekeying_allows_a_reused_sequence_from_a_different_boot() -> None:
    """The old key rejected a restarted device's sequence 1; the new key accepts it."""
    connection = _database_at_version_001()
    try:
        for boot_id, generation in (("boot-a", 1), ("boot-b", 2)):
            connection.execute(
                """
                INSERT INTO device_boots (device_id, boot_id, generation, registered_at)
                VALUES ('device-01', ?, ?, datetime('now'))
                """,
                (boot_id, generation),
            )
        connection.execute(
            """
            INSERT INTO telemetry_events
                (device_id, boot_id, generation, sequence, device_time,
                 received_at, metric, value)
            VALUES ('device-01', 'boot-a', 1, 1, '2026-08-12T09:00:00+00:00',
                    '2026-08-12T09:00:01+00:00', 'temperature', 21.4)
            """
        )

        apply_migrations(connection)

        # Same sequence, different boot: a distinct logical event.
        connection.execute(
            """
            INSERT INTO telemetry_events
                (device_id, boot_id, generation, sequence, device_time,
                 received_at, metric, value)
            VALUES ('device-01', 'boot-b', 2, 1, '2026-08-12T09:05:00+00:00',
                    '2026-08-12T09:05:01+00:00', 'temperature', 22.9)
            """
        )
        assert connection.execute(
            "SELECT COUNT(*) AS total FROM telemetry_events"
        ).fetchone()["total"] == 2
    finally:
        connection.close()


def test_rekeyed_table_still_rejects_a_repeated_logical_event() -> None:
    connection = _connect()
    try:
        apply_migrations(connection)
        connection.execute(
            """
            INSERT INTO device_boots (device_id, boot_id, generation, registered_at)
            VALUES ('device-01', 'boot-a', 1, datetime('now'))
            """
        )
        insert = """
            INSERT INTO telemetry_events
                (device_id, boot_id, generation, sequence, device_time,
                 received_at, metric, value)
            VALUES ('device-01', 'boot-a', 1, 7, '2026-08-12T09:00:00+00:00',
                    '2026-08-12T09:00:01+00:00', 'temperature', 21.4)
        """
        connection.execute(insert)

        try:
            connection.execute(insert)
        except sqlite3.IntegrityError:
            pass
        else:  # pragma: no cover - the constraint is the point of the test
            raise AssertionError("A repeated logical event was accepted.")
    finally:
        connection.close()
