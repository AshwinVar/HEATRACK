"""Dedicated persistent worker: every minute, evaluate rules/freshness for each monitored
profile (one transaction per profile, guarded by an advisory lock so multiple workers never
evaluate the same profile concurrently), then dispatch due notifications."""

from __future__ import annotations

import logging
import signal
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.models import HealthProfile
from app.services.notify import dispatch_due
from app.services.push import PushTransport, get_transport
from app.services.rules import evaluate_profile

log = logging.getLogger("familypulse.worker")


def evaluate_all(factory: sessionmaker[Session], settings: Settings, now: datetime) -> int:
    with factory() as db:
        ids = list(
            db.execute(
                select(HealthProfile.id).where(HealthProfile.monitoring_enabled.is_(True))
            ).scalars()
        )
    created = 0
    for pid in ids:
        with factory() as db:
            got = db.execute(
                text("select pg_try_advisory_xact_lock(hashtext(:k))"), {"k": f"eval:{pid}"}
            ).scalar_one()
            if not got:
                continue
            hp = db.get(HealthProfile, pid)
            if hp is None:
                continue
            alerts = evaluate_profile(db, hp, settings, now)
            db.commit()  # alert + outbox rows commit atomically
            created += len(alerts)
    return created


def run_once(
    factory: sessionmaker[Session],
    settings: Settings,
    transport: PushTransport | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    created = evaluate_all(factory, settings, now)
    sent: dict[str, int] = {}
    if transport is not None:
        sent = dispatch_due(factory, transport, settings, now)
    return {"alerts_created": created, "dispatch": sent}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    factory = get_sessionmaker()
    transport = get_transport(settings)
    if transport is None:
        log.warning("push transport disabled: alerts are stored but no push is sent")
    elif transport.name == "fake":
        log.warning("FAKE push transport active (development only)")
    stop = False

    def _stop(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stop:
        started = time.monotonic()
        try:
            res = run_once(factory, settings, transport)
            # Counts only: never log health values or tokens.
            log.info("cycle alerts_created=%s dispatch=%s", res["alerts_created"], res["dispatch"])
        except Exception:  # noqa: BLE001 - keep the worker alive; next cycle retries
            log.exception("worker cycle failed")
        elapsed = time.monotonic() - started
        for _ in range(int(max(1, settings.worker_interval_seconds - elapsed))):
            if stop:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
