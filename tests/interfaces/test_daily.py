"""「今天的日报」入口 —— 时间注入、覆盖语义、三态退出码。

## 这个文件要证明的四件事

1. **「今天」是注入的，不是机器时间。** 判据是行为：换一个时钟，整条链路
   就跑另一天 —— 而不是「跑我按下回车的那一刻」。
2. **同一天跑两次是覆盖（= 刷新），不是跳过。** 判据也是行为：两次跑之间
   新进来的条目必须出现在第二篇里。
3. **模型挂了日报照样出得来，而且「模型没跑成」看得出来。**
   `application.digest.run` 的机械行兜底做得很干净 —— 干净到日报上完全
   看不出模型没跑。所以这条判据只能在这一层守。
4. **三态退出码互不相同。** 调度器只看得到退出码（见 `deploy/scheduler.sh`），
   所以「跑成了」与「跑成了但模型没跑」共用一个码 = 把后者抹掉。

## 桩的取向：按提示词契约行事

模型桩从**真的发出去的提示词**里读编号，再照抄回去（素材形如
`- [12] [todo] 标题`）。不这么做的话，「素材 → 模型 → 解析 → 回连」这一段
根本没被测到 —— 而 `domain/digest.py` 的 docstring 里记着它的失败形状：
*整条「本地模型写日报」的路等于没跑，而报告上一切正常*。

## 样本全部自己编

群号用 820000001 段，标题是「条目N」这种编出来的东西。没有一条来自
任何真实语料。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from yechao.bootstrap import Settings, make_app
from yechao.domain.days import Clock
from yechao.domain.errors import ModelError
from yechao.domain.models import Item
from yechao.infrastructure.persistence import sqlite_store
from yechao.infrastructure.persistence.sqlite_store import SqliteStore
from yechao.infrastructure.runtime.clock import FixedClock
from yechao.interfaces import daily

ROOT = Path(__file__).resolve().parents[2]

#: ※ 两个**离现在很远**的固定日子。用两天而不是一天，是为了让「今天是注入的」
#: 这件事**可证伪**：如果实现取的是机器时间，两次跑会落在同一天。
DAY_A = "2026-03-05"
DAY_B = "2026-03-06"

#: 群号自己编的（820000001 段）。
GROUP = 820000001

_PURE = Clock()


def _noon(day: str, hour: int = 10) -> int:
    """那一天上午 `hour` 点（本地日，东八区）。"""
    return _PURE.day_start_from_str(day) + hour * 3600


# ── 桩 ──────────────────────────────────────────────────────────────


class _Model:
    """模型桩。三种形态，对应三种真实故障：

      · 正常      —— 按素材里的编号写回每一行
      · `dead`    —— 端点连不上（抛 `ModelError`）
      · `mute`    —— 回了、但回的不是可解析的东西（没有 JSON）

    ※ 后两种是**不同的失败**：下一步动作也不同（查端点 vs 查提示词）。
    但它们在日报上都看不出区别 —— 机械行兜底让两篇日报长得一样。
    """

    def __init__(self, *, dead: bool = False, mute: bool = False) -> None:
        self.dead = dead
        self.mute = mute
        self.calls = 0

    def complete(self, *, system: str, user: str, max_tokens: int = 2048, response_schema: dict | None = None) -> str:
        self.calls += 1
        if self.dead:
            raise ModelError("连不上模型端点 http://127.0.0.1:9/v1/chat/completions")
        if self.mute:
            return "好的，我看看。"  # 没有 JSON
        ids = [int(m) for m in re.findall(r"\[(\d+)\]", user)]
        return json.dumps(
            [{"item_id": i, "line": f"模型写的第 {i} 条"} for i in ids],
            ensure_ascii=False,
        )


# ── 夹具 ────────────────────────────────────────────────────────────


def _configure_llm(a, base_url: str = "http://127.0.0.1:9/v1/chat/completions") -> None:
    """给配置目录写一段 `[llm]`。

    ※ 这一段**不是可有可无的**：`run_today` 在 `[llm]` 没配时**不会去调
    模型**（理由见那边的注释 —— 拿空 URL 去建请求会抛裸 `ValueError`，
    机械行兜底接不住它）。不配这一段，下面每一条「模型写了什么」的用例都会
    静默退化成「模型没跑」，而它们**仍然是绿的**。

    地址是个占位：客户端在用例里被桩替换掉了，这个地址从来不会被连。
    """
    (a.settings.config_dir / "settings.toml").write_text(
        f'[llm]\nbase_url = "{base_url}"\nmodel = "假模型"\n', encoding="utf-8"
    )


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """一个装配好的 App，落在**临时目录**上。

    ※ 清掉这两个环境变量：它们决定组合根选哪个后端 —— 留着的话，用例会去
    连开发机上的 RDS / Redis（那是别人的数据），而不是临时 SQLite。
    """
    monkeypatch.delenv("YECHAO_DB_URL", raising=False)
    monkeypatch.delenv("YECHAO_REDIS_URL", raising=False)
    st = Settings.from_paths(
        data_dir=tmp_path / "data", config_dir=tmp_path / "config"
    )
    a = make_app(settings=st)
    _configure_llm(a)
    yield a
    a.close()


def _clock_at(a, day: str, hour: int = 10):
    """把 App 的时钟钉在那一天。

    ※ 换的是 `App.clock` 本身 —— 那正是判据 ①：这个入口只问时钟端口，
    不问机器时间。所以「换一个时钟就跑另一天」在它身上必须成立。
    """
    a.clock = FixedClock(ts=_noon(day, hour))
    return a


def _seed(a, n: int = 3, *, day: str = DAY_A, title: str = "条目") -> list[int]:
    """往库里塞 `n` 条条目（**直接写库**，不走模型）。

    条目落在 `day` 这一天上午 9 点之后 —— 那是日报窗口（本地日 00:00–24:00）
    之内。
    """
    base = _noon(day, 9)
    items = [
        Item(
            item_id=0, kind="todo", kind_label="要办的事",
            title=f"{title}{i}", detail=None, event_ts=base + i,
            deadline_ts=None, group_id=GROUP, group_name="群1",
            actor=None, actor_account=None, place=None, amount=None,
            links=(), source_count=1, start_ts=None,
        )
        for i in range(n)
    ]
    return a.store.insert_items(items, [])


# ── 判据 ①：今天是注入的 ────────────────────────────────────────────


def test_换一个时钟就跑另一天_不是机器时间(app) -> None:
    """※ 判据是**行为**：两次跑落在两个不同的注入日上。

    为什么不是「断言它等于某一天」：那样只能证明「它算出了一个日子」。
    换成两天之后，**取机器时间**的实现在这条用例上会当场露馅 ——
    两次跑都会落在「今天」，与 `DAY_A` / `DAY_B` 都不等。

    ※ 也**不**去和 `date.today()` 比：那会让这条用例依赖「跑它的那一刻是
    几号」—— 正是本项目要求判据避免的东西。
    """
    _clock_at(app, DAY_A)
    app.model = _Model()
    _seed(app, 2, day=DAY_A)
    first = daily.run_today(app)

    _clock_at(app, DAY_B)
    _seed(app, 3, day=DAY_B)
    second = daily.run_today(app)

    assert first.day == DAY_A, f"第一次跑的不是注入的那一天：{first.day}"
    assert second.day == DAY_B, f"第二次跑的不是注入的那一天：{second.day}"
    assert {d.day for d in app.store.list_digests()} == {DAY_A, DAY_B}


def test_窗口用的是注入日_只收那一天上午的条目(app) -> None:
    """※ 光看 `day` 字段不够 —— 窗口也得跟着走。

    这一条钉的是「日报**内容**来自哪一天」：另一天的条目不能混进来。
    """
    _clock_at(app, DAY_A)
    app.model = _Model()
    _seed(app, 2, day=DAY_A)
    _seed(app, 5, day=DAY_B)  # 另一天，不该进这一篇

    out = daily.run_today(app)

    assert out.lines == 2, (
        f"日报收了 {out.lines} 条 —— 另一天的条目混进来了，"
        f"说明窗口不是按注入的那一天切的"
    )


# ── 判据 ②：同一天跑两次 = 覆盖（刷新）────────────────────────────


def test_同一天跑两次是覆盖_后一次能看到新来的条目(app) -> None:
    """※ 判据是行为，不是「表里有一行」。

    「跳过」的实现在这条用例上会露出原形：第二次跑看不见上午新进来的那一条。
    """
    _clock_at(app, DAY_A)
    app.model = _Model()
    _seed(app, 2, day=DAY_A)
    first = daily.run_today(app)
    assert first.lines == 2

    # 早上跑完之后，这一天又进来一条 —— 这正是「覆盖」要解决的事
    _seed(app, 1, day=DAY_A, title="上午新来的")
    second = daily.run_today(app)

    assert second.lines == 3, (
        f"第二次跑只有 {second.lines} 条 —— 新进来的条目没算进去，"
        f"说明它是「跳过」而不是「覆盖」。"
    )
    same_day = [d for d in app.store.list_digests() if d.day == DAY_A]
    assert len(same_day) == 1, (
        f"同一天留下了 {len(same_day)} 篇日报 —— 用户不知道该看哪一篇。"
    )


def test_同一天跑两次不会攒出重复的引用(app) -> None:
    """※ 覆盖之后，`item_digests` 里指向**旧** digest_id 的行读不出来
    （读路径都按 digest_id 查，而它是自增的）。这一条钉住「读得出来的
    那部分是对的」—— 重复的条目不会出现在详情页里。
    """
    _clock_at(app, DAY_A)
    app.model = _Model()
    _seed(app, 2, day=DAY_A)
    daily.run_today(app)
    out = daily.run_today(app)

    detail = app.store.digest_detail(out.digest_id)
    assert detail is not None
    _, items = detail
    assert len(items) == 2, f"详情页列出 {len(items)} 条 —— 重复引用了"
    assert len({it.item_id for it in items}) == len(items)


# ── 判据 ③：模型挂了日报照样出得来，而且看得出来 ────────────────────


def test_模型端点连不上_日报照样出得来且状态是降级(app) -> None:
    """※ 这一条是「失败要响」的落点。

    模型整批挂掉时日报**不能是空的**（那会让用户以为那天没事），所以用例层
    用机械行兜底 —— 而兜底太干净，日报上完全看不出模型没跑。
    所以判据落在 `status` / `exit_code` / `model_failure` 上。
    """
    _clock_at(app, DAY_A)
    app.model = _Model(dead=True)
    _seed(app, 3, day=DAY_A)

    out = daily.run_today(app)

    assert out.lines == 3, "模型挂了日报不能是空的（机械行要兜住）"
    assert out.by_model == 0 and out.mechanical == 3
    assert out.model_failure, "整批失败的原因要带出来 —— 不然只剩一句「降级」"
    assert out.status == daily.STATUS_DEGRADED
    assert out.exit_code == daily.EXIT_DEGRADED

    # 日报正文里也要能看出这件事（页面上那句话不能是空的）
    detail = app.store.digest_detail(out.digest_id)
    assert detail is not None and "模型这次没跑成" in detail[0].body_md


def test_模型回了但解析不出_也算没跑成(app) -> None:
    """※ 与上一条是**两种不同的失败**（查端点 vs 查提示词），
    但它们在日报上长得一模一样 —— 机械行兜底让两篇都是「有内容」。
    """
    _clock_at(app, DAY_A)
    app.model = _Model(mute=True)
    _seed(app, 2, day=DAY_A)

    out = daily.run_today(app)

    assert out.lines == 2, "解析不出也要有日报（机械行兜底）"
    assert out.by_model == 0
    assert out.status == daily.STATUS_DEGRADED
    assert out.exit_code == daily.EXIT_DEGRADED


def test_模型写回来了_状态是跑成了(app) -> None:
    _clock_at(app, DAY_A)
    app.model = _Model()
    _seed(app, 2, day=DAY_A)

    out = daily.run_today(app)

    assert out.by_model == 2 and out.mechanical == 0
    assert out.model_failure is None
    assert out.status == daily.STATUS_OK
    assert out.exit_code == daily.EXIT_OK


def test_今天没有条目_不是降级(app) -> None:
    """※ 「今天没有日报」与「今天模型没跑成」是**两句不同的话**。

    窗口里没有条目时 `by_model == 0` 是**正常**的（没有东西要写）——
    判成降级会让每一个空白的日子都报红，而那正是「狼来了」。
    但它要**说出来**：不然「今天没内容」与「今天没跑」在日志里一样。
    """
    _clock_at(app, DAY_A)
    app.model = _Model()

    out = daily.run_today(app)

    assert out.lines == 0
    assert out.status == daily.STATUS_OK
    assert out.exit_code == daily.EXIT_OK
    assert out.note, "空白的日子要说清「是没内容，不是没跑」"


# ── 判据 ④：退出码三态 ──────────────────────────────────────────────


def test_三态退出码互不相同_且只有跑成了是0() -> None:
    """※ 调度器**只看得到退出码**（见 `deploy/scheduler.sh`）。

    两个状态共用一个码 = 把其中一个从退出码里抹掉。而「跑成了但模型没跑」
    正是那个会被抹掉的状态 —— 因为它在日报上看起来完全正常。
    """
    codes = {daily.EXIT_OK, daily.EXIT_DEGRADED, daily.EXIT_FAILED}
    assert len(codes) == 3, "三种状态必须各有各的码"
    assert daily.EXIT_OK == 0
    assert daily.EXIT_DEGRADED != 0, "降级必须是**非零**，否则调度器看不见它"
    assert daily.EXIT_FAILED != 0


def test_状态表覆盖了全部状态() -> None:
    """※ 空转自检：`_STATUS_TEXT` / `_EXIT_OF_STATUS` 漏一个状态时，
    那个状态会**静默**地按 `failed` 报（`.get(..., EXIT_FAILED)`）——
    一个永远不会为绿的表比没有表更危险。"""
    for status in (daily.STATUS_OK, daily.STATUS_DEGRADED, daily.STATUS_FAILED):
        assert status in daily._STATUS_TEXT
        assert status in daily._EXIT_OF_STATUS
    assert len(daily._STATUS_TEXT) == 3
    assert len(daily._EXIT_OF_STATUS) == 3


# ── 入口（`python -m yechao.interfaces.daily`）──────────────────────


def _write_config(config_dir: Path, *, llm_base_url: str) -> None:
    """把 `config.example/` 抄一份，再把模型端点改成 `llm_base_url`。"""
    shutil.copytree(ROOT / "config.example", config_dir, dirs_exist_ok=True)
    (config_dir / "settings.toml").write_text(
        f'[llm]\nbase_url = "{llm_base_url}"\nmodel = "假模型"\n',
        encoding="utf-8",
    )


def _seed_db(db: Path, *, day: str = DAY_A, n: int = 1) -> None:
    """直接往库里写 `n` 条条目（不经过 App）。"""
    sqlite_store.init_db(db)
    store = SqliteStore(sqlite_store.connect(db))
    try:
        base = _noon(day, 9)
        store.insert_items(
            [
                Item(
                    item_id=0, kind="todo", kind_label="要办的事",
                    title=f"条目{i}", detail=None, event_ts=base + i,
                    deadline_ts=None, group_id=GROUP, group_name="群1",
                    actor=None, actor_account=None, place=None, amount=None,
                    links=(), source_count=1, start_ts=None,
                )
                for i in range(n)
            ],
            [],
        )
    finally:
        store.conn.close()


def test_入口_模型端点连不上时退出码是3(tmp_path, monkeypatch, capsys) -> None:
    """※ 判据在**退出码**上，不在打印上 —— 调度器只看得到退出码。

    用的是真的 `OpenAIClient`（不是桩）：端点指到本机一个没人听的端口，
    走的是「连不上 ⇒ `ModelError` ⇒ 机械行兜底 ⇒ 降级」这条真路径。
    """
    monkeypatch.delenv("YECHAO_DB_URL", raising=False)
    monkeypatch.delenv("YECHAO_REDIS_URL", raising=False)
    data, cfg = tmp_path / "data", tmp_path / "config"
    _write_config(cfg, llm_base_url="http://127.0.0.1:9/v1/chat/completions")
    _seed_db(data / "yechao.db")

    code = daily.main(
        ["--data-dir", str(data), "--config-dir", str(cfg), "--day", DAY_A]
    )

    assert code == daily.EXIT_DEGRADED, (
        f"退出码是 {code} —— 模型连不上时必须是「跑成了但模型没跑」。\n"
        f"{capsys.readouterr().out}"
    )


def test_入口_没配端点时日报仍然落库(tmp_path, monkeypatch, capsys) -> None:
    """没配 `[llm]` 时也是降级，不是「没跑成」—— 日报照样出得来。"""
    monkeypatch.delenv("YECHAO_DB_URL", raising=False)
    monkeypatch.delenv("YECHAO_REDIS_URL", raising=False)
    data, cfg = tmp_path / "data", tmp_path / "config"
    _write_config(cfg, llm_base_url="")
    _seed_db(data / "yechao.db")

    code = daily.main(
        ["--data-dir", str(data), "--config-dir", str(cfg), "--day", DAY_A]
    )

    assert code == daily.EXIT_DEGRADED, capsys.readouterr().out
    assert (data / "yechao.db").exists()


def test_入口_配置坏了时退出码是1(tmp_path, monkeypatch, capsys) -> None:
    """「没跑成」= 日报**没有**写进库。配置坏掉是这一档。

    ※ 它必须与「降级」区分开：那一档日报**是**写进库了的。
    """
    monkeypatch.setenv("YECHAO_DB_URL", "postgres://u:p@h.example.test/db")
    data, cfg = tmp_path / "data", tmp_path / "config"

    code = daily.main(
        ["--data-dir", str(data), "--config-dir", str(cfg), "--day", DAY_A]
    )

    assert code == daily.EXIT_FAILED
    assert not (data / "yechao.db").exists(), "没跑成的时候不该留下一个空库"


def test_入口_不认识的参数退出码是2(tmp_path, capsys) -> None:
    """※ **不静默忽略**：拼错的开关与「跑成了一次」不能长得一样。"""
    code = daily.main(["--data-dir", str(tmp_path / "data"), "--model", "x"])
    assert code == 2


def test_入口_帮助不跑日报(tmp_path, capsys) -> None:
    """`--help` 是**要认的**（不是「未知参数」）—— 那是任何人拿到一个新
    命令时敲的第一条。"""
    code = daily.main(["--help"])
    assert code == daily.EXIT_OK
    assert "今天的日报" in capsys.readouterr().out
