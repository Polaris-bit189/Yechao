"""常驻循环 —— 「信息流每 10 分钟自己更新」那一条路。

## 这个文件要证明的四件事

1. **一轮真的做了「取数 → 抽取」**，而且只处理**新**消息（第二轮不产出
   重复条目、也不重复写消息）。
2. **一轮失败不打死循环**，但失败**看得见**（日志里说清哪一轮、为什么）。
3. **常驻进程不长期持有写者锁。** 判据是行为：循环活着的时候，另一个
   进程**拿得到**锁 —— 用真的子进程测，因为「一个只在单线程里成立的锁
   不是锁」（同 `tests/infrastructure/test_writer_lock.py` 的取向）。
4. **间隔可配，而且非法值不静默取默认值。**

## 为什么模型是桩、其余全是真的

真的 CLI 装配、真的仓储、真的预筛、真的脱敏、真的核验、真的写者锁 ——
只有 `OpenAIClient.complete` 被换掉（与 `tests/test_end_to_end.py` 同一个
做法）。换掉它是必须的：不换的话，这个文件会去连一个真的模型端点，
于是它**只能在我这台机器上、在我开着那个端点的那些时候**成立。

## 样本

消息来自仓库里的**合成语料**（`domain/synth.py`，36 条、确定性），
没有任何一条来自真实语料。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from yechao.bootstrap import Settings, acquire_writer_lock
from yechao.infrastructure.llm.openai_client import OpenAIClient
from yechao.interfaces import watch

ROOT = Path(__file__).resolve().parents[2]

#: 合成语料一共多少条（`domain/synth.py`）。写在这里是**断言的前提**，
#: 不是配置 —— 语料改了这条数也要跟着改，而那时这个文件会红。
CORPUS_SIZE = 36


# ── 桩 ──────────────────────────────────────────────────────────────


class _Stub:
    """模型桩：把提示词里每一行消息**逐字抄回来**。

    · `boom_after` 给一个数：前 N 次调用抛 `RuntimeError`（**不是**
      `ModelError`）—— 它逃出 `extract._run_batch` 的兜底，于是这一轮
      真的「没跑成」，而不是「跑了但模型没成」。
      ※ 这两种失败必须分得开：前者是这一轮坏了，后者是降级。
    """

    def __init__(self, *, boom_times: int = 0) -> None:
        self.boom_times = boom_times
        self.calls = 0

    def complete(self, *, system: str, user: str, max_tokens: int = 2048, response_schema: dict | None = None) -> str:
        self.calls += 1
        if self.calls <= self.boom_times:
            raise RuntimeError("桩：这一轮故意坏掉")
        found = re.search(r"`([a-z_]+)`", system)
        kind = found.group(1) if found else "todo"
        out = []
        for line in user.splitlines():
            if not re.match(r"^\[\d+\] ", line):
                continue
            body = line.split(": ", 1)[-1].strip()
            if body:
                out.append({"kind": kind, "title": body[:12], "quote": body})
        return json.dumps(out, ensure_ascii=False)


@pytest.fixture()
def stub(monkeypatch):
    """把真客户端的 `complete` 换成桩。**其余一切照旧。**"""
    s = _Stub()

    def _complete(
        self, *, system: str, user: str, max_tokens: int = 2048,
        response_schema: dict | None = None,
    ) -> str:
        return s.complete(system=system, user=user, max_tokens=max_tokens)

    monkeypatch.setattr(OpenAIClient, "complete", _complete)
    return s


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    """一个落在临时目录上的 `Settings`。

    ※ 清掉那两个环境变量：它们决定组合根选哪个后端 —— 留着的话，用例会去
    连开发机上的 RDS / Redis（那是别人的数据）。
    """
    monkeypatch.delenv("YECHAO_DB_URL", raising=False)
    monkeypatch.delenv("YECHAO_REDIS_URL", raising=False)
    data, cfg = tmp_path / "data", tmp_path / "config"
    data.mkdir()
    cfg.mkdir()
    # 群配置自己写一份 —— 这样 `make_app` 的「首启从模板复制」不会触发，
    # 而「默认源是 synth」这件事与配置无关（见 `sources/__init__.py`）。
    (cfg / "groups.toml").write_text(
        '[[group]]\nid = 820000002\nname = "某班"\ntier = "normal"\nenabled = true\n',
        encoding="utf-8",
    )
    return Settings.from_paths(data_dir=data, config_dir=cfg)


# ── 判据 ①：一轮做了什么 ────────────────────────────────────────────


def test_一轮做了取数与抽取(settings, stub) -> None:
    out = watch.run_round(settings, round_no=1)

    assert out.status == watch.STATUS_OK, f"这一轮没跑成：{out.note}"
    assert out.read == CORPUS_SIZE, f"取数读到 {out.read} 条"
    assert out.inserted == CORPUS_SIZE, "第一次跑应当全部是新消息"
    assert out.items > 0, "抽取没有产出条目 —— 取数与抽取至少有一头断了"
    assert out.batches_total >= 1


def test_第二轮不重抽已经抽过的消息(settings, stub) -> None:
    """※ 「只处理新消息」不是循环做的，是用例层那两条保证做的
    （`ingest` 的 `INSERT OR IGNORE` 与 `extract` 的 `covered_msg_ids`）。

    ## 判据为什么不是「第二轮产出 0 条」

    写成那样会**误报**：第二轮看得到的消息集合比第一轮小（已经产出条目的
    那些被 `covered_msg_ids` 滤掉了），而预筛的「刷屏」判据是**在看到的
    那一批里**数的（同一人同群同内容 ≥3 次）—— 第一轮里 3 条重复被判刷屏、
    只留 1 条，第二轮只看得到剩下的 2 条，于是**低于阈值**、被正常保留。
    那是预筛本来就有的行为（手动连跑两次 `refine` 也一样），不是循环的毛病。

    ⇒ 判据落在**它真正要防的那件事**上：**第二轮没有碰第一轮已经抽过的
    那些消息**。重抽的症状是「同一条消息挂在两个条目上」与「条目数翻倍」，
    而它们**都不报错**。
    """
    first = watch.run_round(settings, round_no=1)
    assert first.items > 0, "前提：第一轮真的产出了东西"

    app = _app(settings)
    try:
        srcs_before = app.store.covered_msg_ids()
        ids_before = {it.item_id for it in app.store.all_items()}
        messages_before = app.store.message_count()
    finally:
        app.close()

    second = watch.run_round(settings, round_no=2)

    assert second.inserted == 0, "第二轮又写了一遍消息"

    app = _app(settings)
    try:
        assert app.store.message_count() == messages_before

        new_items = [
            it for it in app.store.all_items() if it.item_id not in ids_before
        ]
        new_srcs = {
            s["msg_id"] for it in new_items for s in app.store.sources_of(it.item_id)
        }
        assert new_srcs & srcs_before == set(), (
            "第二轮重抽了第一轮已经产出条目的消息 —— 那会产出重复条目，"
            f"而重复条目不报错。重抽的：{sorted(new_srcs & srcs_before)}"
        )

        # 每一条来源只挂一个条目（重复条目最直接的那个形状）。
        seen: dict[int, int] = {}
        for it in app.store.all_items():
            for s in app.store.sources_of(it.item_id):
                seen[s["msg_id"]] = seen.get(s["msg_id"], 0) + 1
        dup = {m: n for m, n in seen.items() if n > 1}
        assert not dup, f"这些消息被多个条目引用（重复条目）：{dup}"
    finally:
        app.close()


def test_跑到没有新东西时_会收敛(settings, stub) -> None:
    """※ 循环是**每 10 分钟一轮**的，所以「每轮都产出一大堆」是一个真实的
    失败形状（那说明它在重抽整库，而重复条目**不报错**）。

    ## 为什么要跑好几轮才收敛

    不是循环的毛病，是**预筛本来就有的**行为：刷屏判据是「在**看得到的
    那一批**里，同一人同群同内容 ≥3 次」，而每轮都会把已经产出条目的
    消息滤掉 —— 于是 3 条重复里第一轮只留 1 条（另 2 条记 `spam`），
    第二轮看得到 2 条、低于阈值 ⇒ 被正常保留、各产出一条。手动连跑
    两次 `refine` 也是这个结果。

    ⇒ 判据取两条，都是**最终**性质：
      · 收敛：跑够几轮之后产出归零
      · 不重抽：没有任何一条消息被两个条目引用（重抽最直接的形状）
    """
    seen_rounds = 0
    for n in range(1, 8):
        out = watch.run_round(settings, round_no=n)
        assert out.inserted == 0 or n == 1, "除了第一轮，不该再写入消息"
        seen_rounds = n
        if out.items == 0:
            break
    else:
        pytest.fail(f"跑了 {seen_rounds} 轮还在产出 —— 它没有收敛，说明每轮都在重抽")

    assert seen_rounds >= 2, "第一轮之后至少还要有一轮，这条判据才有意义"

    app = _app(settings)
    try:
        seen: dict[int, int] = {}
        for it in app.store.all_items():
            for src in app.store.sources_of(it.item_id):
                seen[src["msg_id"]] = seen.get(src["msg_id"], 0) + 1
        dup = {m: n for m, n in seen.items() if n > 1}
        assert not dup, f"这些消息被多个条目引用（重抽造出的重复条目）：{dup}"
    finally:
        app.close()


# ── 判据 ②：一轮失败不打死循环，但失败看得见 ────────────────────────


def test_一轮失败不打死循环_而且失败看得见(settings, monkeypatch, capsys) -> None:
    """※ 判据有两半，缺一不可：

      · **不打死**：第 2 轮照常跑（`rounds=2` 真的跑了两轮）
      · **看得见**：日志里说出了「哪一轮、为什么」

    只测前一半的话，一个**静默吞掉失败**的实现照样是绿的 ——
    而它正是本项目最怕的形状（一个失败看起来像成功）。
    """
    boom = _Stub(boom_times=1)

    def _complete(
        self, *, system: str, user: str, max_tokens: int = 2048,
        response_schema: dict | None = None,
    ) -> str:
        return boom.complete(system=system, user=user, max_tokens=max_tokens)

    monkeypatch.setattr(OpenAIClient, "complete", _complete)

    last = watch.loop(
        settings, interval=0, rounds=2, sleep=lambda _s: None
    )

    # ※ 失败的原因走 **stderr**（与 `daily.report` 同一种分法：状态行走
    # stdout，要人停下来看的那几行走 stderr）—— 所以两个流都要看。
    cap = capsys.readouterr()
    text = cap.out + cap.err
    assert "第 1 轮" in text and "第 2 轮" in text, "两轮没有都跑到 —— 第一轮把循环打死了"
    assert "这一轮没成" in text, "第一轮失败了，日志里却什么都没说"
    assert "RuntimeError" in text, "失败的原因没说出来 —— 「哪一轮、为什么」要答得出来"
    assert "循环继续" in text, "失败之后要说明循环还会继续（否则读日志的人以为它死了）"
    # 第 2 轮应当跑成了（桩只坏一次）
    assert last is not None and last.status == watch.STATUS_OK


def test_模型连不上是降级_不是没跑成(settings, monkeypatch) -> None:
    """※ 与「这一轮坏了」**必须分开**：模型没成是降级（条目少了一部分），
    而配置坏了、库连不上是没跑成。

    两者的下一步完全不同：一个去查端点，一个去查配置。
    """
    from yechao.domain.errors import ModelError

    def _complete(
        self, *, system: str, user: str, max_tokens: int = 2048,
        response_schema: dict | None = None,
    ) -> str:
        raise ModelError("连不上模型端点 http://127.0.0.1:9/v1/chat/completions")

    monkeypatch.setattr(OpenAIClient, "complete", _complete)

    out = watch.run_round(settings, round_no=1)

    assert out.status == watch.STATUS_DEGRADED
    assert out.batch_failed > 0
    assert out.exit_code == watch.EXIT_DEGRADED


def test_配置坏了是没跑成(settings, monkeypatch) -> None:
    """※ `groups.toml` 写坏 ⇒ `make_app_or_explain` 抛 —— 那是「没跑成」，
    而且**不带 traceback**（它抛的那句话就是「该改哪个文件」）。"""
    (settings.config_dir / "groups.toml").write_text(
        "[[group]]\nname = \"没有 id\"\n", encoding="utf-8"
    )
    out = watch.run_round(settings, round_no=1)

    assert out.status == watch.STATUS_FAILED
    assert "groups.toml" in out.note, f"没说出是哪份配置坏了：{out.note!r}"


# ── 判据 ③：常驻进程不长期持有写者锁 ────────────────────────────────


def _watch_env(settings: Settings) -> dict[str, str]:
    import os

    env = dict(os.environ)
    env["YECHAO_DATA_DIR"] = str(settings.data_dir)
    env["YECHAO_CONFIG_DIR"] = str(settings.config_dir)
    env["YECHAO_DB"] = str(settings.db_path)
    env.pop("YECHAO_DB_URL", None)
    env.pop("YECHAO_REDIS_URL", None)
    env["PYTHONIOENCODING"] = "utf-8"
    # ※ **必须关掉缓冲**：子进程的 stdout 指向一个文件（不是终端），
    # Python 默认按块缓冲 —— 日志要等进程退出才落盘，而这条判据正是
    # 「等日志里出现第 1 轮」。不关的话它会一直等不到，**而循环其实跑得好好的**。
    env["PYTHONUNBUFFERED"] = "1"
    return env


def test_循环活着的时候_手动命令拿得到写者锁(settings, tmp_path) -> None:
    """※ **判据是行为，而且要用真的子进程。**

    「一个只在单线程里成立的锁不是锁」（`test_writer_lock.py` 的取向）；
    反过来，「一个只在单线程里成立的**放锁**」同样不是。

    做法：起一个**真的** `python -m yechao.interfaces.watch`（间隔 30 秒），
    等它把第 1 轮跑完（日志里出现「第 1 轮」），然后在**测试进程里**
    试拿同一把锁 —— 拿得到才算过。

    ※ 为什么先等「第 1 轮」出现在日志里：那说明**那一轮真的跑过了**
    （而不是「循环还没开始」）。不等的话，这条判据会**空转**：
    循环还没拿锁，测试拿到的是一把没人要的锁，它照样是绿的。
    """
    log = tmp_path / "watch.log"
    env = _watch_env(settings)
    with log.open("w", encoding="utf-8") as fh:
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "yechao.interfaces.watch",
                "--data-dir", str(settings.data_dir),
                "--config-dir", str(settings.config_dir),
                "--interval", "30",
            ],
            cwd=str(ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT,
        )
    try:
        _wait_for(lambda: "第 1 轮" in _read(log), timeout=60,
                  what="第 1 轮跑完（日志里出现「第 1 轮」）")
        assert proc.poll() is None, "循环已经退出了 —— 它本该一直跑着"

        # 循环还活着，此刻它**在睡**。锁必须是空的。
        held, why = acquire_writer_lock(settings.db_path.parent, command="refine")
        assert held is not None, (
            "常驻循环在睡的时候仍然占着写者锁 —— 手动 `refine` 永远拿不到。\n"
            f"{why}"
        )
        held.release()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _wait_for(cond, *, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.1)
    pytest.fail(f"等了 {timeout}s 也没等到：{what}")


# ── 判据 ④：间隔可配，非法值不静默取默认 ────────────────────────────


def test_间隔默认是十分钟() -> None:
    assert watch.DEFAULT_INTERVAL == 600
    assert watch.resolve_interval({}) == 600


def test_间隔可以从环境变量配(monkeypatch) -> None:
    monkeypatch.setenv("YECHAO_WATCH_INTERVAL", "7")
    assert watch.resolve_interval({}) == 7


def test_命令行覆盖环境变量(monkeypatch) -> None:
    monkeypatch.setenv("YECHAO_WATCH_INTERVAL", "7")
    assert watch.resolve_interval({"--interval": "3"}) == 3


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "1.5"])
def test_非法间隔当场报错_不取默认值(bad, monkeypatch) -> None:
    """※ 取默认值的症状是「我改了间隔但它没按我写的来」—— 而
    「每 10 分钟跑一次」与「每 1 分钟跑一次」在日志里长得一样。

    `0` 尤其不能当成「不睡」：那会变成忙等，把 CPU 打满。

    ※ 空串**不在这一档**：`YECHAO_WATCH_INTERVAL=` 与「没设这个变量」
    是同一件事（用户没有表达任何意图），与 `YECHAO_DB_URL` 同一条判据。
    """
    monkeypatch.setenv("YECHAO_WATCH_INTERVAL", bad)
    with pytest.raises(watch._BadValue):
        watch.resolve_interval({})


# ── 入口 ────────────────────────────────────────────────────────────


def test_入口_不认识的参数退出码是2(capsys) -> None:
    assert watch.main(["--data-dir", "x", "--nope"]) == 2


def test_入口_非法间隔退出码是2(capsys) -> None:
    assert watch.main(["--interval", "abc"]) == 2


def test_入口_帮助不跑循环(capsys) -> None:
    assert watch.main(["--help"]) == watch.EXIT_OK
    assert "信息流自动更新" in capsys.readouterr().out


def test_入口_跑一轮就退(settings, stub) -> None:
    """※ `--rounds 1` 是「跑一轮看看成不成」—— 退出码就是那一轮的状态码。

    这条走的是**真入口**（`main`），所以参数解析、状态码映射都过了一遍。
    """
    code = watch.main([
        "--data-dir", str(settings.data_dir),
        "--config-dir", str(settings.config_dir),
        "--rounds", "1",
    ])
    assert code == watch.EXIT_OK


def _app(settings: Settings):
    from yechao.bootstrap import make_app

    return make_app(settings=settings)
