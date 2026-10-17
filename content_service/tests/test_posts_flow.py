import pytest
from httpx import AsyncClient

from tests.conftest import Users, make_post, png_bytes

pytestmark = pytest.mark.asyncio


async def test_requires_token(client: AsyncClient) -> None:
    resp = await client.get("/feed/following")
    assert resp.status_code == 401 and resp.json()["detail"] == "InvalidAccessToken"


async def test_create_get_delete_post(client: AsyncClient, users: Users, tmp_path) -> None:
    alice = await users.create("alice")
    bob = await users.create("bob")

    post = await make_post(client, alice, caption="первый пост", n_images=3)
    assert post["status"] == "published"
    assert [i["position"] for i in post["images"]] == [0, 1, 2]
    assert post["author"]["username"] == "alice" and post["is_mine"] is True

    path = post["images"][0]["url"].split("/media/", 1)[1]
    assert (tmp_path / path).exists()

    seen = (await client.get(f"/posts/{post['id']}", headers=bob)).json()
    assert seen["is_mine"] is False and seen["likes_count"] == 0

    resp = await client.delete(f"/posts/{post['id']}", headers=bob)
    assert resp.status_code == 403 and resp.json()["detail"] == "NotPostAuthor"
    assert (await client.delete(f"/posts/{post['id']}", headers=alice)).status_code == 204
    assert (await client.get(f"/posts/{post['id']}", headers=bob)).status_code == 404
    assert not (tmp_path / path).exists()


