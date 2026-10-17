import pytest
from httpx import AsyncClient

from tests.conftest import SpyEmailSender
from user_service.core.email import EmailDeliveryFailed

pytestmark = pytest.mark.asyncio


async def _register_and_confirm(client: AsyncClient, email_sender: SpyEmailSender) -> None:
    resp = await client.post(
        "/auth/register",
        json={"email": "alice@example.com", "username": "alice", "password": "supersecret1"},
    )
    assert resp.status_code == 201

    code = email_sender.last_code()
    resp = await client.post(
        "/auth/register/confirm", json={"email": "alice@example.com", "code": code}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


async def test_register_confirm_login(client: AsyncClient, email_sender: SpyEmailSender) -> None:
    await _register_and_confirm(client, email_sender)

    resp = await client.post("/auth/login", json={"username": "alice", "password": "supersecret1"})
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body and "refresh_token" in body


async def test_login_before_confirm_is_rejected(
    client: AsyncClient, email_sender: SpyEmailSender
) -> None:
    await client.post(
        "/auth/register",
        json={"email": "bob@example.com", "username": "bob", "password": "supersecret1"},
    )
    resp = await client.post("/auth/login", json={"username": "bob", "password": "supersecret1"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == {"code": "AccountNotActive", "email": "bob@example.com"}


async def test_login_by_email_is_not_supported(
    client: AsyncClient, email_sender: SpyEmailSender
) -> None:
    await _register_and_confirm(client, email_sender)
    resp = await client.post(
        "/auth/login", json={"username": "alice@example.com", "password": "supersecret1"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "InvalidCredentials"


async def test_wrong_code_increments_attempts_and_eventually_blocks(
    client: AsyncClient, email_sender: SpyEmailSender
) -> None:
    await client.post(
        "/auth/register",
        json={"email": "carl@example.com", "username": "carl", "password": "supersecret1"},
    )

    for _ in range(5):
        resp = await client.post(
            "/auth/register/confirm", json={"email": "carl@example.com", "code": "000000"}
        )
    assert resp.status_code == 429


async def test_refresh_rotation_invalidates_old_token(
    client: AsyncClient, email_sender: SpyEmailSender
) -> None:
    await _register_and_confirm(client, email_sender)
    login_resp = await client.post(
        "/auth/login", json={"username": "alice", "password": "supersecret1"}
    )
    old_refresh = login_resp.json()["refresh_token"]

    refresh_resp = await client.post("/auth/refresh", json={"refresh_token": old_refresh})
    assert refresh_resp.status_code == 200

    reuse_resp = await client.post("/auth/refresh", json={"refresh_token": old_refresh})
    assert reuse_resp.status_code == 401


async def test_logout_all_invalidates_access_token(
    client: AsyncClient, email_sender: SpyEmailSender
) -> None:
    await _register_and_confirm(client, email_sender)
    login_resp = await client.post(
        "/auth/login", json={"username": "alice", "password": "supersecret1"}
    )
    access_token = login_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}

    me_resp = await client.get("/users/me", headers=headers)
    assert me_resp.status_code == 200

    logout_all_resp = await client.post("/auth/logout-all", headers=headers)
    assert logout_all_resp.status_code == 204

    me_resp_after = await client.get("/users/me", headers=headers)
    assert me_resp_after.status_code == 401


async def test_password_change_by_email_code(
    client: AsyncClient, email_sender: SpyEmailSender
) -> None:
    await _register_and_confirm(client, email_sender)
    login_resp = await client.post(
        "/auth/login", json={"username": "alice", "password": "supersecret1"}
    )
    tokens = login_resp.json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    resp = await client.post(
        "/auth/password/change/request", json={"current_password": "wrongpass1"}, headers=headers
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "WrongCurrentPassword"

    resp = await client.post(
        "/auth/password/change/request", json={"current_password": "supersecret1"}, headers=headers
    )
    assert resp.status_code == 204
    kind, to_email, code = email_sender.sent[-1]
    assert kind == "change" and to_email == "alice@example.com"

    resp = await client.post(
        "/auth/password/change",
        json={"code": "000000" if code != "000000" else "111111", "new_password": "newsecret22"},
        headers=headers,
    )
    assert resp.status_code == 400

    resp = await client.post(
        "/auth/password/change", json={"code": code, "new_password": "newsecret22"}, headers=headers
    )
    assert resp.status_code == 200
    new_tokens = resp.json()
    assert "access_token" in new_tokens and "refresh_token" in new_tokens

    assert (await client.get("/users/me", headers=headers)).status_code == 401
    resp = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 401
    new_headers = {"Authorization": f"Bearer {new_tokens['access_token']}"}
    assert (await client.get("/users/me", headers=new_headers)).status_code == 200

    resp = await client.post("/auth/login", json={"username": "alice", "password": "supersecret1"})
    assert resp.status_code == 401
    resp = await client.post("/auth/login", json={"username": "alice", "password": "newsecret22"})
    assert resp.status_code == 200


async def test_email_delivery_failure_is_503(
    client: AsyncClient, email_sender: SpyEmailSender
) -> None:
    async def broken(**_kwargs) -> None:
        raise EmailDeliveryFailed()

    email_sender.send_verification_code = broken
    resp = await client.post(
        "/auth/register",
        json={"email": "dan@example.com", "username": "dan", "password": "supersecret1"},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "EmailDeliveryFailed"
