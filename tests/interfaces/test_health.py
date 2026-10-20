"""`GET /api/health` —— 三问（服务 / 库 / 缓存），以及它**为什么这么回状态码**。

## 观察到什么现象 → 为什么这么定 → 怎么验证

**现象**：`deploy/docker-compose.yml` 原来的健康检查探的是
`/api/items?limit=1`。它只证明了「库能读出东西」，有两个盲区：

· **验不到缓存**。缓存挂掉时它照样 200。
· **分不清两种不健康**。「服务活着但库坏了」与「服务根本没起来」在编排层
  看起来是一样的（都只是「不健康」），可排查方向完全不同。

**判据（这一条是判断，不是事实）**：

· 库挂了 ⇒ **503**。理由是这个产品的**每一条**路由都要读库，库挂了整个
  产品就没法用 —— 一个回 200 的健康检查会把这件事说成「健康」。
· 缓存挂了 ⇒ **仍然 200**，但响应体里 `cache.ok: false`、`degraded: true`。
  理由是缓存是**降级**不是故障：产品仍然完整可用（只是慢）。把它也算成
  「不健康」的后果是编排层反复重启一个本来就正常的容器 —— 而重启修不好
  Redis，只会让它一直重启。

**怎么验证**：起一个真 HTTP 服务，分别把库和缓存换成会坏的桩，看状态码与
响应体。※ 用真 HTTP 而不是直接调函数 —— 状态码是 FastAPI 从 `Response`
上读的，直接调函数会把「它到底有没有被用上」这件事测掉。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from yechao.bootstrap import Settings, VERSION, make_app
from yechao.interfaces.api import create_app

ROOT = Path(__file__).resolve().parents[2]


# ── 桩 ──────────────────────────────────────────────────────────────


class _StubStore:
    """一个「坏掉的库」。※ 它只实现被健康端点用到的两个方法。"""

    def __init__(self, ok: bool, detail: str = "桩：库坏了") -> None:
        self.ok = ok
        self.detail = detail
        self.pings = 0

    def backend_name(self) -> str:
        return "stub"

    def ping(self) -> tuple[bool, str]:
        self.pings += 1
        return self.ok, self.detail


class _StubCache:
    """一个「坏掉的缓存」。"""

    def __init__(self, *, ok: bool, errors: int = 0) -> None:
        self.ok = ok
        self.errors = errors
        self.pings = 0

    def backend_name(self) -> str:
        return "stub-cache"

    def ping(self) -> tuple[bool, str]:
        self.pings += 1
        return self.ok, "桩：缓存连不上"

    def stats(self) -> dict:
        return {
            "backend": "stub-cache", "enabled": True, "hits": 0, "misses": 0,
            "lookups": 0, "hit_rate": None, "errors": self.errors,
            "last_error": "ConnectionError: 桩" if self.errors else None,
            "note": "降级" if self.errors else "",
        }


# ── 真 HTTP ─────────────────────────────────────────────────────────


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


@pytest.fixture()
def app_obj(tmp_path):
    st = Settings.from_paths(
        data_dir=tmp_path / "data", config_dir=tmp_path / "config"
    )
    st.ensure_dirs()
    app = make_app(settings=st)
    yield app
    app.close()


# ── 默认装配（库正常 + 缓存是空实现）────────────────────────────────


def test_默认装配下回_200_且三问都有答案(app_obj) -> None:
    with _running(app_obj) as base:
        st, body = _get(base, "/api/health")

    assert st == 200
    assert set(body) >= {"ok", "degraded", "service", "database", "cache"}
    assert body["ok"] is True
    assert body["degraded"] is False

    assert body["service"]["ok"] is True
    assert body["service"]["version"] == VERSION

    assert body["database"]["ok"] is True
    assert body["database"]["backend"] == "sqlite"
    assert body["database"]["detail"], "要说得出「用的是什么」"


def test_空缓存报的是没启用而不是命中率零(app_obj) -> None:
    """※ **这一条是缓存那一问的全部意义。**

    `enabled: false` 与 `hit_rate: null` 说的是「**没启用**」；
    如果它报成 `hit_rate: 0.0`，读的人会去查「为什么一次都没命中」——
    而那个问题在这里根本不存在。
    """
    with _running(app_obj) as base:
        _, body = _get(base, "/api/health")

    cache = body["cache"]
    assert cache["backend"] == "none"
    assert cache["enabled"] is False
    # ※ `ok` 与 `enabled` 是**两个**字段：没故障，也没在工作。
    assert cache["ok"] is True
    assert cache["detail"], "要说得出「没启用不是故障」"
    assert cache["stats"]["hit_rate"] is None
    assert cache["stats"]["hits"] is None


# ── 库坏了 ──────────────────────────────────────────────────────────


def test_库坏了回_503(app_obj) -> None:
    """※ 判据：库挂了整个产品都没法用 ⇒ **不健康**。

    这也是 `deploy/docker-compose.yml` 的 healthcheck 唯一认的形状：
    非 2xx ⇒ `urlopen` 抛 ⇒ 容器被判为 unhealthy。
    """
    broken = _StubStore(ok=False)
    app_obj.reader = broken

    with _running(app_obj) as base:
        st, body = _get(base, "/api/health")

    assert st == 503
    assert body["ok"] is False
    assert body["database"]["ok"] is False
    assert body["database"]["detail"], "坏在哪要说出来"
    assert broken.pings == 1


# ── 缓存坏了 ────────────────────────────────────────────────────────


def test_缓存坏了仍然回_200_但标出降级(app_obj) -> None:
    """※ 判据：缓存是**降级**不是故障 ⇒ 不改 HTTP 状态码。

    重启修不好 Redis，只会让容器一直重启 —— 所以这里回 200，
    但把降级**报出来**（`degraded` + `cache.ok` + `errors`）。
    """
    app_obj.cache = _StubCache(ok=False, errors=3)

    with _running(app_obj) as base:
        st, body = _get(base, "/api/health")

    assert st == 200, "缓存挂了不该让整个服务被判为不健康"
    assert body["ok"] is True
    assert body["degraded"] is True
    assert body["cache"]["ok"] is False
    assert body["cache"]["stats"]["errors"] == 3


def test_缓存降级但探活通时也算降级(app_obj) -> None:
    """※ 阴性对照的补角：**已经出过错**的缓存，即使这一刻 ping 通了，
    也算降级 —— 否则「偶发连不上」会在一半的时间里看不见。"""
    app_obj.cache = _StubCache(ok=True, errors=1)

    with _running(app_obj) as base:
        st, body = _get(base, "/api/health")

    assert st == 200
    assert body["degraded"] is True
    assert body["cache"]["ok"] is True


# ── 只读 ────────────────────────────────────────────────────────────


def test_健康检查只探只读连接(app_obj) -> None:
    """※ 健康检查**不该有能力写主库**。

    这里把读写连接换成一个「被 ping 到就失败」的桩：健康端点如果碰了它，
    这条立刻红。（同一条判据在 `test_api_contract.py` 里有一条 AST 版：
    GET 的函数体里不许出现 `app_obj.store`。）
    """
    touched = _StubStore(ok=True, detail="不该被碰到")
    app_obj.store = touched
    reader = _StubStore(ok=True, detail="只读连接正常")
    app_obj.reader = reader

    with _running(app_obj) as base:
        st, body = _get(base, "/api/health")

    assert st == 200
    assert body["database"]["detail"] == "只读连接正常"
    assert touched.pings == 0, "健康检查碰了读写连接"


# ── 版本号 ──────────────────────────────────────────────────────────


def test_版本号与_pyproject_一致(app_obj) -> None:
    """※ 两处各写一份必漂移，而漂移的症状是 `/api/health` 报出一个
    **不存在的版本** —— 排查「线上跑的是哪一版」时那是一条误导线索。"""
    import tomllib

    declared = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    assert declared["project"]["version"] == VERSION

    with _running(app_obj) as base:
        _, body = _get(base, "/api/health")
    assert body["service"]["version"] == VERSION
