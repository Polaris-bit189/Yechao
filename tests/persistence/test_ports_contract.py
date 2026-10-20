"""端口契约 —— 两个仓储实现、两个缓存实现，都必须**逐字**满足它。

## 观察到什么现象 → 为什么这么定 → 怎么验证

**现象**：本项目真的踩过一次「端口与实现对不上名」—— `reorder.run` 里写的是
`deps.jobs.busy()`，而实现里只有 `any_job_running()`，一跑就 `AttributeError`。
而 `reorder` 整个模块**零测试**，所以这个错位一直没人发现。

**为什么原来的判据不够**：`tests/persistence/test_wiring.py` 里那条
`isinstance(s, ports.JobRepository)` 只查**方法名存不存在**，它查不了签名。
于是「名字对了、参数少一个」这一类错位照样漏 —— 而它的症状同样是运行时
`TypeError`，而且**只在某个后端上出现**（SQLite 那个恰好参数齐）。

**所以这里的判据**：

1. 两个实现都 `isinstance` 通过全部端口（名字层面的覆盖）；
2. 端口方法声明的**每一个参数**，实现里都得有（签名层面的覆盖）。
"""

from __future__ import annotations

import inspect

import pytest

from yechao.application import ports
from yechao.infrastructure.cache.null_cache import NullCache
from yechao.infrastructure.cache.redis_cache import RedisCache
from yechao.infrastructure.persistence import sqlite_store
from yechao.infrastructure.persistence.mysql_store import MySQLStore

STORES = ("SqliteStore", "MySQLStore")

#: 仓储端口。
REPOSITORY_PORTS = (
    ports.MessageRepository,
    ports.ItemRepository,
    ports.EditRepository,
    ports.DigestRepository,
    ports.JobRepository,
    ports.KvRepository,
    # ※ 失败记账那一个**也要在这里**：它只在两个后端里各实现一次，
    # 漏掉一个方法名不会有任何东西红 —— 而症状是「换后端之后查不到账」。
    ports.RunLedgerRepository,
)


def _impl(name: str):
    """拿到一个**没有连接**的实现实例。

    ※ 用 `object.__new__` 跳过 `__init__`：这里要验的是**类上有什么方法**，
    不是「能不能连上库」。真去连的话，这个测试就需要一台 MySQL ——
    而它恰恰是这里没有的东西。
    """
    cls = {"SqliteStore": sqlite_store.SqliteStore, "MySQLStore": MySQLStore}[name]
    return object.__new__(cls)


def _param_names(fn) -> set[str]:
    return {
        n
        for n, p in inspect.signature(fn).parameters.items()
        if n != "self" and p.kind is not inspect.Parameter.VAR_POSITIONAL
    }


def _missing_params(impl, proto) -> list[str]:
    """端口声明的参数里，实现**没有**的那些。"""
    out: list[str] = []
    for name, member in inspect.getmembers(proto):
        if name.startswith("_") or not callable(member):
            continue
        fn = getattr(impl, name, None)
        if fn is None:
            out.append(f"{proto.__name__}.{name}（实现里根本没有这个方法）")
            continue
        gap = _param_names(member) - _param_names(fn)
        if gap:
            out.append(f"{proto.__name__}.{name} 少了参数 {sorted(gap)}")
    return out


def test_端口上确实有方法可查() -> None:
    """※ **空转自检**：端口一个方法都没有时，下面的判据全都恒为真。"""
    n = sum(
        1
        for proto in REPOSITORY_PORTS
        for name, m in inspect.getmembers(proto)
        if not name.startswith("_") and callable(m)
    )
    assert n >= 25, f"只数到 {n} 个端口方法 —— 这个判据在空转"


@pytest.mark.parametrize("impl_name", STORES)
def test_实现满足全部仓储端口(impl_name: str) -> None:
    impl = _impl(impl_name)
    for proto in REPOSITORY_PORTS:
        assert isinstance(impl, proto), f"{impl_name} 不满足 {proto.__name__}"


@pytest.mark.parametrize("impl_name", STORES)
def test_实现的签名覆盖端口(impl_name: str) -> None:
    """※ 名字对得上、参数少一个，症状是运行时 `TypeError` ——
    而且**只在那个后端上出现**。"""
    impl = _impl(impl_name)
    problems: list[str] = []
    for proto in REPOSITORY_PORTS:
        problems += _missing_params(impl, proto)
    assert not problems, (
        f"{impl_name} 与端口的签名对不上：\n  " + "\n  ".join(problems)
    )


@pytest.mark.parametrize("impl_name", STORES)
def test_实现满足存储自描述端口(impl_name: str) -> None:
    """`/api/health` 靠这两个方法回答「库通不通 + 用的是什么」。"""
    impl = _impl(impl_name)
    assert isinstance(impl, ports.StorageBackend)
    assert _missing_params(impl, ports.StorageBackend) == []


def test_签名判据真的会失败() -> None:
    """※ **阴性对照**：一个「少了一个参数」的假实现必须被抓住。

    没有这一条，上面那些判据分不清「签名都对」与「检查器根本没在检查」。
    """

    class _Almost:
        def search(self, kinds=None):  # 端口声明的是 **filters
            return [], 0

        def __getattr__(self, name):  # 其余方法都"有"
            return lambda *a, **k: None

    problems = _missing_params(_Almost(), ports.ItemRepository)
    assert any("search" in p for p in problems), problems


# ── 缓存 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("cache", [NullCache(), RedisCache(object())])
def test_缓存实现满足端口(cache) -> None:
    assert isinstance(cache, ports.Cache)
    assert _missing_params(cache, ports.Cache) == []


def test_两个后端的_stats_键集一致() -> None:
    """※ 消费方（`/api/health`）不该按后端分情况读字段 ——
    那正是「两个形状长得一样、语义不一样」的温床。

    `RedisCache(object())` 只是拿它的**类**，不调它（调了会抛，因为
    `object()` 没有 `get`）。这里只比键集，所以不调。
    """
    assert set(NullCache().stats()) == set(
        RedisCache(object()).stats()
    )
