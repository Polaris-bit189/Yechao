"""信息流自动更新 —— **每 10 分钟一轮**的常驻循环。

## 要防什么

`ingest` 与 `refine` 都是**手动**命令。没人跑，信息流就停在原地 ——
而它与「今天没人发消息」在页面上是**同一句话**：一个空的信息流。

`daily`（`yechao/interfaces/daily.py`）解决的是「每天一份日报」那半边，
它一天只跑一次，而且只跑 digest。这个模块解决的是另一半：**库里的条目
要自己长出来**，否则日报每天读的都是一份越来越旧的快照。

## 三条判据

### ① 一轮失败**不能打死循环**，但必须**看得见**

一轮里会坏的东西很多：模型端点连不上、源读不到、库锁住了、配置改坏了。
它们有一个共同点 —— **下一轮大概率会好**（端点是暂时的，配置改回来就好）。

所以：**记下来，继续睡，下一轮照常**。

※ 但「继续」不等于「沉默」。一个吞掉失败继续跑的循环，在
`docker compose logs` 里与一个一切正常的循环**长得一模一样** ——
而那是本项目最怕的形状：一个失败看起来像成功。所以每一轮都打印
轮次、时间、实际产出与状态，失败的还要把原因打出来。

### ② 常驻进程**不许长期占着写者锁**

写者锁（`infrastructure/runtime/lock.py`）的语义是「同一时刻只允许一个
写者」。这个循环是常驻的 —— 如果它在**整个生命周期**里都握着锁，那么
手动 `python -m yechao refine` **永远拿不到**，而用户看到的是
「已经有一个写者在跑」，去找那个进程又找不到（它就是这个循环）。

⇒ 判据：**锁跟着「一轮」走，不跟着「进程」走。**

    while True:
        拿锁 → 取数 → 抽取 → 放锁
        睡

睡的那一段时间（默认 600 秒里的大约 599 秒）**锁是空的** ——
手动命令随时拿得到。一轮最长也就几十秒，而拿不到锁的手动命令会
**立刻**得到一句「谁在跑、在哪跑」（锁的默认行为就是不等）。

※ 反过来也要对：**一轮里必须拿着锁**。取数与抽取都在改库，
循环与手动 `refine` 撞在一起会产出重复条目 —— 而那**不报错**，
只是让日报里多出一行。

※ 拿不到锁时**不硬闯**（那会让两个写者同时改库），也**不静默跳过**：
这一轮记成 `skipped` 并打进日志。让给手动命令是对的取舍 ——
用户按下回车的那一刻，他要的是**他的**那次跑。

### ③ 间隔可配，默认 10 分钟

`YECHAO_WATCH_INTERVAL`（秒，默认 600）。命令行 `--interval` 覆盖它。

※ 为什么默认 10 分钟：这个产品的输入是**群消息**，而群消息的时效性
以「小时」计（一条通知在半小时内看到就够了）。10 分钟让「页面刷新
就能看到」成立，而代价是每小时 6 轮 —— 每轮在没有新消息时几乎不花钱
（预筛之后没有待处理的消息 ⇒ `extract.run` 连模型都不调）。

## 与 `daily` 的关系

两者都是「定时跑一条已有的路」，取舍也一致：**独立服务 + 主进程**
（见 `deploy/docker-compose.yml` 里 watcher 那一节）。

差别只有两处：

  · `daily` 一天跑一次、跑的是 `digest`；这里每 10 分钟跑一次、
    跑的是 `ingest` → `refine`
  · `daily` 的循环在 **shell** 里（它要算「下一个 07:30」，而那是一个
    日历算术）；这里的循环在 **Python** 里，因为它要拿写者锁、要按轮
    记结果 —— 那是两件 shell 表达不了的事（shell 里做只能再起一个
    python 去问，于是同一个问题有了第二个读法）。

## 只处理新消息

「只处理新消息」不是这个模块做的，是**用例层本来就有**的两条保证：

  · `ingest` 写消息用 `INSERT OR IGNORE`（按 `msg_id`）—— 重跑不会
    多出消息；
  · `extract.run` 第一步就跳过 `covered_msg_ids()`（已经有条目引用过的
    消息，**含被软删的**）—— 重跑不会多出条目。

⇒ 这个循环每轮都跑完整的 `ingest` + `refine`，而「新」这件事由那两条
保证兜住。在这里再写一个「上次跑到哪」的游标，等于给同一个问题**第二个
答案** —— 而两个答案会打架（游标落后于条目时，消息被静默跳过）。

※ 怎么验证：`tests/interfaces/test_watch.py` 里有一条用例跑两轮，
断言第二轮不产出重复条目。
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

# ── 退出码 ──────────────────────────────────────────────────────────
#
# ※ 与 `daily` 同一套三态（0 / 3 / 1），理由也一样：**调度器只看得到
# 退出码**，所以「跑成了」与「跑成了但有降级」不能共用一个码。
#
#   0  这一轮跑成了
#   3  跑了但有降级（有源读不到 / 有整批失败），**或者**这一轮让给了
#      别的写者（`skipped`）
#   1  这一轮没跑成（配置坏了、库连不上、构建 App 就失败了）
#   2  参数不认识
#
# ※ 退出码只在 `--rounds N` 跑完时才有意义（常驻进程靠信号退出，
# 那一路返回 130）。`--rounds 1` 就是「跑一轮看看成不成」。
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_DEGRADED = 3

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

#: 状态 → 人话。※ 未知状态**原样打出来**（`.get(x, x)`）——
#: 吞成「其它」的话，将来新加一种状态时日志里看不出来。
_STATUS_TEXT = {
    STATUS_OK: "跑成了",
    STATUS_DEGRADED: "跑成了但有降级",
    STATUS_SKIPPED: "这一轮让给了别的写者（没跑）",
    STATUS_FAILED: "没跑成",
}

_EXIT_OF_STATUS = {
    STATUS_OK: EXIT_OK,
    STATUS_DEGRADED: EXIT_DEGRADED,
    STATUS_SKIPPED: EXIT_DEGRADED,
    STATUS_FAILED: EXIT_FAILED,
}

#: 默认间隔（秒）。见模块 docstring 判据 ③。
DEFAULT_INTERVAL = 600

#: 心跳文件。healthcheck 读它的 mtime —— 见 compose 里 watcher 的探活。
#:
#: ※ 它与 `deploy/scheduler.sh` 的那个**不是同一个文件**：两个服务各自
#: 探各自的循环，共用一个会让「日报那个卡住了」把「取数那个正常」也报成
#: 不健康，反过来也一样。
HEARTBEAT_NAME = ".watcher-heartbeat"

#: 睡的时候每多久摸一次心跳。
#:
#: ※ 为什么不是「睡一整段、醒来再摸」：那样心跳在两次轮次之间会**过期**
#: `interval` 秒，而 healthcheck 的窗口就得跟着 interval 走 —— 于是
#: 「interval 配大了，探活窗口也得改」变成一条要人工维护的耦合。
#: 分片睡 + 每片摸一次，让探活窗口与 interval **无关**。
_TICK_SECONDS = 5.0

USAGE = """\
夜抄 · 信息流自动更新（每 10 分钟一轮）

