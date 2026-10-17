import pytest
from httpx import AsyncClient

from tests.conftest import Users, make_post

pytestmark = pytest.mark.asyncio


async def test_comments_and_replies(client: AsyncClient, users: Users) -> None:
    alice = await users.create("alice")
    bob = await users.create("bob")
    carl = await users.create("carl")
    post = await make_post(client, alice)
    pid = post["id"]

    c1 = (await client.post(f"/posts/{pid}/comments", headers=bob, json={"text": "круто"})).json()
    c2 = (await client.post(f"/posts/{pid}/comments", headers=carl, json={"text": "ага"})).json()
    r1 = (
        await client.post(
            f"/posts/{pid}/comments",
            headers=alice,
            json={"text": "спасибо", "parent_comment_id": c1["id"]},
        )
    ).json()
    r2 = (
        await client.post(
            f"/posts/{pid}/comments",
            headers=bob,
            json={"text": ":)", "parent_comment_id": r1["id"]},
        )
    ).json()
    assert r2["parent_comment_id"] == c1["id"]

    top = (await client.get(f"/posts/{pid}/comments?limit=1", headers=carl)).json()
    assert [c["id"] for c in top["items"]] == [c1["id"]]
    assert top["items"][0]["replies_count"] == 2
    top2 = (
        await client.get(f"/posts/{pid}/comments?limit=1&cursor={top['next_cursor']}", headers=carl)
    ).json()
    assert [c["id"] for c in top2["items"]] == [c2["id"]] and top2["next_cursor"] is None

    replies = (await client.get(f"/comments/{c1['id']}/replies", headers=carl)).json()
    assert [c["id"] for c in replies["items"]] == [r1["id"], r2["id"]]

    assert (await client.get(f"/posts/{pid}", headers=carl)).json()["comments_count"] == 4

    resp = await client.post(f"/posts/{pid}/comments", headers=bob, json={"text": "   "})
    assert resp.status_code == 400
    resp = await client.post(f"/posts/{pid}/comments", headers=bob, json={"text": "x" * 501})
    assert resp.status_code == 422


async def test_comment_likes_and_delete_rights(client: AsyncClient, users: Users) -> None:
    alice = await users.create("alice")
    bob = await users.create("bob")
    carl = await users.create("carl")
    post = await make_post(client, alice)
    pid = post["id"]
    c1 = (await client.post(f"/posts/{pid}/comments", headers=bob, json={"text": "1"})).json()
    await client.post(
        f"/posts/{pid}/comments", headers=carl, json={"text": "r", "parent_comment_id": c1["id"]}
    )

    assert (await client.post(f"/comments/{c1['id']}/like", headers=carl)).json() == {
        "is_liked": True,
        "likes_count": 1,
    }
    listed = (await client.get(f"/posts/{pid}/comments", headers=carl)).json()["items"][0]
    assert listed["is_liked"] is True and listed["can_delete"] is False
    assert (await client.delete(f"/comments/{c1['id']}/like", headers=carl)).json()[
        "likes_count"
    ] == 0

    resp = await client.delete(f"/comments/{c1['id']}", headers=carl)
    assert resp.status_code == 403
    assert (await client.delete(f"/comments/{c1['id']}", headers=alice)).status_code == 204
    assert (await client.get(f"/posts/{pid}", headers=alice)).json()["comments_count"] == 0


async def test_reply_to_deleted_comment(client: AsyncClient, users: Users) -> None:
    alice = await users.create("alice")
    bob = await users.create("bob")
    post = await make_post(client, alice)
    c1 = (
        await client.post(f"/posts/{post['id']}/comments", headers=bob, json={"text": "1"})
    ).json()
    await client.delete(f"/comments/{c1['id']}", headers=bob)
    resp = await client.post(
        f"/posts/{post['id']}/comments",
        headers=alice,
        json={"text": "r", "parent_comment_id": c1["id"]},
    )
    assert resp.status_code == 404 and resp.json()["detail"] == "CommentNotFound"
