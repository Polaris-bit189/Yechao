"""后端选择：`YECHAO_DB_URL` / `YECHAO_REDIS_URL` → 装配出什么。

## 观察到什么现象 → 为什么这么定 → 怎么验证

**现象**：云上那条路（RDS / Redis）与本地那条路（SQLite / 空缓存）的差别
只在**装配**这一步。而装配错了**不会报错**：程序照常跑，只是数据写进了
另一个地方。这是「一个失败如果看起来像成功」的教科书例子。

**所以这里的判据是**：

1. 没配 ⇒ 用本机默认（SQLite + 空缓存）。这是**默认**，不是回落。
2. 配了但配不通 ⇒ **报错**，绝不悄悄换成本地默认。
3. 配了不认识的 scheme ⇒ 报错并列出支持哪几种。
"""

from __future__ import annotations

import sys
import types

import pytest

from yechao.bootstrap import (
    DEFAULT_CACHE_TTL,
    Settings,
    make_app,
    read_cache_ttl,
    resolve_db_target,
)
from yechao.domain.errors import ConfigError


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """※ 环境变量是**进程级**的 —— 不隔离的话，一个用例配了 MySQL，
    后面的用例会跟着连 MySQL，而失败信息指向的是别处。"""
    for name in ("YECHAO_DB_URL", "YECHAO_REDIS_URL", "YECHAO_DATA_DIR",
                 "YECHAO_CONFIG_DIR", "YECHAO_DB"):
        monkeypatch.delenv(name, raising=False)


def _settings(tmp_path) -> Settings:
    st = Settings.from_paths(
        data_dir=tmp_path / "data", config_dir=tmp_path / "config"
    )
    st.ensure_dirs()
    return st


# ── 连接串 → 后端 ───────────────────────────────────────────────────


def test_没配时用本机默认(tmp_path) -> None:
    backend, target = resolve_db_target("", default_path=tmp_path / "y.db")
    assert backend == "sqlite"
    assert target == str(tmp_path / "y.db")


def test_sqlite_连接串指定库文件() -> None:
    assert resolve_db_target("sqlite:///tmp/a.db", default_path="x") == (
        "sqlite", "/tmp/a.db"
    )


def test_mysql_连接串原样交给适配器() -> None:
    url = "mysql://u:p@h.example.test:3306/yechao"
    assert resolve_db_target(url, default_path="x") == ("mysql", url)


@pytest.mark.parametrize("url", ["postgres://u:p@h/db", "oracle://x", "file:///a"])
def test_不认识的_scheme_报错并列出支持的(url: str) -> None:
    """※ 「认不出就猜一个」在这里是最坏的行为：猜成 SQLite 的话，
    用户会以为连上了他的库。"""
    with pytest.raises(ConfigError, match="只认"):
        resolve_db_target(url, default_path="x")


def test_sqlite_连接串没有路径时报错() -> None:
    """※ 这里**不**替他填一个默认路径 —— 那会让他以为连的是自己写的那个库。"""
    with pytest.raises(ConfigError, match="路径"):
        resolve_db_target("sqlite://", default_path="x")


# ── 装配：配不通要响 ────────────────────────────────────────────────


def test_默认装配是_sqlite_加空缓存(tmp_path) -> None:
    app = make_app(settings=_settings(tmp_path))
    try:
        assert app.store.backend_name() == "sqlite"
        assert app.reader.backend_name() == "sqlite"
        assert app.cache.backend_name() == "none"
    finally:
        app.close()


def test_配了_mysql_但驱动没装时报错而不是回落(monkeypatch, tmp_path) -> None:
    """※ **这是本文件里最要紧的一条。**

    回落的症状：用户配了一个 RDS，程序一声不吭地在**本地文件**里写数据。
    而它看起来是成功的 —— 网页能开、条目能出、日报能生成。直到有人发现
    云上的库里什么都没有。
    """
    monkeypatch.setitem(sys.modules, "pymysql", None)
    monkeypatch.setenv("YECHAO_DB_URL", "mysql://u:p@h.example.test/yechao")
    with pytest.raises(ConfigError, match="pip install pymysql"):
        make_app(settings=_settings(tmp_path))


def test_配了_mysql_但连接串不完整时报错(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("YECHAO_DB_URL", "mysql://h.example.test")
    with pytest.raises(ConfigError, match="缺了"):
        make_app(settings=_settings(tmp_path))


def test_配了不认识的数据库_scheme_时报错(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("YECHAO_DB_URL", "postgres://u:p@h/db")
    with pytest.raises(ConfigError, match="只认"):
        make_app(settings=_settings(tmp_path))


def test_配了_redis_但驱动没装时报错而不是回落(monkeypatch, tmp_path) -> None:
    """与数据库那条对称：**没装驱动 ⇒ 报错**。

    回落成空缓存的症状是「用户以为缓存开着，其实什么都没缓存」。
    """
    monkeypatch.setitem(sys.modules, "redis", None)
    monkeypatch.setenv("YECHAO_REDIS_URL", "redis://h.example.test:6379/0")
    with pytest.raises(ConfigError, match="pip install redis"):
        make_app(settings=_settings(tmp_path))


def test_配了_redis_时装配出真实现(monkeypatch, tmp_path) -> None:
    """阴性对照：上面那条只证明了「会报错」，这条证明**配好了就真的接上**。"""
    mod = types.ModuleType("redis")

    class _Client:
        def get(self, k): return None
        def setex(self, k, ttl, v): pass
        def delete(self, k): pass
        def ping(self): return True

    class _R:
        @staticmethod
        def from_url(url, **kw): return _Client()

    mod.Redis = _R
    monkeypatch.setitem(sys.modules, "redis", mod)
    monkeypatch.setenv("YECHAO_REDIS_URL", "redis://h.example.test:6379/0")

    app = make_app(settings=_settings(tmp_path))
    try:
        assert app.cache.backend_name() == "redis"
        assert app.cache.stats()["enabled"] is True
    finally:
        app.close()


# ── 缓存 TTL 从配置文件读 ───────────────────────────────────────────


def test_没配_cache_段时用默认_TTL(tmp_path) -> None:
    st = _settings(tmp_path)
    assert read_cache_ttl(st) == DEFAULT_CACHE_TTL


def test_cache_段里写了_TTL_就用它(tmp_path) -> None:
    """※ TTL 不是机密，所以它在**配置文件**里（能被导出、能被评委看见），
    而连接串在环境变量里 —— 与 `[llm]` / `YECHAO_API_KEY` 是同一条分界。"""
    st = _settings(tmp_path)
    (st.config_dir / "settings.toml").write_text(
        "[cache]\nttl_seconds = 3\n", encoding="utf-8"
    )
    assert read_cache_ttl(st) == 3


def test_TTL_写了不合法的值时报错而不是静默用默认(tmp_path) -> None:
    """※ 「配了 30 但实际跑 10」与「没配」在行为上一样、在意图上完全不同。

    静默取默认值会让前者**永远查不出来** —— 而用户会一直以为自己配上了。
    """
    st = _settings(tmp_path)
    (st.config_dir / "settings.toml").write_text(
        '[cache]\nttl_seconds = "十秒"\n', encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="非负整数"):
        read_cache_ttl(st)


def test_TTL_为零是合法的(tmp_path) -> None:
    """※ 阴性对照：0 是「不过期」这个**显式**的选择，不是「没配」。"""
    st = _settings(tmp_path)
    (st.config_dir / "settings.toml").write_text(
        "[cache]\nttl_seconds = 0\n", encoding="utf-8"
    )
    assert read_cache_ttl(st) == 0