用法：
  python -m yechao.interfaces.watch [选项]

选项：
  --data-dir DIR       数据目录（默认 data/）
  --config-dir DIR     配置目录（默认 config/）
  --db PATH            主库路径（默认 <data-dir>/yechao.db）
  --interval SECONDS   两轮之间隔多久（默认取 YECHAO_WATCH_INTERVAL，再默认 600）
  --rounds N           跑 N 轮就退出（默认 0 = 一直跑；`--rounds 1` = 跑一轮看看）
  --lock-wait SECONDS  拿不到写者锁时最多等多久（默认 0 = 立刻让给别的写者）
  --budget N           抽取每一趟最多跑几批（默认 200，与 `refine` 一致）
  --source NAME        每轮取数用哪个源（逗号分隔可给多个）。
                       不给就取 YECHAO_WATCH_SOURCES，再不给用默认源（合成语料）。
                       源名不认识会在每一轮里报出来（不静默回落到合成语料）。

退出码（只在 --rounds 跑完时有意义）：
  0  跑成了
  3  跑了但有降级，或者这一轮让给了别的写者
  1  没跑成
  2  参数不认识
"""


@dataclass
class RoundOutcome:
    """一轮的结果 —— **给调用方判断用**，不是给打印用。"""

    round_no: int
    #: 这一轮开始的本地时刻（`YYYY-MM-DD HH:MM:SS`）。
    started_at: str = ""
    #: ※ 初值是**空串**（不是 `ok`、也不是 `failed`）：它表示「还没判」，
    #: 由 `_decide_status` 从实际产出上填。
    #:
    #: 初值写成 `ok` 是最危险的一种 —— 任何一条忘了标失败的路都会报成
    #: 「跑成了」，而那正是本项目最怕的形状（失败看起来像成功）。
    #: 写成 `failed` 则相反：一个跑得好好的轮次会被报成没跑成（第一版就是
    #: 这么写的，实测踩到：状态恒为 failed，因为 `_decide_status` 见到
    #: `failed` 就早退了）。
    #: 空串两头的毛病都没有 —— 它的退出码落在 `EXIT_FAILED`（fail-safe），
    #: 而正常路径一定会经过 `_decide_status`。
    status: str = ""
    #: 取数：读到几条 / 新写入几条 / 哪几个源没读到。
    read: int = 0
    inserted: int = 0
    #: **逐个源**读到几条。
    #:
    #: ※ 只报一个总数是不够的：`docker compose logs watcher` 里那行
    #: 「取数：读到 N 条」**看不出读的是哪个源** —— 一个永远在读合成语料的
    #: 循环与一个正常读真源的循环，在日志里长得一模一样。而这个循环是
    #: 无人值守的，没人会去逐行核对配置。
    per_source: dict[str, int] = field(default_factory=dict)
    source_errors: list[str] = field(default_factory=list)
    #: 抽取：产出条目 / 批次 / 被追踪而单独抽了一趟的消息数。
    items: int = 0
    batches_ok: int = 0
    batches_total: int = 0
    forced: int = 0
    #: 有整批失败（模型那一侧没跑成）。
    batch_failed: int = 0
    #: 这一轮为什么没成 / 为什么让路。空串 = 没什么要补的。
    note: str = ""
    #: 这一轮真的花掉的秒数。
    seconds: float = 0.0

    @property
    def exit_code(self) -> int:
        return _EXIT_OF_STATUS.get(self.status, EXIT_FAILED)

    @property
    def status_text(self) -> str:
        return _STATUS_TEXT.get(self.status, self.status)


# ── 一轮 ────────────────────────────────────────────────────────────


def run_round(
    settings: Any,
    *,
    round_no: int,
    lock_wait: float = 0.0,
    budget: int = 200,
    sources: list[str] | None = None,
) -> RoundOutcome:
    """跑**一轮**：拿锁 → 取数 → 抽取 → 放锁。

    ※ 锁的获取与释放都在这一个函数里，**不跨越它** —— 那是判据 ② 的
    全部内容（见模块 docstring）。返回时锁一定已经放掉了（`finally`），
    包括中途抛异常的情况。

    ※ 这一轮里**重建一次 App**（而不是全程共用）：配置是用户随时能改的
    （`tracks.toml` / `groups.toml` / `settings.toml`），共用一份会让
    「刚加的追踪规则下一轮才生效」—— 而那一轮的延迟是 10 分钟。
    代价是每轮重开一次连接，在 10 分钟的粒度上可以忽略。
    """
    from yechao.bootstrap import acquire_writer_lock, make_app_or_explain
    from yechao.domain.errors import YechaoError

    out = RoundOutcome(round_no=round_no, started_at=_stamp())
    began = time.monotonic()

    held, why = acquire_writer_lock(
        settings.db_path.parent, command="watch", wait_seconds=lock_wait
    )
    if why:
        # ※ 让路，不硬闯。见模块 docstring 判据 ②。
        out.status = STATUS_SKIPPED
        out.note = why
        out.seconds = time.monotonic() - began
        return out

    try:
        try:
            app = make_app_or_explain(settings=settings)
        except SystemExit as e:
            # ※ `make_app_or_explain` 抛的那句话是「该改哪个文件」——
            # 比 traceback 有用，所以原样带出来。
            out.status = STATUS_FAILED
            out.note = str(e).strip()
            return out
        except YechaoError as e:
            out.status = STATUS_FAILED
            out.note = str(e)
            return out

        try:
            _ingest(app, out, sources)
            _refine(app, out, budget=budget)
        except Exception as e:  # noqa: BLE001 —— 见下面那段
            # ※ **兜住所有异常**，不是偷懒。一轮里能坏的东西包括
            # `OSError`（库文件被删）、`sqlite3.Error`（锁超时）、
            # 模型客户端的裸 `ValueError`（端点地址写歪了）—— 它们都不是
            # `YechaoError`。漏掉任何一种，循环就会**退出**，而那正是
            # 判据 ① 要防的（一轮失败打死整个循环）。
            #
            # ※ 兜住不等于吞掉：类型名与消息都会打进日志，下一轮照常。
            out.status = STATUS_FAILED
            out.note = f"{type(e).__name__}: {e}"
        finally:
            app.close()
    finally:
        held.release()

    out.seconds = time.monotonic() - began
    _decide_status(out)
    return out


def _ingest(app: Any, out: RoundOutcome, sources: list[str] | None) -> None:
    """取数。**有源读不到不算这一轮没成**（局部失败不放大），但要记下来。

    ※ `sources` 是**源名**（`--source` / `YECHAO_WATCH_SOURCES` 递进来的），
    不给就由组合根用默认源。入口层只递名字、不 import 适配层 ——
    见 `application/ports.py` 与 `cli._cmd_ingest` 里同一条说明。
    """
    from yechao.application import ingest as ingest_uc

    outcome = ingest_uc.run(app.ingest_deps(sources or None))
    out.read = outcome.read
    out.inserted = outcome.inserted
    out.per_source = dict(outcome.per_source)
    out.source_errors = [f"{name}: {err}" for name, err in outcome.errors]


def _refine(app: Any, out: RoundOutcome, *, budget: int) -> None:
    """抽取。走的是 `application.track.run` —— 与 `python -m yechao refine`
    **同一条路**（含追踪那两趟）。

    ※ 复用同一条路而不是在这儿另写一遍：两份实现必漂移，而漂移的症状是
    「手动跑出来的条目与自动跑出来的不一样」，两边单看都正常。
    """
    from yechao.application import track as track_uc

    rules = app.config.tracks()
    outcome = track_uc.run(app.extract_deps(), rules, budget=budget)
    out.items = len(outcome.items)
    out.batches_ok = outcome.batches_ok
    out.batches_total = outcome.batches_total
    out.forced = outcome.forced
    out.batch_failed = outcome.ledger.count("batch_failed")


def _decide_status(out: RoundOutcome) -> None:
    """从**实际产出**上读状态。**不重推用例层的规则。**

    · 有源读不到 ⇒ 降级（消息少了一部分，而那不是「今天很安静」）
    · 有整批失败 ⇒ 降级（模型那一侧没跑成，条目少了一部分）
    · 两者都没有 ⇒ 跑成了（哪怕这一轮一条新消息都没有 —— 那是**安静**，
      不是失败）

    ※ 「这一轮什么都没做」与「这一轮没做成」是**两句不同的话**。
    前者是 `ok`（库里已经是最新的），后者是 `failed`。

    ※ 它**只在状态还没定的时候**填（空串）。已经标成 `failed` / `skipped`
    的轮次不该被这里改成 `ok` —— 那是「失败看起来像成功」的另一种写法。
    """
    if out.status in (STATUS_FAILED, STATUS_SKIPPED):
        return
    out.status = STATUS_DEGRADED if (out.source_errors or out.batch_failed) else STATUS_OK


# ── 打印 ────────────────────────────────────────────────────────────


def _say(msg: str = "") -> None:
    print(msg)


def _fail(msg: str) -> None:
    print(f"  ! {msg}", file=sys.stderr)


def _stamp() -> str:
    """本地时刻。※ 用 `time.strftime` 而不是自己拼 —— 它跟着 TZ 走，
    而容器里的 TZ 是 `Asia/Shanghai`（见 `deploy/Dockerfile`）。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def report(out: RoundOutcome, *, interval: int | None = None) -> None:
    """把这一轮**说出来**。

    ※ 打印不是装饰：`docker compose logs watcher` 里能看到的只有这些行。
    一个不打印的循环，失败与成功在日志里长得一样。
    """
    _say(f"[{out.started_at}] 第 {out.round_no} 轮（用了 {out.seconds:.1f}s）")
    if out.status == STATUS_SKIPPED:
        _say(f"  · 跳过：{out.note.splitlines()[0] if out.note else '有别的写者在跑'}")
        _say("  · 这一轮不硬闯（两个写者同时改库会产出重复条目，而且不报错）")
        _say("  · 下一轮照常 —— 循环不会因为让路而退出")
        return

    if out.status == STATUS_FAILED:
        # ※ **失败要看得见**，而且要说清「下一轮还会跑」——
        # 否则读日志的人第一反应是「这个循环死了」。
        _fail(f"这一轮没成：{out.note}")
        _fail("循环继续 —— 下一轮照常跑（一轮失败不打死循环）")
        _say(f"  状态 {out.status_text}（退出码 {out.exit_code}）")
        return

    _say(f"  · 取数：读到 {out.read} 条，写入 {out.inserted} 条")
    # ※ **逐个源**报数（与 `ingest` 那条命令同一个形状）。不给 `--source` 时
    # 走的是默认源，而「我明明想读会议转写，怎么读的是合成语料」必须是
    # **一眼可见**的 —— 这个循环无人值守，看不见就等于没有。
    for name in sorted(out.per_source):
        _say(f"      · 源 {name}：{out.per_source[name]} 条")
    _say(
        f"  · 抽取：条目 {out.items} 条，批次 {out.batches_ok}/{out.batches_total} 成功"
        + (f"，追踪补抽 {out.forced} 条" if out.forced else "")
    )
    if out.source_errors:
        for err in out.source_errors:
            _fail(f"源读不到：{err}")
        _fail("上面的数字**不是全部** —— 有几个源没读到")
    if out.batch_failed:
        _fail(f"有 {out.batch_failed} 条消息整批失败（模型那一侧没跑成）")
    _say(f"  状态 {out.status_text}（退出码 {out.exit_code}）")
    if interval:
        _say(f"  下一次大约 {_stamp()} + {interval}s")


