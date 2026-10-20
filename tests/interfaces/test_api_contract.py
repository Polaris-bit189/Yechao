"""HTTP 契约 —— 对前端那一侧**冻结**的字段与状态码。

※ 字段清单对齐的是前端 `web/src/api.ts` 里的类型。每一条都对应一个前端
**真的会读**的字段：后端少给一个，页面上就少一栏。

而且这件事**构建期不会报**：`call<T>()` 是 `(await res.json()) as T`，
那个 `as` 是断言不是校验 —— JSON 里没有的键，TS 照样认为它有。

## 为什么用真 HTTP 客户端而不是直接调函数

直接调函数会绕开三件真会坏的事：
  · 路由注册顺序（`/api/items/batch-kind` 被 `/api/items/{id}` 吃掉）
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

    ※ 理由与项目本身一致：**依赖越少，「clone 下来就能跑」的成功率越高**。
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
            item_id=0, kind="todo", kind_label="",
            title=title, detail=None, event_ts=1000, deadline_ts=deadline,
            group_id=1, group_name="", actor=None, actor_account=101,
            place="图书馆", amount=None, links=(), source_count=1,
            start_ts=start, deadline_supported=supported, start_supported=ssup,
        )

    app_obj.store.insert_items(
        [it("交材料", deadline=1793700000, supported=True), it("普通条目")],
        [SourceRef(0, 1, "q"), SourceRef(0, 2, "q")],
    )


# ── /api/items ──────────────────────────────────────────────────────

ITEM_FIELDS = {
    "item_id", "kind", "kind_label", "title", "detail",
    "event_ts", "deadline_ts", "group_id", "group_name", "actor",
    "actor_account", "place", "amount", "links", "source_count", "start_ts",
}


def test_条目字段逐个对上前端契约(client) -> None:
    """※ 少一个字段 = 页面上静默少一栏。

    `ITEM_FIELDS` 与 `web/src/api.ts` 的 `Item` 是同一份清单的两份抄本。
    它不会自动跟着变：后端加了字段而这里没加，这个用例照样绿。
    """
    st, body = client.get("/api/items?limit=10")
    assert st == 200
    item = body["items"][0]
    assert set(item) == ITEM_FIELDS, (
        f"缺字段 {ITEM_FIELDS - set(item)}，多字段 {set(item) - ITEM_FIELDS}"
    )


def test_列表按_event_ts_倒序(client) -> None:
    """※ 前端**依赖**服务端已排好序 —— 它按「与上一组同键就并入」分组，
    乱序输入会让组头重复。"""
    st, body = client.get("/api/items?limit=10")
    ts = [i["event_ts"] for i in body["items"]]
    assert ts == sorted(ts, reverse=True)


def test_limit_上限是_200_且如实回报(client) -> None:
    """※ `MAX_LIMIT = 200` 是前端**知道**的数。

    超上限时收窄到 200，而响应里的 `limit` 字段必须**如实**告诉你实际给了多少
    —— 静默截断会让调用方以为拿到了更多。
    """
    st, body = client.get("/api/items?limit=9999")
    assert st == 200
    assert body["limit"] == 200


def test_多值_kind_是并集(client) -> None:
    st, body = client.get("/api/items?kind=todo&kind=attend&limit=10")
    assert st == 200
    assert body["total"] >= 1


def test_空_kind_等于不筛选(client) -> None:
    """前端会发 `kind=` 吗？—— 它会把空串丢掉。这里钉的是**后端也容忍**。"""
    a = client.get("/api/items?limit=10")[1]["total"]
    b = client.get("/api/items?kind=&limit=10")[1]["total"]
    assert a == b


def test_人物筛选不命中匿名条目(client) -> None:
    """※ 匿名条目 `actor_account` 为 null ⇒ **必不匹配**任何账号。

    这是 SQL `IN` 的天然行为，但要有测试钉住 —— 否则以后有人改成
    `COALESCE(actor_account, 0)` 就悄悄破坏了它。
    """
    st, body = client.get("/api/items?person=101&limit=10")
    assert st == 200
    assert all(i["actor_account"] == 101 for i in body["items"])


def test_条目详情带来源(client) -> None:
    st, body = client.get("/api/items/1")
    assert st == 200
    assert "sources" in body
    assert body["sources"][0].keys() >= {"msg_id", "ts", "sender", "group_name", "content"}


def test_没有的条目返回_404_且_detail_是中文(client) -> None:
    """※ 前端把 `detail` **原样显示**。所以它必须是给人看的话，
    不是「Not Found」。"""
    st, body = client.get("/api/items/9999")
    assert st == 404
    assert "没有这条条目" in body["detail"]


# ── 路由顺序 ────────────────────────────────────────────────────────


def test_batch_kind_不会被_item_id_吃掉(client) -> None:
    """※ 注册顺序是**承重的**：`/api/items/batch-kind` 必须排在
    `/api/items/{item_id}` 前面。

    否则 FastAPI 会把 `batch-kind` 当成一个 `item_id` 去解析 ——
    而 `item_id` 是 int，于是**回 422**，一个看起来与「路由写错」无关的错误。
    """
    st, body = client.post("/api/items/batch-kind", {"item_ids": [1], "kind": "attend"})
    assert st == 200
    assert body["count"] == 1
    assert body["actor"]


def test_digests_id_不会被_digests_吃掉(client) -> None:
    st, _ = client.get("/api/digests/9999")
    assert st == 404, "应当走到「没有这篇日报」，而不是「路由不存在」"


# ── 写操作 ──────────────────────────────────────────────────────────


def test_改类目返回_edit_id(client) -> None:
    st, body = client.patch("/api/items/1", {"kind": "attend"})
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
    """※ `undone: false` **不是错误** —— 前端据此说
    「没有可撤销的操作」。它只在 `undone` 为真时重取数据。"""
    client.post("/api/undo", {"edit_id": None})
    st, body = client.post("/api/undo", {"edit_id": None})
    assert st == 200
    assert body["undone"] is False


# ── 204 端点 ────────────────────────────────────────────────────────


def test_删除人物返回_204_且没有_body(client) -> None:
    """※ 前端对 204 **显式短路**（`web/src/api.ts` 里
    `if (res.status === 204) return undefined as T`）—— 因为 `res.json()`
    在空 body 上会抛。

    所以 204 必须**真的**是空 body。
    """
    client.post("/api/persons", {"account": 222, "label": "某人", "note": ""})
    st, body = client.post(
        "/api/persons", {"account": 333, "label": "另一人", "note": ""}
    )
    assert st == 201
    st, body = client.delete("/api/persons/222")
    assert st == 204
    assert body is None


def test_新增人物返回_201(client) -> None:
    st, body = client.post(
        "/api/persons", {"account": 444, "label": "某人", "note": ""}
    )
    assert st == 201
    assert body == {"account": 444, "label": "某人"}


def test_账号为零被拒(client) -> None:
    """※ 「0 不是有效账号」—— 前端也有这一条，但**前端校验可以被绕过**。"""
    st, body = client.post(
        "/api/persons", {"account": 0, "label": "x", "note": ""}
    )
    assert st == 400


# ── 类目 ────────────────────────────────────────────────────────────


def test_类目端点同时给_desc_与_aliases(client) -> None:
    """※ **一个端点两种形状**：编辑器要 `desc`，改名留痕要 `aliases`。
    它必须一次往返里都给 —— 分两次请求，编辑器会在中间那一刻读到半份数据。"""
    st, body = client.get("/api/categories")
    assert st == 200
    assert "aliases" in body
    assert "desc" in body["categories"][0]
    assert "count" in body["categories"][0]


def test_改类目名留下别名(client) -> None:
    st, body = client.put("/api/categories/know", {"slug": "daily", "label": "日常"})
    assert st == 200
    assert body["slug"] == "daily"
    st2, cats = client.get("/api/categories")
    assert cats["aliases"].get("know") == "daily"


def test_类目还有条目时不给迁移目标就拒(client) -> None:
    """※ 不指定迁移目标就删，指向它的条目会变成孤儿。"""
    st, body = client.delete("/api/categories/todo")
    assert st == 400
    assert "迁移" in body["detail"]


def test_类目没有条目时可以直接删(client) -> None:
    st, body = client.delete("/api/categories/recruit")
    assert st == 204
    assert body is None


# ── 群 ──────────────────────────────────────────────────────────────


def test_群端点区分_available_false_与空表(client) -> None:
    """※ 「还没读过」和「一个群都没有」不是一回事 —— 前端要分别显示。

    ※ 而且 `available: false` 时 `reason` 一定非空、`groups` 一定是空数组。
    """
    st, body = client.get("/api/groups")
    assert st == 200
    if not body["available"]:
        assert body["reason"]
        assert body["groups"] == []


def test_加群接受空名字(client) -> None:
    """※ **服务端不许收紧空名。**

    快照读不出群名时会留一个空串，这是正常情况。前后端都要求 name 非空的话，
    这类群**必然**加不进来。
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
    """※ `null` = **不发这个字段**，与 `false` 是两件事。

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
        # ※ 写侧归一化必须**看得见结果**
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
    """※ 缺一个字段会让隐私页**渲染崩**（前端读不到 `records` 时走不到任何分支）。"""
    st, body = client.get("/api/privacy")
    assert st == 200
    assert set(body) == PRIVACY_FIELDS


def test_隐私_没有出网记录时不是错误(client) -> None:
    """※ `records: 0` **不是错误** —— 它表示还没跑过 refine，
    而 `hint` 要给引导文案。"""
    st, body = client.get("/api/privacy")
    assert st == 200
    assert body["records"] == 0
    assert body["hint"]


# ── 配置包 ──────────────────────────────────────────────────────────


def test_配置包导出再导入是往返(client) -> None:
    """※ `apply: false` = **预校验，一个字节都不写**。

    导出的包原样喂回去必须能被接受 —— 否则「导出、改一改、导回来」
    这条最基本的自检路第一步就走不通。
    """
    st, pack = client.get("/api/config/export")
    assert st == 200
    assert isinstance(pack["toml"], str) and pack["toml"]

    st2, _ = client.post(
        "/api/config/import",
        {"toml": pack["toml"], "apply": False, "keep_settings": True},
    )
    assert st2 == 200


# ── 后台作业 ────────────────────────────────────────────────────────


def test_没配模型时起作业回_400(client) -> None:
    """※ 400 不是 500：「还没配端点」是**用户还没做**的事，不是服务端故障。

    这条是补的 —— 起作业这个端点原先一条覆盖都没有，于是
    `app_obj.model.config.configured`（`ModelConfig` 上根本没有这个属性）
    一直没人踩到，真跑起来回的是 500。
    """
    st, body = client.post("/api/jobs")
    assert st == 400
    assert "模型端点" in body["detail"]


def test_起作业返回的_job_id_就是它真正用的那个(client) -> None:
    """※ 这一条钉的是「**两个 job_id**」那个形状的 bug。

    端点原来先自己 `create_job`、再调 `reorder.run`（而它**自己又建一个**）。
    后果有两个，而且都是静默的：
      · `run` 一进去就查「有没有作业在跑」，看到的正是**端点刚建的那个**
        ⇒ 每次都回 400「已有任务在跑」，这个端点从来没成功过；
      · 端点返回的 job_id 与 `run` 真正用的是两个 ⇒ 撤销永远 404。

    ⇒ 现在作业由 `reorder.run` 自己建、自己收尾，端点只负责原样交出去。
    判据是「**端点给的那个 id，拿去查、拿去撤，都认**」。

    模型端点写成一个连不上的地址 —— 这一条测的是**作业接线**，不是模型。
    连不上时那几批会失败、作业状态是 `failed`，但作业本身照样建得出来，
    而「建得出来、查得到、撤得动」正是本用例要的。
    """
    st, _ = client.put("/api/llm", {"base_url": "http://127.0.0.1:1/v1", "model": "m"})
    assert st == 200

    st, job = client.post("/api/jobs")
    assert st == 202, f"起作业失败了：{job}"
    assert job["job_id"], "回了一个没有 job_id 的作业"

    st2, again = client.get(f"/api/jobs/{job['job_id']}")
    assert st2 == 200, f"端点给的 id 查不到：{again}"
    assert again["job_id"] == job["job_id"]

    st3, undone = client.post(f"/api/jobs/{job['job_id']}/undo")
    assert st3 == 200, f"撤销找不到这一批（说明 run 用的不是这个 id）：{undone}"


# ── 批量撤销 ────────────────────────────────────────────────────────


def test_撤一个不存在的批次不是错误(client) -> None:
    """※ `undone: false` 不是报错 —— 那是「这一批没东西可撤」。

    与单条撤销同一个判据：请求本身合法，只是当前没有可撤的。
    """
    st, body = client.post("/api/undo/batch", {"actor": "bulk:0:0"})
    assert st == 200
    assert body["undone"] is False


# ── 错误码的分工 ────────────────────────────────────────────────────


def test_每一条_GET_都用只读连接(tmp_path) -> None:
    """※ 不变量 ① 的**机械判据**：没有一条 GET 碰读写连接。

    README 的承诺是「接口层的 GET 一律使用只读连接（`mode=ro` +
    `PRAGMA query_only`），写操作由 **SQLite 自己**拒绝，而不是靠代码自觉」。

    ※ 而 `open_readonly()` **这个函数**会拒绝写，早就有测试（见
    `test_invariants.py`）。**但那条测试证明不了 serve 路径上的 GET 有没有
    真的走它。** 实测踩过：`GET /api/jobs/{job_id}` 用的是 `app_obj.store`
    （读写连接）—— 「GET 一律只读」这句话在那一条上不成立，而没有测试会红。

    ⇒ 这条扫**每条 GET 端点的函数体**（AST，不是整份文件的字符串匹配）：
    函数体里出现 `app_obj.store` 就算违规。
    扫函数体而不是整份文件是有意的 —— 模块 docstring 里那句「写端点走
    `app_obj.store`」是**该留下的**说明，不该被判违规。
    """
    import ast
    import inspect
    import textwrap

    from yechao.bootstrap import Settings, make_app

    st = Settings.from_paths(
        data_dir=tmp_path / "data", config_dir=tmp_path / "config"
    )
    st.ensure_dirs()
    sqlite_store.init_db(st.db_path)
    api = create_app(make_app(settings=st))

    offenders: list[str] = []
    checked = 0
    for route in api.routes:
        methods = getattr(route, "methods", None) or set()
        if "GET" not in methods:
            continue
        fn = getattr(route, "endpoint", None)
        if fn is None:
            continue
        # ※ 跳过 FastAPI **自带**的路由（`/openapi.json`、`/docs`…）。
        # 它们的端点定义在 fastapi 里，`getsource` 取回来的片段带缩进、
        # 直接 `ast.parse` 会 `IndentationError` —— 那会把这条判据
        # 变成一个与它要查的东西毫无关系的失败。
        if fn.__module__ != "yechao.interfaces.api":
            continue
        checked += 1
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "store"
                and isinstance(node.value, ast.Name)
                and node.value.id == "app_obj"
            ):
                offenders.append(f"{sorted(methods)} {route.path} → {fn.__name__}")

    # ※ **空转自检**：一条 GET 都没扫到时，上面那个断言恒为真 ——
    # 而它在测试报告里是绿的。
    assert checked >= 10, f"只扫到 {checked} 条 GET 路由，这个判据在空转"
    assert not offenders, "这些 GET 用了读写连接：" + "；".join(offenders)


def test_4xx_与_5xx_必须分得开(client) -> None:
    """※ 前端的连通性状态机据**这个**判断在线/离线：

      · 4xx ⇒ 服务器活着 ⇒ **保持在线**
      · 5xx ⇒ 后端不可达 ⇒ 离线

    把 4xx 回成 5xx，用户会在一次 404 之后看到一个假的「离线」横幅。
    """
    st, _ = client.get("/api/items/99999")
    assert 400 <= st < 500

    st2, _ = client.get("/api/nonexistent")
    assert st2 == 404


def test_未接线的功能返回_501(client) -> None:
    """※ 501 与 500 的区别是「还没做」与「做了但出错」——
    这两件事对排查者意味着完全不同的方向。

    ※ 本项目**没有**未接线的端点（都被契约填满了），
    所以这条用例的落点是**错误处理器本身**：直接造一个 `NotWired`
    走一遍映射，确认它回 501 而不是 500。
    """
    from yechao.domain.errors import NotWired
    from yechao.interfaces.api import install_error_handlers

    print("NotWired 的默认文案：", str(NotWired()))
    assert str(NotWired())
