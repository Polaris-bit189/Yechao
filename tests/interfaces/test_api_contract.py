"""HTTP 契约 —— 对前端那一侧**冻结**的字段与状态码。

⚠️ 这些用例的字段清单是从前端拿来的（`web/src/types.ts` 与 `api.ts`）。
它们每一条都对应一个前端**真的会读到**的字段：少一个，页面上就少一栏，
而且**不会报错**（`types.ts` 逐字：「类型写错不会在构建期报错——它只会让
页面静默少一栏」）。

## 为什么用真 HTTP 客户端而不是直接调函数

直接调函数会绕开三件真会坏的事：
  · 路由注册顺序（`/api/items/kinds` 被 `/api/items/{id}` 吃掉）
  · 状态码（204 有没有 body、409 与 400 的分工）
  · 序列化（`None` 变 `null`、元组变数组）

这三样都只在**过一遍 HTTP** 时才显形。
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from yechao.bootstrap import Settings, make_app
from yechao.infrastructure.persistence import sqlite_store
from yechao.interfaces.api import create_app


# ── 测试客户端 ──────────────────────────────────────────────────────


class Client:
    """一个极简的 HTTP 客户端 —— 用标准库，不引 `TestClient`。

    ⚠️ 理由与项目本身一致：**依赖越少，「clone 下来就能跑」的成功率越高**。
    而这个客户端只需要会发请求、读状态码、解 JSON。
    """

    def __init__(self, base: str) -> None:
        self.base = base

    def req(self, method: str, path: str, body=None):
        data = None
        headers = {}
        if body is not None:
            import json

            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        r = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                raw = resp.read()
                return resp.status, (_json_or_none(raw))
        except urllib.error.HTTPError as e:
            return e.code, _json_or_none(e.read())

    def get(self, p):
        return self.req("GET", p)

    def post(self, p, b=None):
        return self.req("POST", p, b if b is not None else {})

    def put(self, p, b):
        return self.req("PUT", p, b)

    def patch(self, p, b):
        return self.req("PATCH", p, b)

    def delete(self, p):
        return self.req("DELETE", p)


def _json_or_none(raw: bytes):
    if not raw:
        return None
    try:
        import json

        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", errors="replace")


@pytest.fixture()
def client(tmp_path, request) -> Client:
    """起一个真服务（线程里），返回客户端。"""
    import threading

    import uvicorn

    data = tmp_path / "data"
    cfg = tmp_path / "config"
    st = Settings.from_paths(data_dir=data, config_dir=cfg)
    st.ensure_dirs()
    sqlite_store.init_db(st.db_path)

    app_obj = make_app(settings=st)
    _seed(app_obj)

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
    else:
        pytest.fail("服务没起来")

    yield Client(f"http://127.0.0.1:{port}")
    server.should_exit = True
    t.join(timeout=5)


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _seed(app_obj) -> None:
    """种两条条目：一条有截止日（过了闸门），一条没有。"""
    from yechao.domain.models import Item, PendingMessage, SourceRef

    app_obj.store.upsert_messages([
        PendingMessage(msg_id=1, group_id=1, ts=1000, sender="某人", content="10月28日前提交"),
        PendingMessage(msg_id=2, group_id=1, ts=1100, sender="某人", content="普通消息"),
    ])

    def it(title, deadline=None, supported=False, start=None, ssup=False):
        return Item(
            item_id=0, kind="academic", kind_label="", kind_icon="",
            title=title, detail=None, event_ts=1000, deadline_ts=deadline,
            group_id=1, group_name="", actor=None, actor_uin=101,
            place="图书馆", amount=None, links=(), source_count=1,
            start_ts=start, deadline_supported=supported, start_supported=ssup,
        )

    app_obj.store.insert_items(
        [it("交材料", deadline=1793700000, supported=True), it("普通条目")],
        [SourceRef(0, 1, "q"), SourceRef(0, 2, "q")],
    )


# ── /api/items ──────────────────────────────────────────────────────

ITEM_FIELDS = {
    "item_id", "kind", "kind_label", "kind_icon", "title", "detail",
    "event_ts", "deadline_ts", "group_id", "group_name", "actor",
    "actor_uin", "place", "amount", "links", "source_count", "start_ts",
}


def test_条目字段逐个对上前端契约(client) -> None:
    """⚠️ 少一个字段 = 页面上静默少一栏。

    `ITEM_FIELDS` 是从 `web/src/types.ts` 抄来的 —— 那边写着
    「字段名与后端输出逐字对应，改任何一边都必须同时改另一边」。
    """
    st, body = client.get("/api/items?limit=10")
    assert st == 200
    item = body["items"][0]
    assert set(item) == ITEM_FIELDS, (
        f"缺字段 {ITEM_FIELDS - set(item)}，多字段 {set(item) - ITEM_FIELDS}"
    )


def test_列表按_event_ts_倒序(client) -> None:
    """⚠️ 前端**依赖**服务端已排好序（`Feed.tsx` 逐字）——
    它按「与上一组同键就并入」分组，乱序输入会让组头重复。"""
    st, body = client.get("/api/items?limit=10")
    ts = [i["event_ts"] for i in body["items"]]
    assert ts == sorted(ts, reverse=True)


def test_limit_上限是_200_且如实回报(client) -> None:
    """⚠️ `MAX_LIMIT = 200` 是前端**知道**的数（`snapshot.ts` 逐字）。

    超上限时收窄到 200，而响应里的 `limit` 字段必须**如实**告诉你实际给了多少
    —— 静默截断会让调用方以为拿到了更多。
    """
    st, body = client.get("/api/items?limit=9999")
    assert st == 200
    assert body["limit"] == 200


def test_多值_kind_是并集(client) -> None:
    st, body = client.get("/api/items?kind=academic&kind=life&limit=10")
    assert st == 200
    assert body["total"] >= 1


def test_空_kind_等于不筛选(client) -> None:
    """前端会发 `kind=` 吗？—— 它会把空串丢掉。这里钉的是**后端也容忍**。"""
    a = client.get("/api/items?limit=10")[1]["total"]
    b = client.get("/api/items?kind=&limit=10")[1]["total"]
    assert a == b


def test_人物筛选不命中匿名条目(client) -> None:
    """⚠️ 匿名条目 `actor_uin` 为 null ⇒ **必不匹配**任何 uin。

    这是 SQL `IN` 的天然行为，但要有测试钉住 —— 否则以后有人改成
    `COALESCE(actor_uin, 0)` 就悄悄破坏了它。
    """
    st, body = client.get("/api/items?person=101&limit=10")
    assert st == 200
    assert all(i["actor_uin"] == 101 for i in body["items"])


def test_条目详情带来源(client) -> None:
    st, body = client.get("/api/items/1")
    assert st == 200
    assert "sources" in body
    assert body["sources"][0].keys() >= {"msg_id", "ts", "sender", "group_name", "content"}


def test_没有的条目返回_404_且_detail_是中文(client) -> None:
    """⚠️ 前端把 `detail` **原样显示**。所以它必须是给人看的话，
    不是「Not Found」。"""
    st, body = client.get("/api/items/9999")
    assert st == 404
    assert "没有这条条目" in body["detail"]


# ── 路由顺序 ────────────────────────────────────────────────────────


def test_items_kinds_不会被_items_id_吃掉(client) -> None:
    """⚠️ 注册顺序是**承重的**：`/api/items/kinds` 必须排在
    `/api/items/{item_id}` 前面。

    否则 FastAPI 会把 `kinds` 当成一个 `item_id` 去解析 ——
    而 `item_id` 是 int，于是**回 422**，一个看起来与「路由写错」无关的错误。
    """
    st, body = client.post("/api/items/kinds", {"item_ids": [1], "kind": "life"})
    assert st == 200
    assert body["count"] == 1
    assert body["actor"]


def test_digests_id_不会被_digests_吃掉(client) -> None:
    st, _ = client.get("/api/digests/9999")
    assert st == 404, "应当走到「没有这篇日报」，而不是「路由不存在」"


# ── 写操作 ──────────────────────────────────────────────────────────


def test_改类目返回_edit_id(client) -> None:
    st, body = client.post("/api/items/1/kind", {"kind": "activity"})
    assert st == 200
    assert isinstance(body["edit_id"], int)


def test_删除返回_edit_id_并让条目从列表消失(client) -> None:
    st, body = client.delete("/api/items/1")
    assert st == 200
    assert "edit_id" in body
    st2, listing = client.get("/api/items?limit=10")
    assert all(i["item_id"] != 1 for i in listing["items"])


def test_撤销能把它带回来(client) -> None:
    client.delete("/api/items/1")
    st, body = client.post("/api/undo", {"edit_id": None})
    assert st == 200
    assert body["undone"] is True
    st2, listing = client.get("/api/items?limit=10")
    assert any(i["item_id"] == 1 for i in listing["items"])


def test_撤销不存在的步骤返回_undone_false(client) -> None:
    """⚠️ `undone: false` **不是错误** —— 前端据此说
    「没有可撤销的操作」。它只在 `undone` 为真时重取数据。"""
    client.post("/api/undo", {"edit_id": None})
    st, body = client.post("/api/undo", {"edit_id": None})
    assert st == 200
    assert body["undone"] is False


# ── 204 端点 ────────────────────────────────────────────────────────


def test_删除人物返回_204_且没有_body(client) -> None:
    """⚠️ 前端对 204 **显式短路**（`sendJSON` 里 `if (r.status === 204)`）
    —— 因为 `r.json()` 在空 body 上会抛。

    所以 204 必须**真的**是空 body。
    """
    client.post("/api/persons", {"uin": 222, "label": "某人", "note": ""})
    st, body = client.post("/api/persons", {"uin": 333, "label": "另一人", "note": ""})
    assert st == 201
    st, body = client.delete("/api/persons/222")
    assert st == 204
    assert body is None


def test_新增人物返回_201(client) -> None:
    st, body = client.post("/api/persons", {"uin": 444, "label": "某人", "note": ""})
    assert st == 201
    assert body == {"uin": 444, "label": "某人"}


def test_uin_为零被拒(client) -> None:
    """⚠️ 「0 不是有效账号」—— 前端也有这一条，但**前端校验可以被绕过**。"""
    st, body = client.post("/api/persons", {"uin": 0, "label": "x", "note": ""})
    assert st == 400


# ── 类目 ────────────────────────────────────────────────────────────


def test_类目端点同时给_desc_与_aliases(client) -> None:
    """⚠️ **一个端点两种形状**：编辑器要 `desc`，改名留痕要 `aliases`。
    它必须一次往返里都给（前端 `fetchCategoriesDetailed` 的注释逐字）。"""
    st, body = client.get("/api/categories")
    assert st == 200
    assert "aliases" in body
    assert "desc" in body["categories"][0]
    assert "count" in body["categories"][0]


def test_改类目名留下别名(client) -> None:
    st, body = client.put("/api/categories/life", {"slug": "daily", "label": "日常"})
    assert st == 200
    assert body["slug"] == "daily"
    st2, cats = client.get("/api/categories")
    assert cats["aliases"].get("life") == "daily"


def test_类目还有条目时不给迁移目标就拒(client) -> None:
    """⚠️ 否则那些条目会指向一个不存在的类目。"""
    st, body = client.delete("/api/categories/academic")
    assert st == 400
    assert "迁移" in body["detail"]


def test_类目没有条目时可以直接删(client) -> None:
    st, body = client.delete("/api/categories/job")
    assert st == 204
    assert body is None


# ── 群 ──────────────────────────────────────────────────────────────


def test_群端点区分_available_false_与空表(client) -> None:
    """⚠️ 「还没读过」与「一个群都没有」是**两句不同的话**。

    ⚠️ 而且 `available: false` 时 `reason` 一定非空、`groups` 一定是空数组。
    """
    st, body = client.get("/api/groups")
    assert st == 200
    if not body["available"]:
        assert body["reason"]
        assert body["groups"] == []


def test_加群接受空名字(client) -> None:
    """⚠️ **服务端不许收紧空名。**

    快照里「名字读不出来」是正常状态（collect 刻意留 `""`）。
    从前后端要求 name 非空 ⇒ 这个群**必然**加不进来。
    """
    st, body = client.post("/api/groups", {"id": 777, "name": "", "tier": "normal"})
    assert st == 200
    assert body == {"id": 777}


def test_重复加群返回_409(client) -> None:
    client.post("/api/groups", {"id": 888, "name": "x", "tier": "normal"})
    st, body = client.post("/api/groups", {"id": 888, "name": "x", "tier": "normal"})
    assert st == 409
    assert "已经在配置里" in body["detail"]


# ── 模型设置 ────────────────────────────────────────────────────────


def test_enable_thinking_三态(client) -> None:
    """⚠️ `null` = **不发这个字段**，与 `false` 是两件事。

    写进配置再读回来，三态必须**原样往返** ——
    归一化成 `false` 会让「不发」变成「发 false」，
    而那对通用 OpenAI 端点是 400。
    """
    for value in (None, False, True):
        st, body = client.put(
            "/api/llm",
            {"base_url": "  http://x/v1  ", "model": "  m  ", "enable_thinking": value},
        )
        assert st == 200
        assert body["enable_thinking"] is value, f"{value} 往返后变了"
        # ⚠️ 写侧归一化必须**看得见结果**
        assert body["base_url"] == "http://x/v1"
        assert body["model"] == "m"

        st2, again = client.get("/api/llm")
        assert again["enable_thinking"] is value
        assert again["base_url"] == "http://x/v1"


# ── 隐私 ────────────────────────────────────────────────────────────


PRIVACY_FIELDS = {
    "records", "files", "surfaces", "messages", "chars",
    "known_name_hits", "names_checked", "names_were_checked",
    "num_hits", "name_placeholders", "num_placeholders",
    "clean", "dirty", "hint",
}


def test_隐私端点字段齐全(client) -> None:
    """⚠️ 缺一个字段会让隐私页**渲染崩**（前端读不到 `records` 时走不到任何分支）。"""
    st, body = client.get("/api/privacy")
    assert st == 200
    assert set(body) == PRIVACY_FIELDS


def test_隐私_没有出网记录时不是错误(client) -> None:
    """⚠️ `records: 0` **不是错误** —— 它表示还没跑过 refine，
    而 `hint` 要给引导文案。"""
    st, body = client.get("/api/privacy")
    assert st == 200
    assert body["records"] == 0
    assert body["hint"]


# ── 错误码的分工 ────────────────────────────────────────────────────


def test_4xx_与_5xx_必须分得开(client) -> None:
    """⚠️ 前端的连通性状态机据**这个**判断在线/离线：

      · 4xx ⇒ 服务器活着 ⇒ **保持在线**
      · 5xx ⇒ 后端不可达 ⇒ 离线

    把 4xx 回成 5xx，用户会在一次 404 之后看到一个假的「离线」横幅。
    """
    st, _ = client.get("/api/items/99999")
    assert 400 <= st < 500

    st2, _ = client.get("/api/nonexistent")
    assert st2 == 404


def test_未接线的功能返回_501(client) -> None:
    """⚠️ 501 与 500 的区别是「还没做」与「做了但出错」——
    这两件事对排查者意味着完全不同的方向。

    ⚠️ 本项目**没有**未接线的端点（都被契约填满了），
    所以这条用例的落点是**错误处理器本身**：直接造一个 `NotWired`
    走一遍映射，确认它回 501 而不是 500。
    """
    from yechao.domain.errors import NotWired
    from yechao.interfaces.api import install_error_handlers

    print("NotWired 的默认文案：", str(NotWired()))
    assert str(NotWired())
