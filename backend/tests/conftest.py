"""Tests run against a real PostgreSQL database (FP_TEST_DATABASE_URL), migrated with
Alembic, using real ES256 JWT verification via an injected key resolver."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

TEST_DB = os.environ.get(
    "FP_TEST_DATABASE_URL", "postgresql+psycopg://postgres@127.0.0.1:5432/familypulse_test"
)
os.environ.update(
    FP_APP_ENV="test",
    FP_DATABASE_URL=TEST_DB,
    FP_AUTH_ISSUER="https://test-project.supabase.co/auth/v1",
    FP_AUTH_AUDIENCE="authenticated",
    FP_PUSH_TRANSPORT="disabled",
    FP_AUTH_JWT_SECRET="hs256-test-secret-not-a-real-secret-0123456789",
)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import auth  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_engine, get_sessionmaker  # noqa: E402
from app.deps import get_now  # noqa: E402
from app.main import create_app  # noqa: E402

ISSUER = os.environ["FP_AUTH_ISSUER"]
EC_KEY = ec.generate_private_key(ec.SECP256R1())
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())
T0 = datetime(2026, 10, 4, 6, 0, tzinfo=UTC)  # 11:30 in Asia/Kolkata


class StaticResolver:
    def key_for(self, token: str, header: dict[str, Any]) -> Any:
        if header.get("alg") == "HS256":
            return get_settings().auth_jwt_secret
        return EC_KEY.public_key()


@pytest.fixture(scope="session", autouse=True)
def _migrate() -> None:
    engine = get_engine()
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    cfg.attributes["database_url"] = TEST_DB
    command.upgrade(cfg, "head")
    auth.set_key_resolver(StaticResolver())


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    yield
    with get_engine().begin() as c:
        tables = c.execute(
            text(
                "select string_agg(format('%I', tablename), ',') from pg_tables "
                "where schemaname='public' and tablename <> 'alembic_version'"
            )
        ).scalar_one()
        c.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def advance(self, **kw: float) -> datetime:
        self.now = self.now + timedelta(**kw)
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(clock: Clock) -> Iterator[TestClient]:
    app = create_app(get_settings())
    app.dependency_overrides[get_now] = lambda: clock.now
    with TestClient(app) as c:
        yield c


def make_token(
    sub: uuid.UUID | str | None,
    *,
    exp_delta: timedelta = timedelta(hours=1),
    iss: str = ISSUER,
    aud: str = "authenticated",
    key: Any = None,
    alg: str = "ES256",
    email: str | None = None,
    role: str = "authenticated",
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": iss,
        "aud": aud,
        "iat": int(now.timestamp()),
        "exp": int((now + exp_delta).timestamp()),
        "role": role,
    }
    if sub is not None:
        claims["sub"] = str(sub)
    if email:
        claims["email"] = email
    return jwt.encode(claims, key or EC_KEY, algorithm=alg)


class User:
    def __init__(self, client: TestClient, name: str) -> None:
        self.id = uuid.uuid4()
        self.client = client
        self.token = make_token(self.id, email=f"{name}@example.test")
        self.h = {"Authorization": f"Bearer {self.token}"}
        r = client.patch("/v1/me", json={"display_name": name}, headers=self.h)
        assert r.status_code == 200, r.text

    def get(self, url: str, **kw: Any) -> Any:
        return self.client.get(url, headers=self.h, **kw)

    def post(self, url: str, json: Any = None, **kw: Any) -> Any:
        return self.client.post(url, json=json, headers=self.h, **kw)

    def put(self, url: str, json: Any = None) -> Any:
        return self.client.put(url, json=json, headers=self.h)

    def delete(self, url: str) -> Any:
        return self.client.delete(url, headers=self.h)


@pytest.fixture
def factory() -> Any:
    return get_sessionmaker()


INSTALL = "install-wearer-0001"
CAPS_ALL = {
    "health_connect": "available",
    "background_read": "granted",
    "metrics": {
        m: {"supported": True, "permission": "granted"}
        for m in ("heart_rate", "resting_heart_rate", "steps", "sleep")
    },
}


@pytest.fixture
def world(client: TestClient) -> dict[str, Any]:
    """Wearer with a live profile and registered collector; caregiver with an ACTIVE grant
    obtained through the real invite -> accept -> confirm flow; plus an unrelated stranger."""
    wearer, carer, stranger = User(client, "mother"), User(client, "son"), User(client, "x")
    r = wearer.post("/v1/health-profiles", {"display_name": "Mum", "contact_phone": "+91 98 7654"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = wearer.post(
        "/v1/collector-devices",
        {
            "health_profile_id": pid,
            "installation_id": INSTALL,
            "platform": "android",
            "capabilities": CAPS_ALL,
        },
    )
    assert r.status_code == 201, r.text
    collector_id = r.json()["id"]
    inv = wearer.post(f"/v1/health-profiles/{pid}/invitations", {}).json()
    g = carer.post("/v1/invitations/accept", {"token": inv["token"]}).json()
    r = wearer.post(f"/v1/grants/{g['id']}/confirm")
    assert r.json()["status"] == "active"
    return {
        "wearer": wearer,
        "carer": carer,
        "stranger": stranger,
        "pid": pid,
        "grant_id": g["id"],
        "collector_id": collector_id,
    }


def hr_record(
    rid: str,
    start: datetime,
    values: list[float],
    *,
    version: int = 1,
    origin: str = "com.fitbit.FitbitMobile",
    step_minutes: int = 1,
) -> dict[str, Any]:
    samples = [
        {
            "metric": "heart_rate",
            "value": v,
            "unit": "bpm",
            "time": (start + timedelta(minutes=i * step_minutes)).isoformat(),
        }
        for i, v in enumerate(values)
    ]
    end = start + timedelta(minutes=(len(values) - 1) * step_minutes)
    return {
        "record_type": "HeartRateRecord",
        "source_record_id": rid,
        "data_origin": origin,
        "record_version": version,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "samples": samples,
        "device": {"type": "WATCH", "manufacturer": "Google", "model": "Fitbit"},
    }


def daily_record(
    rtype: str,
    rid: str,
    start: datetime,
    value: float,
    *,
    end: datetime | None = None,
    origin: str = "com.fitbit.FitbitMobile",
    version: int = 1,
) -> dict[str, Any]:
    metric, unit = {
        "RestingHeartRateRecord": ("resting_heart_rate", "bpm"),
        "StepsRecord": ("steps", "count"),
        "SleepSessionRecord": ("sleep", "min"),
    }[rtype]
    rec: dict[str, Any] = {
        "record_type": rtype,
        "source_record_id": rid,
        "data_origin": origin,
        "record_version": version,
        "start_time": start.isoformat(),
        "samples": [{"metric": metric, "value": value, "unit": unit, "time": start.isoformat()}],
    }
    if end is not None:
        rec["end_time"] = end.isoformat()
    return rec


def batch(
    records: list[dict[str, Any]],
    *,
    deletions: list[str] | None = None,
    mode: str = "incremental",
    batch_id: str | None = None,
    data_mode: str = "live",
    installation: str = INSTALL,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "installation_id": installation,
        "batch_id": batch_id or str(uuid.uuid4()),
        "mode": mode,
        "data_mode": data_mode,
        "records": records,
        "deletions": [{"source_record_id": d} for d in (deletions or [])],
    }
