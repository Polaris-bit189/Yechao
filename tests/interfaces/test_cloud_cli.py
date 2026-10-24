"""云端 dry-run —— **完整**与**名单非空**。

## 这两条都是实测出来的，不是推测

  · 21 条真实条目的 payload JSON 有 **3164 字**，而 CLI 原来打印
    `json[:3000]`，却写着「**全部内容**」—— 尾巴被切掉，连一句截断提示
    都没有。人拿这份输出做出网边界核对时，会以为自己看完了。
  · 容器里的 `config/groups.toml` 是从 `config.example` 落下来的
    （四个群都在、`members` 全空），`demo` 因为文件存在而整份跳过，
    于是 dry-run 报「比对过 **0** 个」—— 姓名那一维根本没查。

## 判据为什么落在行为上

`dry-run` 的价值就是「发之前肉眼核一遍」。所以判据必须是**输出里真的
有每一条**，而不是「代码里没有 `[:3000]`」——后者挡不住换一种写法再截一次。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from yechao.domain.days import Clock
from yechao.domain.models import Item
from yechao.infrastructure.persistence import sqlite_store
from yechao.infrastructure.persistence.sqlite_store import SqliteStore
from yechao.interfaces.cli import main

ROOT = Path(__file__).resolve().parents[2]

#: `cloud` 取的正是合成语料的最后一天（`max(CORPUS.ts)`）。
DAY = "2026-10-19"

#: 合成语料里的群号段，自己编的。
GROUP = 820000001

_CLOCK = Clock()


def _argv(workdir, *extra: str) -> list[str]:
    data, cfg = workdir
    return [
        "--data-dir", str(data),
        "--config-dir", str(cfg),
        *extra,
    ]


def _seed_items(db: Path, n: int) -> None:
    """往库里写 `n` 条**长详情**条目 —— 让 payload 远超 3000 字。

    详情的长度是故意的：短条目下 JSON 可能不到 3000 字，截断与不截断
    看不出区别，这条用例就会恒绿。
    """
    base = _CLOCK.day_start_from_str(DAY) + 10 * 3600
    items = [
        Item(
            item_id=0,
            kind="todo",
            kind_label="要办的事",
            title=f"条目{i}",
            detail="详情" * 40,
            event_ts=base + i,
            deadline_ts=None,
            group_id=GROUP,
            group_name="群1",
            actor=None,
            actor_account=None,
            place=None,
            amount=None,
            links=(),
            source_count=1,
            start_ts=None,
        )
        for i in range(n)
    ]
    store = SqliteStore(sqlite_store.connect(db))
    try:
        store.insert_items(items, [])
    finally:
        store.conn.close()


@pytest.fixture()
def workdir(tmp_path, monkeypatch):
    """临时数据/配置目录，配置先抄一份 `config.example`。

    ※ 抄 `config.example` 是**故意**的：它正是容器里那种「群都在、
    成员名单全空」的形状。不抄的话，`demo` 会自己写一份带名单的配置，
    这条用例就测不到那个坑。
    """
    monkeypatch.delenv("YECHAO_DB_URL", raising=False)
    monkeypatch.delenv("YECHAO_REDIS_URL", raising=False)
    data, cfg = tmp_path / "data", tmp_path / "config"
    shutil.copytree(ROOT / "config.example", cfg)
    return data, cfg


def _add_cloud_section(cfg: Path) -> None:
    settings = cfg / "settings.toml"
    prev = settings.read_text(encoding="utf-8") if settings.exists() else ""
    settings.write_text(
        prev
        + "\n[cloud]\n"
        + 'base_url = "http://127.0.0.1:9/v1/chat/completions"\n'
        + 'model = "假模型"\n',
        encoding="utf-8",
    )


def test_dry_run_打印全部条目_且姓名名单非空(workdir, capsys) -> None:
    data, cfg = workdir

    # 先在 `config.example` 那份「空名单」配置上跑 demo —— 它应当补名单。
    assert main(["demo", *_argv(workdir)]) == 0

    _seed_items(data / "yechao.db", 30)
    _add_cloud_section(cfg)

    code = main(["cloud", "--dry-run", *_argv(workdir)])
    out = capsys.readouterr().out

    assert code == 0, out
    assert "没有发出任何请求" in out

    # ※ 截断判据：少一条都说明打印的不是「全部内容」。
    assert out.count('"item_id"') == 30, (
        f"输出里只有 {out.count('\"item_id\"')} 条 —— dry-run 被截断了"
    )

    # ※ 白名单的阴性对照：这些键一个都不该出现。
    assert '"actor"' not in out
    assert '"group_name"' not in out

    # ※ 名单非空才是「查了」；0 的含义是「根本没查」。
    m = re.search(r"姓名名单：比对过 (\d+) 个", out)
    assert m is not None, "dry-run 没有报告比对了几个名字"
    assert int(m.group(1)) > 0, "名单是空的 —— 姓名那一维没查"
