from __future__ import annotations

import uuid
from datetime import timedelta

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from tests.conftest import OTHER_KEY, User, batch, hr_record, make_token


def _get(client, token, url="/v1/me"):
    return client.get(url, headers={"Authorization": f"Bearer {token}"})


def test_valid_es256_token_accepted(client):
    assert _get(client, make_token(uuid.uuid4())).status_code == 200


def test_missing_token_rejected(client):
    assert client.get("/v1/me").status_code == 401


def test_expired_jwt_rejected(client):
    tok = make_token(uuid.uuid4(), exp_delta=timedelta(minutes=-5))
    r = _get(client, tok)
    assert r.status_code == 401 and r.json()["detail"] == "invalid_token"


def test_wrong_issuer_audience_signature_rejected(client):
    sub = uuid.uuid4()
    assert _get(client, make_token(sub, iss="https://evil.supabase.co/auth/v1")).status_code == 401
    assert _get(client, make_token(sub, aud="anon")).status_code == 401
    assert _get(client, make_token(sub, key=OTHER_KEY)).status_code == 401


def test_unsigned_or_subjectless_tokens_rejected(client):
    unsigned = jwt.encode(
        {"sub": str(uuid.uuid4()), "aud": "authenticated"}, None, algorithm="none"
    )
    assert _get(client, unsigned).status_code == 401
    assert _get(client, make_token(None)).status_code == 401
    assert _get(client, make_token("not-a-uuid")).status_code == 401


def test_service_role_token_cannot_act_as_user(client):
    assert _get(client, make_token(uuid.uuid4(), role="service_role")).status_code == 401


def test_hs256_legacy_secret_path_verified(client):
    from app.config import get_settings

    secret = get_settings().auth_jwt_secret
    good = make_token(uuid.uuid4(), key=secret, alg="HS256")
    bad = make_token(uuid.uuid4(), key="wrong-secret-wrong-secret-wrong-secret", alg="HS256")
    assert _get(client, good).status_code == 200
    assert _get(client, bad).status_code == 401


def test_disallowed_algorithm_rejected(client):
    other = ec.generate_private_key(ec.SECP384R1())
    tok = make_token(uuid.uuid4(), key=other, alg="ES384")  # alg not allowed
    assert _get(client, tok).status_code == 401


def test_cross_user_access_denied(world):
    s, pid = world["stranger"], world["pid"]
    for url in (
        f"/v1/health-profiles/{pid}/dashboard",
        f"/v1/health-profiles/{pid}/alerts",
        f"/v1/health-profiles/{pid}/rules",
        f"/v1/health-profiles/{pid}/measurements?metric=heart_rate"
        "&from=2026-10-01T00:00:00Z&to=2026-10-04T00:00:00Z",
    ):
        assert s.get(url).status_code == 404, url
    assert [p["id"] for p in s.get("/v1/health-profiles").json()] == []
    # Supplying someone else's profile id never grants anything.
    r = s.post(f"/v1/health-profiles/{pid}/invitations", {})
    assert r.status_code == 404


def test_caregiver_cannot_ingest_or_act_as_owner(world, client):
    c, pid = world["carer"], world["pid"]
    r = c.post(f"/v1/health-profiles/{pid}/ingest", batch([hr_record("x", world_time(), [70, 71])]))
    assert r.status_code == 403
    assert (
        c.post(
            "/v1/collector-devices",
            {
                "health_profile_id": pid,
                "installation_id": "carer-install-01",
                "platform": "android",
            },
        ).status_code
        == 403
    )
    assert c.post(f"/v1/health-profiles/{pid}/invitations", {}).status_code == 403
    rules = c.get(f"/v1/health-profiles/{pid}/rules").json()
    assert (
        c.put(f"/v1/health-profiles/{pid}/rules/{rules[0]['id']}", {"enabled": False}).status_code
        == 403
    )
    assert c.post(f"/v1/grants/{world['grant_id']}/confirm").status_code == 404
    assert c.delete(f"/v1/grants/{world['grant_id']}").status_code == 404
    assert c.delete(f"/v1/health-profiles/{pid}").status_code == 403
    assert c.post(f"/v1/health-profiles/{pid}/check-ins").status_code == 403
    assert c.post(f"/v1/collector-devices/{world['collector_id']}/heartbeat", {}).status_code == 404


def test_ingest_requires_registered_installation(world):
    w, pid = world["wearer"], world["pid"]
    r = w.post(
        f"/v1/health-profiles/{pid}/ingest",
        batch([hr_record("x", world_time(), [70])], installation="unregistered-01"),
    )
    assert r.status_code == 403 and r.json()["detail"] == "collector_not_registered"


def test_category_scoped_grant(client, world):
    w, pid = world["wearer"], world["pid"]
    other = User(client, "sleep-only")
    inv = w.post(f"/v1/health-profiles/{pid}/invitations", {"categories": ["sleep"]}).json()
    g = other.post("/v1/invitations/accept", {"token": inv["token"]}).json()
    w.post(f"/v1/grants/{g['id']}/confirm")
    d = other.get(f"/v1/health-profiles/{pid}/dashboard").json()
    assert [m["metric"] for m in d["metrics"]] == ["sleep"]
    assert d["collector"] is None
    url = (
        f"/v1/health-profiles/{pid}/measurements?metric=heart_rate"
        "&from=2026-10-01T00:00:00Z&to=2026-10-04T00:00:00Z"
    )
    assert other.get(url).status_code == 403
    assert other.get(f"/v1/health-profiles/{pid}/alerts").status_code == 403


def world_time():
    from tests.conftest import T0

    return T0 - timedelta(minutes=10)
