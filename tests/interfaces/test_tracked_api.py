"""`GET /api/tracked` —— 追踪的**出口**。

## 为什么走真 HTTP 而不是直接调函数

与 `test_api_contract.py` 同一条理由：直接调函数会绕开三件真会坏的事 ——
路由注册顺序、状态码（400 与 200 的分工）、序列化（`None` 变 `null`、
元组变数组）。这三样都只在**过一遍 HTTP** 时才显形。

## 标记是**算**出来的，不是存下来的

所以这个文件里没有「跑一次抽取、再查标记」的步骤：写一份 `tracks.toml`、
种几条条目，然后查 —— 判据就是那个。改规则之后立刻生效这件事由
`test_改了规则立刻生效` 单独钉住（它是「标记不落库」那个取舍的**理由**）。

## 样本全部自己编

群号用 820000001 段、账号用 17700000000 段、标题是编出来的短句。
没有一条来自任何真实语料。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from yechao.bootstrap import Settings, make_app
from yechao.domain.models import Item, PendingMessage, SourceRef
from yechao.interfaces.api import create_app

GROUP = 820000001
TRACKED = 17700001001
OTHER = 17700002002


# ── 测试客户端（标准库，不引 TestClient —— 与 `test_api_contract.py` 同一条取向）──


class Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def get(self, path: str):
        # ※ **查询串要按百分号编码**：`term:奖学金` 里的中文直接拼进请求行
        # 会让 `http.client` 抛 `UnicodeEncodeError`（请求行必须是 ASCII）。
        # 这不是「测试写得麻烦」—— 真实调用方（前端 / curl）同样要编码，
        # 而漏掉它的症状是一条**看起来像服务没起来**的异常。
        from urllib.parse import quote

        req = urllib.request.Request(
            self.base + quote(path, safe="/?&=:"), method="GET"
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"null")
            except ValueError:
                return e.code, raw.decode("utf-8", "replace")


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_client(tmp_path, monkeypatch, tracks: str = "") -> Client:
    monkeypatch.delenv("YECHAO_DB_URL", raising=False)
    monkeypatch.delenv("YECHAO_REDIS_URL", raising=False)
    data, cfg = tmp_path / "data", tmp_path / "config"
    data.mkdir()
    cfg.mkdir()
    (cfg / "groups.toml").write_text(
        f'[[group]]\nid = {GROUP}\nname = "某班"\ntier = "normal"\nenabled = true\n',
        encoding="utf-8",
    )
    (cfg / "tracks.toml").write_text(tracks, encoding="utf-8")

    app_obj = make_app(settings=Settings.from_paths(data_dir=data, config_dir=cfg))
    _seed(app_obj)

    import uvicorn

    api = create_app(app_obj)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(api, host="127.0.0.1", port=port, log_level="critical")
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    else:  # pragma: no cover
        pytest.fail("服务没起来")

    client = Client(f"http://127.0.0.1:{port}")
    client._server = server  # type: ignore[attr-defined]
    client._thread = t  # type: ignore[attr-defined]
    client._app = app_obj  # type: ignore[attr-defined]
    return client


def _stop(client: Client) -> None:
    client._server.should_exit = True  # type: ignore[attr-defined]
    client._thread.join(timeout=5)  # type: ignore[attr-defined]
    client._app.close()  # type: ignore[attr-defined]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, _TRACKS)
    yield c
    _stop(c)


_TRACKS = f"""\
[[track]]
kind = "person"
value = {TRACKED}

