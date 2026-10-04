from __future__ import annotations

from tests.conftest import User


def test_pairing_requires_wearer_confirmation(client):
    w, c = User(client, "mother"), User(client, "son")
    pid = w.post("/v1/health-profiles", {"display_name": "Mum"}).json()["id"]
    inv = w.post(f"/v1/health-profiles/{pid}/invitations", {}).json()
    assert len(inv["token"]) >= 20
    g = c.post("/v1/invitations/accept", {"token": inv["token"]})
    assert g.status_code == 201 and g.json()["status"] == "pending"
    # Pending grant gives no access.
    assert c.get(f"/v1/health-profiles/{pid}/dashboard").status_code == 404
    # Wearer sees the caregiver's identity before confirming.
    pending = w.get(f"/v1/health-profiles/{pid}/grants").json()
    assert pending[0]["caregiver_email"] == "son@example.test"
    assert w.post(f"/v1/grants/{g.json()['id']}/confirm").json()["status"] == "active"
    assert c.get(f"/v1/health-profiles/{pid}/dashboard").status_code == 200
    assert [p["role"] for p in c.get("/v1/health-profiles").json()] == ["caregiver"]


def test_invitation_single_use_and_expiry(client, clock):
    w, c1, c2 = User(client, "mother"), User(client, "a"), User(client, "b")
    pid = w.post("/v1/health-profiles", {"display_name": "Mum"}).json()["id"]
    inv = w.post(f"/v1/health-profiles/{pid}/invitations", {}).json()
    assert c1.post("/v1/invitations/accept", {"token": inv["token"]}).status_code == 201
    assert c2.post("/v1/invitations/accept", {"token": inv["token"]}).status_code == 410
    inv2 = w.post(f"/v1/health-profiles/{pid}/invitations", {}).json()
    clock.advance(minutes=31)
    assert c2.post("/v1/invitations/accept", {"token": inv2["token"]}).status_code == 410
    assert w.post("/v1/invitations/accept", {"token": "x" * 32}).status_code == 410


def test_owner_cannot_accept_own_invitation(client):
    w = User(client, "mother")
    pid = w.post("/v1/health-profiles", {"display_name": "Mum"}).json()["id"]
    inv = w.post(f"/v1/health-profiles/{pid}/invitations", {}).json()
    assert w.post("/v1/invitations/accept", {"token": inv["token"]}).status_code == 400


def test_revocation_effective_on_next_request(world):
    w, c, pid = world["wearer"], world["carer"], world["pid"]
    assert c.get(f"/v1/health-profiles/{pid}/dashboard").status_code == 200
    r = w.delete(f"/v1/grants/{world['grant_id']}")
    assert r.json()["status"] == "revoked"
    assert c.get(f"/v1/health-profiles/{pid}/dashboard").status_code == 404
    assert c.get(f"/v1/health-profiles/{pid}/alerts").status_code == 404
    assert c.get("/v1/health-profiles").json() == []
    # A revoked grant cannot be re-confirmed.
    assert w.post(f"/v1/grants/{world['grant_id']}/confirm").status_code == 409


def test_owner_deletion_removes_data_and_grants(world, factory):
    from sqlalchemy import func, select

    from app.models import AuditEvent, CaregiverGrant, HealthProfile

    w, c, pid = world["wearer"], world["carer"], world["pid"]
    assert w.delete(f"/v1/health-profiles/{pid}").status_code == 204
    assert c.get(f"/v1/health-profiles/{pid}/dashboard").status_code == 404
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(HealthProfile)) == 0
        assert db.scalar(select(func.count()).select_from(CaregiverGrant)) == 0
        actions = db.scalars(select(AuditEvent.action)).all()
        assert "health_profile.delete" in actions


def test_simulation_and_live_profiles_are_separate(client):
    w = User(client, "mother")
    live = w.post("/v1/health-profiles", {"display_name": "Mum"}).json()
    sim = w.post("/v1/health-profiles", {"display_name": "Demo", "data_mode": "simulation"}).json()
    assert live["data_mode"] == "live" and sim["data_mode"] == "simulation"
    assert w.post("/v1/health-profiles", {"display_name": "Again"}).status_code == 409


def test_delete_me_removes_all_user_data(world, factory):
    from sqlalchemy import func, select

    from app.models import CaregiverGrant, HealthProfile, Profile, PushDevice

    c = world["carer"]
    c.post(
        "/v1/push-devices",
        {"installation_id": "carer-phone-001", "token": "t" * 20, "platform": "ios"},
    )
    assert c.delete("/v1/me").status_code == 204
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(CaregiverGrant)) == 0
        assert db.scalar(select(func.count()).select_from(PushDevice)) == 0
        assert db.get(Profile, c.id) is None
        assert db.scalar(select(func.count()).select_from(HealthProfile)) == 1  # wearer's
    w = world["wearer"]
    assert w.delete("/v1/me").status_code == 204
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(HealthProfile)) == 0
