from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import RLock
from typing import Protocol

from telemetry_gateway.errors import UnknownBootError
from telemetry_gateway.migrations import apply_migrations
from telemetry_gateway.models import (
    BootRegistrationInput,
    BootRegistrationResult,
    DeviceState,
    IngestResult,
    TelemetryEvent,
    TelemetryInput,
)


class TelemetryRepository(Protocol):
    def register_boot(self, event: BootRegistrationInput) -> BootRegistrationResult: ...

    def preview_state(self, event: TelemetryInput, received_at: str) -> DeviceState: ...

    def ingest(self, event: TelemetryInput, received_at: str) -> IngestResult: ...

    def list_current_states(self) -> list[DeviceState]: ...

    def list_events(self, limit: int) -> list[TelemetryEvent]: ...

    def ping(self) -> bool: ...


class TelemetryStore:
    def __init__(self, filename: str = "data/telemetry.db") -> None:
        if filename != ":memory:":
            Path(filename).parent.mkdir(parents=True, exist_ok=True)

        self._lock = RLock()
        self._connection = sqlite3.connect(
            filename,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 3000")
        if filename != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        apply_migrations(self._connection)

    def register_boot(self, event: BootRegistrationInput) -> BootRegistrationResult:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._get_boot(event.deviceId, event.bootId)
                if existing is not None:
                    self._connection.commit()
                    return BootRegistrationResult(
                        device_id=existing["device_id"],
                        boot_id=existing["boot_id"],
                        generation=existing["generation"],
                        created=False,
                    )

                row = self._connection.execute(
                    """
                    SELECT COALESCE(MAX(generation), 0) + 1 AS generation
                    FROM device_boots
                    WHERE device_id = ?
                    """,
                    (event.deviceId,),
                ).fetchone()
                generation = int(row["generation"])

                self._connection.execute(
                    """
                    INSERT INTO device_boots
                        (device_id, boot_id, generation, registered_at)
                    VALUES (?, ?, ?, datetime('now'))
                    """,
                    (event.deviceId, event.bootId, generation),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

        return BootRegistrationResult(
            device_id=event.deviceId,
            boot_id=event.bootId,
            generation=generation,
            created=True,
        )

    def preview_state(self, event: TelemetryInput, received_at: str) -> DeviceState:
        with self._lock:
            boot = self._get_boot(event.deviceId, event.bootId)
        if boot is None:
            raise UnknownBootError()
        return self._make_state(event, int(boot["generation"]), received_at)

    def ingest(self, event: TelemetryInput, received_at: str) -> IngestResult:
        with self._lock:
            boot = self._get_boot(event.deviceId, event.bootId)
            if boot is None:
                raise UnknownBootError()
            generation = int(boot["generation"])

            self._connection.execute("BEGIN IMMEDIATE")
            try:
                # The UNIQUE key on (device_id, boot_id, sequence) is the
                # protocol's logical event identity, so OR IGNORE makes an
                # at-least-once redelivery a no-op instead of a second audit row.
                insert = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO telemetry_events
                        (device_id, boot_id, generation, sequence, device_time,
                         received_at, metric, value)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.deviceId,
                        event.bootId,
                        generation,
                        event.sequence,
                        event.deviceTime.isoformat(),
                        received_at,
                        event.metric,
                        event.value,
                    ),
                )

                if insert.rowcount == 0:
                    self._connection.commit()
                    return IngestResult(duplicate=True, current_changed=False)

                update = self._connection.execute(
                    """
                    INSERT INTO current_state
                        (device_id, metric, boot_id, generation, sequence,
                         device_time, received_at, value)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(device_id, metric) DO UPDATE SET
                        boot_id = excluded.boot_id,
                        generation = excluded.generation,
                        sequence = excluded.sequence,
                        device_time = excluded.device_time,
                        received_at = excluded.received_at,
                        value = excluded.value
                    -- Recency is decided by the server-assigned boot
                    -- generation, then by sequence within that boot. deviceTime
                    -- is diagnostic only: device clocks can be early, late, or
                    -- far in the future, so it must never move state backward.
                    WHERE excluded.generation > current_state.generation
                       OR (
                            excluded.generation = current_state.generation
                            AND excluded.sequence > current_state.sequence
                          )
                    """,
                    (
                        event.deviceId,
                        event.metric,
                        event.bootId,
                        generation,
                        event.sequence,
                        event.deviceTime.isoformat(),
                        received_at,
                        event.value,
                    ),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

        current_changed = update.rowcount > 0
        return IngestResult(
            duplicate=False,
            current_changed=current_changed,
            state=(
                self._make_state(event, generation, received_at)
                if current_changed
                else None
            ),
        )

    def list_current_states(self) -> list[DeviceState]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT device_id, boot_id, generation, sequence, device_time,
                       received_at, metric, value
                FROM current_state
                ORDER BY device_id, metric
                """
            ).fetchall()
        return [self._state_from_row(row) for row in rows]

    def list_events(self, limit: int) -> list[TelemetryEvent]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, device_id, boot_id, generation, sequence, device_time,
                       received_at, metric, value
                FROM telemetry_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            TelemetryEvent(event_id=int(row["id"]), state=self._state_from_row(row))
            for row in rows
        ]

    def ping(self) -> bool:
        with self._lock:
            row = self._connection.execute("SELECT 1 AS ok").fetchone()
        return row is not None and row["ok"] == 1

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _get_boot(self, device_id: str, boot_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT device_id, boot_id, generation
            FROM device_boots
            WHERE device_id = ? AND boot_id = ?
            """,
            (device_id, boot_id),
        ).fetchone()

    @staticmethod
    def _make_state(
        event: TelemetryInput, generation: int, received_at: str
    ) -> DeviceState:
        return DeviceState(
            device_id=event.deviceId,
            boot_id=event.bootId,
            generation=generation,
            sequence=event.sequence,
            device_time=event.deviceTime.isoformat(),
            received_at=received_at,
            metric=event.metric,
            value=event.value,
        )

    @staticmethod
    def _state_from_row(row: sqlite3.Row) -> DeviceState:
        return DeviceState(
            device_id=row["device_id"],
            boot_id=row["boot_id"],
            generation=int(row["generation"]),
            sequence=int(row["sequence"]),
            device_time=row["device_time"],
            received_at=row["received_at"],
            metric=row["metric"],
            value=float(row["value"]),
        )
