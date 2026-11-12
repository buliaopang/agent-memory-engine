"""Idempotency-Key handling (Phase 3b, ADR-027).

Protocol, per (actor_id, key):
1. ``begin``  - one BEGIN IMMEDIATE transaction: drop the row if expired, then
   * no row            -> insert a 'pending' reservation and return NEW (caller runs the request)
   * same fingerprint  -> 'done': return REPLAY with the stored status/body;
                          'pending': raise IdempotencyInProgressError (409)
   * other fingerprint -> raise IdempotencyKeyMismatchError (422)
2. ``finish`` - store status + body ('done'), or ``abandon`` - delete the reservation (5xx), so
   a retry may run again. The primary key makes concurrent first requests race-safe: exactly
   one reservation wins, the others see 'pending'.
Bodies are kept only until expires_at; ``purge_expired`` deletes old rows (never audit rows).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from ame.errors import (
    IdempotencyInProgressError,
    IdempotencyKeyMismatchError,
    ValidationFailedError,
)
from ame.infrastructure.models import IdempotencyKey
from ame.infrastructure.unit_of_work import UnitOfWork

MAX_KEY_LEN = 255
_PURGE_BATCH = 100


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def request_fingerprint(method: str, path: str, query: str, body: bytes) -> str:
    h = hashlib.sha256()
    for part in (method.upper().encode(), path.encode(), query.encode(), body):
        h.update(len(part).to_bytes(8, "big") + part)
    return h.hexdigest()


def validate_key(key: str) -> str:
    if not 1 <= len(key) <= MAX_KEY_LEN or not all(0x21 <= ord(c) <= 0x7E for c in key):
        raise ValidationFailedError(
            "Idempotency-Key must be 1-255 visible ASCII characters", {"header": "Idempotency-Key"}
        )
    return key


@dataclass(frozen=True)
class Replay:
    status_code: int
    body: str


class IdempotencyService:
    def __init__(self, sf: sessionmaker[Session], ttl_hours: float = 24.0) -> None:
        self._sf = sf
        self._ttl = timedelta(hours=ttl_hours)

    def begin(
        self, actor_id: str, key: str, fingerprint: str, now: datetime | None = None
    ) -> Replay | None:
        """Return None when the caller must execute the request (reservation taken)."""
        validate_key(key)
        t = now or datetime.now(UTC)
        ts = _iso(t)
        with UnitOfWork(self._sf) as uow:
            uow.idempotency.purge_expired(ts, limit=_PURGE_BATCH)
            row = uow.idempotency.get(actor_id, key)
            if row is None:
                uow.idempotency.add(
                    IdempotencyKey(
                        actor_id=actor_id,
                        idem_key=key,
                        request_hash=fingerprint,
                        state="pending",
                        created_at=ts,
                        expires_at=_iso(t + self._ttl),
                    )
                )
                uow.commit()
                return None
            if row.request_hash != fingerprint:
                raise IdempotencyKeyMismatchError(
                    "Idempotency-Key was already used with a different request"
                )
            if row.state != "done" or row.status_code is None or row.response_body is None:
                raise IdempotencyInProgressError(
                    "A request with this Idempotency-Key is still in progress"
                )
            return Replay(int(row.status_code), row.response_body)

    def finish(self, actor_id: str, key: str, status_code: int, body: str) -> None:
        with UnitOfWork(self._sf) as uow:
            row = uow.idempotency.get(actor_id, key)
            if row is not None and row.state == "pending":
                row.state, row.status_code, row.response_body = "done", status_code, body
                uow.commit()

    def abandon(self, actor_id: str, key: str) -> None:
        with UnitOfWork(self._sf) as uow:
            row = uow.idempotency.get(actor_id, key)
            if row is not None and row.state == "pending":
                uow.idempotency.remove(row)
                uow.commit()

    def purge_expired(self, now: datetime | None = None) -> int:
        with UnitOfWork(self._sf) as uow:
            n = uow.idempotency.purge_expired(_iso(now or datetime.now(UTC)))
            uow.commit()
            return n

    def status(self) -> dict[str, int]:
        with UnitOfWork(self._sf, read_only=True) as uow:
            return uow.idempotency.count_by_state()