# ── 循环 ────────────────────────────────────────────────────────────


def loop(
    settings: Any,
    *,
    interval: int = DEFAULT_INTERVAL,
    rounds: int = 0,
    lock_wait: float = 0.0,
    budget: int = 200,
    sources: list[str] | None = None,
    sleep: Any = None,
    clock: Any = None,
) -> RoundOutcome | None:
    """常驻循环。`rounds > 0` 时跑完就返回（给测试与 `--rounds` 用）。

    ※ `sleep` 与 `clock` 可注入：测试要跑「两轮之间隔 1 秒」，
    而真的睡 600 秒的用例没人会跑。这与 `daily` 的「时间从时钟端口取」
    是同一条判据 —— **不注入的后果是一条只在我这台机器上成立的判据**。
    """
    _sleep = sleep or time.sleep
    _now = clock or time.monotonic

    heartbeat = Path(settings.data_dir) / HEARTBEAT_NAME
    last: RoundOutcome | None = None
    n = 0

    while rounds <= 0 or n < rounds:
        n += 1
        _touch(heartbeat)
        out = run_round(
            settings, round_no=n, lock_wait=lock_wait, budget=budget, sources=sources
        )
        report(out, interval=interval if (rounds <= 0 or n < rounds) else None)
        last = out

        if rounds > 0 and n >= rounds:
            break
        _sleep_keeping_heartbeat(_sleep, _now, interval, heartbeat)

    return last