async def test_create_post_validation(client: AsyncClient, users: Users) -> None:
    alice = await users.create("alice")
    img = ("images", ("a.png", png_bytes(), "image/png"))

    resp = await client.post("/posts", headers=alice, files=[img], data={"aspect_ratio": "3:2"})
    assert resp.status_code == 400 and resp.json()["detail"] == "InvalidAspectRatio"

    resp = await client.post(
        "/posts", headers=alice, files=[img] * 11, data={"aspect_ratio": "1:1"}
    )
    assert resp.json()["detail"] == "InvalidImagesCount"

    resp = await client.post(
        "/posts",
        headers=alice,
        files=[img, ("images", ("a.png", b"nope", "image/png"))],
        data={"aspect_ratio": "1:1"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == {"code": "InvalidImage", "image_index": 1}

    small = ("images", ("s.png", png_bytes(size=(500, 500)), "image/png"))
    resp = await client.post(
        "/posts", headers=alice, files=[img, img, small], data={"aspect_ratio": "1:1"}
    )
    assert resp.json()["detail"] == {"code": "ImageTooSmall", "image_index": 2}

    wide = ("images", ("w.png", png_bytes(size=(1200, 600)), "image/png"))
    resp = await client.post("/posts", headers=alice, files=[wide], data={"aspect_ratio": "1:1"})
    assert resp.json()["detail"] == {"code": "ImageAspectRatioMismatch", "image_index": 0}

    portrait = ("images", ("p.png", png_bytes(size=(600, 750)), "image/png"))
    resp = await client.post(
        "/posts", headers=alice, files=[portrait], data={"aspect_ratio": "4:5"}
    )
    assert resp.status_code == 201


async def test_following_feed_and_pagination(client: AsyncClient, users: Users) -> None:
    alice = await users.create("alice")
    bob = await users.create("bob")
    carl = await users.create("carl")
    await users.follow("alice", "bob")

    b1 = await make_post(client, bob, "b1")
    await make_post(client, carl, "c1")
    a1 = await make_post(client, alice, "a1")
    b2 = await make_post(client, bob, "b2")

    page1 = (await client.get("/feed/following?limit=1", headers=alice)).json()
    assert [p["id"] for p in page1["items"]] == [b2["id"]]
    page2 = (
        await client.get(f"/feed/following?limit=1&cursor={page1['next_cursor']}", headers=alice)
    ).json()
    assert [p["id"] for p in page2["items"]] == [b1["id"]]
    assert page2["next_cursor"] is None
    assert a1["id"] not in [p["id"] for p in page1["items"] + page2["items"]]

    assert (await client.post(f"/posts/{b2['id']}/hide", headers=alice)).status_code == 204
    feed = (await client.get("/feed/following", headers=alice)).json()
    assert b2["id"] not in [p["id"] for p in feed["items"]]
    assert (await client.get(f"/posts/{b2['id']}", headers=alice)).status_code == 200
    await client.delete(f"/posts/{b2['id']}/hide", headers=alice)
    feed = (await client.get("/feed/following", headers=alice)).json()
    assert b2["id"] in [p["id"] for p in feed["items"]]


async def test_author_grid_and_processing_visibility(client: AsyncClient, users: Users) -> None:
    alice = await users.create("alice")
    bob = await users.create("bob")
    p1 = await make_post(client, alice, "p1")
    p2 = await make_post(client, alice, "p2")
    await users.sql("UPDATE posts SET status = 'processing' WHERE id = :i", i=p2["id"])

    own = (await client.get("/posts?author=alice", headers=alice)).json()
    assert [p["id"] for p in own["items"]] == [p2["id"], p1["id"]]
    other = (await client.get("/posts?author=ALICE&limit=3", headers=bob)).json()
    assert [p["id"] for p in other["items"]] == [p1["id"]]
    assert (await client.get(f"/posts/{p2['id']}", headers=bob)).status_code == 404
    assert (await client.get("/posts?author=nobody", headers=bob)).status_code == 404


async def test_likes_favorites_reposts(client: AsyncClient, users: Users) -> None:
    alice = await users.create("alice")
    bob = await users.create("bob")
    post = await make_post(client, alice)
    pid = post["id"]

    resp = await client.post(f"/posts/{pid}/like", headers=bob)
    assert resp.json() == {"is_liked": True, "likes_count": 1}
    resp = await client.post(f"/posts/{pid}/like", headers=bob)
    assert resp.json()["likes_count"] == 1
    await client.post(f"/posts/{pid}/like", headers=alice)

    assert (await client.post(f"/posts/{pid}/favorite", headers=bob)).json() == {
        "is_favorited": True
    }
    assert (await client.post(f"/posts/{pid}/repost", headers=bob)).json() == {
        "is_reposted": True,
        "reposts_count": 1,
    }

    seen = (await client.get(f"/posts/{pid}", headers=bob)).json()
    assert (seen["likes_count"], seen["is_liked"], seen["is_favorited"], seen["is_reposted"]) == (
        2,
        True,
        True,
        True,
    )

    favs = (await client.get("/favorites", headers=bob)).json()
    assert [p["id"] for p in favs["items"]] == [pid]

    assert (await client.delete(f"/posts/{pid}/like", headers=bob)).json() == {
        "is_liked": False,
        "likes_count": 1,
    }
    await client.delete(f"/posts/{pid}/favorite", headers=bob)
    assert (await client.get("/favorites", headers=bob)).json()["items"] == []
    assert (await client.post("/posts/999999/like", headers=bob)).status_code == 404


async def test_popular_batch_and_search(client: AsyncClient, users: Users) -> None:
    alice = await users.create("alice")
    bob = await users.create("bob")
    carl = await users.create("carl")
    p1 = await make_post(client, alice, "закат над морем")
    p2 = await make_post(client, alice, "котик")
    p3 = await make_post(client, bob, "старый пост")
    await users.sql(
        "UPDATE posts SET created_at = now() - interval '20 days' WHERE id = :i", i=p3["id"]
    )
    for h in (bob, carl):
        await client.post(f"/posts/{p2['id']}/like", headers=h)
    await client.post(f"/posts/{p3['id']}/like", headers=carl)

    popular = (await client.get("/posts/popular?days=7", headers=carl)).json()
    assert [p["id"] for p in popular["items"]] == [p2["id"], p1["id"]]
    own = (await client.get("/posts/popular?days=7", headers=alice)).json()
    assert own["items"] == []
    popular30 = (await client.get("/posts/popular?days=30&limit=2", headers=carl)).json()
    assert [p["id"] for p in popular30["items"]] == [p2["id"], p3["id"]]
    assert popular30["next_cursor"]

    batch = (
        await client.post("/posts/batch", headers=carl, json={"ids": [p3["id"], 999, p1["id"]]})
    ).json()
    assert [p["id"] for p in batch["items"]] == [p3["id"], p1["id"]]

    found = (await client.get("/search/posts?q=МОРЕМ", headers=carl)).json()
    assert [p["id"] for p in found["items"]] == [p1["id"]]
    explore = (await client.get("/search/posts", headers=carl)).json()
    assert len(explore["items"]) == 3


async def test_report_and_admin(client: AsyncClient, users: Users) -> None:
    alice = await users.create("alice")
    bob = await users.create("bob")
    admin = await users.create("admin1", role="admin")
    post = await make_post(client, alice)

    resp = await client.post(
        f"/posts/{post['id']}/report",
        headers=bob,
        json={"reason": "spam", "description": "реклама"},
    )
    assert resp.status_code == 201
    resp = await client.post(f"/posts/{post['id']}/report", headers=bob, json={"reason": "spam"})
    assert resp.status_code == 409 and resp.json()["detail"] == "AlreadyReported"
    resp = await client.post(f"/posts/{post['id']}/report", headers=bob, json={"reason": "nope"})
    assert resp.status_code == 422

    assert (await client.get("/admin/reports", headers=bob)).status_code == 403
    reports = (await client.get("/admin/reports", headers=admin)).json()["items"]
    assert reports[0]["description"] == "[spam] реклама"
    assert reports[0]["reporter_username"] == "bob"

    assert (await client.delete(f"/posts/{post['id']}", headers=admin)).status_code == 204
    assert (await client.get("/admin/reports", headers=admin)).json()["items"] == []


async def test_blocked_author_is_hidden(client: AsyncClient, users: Users) -> None:
    alice = await users.create("alice")
    bob = await users.create("bob")
    post = await make_post(client, alice)
    await users.set_status("alice", "blocked")
    assert (await client.get(f"/posts/{post['id']}", headers=bob)).status_code == 404
    assert (await client.get("/feed/following", headers=alice)).status_code == 403


async def test_posts_count_events_and_purge(client: AsyncClient, users: Users, tmp_path) -> None:
    alice = await users.create("alice")
    bob = await users.create("bob")
    a1 = await make_post(client, alice)
    a2 = await make_post(client, alice)
    b1 = await make_post(client, bob)
    await client.delete(f"/posts/{a2['id']}", headers=alice)
    await client.post(f"/posts/{b1['id']}/like", headers=alice)
    await client.post(f"/posts/{b1['id']}/comments", headers=alice, json={"text": "привет"})
    await client.post(f"/posts/{a1['id']}/comments", headers=bob, json={"text": "ответ"})
    alice_id = users.id("alice")

    counts = [
        p for k, p in await users.outbox() if k == "posts.counted" and p["user_id"] == alice_id
    ]
    assert [c["posts_count"] for c in counts] == [1, 2, 1]

    image = tmp_path / a1["images"][0]["url"].split("/media/", 1)[1]
    assert image.exists()
    await users.delete("alice")
    assert not image.exists()
    assert (await client.get(f"/posts/{a1['id']}", headers=bob)).status_code == 404
    post = (await client.get(f"/posts/{b1['id']}", headers=bob)).json()
    assert post["likes_count"] == 0 and post["comments_count"] == 0
    assert (await client.get("/feed/following", headers=alice)).status_code == 403
    await users.set_status("alice", "active")
    assert (await client.get("/feed/following", headers=alice)).status_code == 403


async def test_logout_all_and_unknown_user(client: AsyncClient, users: Users) -> None:
    alice = await users.create("alice")
    assert (await client.get("/feed/following", headers=alice)).status_code == 200
    users.by_name["alice"]["token_version"] = 2
    await users.set_status("alice", "active")
    assert (await client.get("/feed/following", headers=alice)).status_code == 401

    await users.sql("DELETE FROM user_replicas")
    bob = await users.create("bob")
    await users.sql("DELETE FROM user_replicas")
    assert (await client.get("/feed/following", headers=bob)).status_code == 200
