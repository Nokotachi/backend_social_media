import io

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import SpyEmailSender
from user_service.auth.models import User
from user_service.core.bus import OutboxEvent, dispatch
from user_service.settings import get_settings
from user_service.users.events import HANDLERS

pytestmark = pytest.mark.asyncio


async def _signup(client: AsyncClient, spy: SpyEmailSender, username: str) -> dict[str, str]:
    email = f"{username}@example.com"
    resp = await client.post(
        "/auth/register",
        json={"email": email, "username": username, "password": "supersecret1"},
    )
    assert resp.status_code == 201, resp.text
    resp = await client.post(
        "/auth/register/confirm", json={"email": email, "code": spy.last_code()}
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post("/auth/login", json={"username": username, "password": "supersecret1"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_requires_token(client: AsyncClient) -> None:
    resp = await client.get("/users/me")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "InvalidAccessToken"


async def test_email_is_case_insensitive(client: AsyncClient, email_sender: SpyEmailSender) -> None:
    await _signup(client, email_sender, "alice")
    resp = await client.post(
        "/auth/register",
        json={"email": "ALICE@Example.com", "username": "alice2", "password": "supersecret1"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "EmailAlreadyRegistered"
    resp = await client.post(
        "/auth/register",
        json={"email": "other@example.com", "username": "ALICE", "password": "supersecret1"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "UsernameAlreadyTaken"
    resp = await client.post("/auth/login", json={"username": "Alice", "password": "supersecret1"})
    assert resp.status_code == 200


async def test_me_and_update(client: AsyncClient, email_sender: SpyEmailSender) -> None:
    alice = await _signup(client, email_sender, "alice")
    me = (await client.get("/users/me", headers=alice)).json()
    assert me["username"] == "alice" and me["display_name"] == "alice"
    assert me["follower_count"] == 0 and me["posts_count"] == 0

    resp = await client.patch(
        "/users/me", headers=alice, json={"display_name": "Alice A.", "bio": "hi"}
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Alice A." and resp.json()["bio"] == "hi"

    resp = await client.patch("/users/me", headers=alice, json={"bio": "x" * 151})
    assert resp.status_code == 422


async def test_avatar_upload(client: AsyncClient, email_sender: SpyEmailSender, tmp_path) -> None:
    get_settings().media_root = str(tmp_path)
    alice = await _signup(client, email_sender, "alice")
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(buf, format="PNG")
    resp = await client.post(
        "/users/me/avatar", headers=alice, files={"image": ("a.png", buf.getvalue(), "image/png")}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["avatar_url"].endswith(".png")

    buf = io.BytesIO()
    Image.new("RGB", (30, 10), "blue").save(buf, format="JPEG")
    resp = await client.post(
        "/users/me/avatar", headers=alice, files={"image": ("a.jpg", buf.getvalue(), "image/jpeg")}
    )
    saved = tmp_path / resp.json()["avatar_url"].split("/media/", 1)[1]
    with Image.open(saved) as img:
        assert img.size == (10, 10)

    resp = await client.post(
        "/users/me/avatar", headers=alice, files={"image": ("a.png", b"not an image", "image/png")}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "InvalidImage"


async def test_follow_unfollow_counters_and_lists(
    client: AsyncClient, email_sender: SpyEmailSender
) -> None:
    alice = await _signup(client, email_sender, "alice")
    bob = await _signup(client, email_sender, "bob")
    carl = await _signup(client, email_sender, "carl")

    resp = await client.post("/users/alice/follow", headers=bob)
    assert resp.status_code == 200
    assert resp.json() == {"is_following": True, "follower_count": 1}
    resp = await client.post("/users/alice/follow", headers=bob)
    assert resp.json()["follower_count"] == 1
    await client.post("/users/alice/follow", headers=carl)

    resp = await client.post("/users/bob/follow", headers=bob)
    assert resp.status_code == 400 and resp.json()["detail"] == "CannotFollowYourself"
    resp = await client.post("/users/nobody/follow", headers=bob)
    assert resp.status_code == 404

    profile = (await client.get("/users/ALICE", headers=bob)).json()
    assert profile["follower_count"] == 2
    assert profile["is_following"] is True and profile["is_me"] is False
    assert (await client.get("/users/bob", headers=bob)).json()["followee_count"] == 1

    page1 = (await client.get("/users/alice/followers?limit=1", headers=alice)).json()
    assert [u["username"] for u in page1["items"]] == ["carl"]
    assert page1["next_cursor"]
    page2 = (
        await client.get(
            f"/users/alice/followers?limit=1&cursor={page1['next_cursor']}", headers=alice
        )
    ).json()
    assert [u["username"] for u in page2["items"]] == ["bob"]
    assert page2["next_cursor"] is None

    following = (await client.get("/users/bob/following", headers=bob)).json()
    assert [u["username"] for u in following["items"]] == ["alice"]
    assert following["items"][0]["is_following"] is True

    resp = await client.delete("/users/alice/follow", headers=bob)
    assert resp.json() == {"is_following": False, "follower_count": 1}
    resp = await client.delete("/users/alice/follow", headers=bob)
    assert resp.json()["follower_count"] == 1
    assert (await client.get("/users/bob", headers=bob)).json()["followee_count"] == 0


async def test_search(client: AsyncClient, email_sender: SpyEmailSender) -> None:
    alice = await _signup(client, email_sender, "alice")
    await _signup(client, email_sender, "alicia")
    bob = await _signup(client, email_sender, "bob")
    await client.patch("/users/me", headers=bob, json={"display_name": "Bob Alison"})

    resp = await client.get("/search/users?q=ali", headers=alice)
    names = [u["username"] for u in resp.json()["items"]]
    assert names == ["alice", "alicia", "bob"]

    resp = await client.get("/search/users?q=alicia", headers=alice)
    assert [u["username"] for u in resp.json()["items"]] == ["alicia"]

    page1 = (await client.get("/search/users?q=ali&limit=2", headers=alice)).json()
    page2 = (
        await client.get(
            f"/search/users?q=ali&limit=2&cursor={page1['next_cursor']}", headers=alice
        )
    ).json()
    assert [u["username"] for u in page2["items"]] == ["bob"]


async def test_pending_user_is_invisible(client: AsyncClient, email_sender: SpyEmailSender) -> None:
    alice = await _signup(client, email_sender, "alice")
    await client.post(
        "/auth/register",
        json={"email": "p@example.com", "username": "pending1", "password": "supersecret1"},
    )
    assert (await client.get("/users/pending1", headers=alice)).status_code == 404
    assert (await client.get("/search/users?q=pend", headers=alice)).json()["items"] == []


async def _events(db: AsyncSession) -> list[tuple[str, dict]]:
    rows = await db.scalars(select(OutboxEvent).order_by(OutboxEvent.id))
    return [(e.routing_key, e.payload) for e in rows]


async def test_delete_account_fixes_counters(
    client: AsyncClient, email_sender: SpyEmailSender, db_session: AsyncSession
) -> None:
    alice = await _signup(client, email_sender, "alice")
    bob = await _signup(client, email_sender, "bob")
    bob_id = (await client.get("/users/me", headers=bob)).json()["id"]
    await client.post("/users/alice/follow", headers=bob)
    await client.post("/users/bob/follow", headers=alice)

    resp = await client.request("DELETE", "/users/me", headers=bob, json={"password": "wrong"})
    assert resp.status_code == 403

    resp = await client.request(
        "DELETE", "/users/me", headers=bob, json={"password": "supersecret1"}
    )
    assert resp.status_code == 204
    assert (await _events(db_session))[-1] == ("user.deleted", {"id": bob_id})

    me = (await client.get("/users/me", headers=alice)).json()
    assert me["follower_count"] == 0 and me["followee_count"] == 0
    assert (await client.get("/users/me", headers=bob)).status_code == 401


async def test_admin_block(
    client: AsyncClient, email_sender: SpyEmailSender, db_session: AsyncSession
) -> None:
    admin = await _signup(client, email_sender, "admin1")
    bob = await _signup(client, email_sender, "bob")
    resp = await client.post("/admin/users/bob/block", headers=bob)
    assert resp.status_code == 403

    await db_session.execute(update(User).where(User.username == "admin1").values(role="admin"))
    await db_session.commit()

    resp = await client.post("/admin/users/bob/block", headers=admin)
    assert resp.status_code == 204
    assert (await client.get("/users/me", headers=bob)).status_code == 401
    resp = await client.post("/auth/login", json={"username": "bob", "password": "supersecret1"})
    assert resp.status_code == 403 and resp.json()["detail"] == "AccountBlocked"
    assert (await client.get("/users/bob", headers=admin)).status_code == 404

    resp = await client.post("/admin/users/bob/unblock", headers=admin)
    assert resp.status_code == 204
    resp = await client.post("/auth/login", json={"username": "bob", "password": "supersecret1"})
    assert resp.status_code == 200


async def test_username_with_dot(client: AsyncClient, email_sender: SpyEmailSender) -> None:
    headers = await _signup(client, email_sender, "anna.k")
    assert (await client.get("/users/anna.k", headers=headers)).status_code == 200
    resp = await client.get("/search/users?q=a", headers=headers)
    assert resp.status_code == 422


async def test_login_lockout_after_5_failures(
    client: AsyncClient, email_sender: SpyEmailSender
) -> None:
    await _signup(client, email_sender, "alice")
    for login in ("alice", "Alice", "alice", "ALICE", "alice"):
        resp = await client.post("/auth/login", json={"username": login, "password": "wrongpass1"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "InvalidCredentials"
    resp = await client.post("/auth/login", json={"username": "aLiCe", "password": "supersecret1"})
    assert resp.status_code == 429
    assert resp.json()["detail"] == "TooManyLoginAttempts"
    assert int(resp.headers["Retry-After"]) > 0

    for _ in range(5):
        await client.post("/auth/login", json={"username": "ghost", "password": "wrongpass1"})
    resp = await client.post("/auth/login", json={"username": "ghost", "password": "wrongpass1"})
    assert resp.status_code == 429


async def test_posts_count_comes_from_content_events(
    client: AsyncClient, email_sender: SpyEmailSender, db_session: AsyncSession
) -> None:
    alice = await _signup(client, email_sender, "alice")
    bob = await _signup(client, email_sender, "bob")
    alice_id = (await client.get("/users/me", headers=alice)).json()["id"]
    assert (await client.get("/users/alice", headers=bob)).json()["posts_count"] == 0

    await dispatch(
        db_session, HANDLERS, "posts.counted", 5, {"user_id": alice_id, "posts_count": 7}
    )
    await dispatch(
        db_session, HANDLERS, "posts.counted", 3, {"user_id": alice_id, "posts_count": 2}
    )
    await dispatch(db_session, HANDLERS, "posts.counted", 6, {"user_id": 999, "posts_count": 1})
    await db_session.commit()
    assert (await client.get("/users/alice", headers=bob)).json()["posts_count"] == 7


async def test_events_for_other_services(
    client: AsyncClient, email_sender: SpyEmailSender, db_session: AsyncSession
) -> None:
    alice = await _signup(client, email_sender, "alice")
    await _signup(client, email_sender, "bob")
    ids = {u.username: u.id for u in (await db_session.scalars(select(User))).all()}

    await client.patch("/users/me", headers=alice, json={"display_name": "Алиса"})
    await client.post("/users/bob/follow", headers=alice)
    await client.delete("/users/bob/follow", headers=alice)
    await client.post("/auth/logout-all", headers=alice)

    events = await _events(db_session)
    alice_updates = [p for k, p in events if k == "user.updated" and p["id"] == ids["alice"]]
    assert alice_updates[0]["status"] == "active"
    assert alice_updates[1]["display_name"] == "Алиса"
    assert alice_updates[-1]["token_version"] == 2
    follows = [p for k, p in events if k == "follow.changed"]
    assert [f["following"] for f in follows] == [True, False]
    assert follows[0]["follower_id"] == ids["alice"] and follows[0]["followee_id"] == ids["bob"]
