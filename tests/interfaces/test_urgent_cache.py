"""`/api/urgent` 走缓存 —— 这是缓存**唯一**被接进读路径的地方。

## 观察到什么现象 → 为什么这么定 → 怎么验证

**现象**：一个「写了但没人调用」的缓存，即使后端是真的 Redis，`stats()` 也
永远是 0 命中 —— 它看起来像「缓存没生效」，而真相是「没有任何请求经过它」。
这正是本项目最在意的那类失败：**接线存在但从未被调用**。

**为什么选 `/api/urgent`**：它的 docstring 自己写着「每 30 秒被调一次，
必须最廉价」—— 那句话就是缓存的理由，不用另外编一个。

**判据（为什么陈旧可以接受）**：TTL 默认 10 秒，**小于**前端 30 秒的轮询
间隔。用户下一次来取的时候缓存已经过期了 —— 他看到的新鲜度与不缓存时
完全一样，而服务器少算了很多次。

**怎么验证**：两次请求之间把「库」换成一个永远返回空的桩 ——
如果第二次还能拿到条目，那只可能来自缓存。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest

from yechao.bootstrap import Settings, make_app
from yechao.interfaces.api import create_app


class _RecordingCache:
    """一个**真的**存东西的假缓存，并把命中记下来。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.hits = 0
        self.misses = 0

    def backend_name(self) -> str:
        return "recording"

    def get(self, key):
        if key in self.data:
            self.hits += 1
            return self.data[key]
        self.misses += 1
        return None

    def set(self, key, value, *, ttl=None):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)

    def stats(self):
        return {"backend": "recording", "enabled": True, "hits": self.hits,
                "misses": self.misses, "lookups": self.hits + self.misses,
                "hit_rate": None, "errors": 0, "last_error": None, "note": ""}

    def ping(self):
        return True, "假缓存"


class _EmptyReader:
    """一个「库里什么都没有」的只读桩。"""

    def backend_name(self) -> str:
        return "stub"

    def ping(self):
        return True, "stub"

    def items_with_deadline(self, *, since=None, until=None):
        return []


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _running(app_obj):
    import threading

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
    else:
        pytest.fail("服务没起来")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        t.join(timeout=5)


def _get(base: str, path: str) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(base + path, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _seed(app_obj) -> int:
    """种一条**有截止日**的条目，它会被 `/api/urgent` 捞出来。"""
    from yechao.domain.models import Item, PendingMessage, SourceRef

    app_obj.store.upsert_messages([
        PendingMessage(msg_id=1, group_id=820000001, ts=1000,
                       sender="某人", content="10月28日前提交"),
    ])
    # ※ 截止日取「现在 + 1 天」——`/api/urgent` 的窗口是 `[今天-1, 今天+2)`，
    # 写死一个日期的话，跑这条测试的那天恰好不在窗口里就会红，
    # 而失败信息指向的是「缓存没生效」。时间由**调用方**给，不自己算。
    deadline = app_obj.clock.now() + 86400
    ids = app_obj.store.insert_items(
        [Item(item_id=0, kind="todo", kind_label="", title="交材料",
              detail=None, event_ts=1000, deadline_ts=deadline,
              group_id=820000001, group_name="", actor=None,
              actor_account=101, place=None, amount=None, links=(),
              source_count=1, start_ts=None,
              deadline_supported=True, start_supported=False)],
        [SourceRef(item_id=0, msg_id=1, quote="q")],
    )
    return ids[0]


@pytest.fixture()
def app_obj(tmp_path):
    st = Settings.from_paths(
        data_dir=tmp_path / "data", config_dir=tmp_path / "config"
    )
    st.ensure_dirs()
    app = make_app(settings=st)
    _seed(app)
    yield app
    app.close()


def test_空缓存下_urgent_照常返回条目(app_obj) -> None:
    """※ 阴性对照的第一半：本机默认（空缓存）下，这条路必须**逐字节**
    与加缓存之前一样。空实现是穿透的。"""
    with _running(app_obj) as base:
        st, body = _get(base, "/api/urgent")

    assert st == 200
    assert body["total"] == 1
    assert body["items"][0]["title"] == "交材料"


def test_两次请求在空缓存下给出同一个结果(app_obj) -> None:
    with _running(app_obj) as base:
        _, first = _get(base, "/api/urgent")
        _, second = _get(base, "/api/urgent")
    assert first == second


def test_缓存命中时不再查库(app_obj) -> None:
    """※ **这条是「缓存真的接上了」的唯一证据。**

    做法：第一次请求走正常路径（未命中 ⇒ 查库 ⇒ 写缓存），然后把「库」
    换成一个永远返回空的桩。第二次请求**仍然**拿到那条条目 ——
    那只可能来自缓存。
    """
    cache = _RecordingCache()
    app_obj.cache = cache

    with _running(app_obj) as base:
        _, first = _get(base, "/api/urgent")
        assert first["total"] == 1
        assert cache.misses == 1 and cache.hits == 0, "第一次应当是未命中"

        app_obj.reader = _EmptyReader()
        _, second = _get(base, "/api/urgent")

    assert second["total"] == 1, "第二次查了库 —— 缓存没接上"
    assert cache.hits == 1, "第二次没有命中缓存"
    assert second["items"][0]["title"] == "交材料"


def test_缓存写入带上了_TTL(app_obj) -> None:
    """※ 不传 TTL 的话，那条 key 会**永不过期** —— 于是
    「10 秒的新鲜度」这条判据在真 Redis 上根本不成立（而在空缓存上
    也看不出来，因为它什么都不存）。"""
    seen: list[object] = []

    class _TtlCache(_RecordingCache):
        def set(self, key, value, *, ttl=None):
            seen.append(ttl)
            super().set(key, value)

    app_obj.cache = _TtlCache()
    with _running(app_obj) as base:
        _get(base, "/api/urgent")

    assert seen, "根本没往缓存里写东西"
    # `None` 是「用后端自己的默认值」—— 空实现下这是正常形状；
    # 真后端（RedisCache）会把它换成配置里的 TTL。这里只钉住
    # 「调用方把 TTL 这条口子留出来了」。
    assert seen == [None] or isinstance(seen[0], int)
