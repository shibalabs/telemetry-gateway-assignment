from __future__ import annotations

import sqlite3
from collections.abc import Callable

Migration = tuple[int, Callable[[sqlite3.Connection], None]]


def migration_001(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE device_boots (
            device_id TEXT NOT NULL,
            boot_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            registered_at TEXT NOT NULL,
            PRIMARY KEY (device_id, boot_id),
            UNIQUE (device_id, generation)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE telemetry_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            boot_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            device_time TEXT NOT NULL,
            received_at TEXT NOT NULL,
            metric TEXT NOT NULL,
            value REAL NOT NULL,
            UNIQUE (device_id, sequence),
            FOREIGN KEY (device_id, boot_id)
                REFERENCES device_boots (device_id, boot_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE current_state (
            device_id TEXT NOT NULL,
            metric TEXT NOT NULL,
            boot_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            device_time TEXT NOT NULL,
            received_at TEXT NOT NULL,
            value REAL NOT NULL,
            PRIMARY KEY (device_id, metric),
            FOREIGN KEY (device_id, boot_id)
                REFERENCES device_boots (device_id, boot_id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX telemetry_events_received_at_idx
        ON telemetry_events (received_at DESC)
        """
    )


def migration_002(connection: sqlite3.Connection) -> None:
    """Key the raw audit table on the protocol's logical event identity.

    Migration 001 declared ``UNIQUE (device_id, sequence)``. The protocol
    defines a logical event as ``(deviceId, bootId, sequence)`` and restarts
    ``sequence`` at 1 for every boot, so the old key made the first events of
    every new boot collide with the previous boot's events. Those events were
    silently discarded as duplicates.

    The table is rebuilt rather than altered because SQLite cannot drop a
    UNIQUE constraint in place. Every existing row is copied with its original
    ``id``, so the raw audit history is preserved.
    """
    connection.execute(
        """
        CREATE TABLE telemetry_events_rekeyed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            boot_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            device_time TEXT NOT NULL,
            received_at TEXT NOT NULL,
            metric TEXT NOT NULL,
            value REAL NOT NULL,
            UNIQUE (device_id, boot_id, sequence),
            FOREIGN KEY (device_id, boot_id)
                REFERENCES device_boots (device_id, boot_id)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO telemetry_events_rekeyed
            (id, device_id, boot_id, generation, sequence, device_time,
             received_at, metric, value)
        SELECT id, device_id, boot_id, generation, sequence, device_time,
               received_at, metric, value
        FROM telemetry_events
        """
    )
    connection.execute("DROP TABLE telemetry_events")
    connection.execute("ALTER TABLE telemetry_events_rekeyed RENAME TO telemetry_events")
    connection.execute(
        """
        CREATE INDEX telemetry_events_received_at_idx
        ON telemetry_events (received_at DESC)
        """
    )


MIGRATIONS: list[Migration] = [(1, migration_001), (2, migration_002)]


def apply_migrations(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        row[0]
        for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    }
    pending = [entry for entry in MIGRATIONS if entry[0] not in applied]
    if not pending:
        return

    # Table rebuilds re-create parent/child relationships mid-migration.
    # Foreign key enforcement is disabled for the duration and the result is
    # verified before it is restored. PRAGMA foreign_keys is a no-op inside a
    # transaction, so this must stay outside the per-migration BEGIN.
    foreign_keys_enabled = bool(
        connection.execute("PRAGMA foreign_keys").fetchone()[0]
    )
    if foreign_keys_enabled:
        connection.execute("PRAGMA foreign_keys = OFF")

    try:
        for version, migration in pending:
            connection.execute("BEGIN IMMEDIATE")
            try:
                migration(connection)
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, datetime('now'))",
                    (version,),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"Migrations left {len(violations)} foreign key violation(s)."
            )
    finally:
        if foreign_keys_enabled:
            connection.execute("PRAGMA foreign_keys = ON")
