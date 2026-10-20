"""CLI —— 三步复现是红线。

⚠️ 这些用例断言的是**行为**，不是读代码：

  · 「`demo` 不需要密钥」→ 把 socket 禁掉再跑一遍
  · 「失败要响」→ 断言**退出码**，不是断言打印了什么
  · 「坏配置不静默兜底」→ 写一份坏配置，断言它**报错**而不是用默认值

一个失败如果看起来像成功，它比失败本身更危险
—— 所以退出码是本文件的主判据。
"""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest

from yechao.interfaces.cli import main


@pytest.fixture()
def workdir(tmp_path: Path) -> tuple[Path, Path]:
    """一对空的数据/配置目录。"""
    return tmp_path / "data", tmp_path / "config"


def _argv(workdir, *extra: str) -> list[str]:
    data, cfg = workdir
    return [
        "--data-dir", str(data),
        "--config-dir", str(cfg),
        *extra,
    ]


# ── demo ────────────────────────────────────────────────────────────


def test_demo_不需要网络(workdir, monkeypatch, capsys) -> None:
    """⚠️ **离线建库，不需密钥** —— 这一条由**行为**钉住。

    做法：把「建连接」这条路径换成一个一被调用就抛的桩。
    如果 `demo` 里任何一处试图联网，它会当场炸。

    ⚠️ **第一版禁错了对象，记在这里。**

    第一版把 `socket.socket` 整个换掉 —— 而 `socket` 是一个**类**，
    `ssl` 模块在 import 时就写了 `class SSLSocket(socket)`。
    于是测试失败在**导入期**，报错是 `TypeError: function() argument
    'code' must be code, not str` —— 一个与「联没联网」毫无关系的信息。

    这个误报很危险：它看起来像「`demo` 联网了」，而真相是测试自己
    破坏了 `ssl` 的导入。**判据禁错了对象，会造出一个指向错误方向的证据。**

    ⇒ 现在只禁**建连接**（`create_connection` 与 `socket.connect`），
    不禁「socket 这个类型存在」。
    """
    def _no_network(*a, **kw):
        raise AssertionError("demo 试图联网 —— 它必须离线可跑")

    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket.socket, "connect", _no_network, raising=False)
    monkeypatch.setattr(socket.socket, "connect_ex", _no_network, raising=False)

    code = main(["demo", *_argv(workdir)])
    assert code == 0

    data, cfg = workdir
    assert (data / "yechao.db").exists()
    assert (cfg / "groups.toml").exists()
    assert (cfg / "categories.toml").exists()


def test_demo_把语料写进库(workdir, capsys) -> None:
    code = main(["demo", *_argv(workdir)])
    assert code == 0

    from yechao.bootstrap import Settings
    from yechao.infrastructure.persistence import sqlite_store

    data, cfg = workdir
    st = Settings.from_paths(data_dir=data, config_dir=cfg)
    conn = sqlite_store.connect(st.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()
    assert n > 0, "demo 之后库里必须有消息（否则后面的 refine 无事可做）"


def test_demo_报告里必须看得见预筛的数字(workdir, capsys) -> None:
    """⚠️ 「筛掉了多少、为什么」必须在 demo 阶段就可见。

    否则评委跑完 demo 看到一堆消息，不知道后面会发生什么 ——
    而那会让「预筛」这个环节在演示里等于不存在。
    """
    main(["demo", *_argv(workdir)])
    out = capsys.readouterr().out
    assert "预筛" in out
    assert "保留" in out
    assert "丢弃" in out


def test_demo_重跑是幂等的(workdir, capsys) -> None:
    """⚠️ 重跑不该产生重复消息 —— 而它会重写配置是**警告过**的。

    配置文件的语义是「只在不存在时写」：已经存在的配置是**用户的**，
    覆盖它等于把用户的编辑丢掉，而 demo 看起来「成功」。
    """
    main(["demo", *_argv(workdir)])
    _, cfg = workdir
    before = (cfg / "categories.toml").read_text(encoding="utf-8")

    code = main(["demo", *_argv(workdir)])
    assert code == 0
    after = (cfg / "categories.toml").read_text(encoding="utf-8")
    assert before == after, "用户已有的配置被覆盖了"

    out = capsys.readouterr().out
    assert "保留不动" in out


# ── refine 的失败要响 ────────────────────────────────────────────────


def test_没配模型时_refine_非零退出且指出去哪配(workdir, capsys) -> None:
    """⚠️ 不兜底到一个默认端点 —— 那会把「没配」变成「连不上」，
    而两个要查的方向完全不同。"""
    main(["demo", *_argv(workdir)])
    code = main(["refine", *_argv(workdir)])
    assert code != 0, "没配模型却返回 0 —— 这正是「失败看起来像成功」"

    out = capsys.readouterr()
    text = out.out + out.err
    assert "settings.toml" in text or "base_url" in text, (
        "错误提示必须指出去哪配 —— 只说「失败了」等于没说"
    )


# ── 参数 ────────────────────────────────────────────────────────────


def test_不认识的参数返回_2(capsys) -> None:
    assert main(["demo", "--nonsense"]) == 2
    out = capsys.readouterr()
    assert "不认识的参数" in (out.out + out.err)


def test_不认识的命令返回_2(capsys) -> None:
    assert main(["frobnicate"]) == 2


def test_没有命令时打印用法并返回_0(capsys) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "demo" in out and "refine" in out and "serve" in out


# ── 配置坏了不许静默兜底 ─────────────────────────────────────────────


def test_坏配置必须报错而不是用默认值(workdir, capsys) -> None:
    """⚠️ 判据：**文件存在但读不出来 ⇒ 抛**。

    与「文件不存在」（第一次跑，建一份默认的）是两件事。
    混起来的话，用户改坏了的配置会被静默忽略 ——
    而他以为设置生效了，直到某天发现群根本没被监视。
    """
    data, cfg = workdir
    main(["demo", *_argv(workdir)])
    (cfg / "groups.toml").write_text("[[group]]\nname = '缺了 id'\n", encoding="utf-8")

    code = main(["doctor", *_argv(workdir)])
    assert code != 0, "坏配置没被报出来"

    out = capsys.readouterr()
    assert "id" in (out.out + out.err)


# ── 模块入口真的能跑 ────────────────────────────────────────────────


def test_python_dash_m_yechao_能跑(tmp_path: Path) -> None:
    """⚠️ **三步复现写的是 `python -m yechao`**，不是 `import`。

    这条用子进程真跑一遍 —— 它能抓到「包能 import 但入口坏了」那一类问题
    （比如 `__main__.py` 的签名与 CLI 的 `main` 对不上）。
    """
    r = subprocess.run(
        [sys.executable, "-m", "yechao", "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert r.returncode == 0, r.stderr[-800:]
    assert "demo" in r.stdout