[[track]]
kind = "term"
value = "奖学金"
"""


def _seed(app_obj) -> None:
    """种四条条目：

      1. 被追踪的那个人发的（`actor_account = TRACKED`）
      2. 标题里有「奖学金」
      3. 详情里有「奖学金」
      4. 什么都不命中（对照组）
    """
    app_obj.store.upsert_messages([
        PendingMessage(msg_id=1, group_id=GROUP, ts=1000, sender="甲", content="甲的消息"),
    ])

    def it(title, *, account=None, detail=None):
        return Item(
            item_id=0, kind="todo", kind_label="", title=title, detail=detail,
            event_ts=1000, deadline_ts=None, group_id=GROUP, group_name="",
            actor=None, actor_account=account, place=None, amount=None,
            links=(), source_count=1, start_ts=None,
        )

    app_obj.store.insert_items(
        [
            it("被追踪的人说的话", account=TRACKED),
            it("关于奖学金评定的通知"),
            it("通知", detail="奖学金名单已公示"),
            it("什么都不命中的一条", account=OTHER),
        ],
        [SourceRef(0, 1, "q")],
    )


# ── 判据一：查得到，而且带「命中了哪条规则」────────────────────────


def test_按规则查_带出命中的规则(client) -> None:
    st, body = client.get("/api/tracked")

    assert st == 200
    keys = [it["tracked"] for it in body["items"]]
    flat = {r["key"] for hit in keys for r in hit}
    assert flat == {f"person:{TRACKED}", "term:奖学金"}

    # 每条条目带的是**它自己**命中的那几条 —— 不是「全部规则」。
    by_title = {it["title"]: [r["key"] for r in it["tracked"]] for it in body["items"]}
    assert by_title["被追踪的人说的话"] == [f"person:{TRACKED}"]
    assert by_title["关于奖学金评定的通知"] == ["term:奖学金"]
    assert by_title["通知"] == ["term:奖学金"]


def test_没命中的条目不在结果里(client) -> None:
    st, body = client.get("/api/tracked")
    assert st == 200
    titles = {it["title"] for it in body["items"]}
    assert "什么都不命中的一条" not in titles
    assert body["total"] == 3


def test_每条规则带命中数(client) -> None:
    """※ 「配了但一次都没响」是这里最该被看见的一格 ——
    一个空的 `items` 看不出它来。"""
    st, body = client.get("/api/tracked")
    assert st == 200
    hits = {r["key"]: r["hits"] for r in body["rules"]}
    assert hits == {f"person:{TRACKED}": 1, "term:奖学金": 2}


def test_按单条规则筛(client) -> None:
    st, body = client.get(f"/api/tracked?rule=term:奖学金")
    assert st == 200
    assert body["total"] == 2
    assert all(
        it["tracked"] == [{"kind": "term", "value": "奖学金", "key": "term:奖学金"}]
        for it in body["items"]
    )

    st, body = client.get(f"/api/tracked?rule=person:{TRACKED}")
    assert st == 200
    assert [it["title"] for it in body["items"]] == ["被追踪的人说的话"]


def test_规则不在配置里回400而不是空列表(client) -> None:
    """※ **400，不是空列表。**

    空列表会被读成「这条规则没有命中过任何东西」—— 而真相是「这条规则
    根本不存在」。两者的下一步完全不同：一个去改判据，一个去改规则。
    与 `ledger --run` 那条同一条判据（「查不到这一次跑」与「这一趟没有账」
    必须分开说）。
    """
    st, body = client.get("/api/tracked?rule=term:不存在的东西")
    assert st == 400
    assert "不存在的东西" in body["detail"]
    assert "term:奖学金" in body["detail"], "要把现在有哪几条列出来"


# ── 判据二：标记是**算**的，所以改规则立刻生效 ──────────────────────


def test_改了规则立刻生效(tmp_path, monkeypatch) -> None:
    """※ 这一条是「标记不落库」那个取舍的**理由**。

    如果标记是抽取时存下来的，那么「加一条名词规则」对**已经抽过的**
    条目不会有任何效果 —— 而用户看到的是「我加了规则，这里什么都没有」，
    两边代码单看都正常。
    """
    c = _make_client(tmp_path, monkeypatch, _TRACKS)
    try:
        st, body = c.get("/api/tracked")
        assert st == 200 and body["total"] == 3
        cfg = c._app.settings.config_dir  # type: ignore[attr-defined]
        data = c._app.settings.data_dir  # type: ignore[attr-defined]
        _stop(c)

        # 再加一条名词规则，指向一条**早就存在**的条目。
        (cfg / "tracks.toml").write_text(
            _TRACKS + '\n[[track]]\nkind = "term"\nvalue = "什么都不命中"\n',
            encoding="utf-8",
        )

        app_obj = make_app(settings=Settings.from_paths(data_dir=data, config_dir=cfg))
        import uvicorn

        api = create_app(app_obj)
        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(api, host="127.0.0.1", port=port, log_level="critical")
        )
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        c2 = Client(f"http://127.0.0.1:{port}")
        c2._server = server  # type: ignore[attr-defined]
        c2._thread = t  # type: ignore[attr-defined]
        c2._app = app_obj  # type: ignore[attr-defined]
        try:
            st, body = c2.get("/api/tracked")
            assert st == 200
            assert body["total"] == 4, (
                "新加的规则对**已经存在的**条目没有生效 —— "
                "那说明标记是存下来的，而不是读的时候算的"
            )
            assert "什么都不命中的一条" in {it["title"] for it in body["items"]}
        finally:
            _stop(c2)
    except Exception:
        raise


# ── 判据三：空配置与边界 ────────────────────────────────────────────


def test_没有规则时是空列表而不是报错(tmp_path, monkeypatch) -> None:
    """※ 「没配追踪」是一个**合法状态**（它默认就是关的）——
    回 500 会让网页上那一块直接坏掉，而它本该什么都不显示。"""
    c = _make_client(tmp_path, monkeypatch, "")
    try:
        st, body = c.get("/api/tracked")
        assert st == 200
        assert body["items"] == []
        assert body["rules"] == []
        assert body["total"] == 0
    finally:
        _stop(c)


def test_匿名条目不被人物规则命中(tmp_path, monkeypatch) -> None:
    """※ 与 `search(persons=...)` 同一条判据：匿名条目的 `actor_account`
    是 `None`，**不允许用 0 冒充** —— 冒充会让「追踪某个账号」意外命中
    所有匿名条目，而那看起来像「这个人今天说了很多」。"""
    c = _make_client(
        tmp_path, monkeypatch, '[[track]]\nkind = "person"\nvalue = 0\n'
    )
    try:
        st, body = c.get("/api/tracked")
        # `value = 0` 本身就被值对象拒绝（账号必须是正整数）⇒ 400。
        assert st == 400
    finally:
        _stop(c)


def test_回看有上限而且说出来(client) -> None:
    """※ **不静默截断**：一个空的 `items` 看不出「看完了没命中」与
    「没看完」的区别，而这两句话的下一步完全不同。"""
    st, body = client.get("/api/tracked")
    assert st == 200
    assert body["scanned"] == 4, "扫的条数要如实报出来"
    assert body["truncated"] is False
    assert body["limit"] == 50 and body["offset"] == 0


def test_分页(client) -> None:
    st, body = client.get("/api/tracked?limit=1&offset=1")
    assert st == 200
    assert body["total"] == 3, "总数是**命中总数**，不是这一页的条数"
    assert len(body["items"]) == 1


def test_条目字段仍然对上前端契约(client) -> None:
    """※ `/api/tracked` 复用 `view.item_out` —— 所以它多出来的只有
    `tracked` 这一个键，而**别的键一个不少**。

    少一个键 = 页面上静默少一栏（`web/src/api.ts` 的 `as T` 是断言，
    不是校验）。
    """
    st, body = client.get("/api/tracked")
    assert st == 200
    item = body["items"][0]
    expected = {
        "item_id", "kind", "kind_label", "title", "detail",
        "event_ts", "deadline_ts", "group_id", "group_name", "actor",
        "actor_account", "place", "amount", "links", "source_count",
        "start_ts", "tracked",
    }
    assert set(item) == expected, (
        f"缺字段 {expected - set(item)}，多字段 {set(item) - expected}"
    )