def _touch(path: Path) -> None:
    """摸一下心跳文件。

    ※ 摸不了**不抛**：心跳是「这个循环还活着」的证据，而它写不进去
    （数据目录被删、权限变了）**不该让循环停下来** —— 那会把一个
    「探活看不见」的问题升级成「服务停了」。失败会由 healthcheck
    那边报出来（文件不新鲜 ⇒ unhealthy），那才是它该被看见的地方。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        pass


def _sleep_keeping_heartbeat(
    sleep: Any, now: Any, seconds: float, heartbeat: Path
) -> None:
    """分片睡，每片摸一次心跳。见 `_TICK_SECONDS`。"""
    deadline = now() + max(0.0, float(seconds))
    while True:
        remaining = deadline - now()
        if remaining <= 0:
            return
        sleep(min(_TICK_SECONDS, remaining))
        _touch(heartbeat)


# ── 入口 ────────────────────────────────────────────────────────────


def _parse(argv: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    """极简参数解析。返回 `(认出来的, 不认识的)`。

    ※ 与 `daily._parse` 同一个取向：不认识的参数**不静默忽略** ——
    调用方（`deploy/watcher.sh`）把它当拼错的信号，退出码 2。
    """
    known = {
        "--data-dir", "--config-dir", "--db", "--interval",
        "--rounds", "--lock-wait", "--budget", "--source",
    }
    out: dict[str, str] = {}
    unknown: list[str] = []
    i = 0
    args = list(argv)
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help", "help"):
            out["--help"] = ""
            i += 1
            continue
        if a in known:
            if i + 1 >= len(args):
                unknown.append(a)  # 给了开关没给值
                break
            out[a] = args[i + 1]
            i += 2
            continue
        unknown.append(a)
        i += 1
    return out, unknown


def _int_opt(opts: dict[str, str], key: str, default: int) -> int:
    """读一个整数开关。**非法值不静默取默认值。**

    ※ `--interval abc` 取默认值的话，用户以为改了间隔而其实没改 ——
    而「每 10 分钟跑一次」与「每 1 分钟跑一次」在日志里长得一样。
    所以非法值走 `unknown`（退出码 2），与拼错开关同一档。
    """
    raw = opts.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise _BadValue(f"{key} 需要一个整数，现在给的是 {raw!r}") from None


class _BadValue(Exception):
    """参数值不合法。"""


def resolve_interval(opts: dict[str, str]) -> int:
    """这一轮的间隔：命令行 > `YECHAO_WATCH_INTERVAL` > 600。"""
    import os

    if "--interval" in opts:
        raw: Any = opts["--interval"]
    else:
        # ※ 环境变量**空串 = 没设**（用默认值）。与 `YECHAO_DB_URL` 同一
        # 条判据：`os.environ.get(...)` 拿到空串时用户没有表达任何意图。
        # 而 compose 那边写的是 `${VAR:-600}`，空值也到不了这里。
        raw = os.environ.get("YECHAO_WATCH_INTERVAL") or DEFAULT_INTERVAL
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise _BadValue(
            f"间隔需要一个整数秒数，现在给的是 {raw!r}"
            f"（命令行 --interval，或者环境变量 YECHAO_WATCH_INTERVAL）"
        ) from None
    if value <= 0:
        # ※ 0 或负数**不当成「不睡」**：那会让循环变成忙等，把 CPU 打满。
        # 「跑一轮就退」有它自己的开关（`--rounds 1`）。
        raise _BadValue(f"间隔必须是正数秒（现在是 {value}）；跑一轮就退请用 --rounds 1")
    return value


#: 源名的环境变量。**部署时用它**：容器里的循环没有命令行，
#: 而 `--source` 那个开关是给手动跑一轮的人用的。
SOURCES_ENV = "YECHAO_WATCH_SOURCES"


def resolve_sources(opts: dict[str, str]) -> tuple[list[str], str]:
    """这一轮用哪些源，以及**它是从哪儿来的**（给启动横幅用）。

    顺序：命令行 > `YECHAO_WATCH_SOURCES` > 默认源（合成语料）。

    ※ 返回「从哪儿来」不是装饰：默认源是**合成语料**，而一个无人值守的循环
    读着演示数据、每轮都绿，是这条判据要防的那件事。所以用了默认值要能被
    看出来 —— 见 `main` 里那行横幅。
    """
    import os

    raw = opts.get("--source")
    where = "--source"
    if raw is None:
        raw = os.environ.get(SOURCES_ENV)
        where = SOURCES_ENV
    if raw is None or not raw.strip():
        return [], "默认源"
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        raise _BadValue(f"{where} 给了一串空白 —— 它应当是一个或多个源名（逗号分隔）")
    return names, where


def main(argv: Sequence[str] | None = None) -> int:
    """入口。返回**退出码**（不 `sys.exit` —— 那样测试只能去截 `SystemExit`）。"""
    from yechao.interfaces import cli

    # 中文 Windows 控制台是 GBK，而这里的输出全是中文。复用 CLI 那一份
    # `_fix_console` —— 两个入口对「控制台怎么处理」只能有一个答案。
    cli._fix_console()

    opts, unknown = _parse(sys.argv[1:] if argv is None else argv)

    if "--help" in opts:
        _say(USAGE)
        return EXIT_OK

    from yechao.bootstrap import Settings

    try:
        interval = resolve_interval(opts)
        rounds = _int_opt(opts, "--rounds", 0)
        budget = _int_opt(opts, "--budget", 200)
        lock_wait = float(opts.get("--lock-wait", 0.0))
        sources, sources_where = resolve_sources(opts)
    except (_BadValue, ValueError) as e:
        _fail(str(e))
        _say()
        _say(USAGE)
        return 2

    if unknown:
        _fail(f"不认识的参数：{' '.join(unknown)}")
        _say()
        _say(USAGE)
        return 2

    st = Settings.from_paths(
        data_dir=opts.get("--data-dir"),
        config_dir=opts.get("--config-dir"),
        db_path=opts.get("--db"),
    )

    _say("夜抄 · 信息流自动更新")
    _say(f"  数据目录 {st.data_dir}")
    _say(f"  配置目录 {st.config_dir}")
    _say(f"  间隔 {interval} 秒" + ("（只跑一轮）" if rounds == 1 else ""))
    if sources:
        _say(f"  数据源 {'、'.join(sources)}（来自 {sources_where}）")
    else:
        # ※ **用了默认值要说出来**，而且要说清下一步 —— 这个循环是无人值守的，
        # 一个永远在读合成语料的 watcher 与一个读真源的 watcher，
        # 在 `docker compose logs` 里长得一模一样。
        _say("  数据源 默认源（合成语料）")
        _say(
            f"    ※ 这不是你的群消息。要读真实来源，用 --source 或 "
            f"{SOURCES_ENV} 指定（见 python -m yechao --help 的源列表）。"
        )
    _say()

    try:
        last = loop(
            st, interval=interval, rounds=rounds,
            lock_wait=lock_wait, budget=budget, sources=sources or None,
        )
    except KeyboardInterrupt:
        _say()
        _say("被中断 —— 循环退出")
        # ※ 130 是「被 SIGINT 终止」的约定退出码（与 `cli.main` 同一档）。
        return 130

    return last.exit_code if last is not None else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
