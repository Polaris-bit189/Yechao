"""摄取用例 —— 读源 → 写库。

## 要防什么

**「读不到」与「本来就没有」必须分开**。

```
这个群没消息        → 返回空
这个群读不到        → 返回空     ← 形状相同，这就是问题
```

下游看到两个空，会给出同一份日报，并用「今天很安静」的口气叙述它。

⇒ 判据：**读不到就抛，不许返回空**（由数据源实现守）。本模块负责的是
**抛了之后怎么办**。

## 抛了之后：其余源还应不应该落库

我的判断：**应该。** 一个群读不到不该让其余群的消息也进不了库 —— 那会把
一次局部失败放大成全量失败。

但**退出码要非零**，而且失败**要记在某个地方**（`IngestOutcome.errors`）。
「一个失败如果看起来像成功，它比失败本身更危险」——
静默继续，用户看到的是「今天只有这些」，而不是「今天有三个群没读到」。

⇒ 所以本模块的设计是：
  · 逐个源读，**失败的不阻断其余**
  · 但**每一个失败都记进 `errors`**
  · **由调用方（CLI）决定退出码** —— 用例层不 `sys.exit`

## ※ 账要落库，而且**一轮都不能少**

`errors` 原来只活在这一次的返回值里：CLI 打印完，它就没了。而
「哪几个源没读到」恰恰是事后最要问的一句话（「今天怎么只有这些？」）。

⇒ 现在每一轮摄取都留一条 `runs` 抬头 + 若干 `run_entries` 账，
并且**与消息同一个事务**（`upsert_messages(..., run=run)`）。

※ 注意这里调 `upsert_messages` **不再有 `if collected:` 这个条件**：
一轮「一个源都没读到」的摄取**正是最需要留下证据的那一种**，
而它原来什么都不会写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from yechao.application.jobs import new_run_id
from yechao.domain.errors import (
    RUN_FAILED,
    RUN_INGEST,
    RUN_OK,
    FailureLedger,
    RunRecord,
    SourceError,
)

#: 账的种类：一个源没读到。
#:
#: ※ 只此一种。摄取这一路没有别的「没成的事」—— 「这个源本来就没有新
#: 消息」返回空、**不记账**，而那是刻意的：账记的是**失败**，不是安静。
SOURCE_UNREADABLE = "source_unreadable"


@dataclass
class IngestDeps:
    messages: Any   # MessageRepository
    sources: list[Any]  # 若干 MessageSource
    #: ※ 时钟**必给**：账要记「什么时候跑的」。
    #: 与 `ExtractDeps` / `DigestDeps` 同一个形状 —— 三个用例都从端口拿时间，
    #: 谁也不许自己去取。
    clock: Any      # Clock


@dataclass
class IngestOutcome:
    """一次摄取的结果。

    ※ `errors` 与「读到 0 条」**必须分开**：
      · `errors` 非空 ⇒ 有些源**没读到**，退出码要非零
      · `errors` 为空且 `inserted == 0` ⇒ 源**真的没有新消息**
    这两件事如果混成一个「0」，用户与评委都分不出来。
    """

    inserted: int = 0
    read: int = 0
    #: 这一轮的账（哪个源没读到）。它会随消息一起落库。
    ledger: FailureLedger = field(default_factory=FailureLedger)
    #: 这一次跑在库里的 id（`runs.run_id`）。见 `extract.ExtractOutcome`。
    run_id: str = ""
    #: `[(源名, 错误)]`。
    errors: list[tuple[str, str]] = field(default_factory=list)
    #: 每个源读到了几条（**含**读不到的那些，值为 0）。
    per_source: dict[str, int] = field(default_factory=dict)
    #: 这次**真的读到**的消息（按源的顺序拼接，**不含**读不到的那些源）。
    #:
    #: ※ 为什么要把它交出来：`demo` 除了入库，还要对**同一批**消息跑预筛、
    #: 统计每群条数。让它自己去 `messages()` 再取一份，就是「两次读碰巧一样」
    #: —— 一旦源改成有状态（游标、时间窗），两次读会**不同**，而报告会把
    #: 两个不同批次的数字并排显示，且不报错。
    #:
    #: ⇒ 判据：报告里出现的每一个数字，都必须来自**这一次读到的**那一批。
    messages: list[Any] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def run(
    deps: IngestDeps,
    *,
    since: int | None = None,
    until: int | None = None,
) -> IngestOutcome:
    """从所有源读一遍并写库。"""
    outcome = IngestOutcome()
    started_at = deps.clock.now()
    collected: list[Any] = []

    for src in deps.sources:
        name = getattr(src, "name", type(src).__name__)
        try:
            msgs = src.read(since=since, until=until)
        except SourceError as e:
            # ※ **不中断**，但**必须记账**。见模块 docstring。
            outcome.errors.append((name, str(e)))
            outcome.per_source[name] = 0
            # ※ 账里存的是**原因码**（源名 + 异常类型名），**不是** `str(e)`
            # —— 后者的上下文里可能带路径、URL 之类的东西，而账里只放
            # 计数、标识与原因。给人看的那一份在 `errors` 里，CLI 会打印。
            outcome.ledger.record(
                SOURCE_UNREADABLE, reason=f"{name}:{type(e).__name__}"
            )
            continue
        outcome.per_source[name] = len(msgs)
        outcome.read += len(msgs)
        collected.extend(msgs)

    outcome.messages = collected

    # ※ **不管读没读到，都要走这一趟**：一轮「一个源都没读到」的摄取
    # 正是最需要留下证据的那种跑（见模块 docstring）。
    #
    # ※ `produced` 记的是「**交给库**多少条」，不是「其中新写了几条」——
    # 后者只有 `upsert_messages` 知道（它才看得见重复键），而账与消息在
    # 同一个事务里，提交之前拿不到那个数。记一个**写入前拿得到**的数，
    # 比记一个编出来的数好；「新写了几条」CLI 会打印。
    #
    # ※ 幂等：已存在的按 `msg_id` 跳过。重跑摄取不该产出重复消息
    # （重复消息会导致重复条目，而那是无声的）。
    run = RunRecord(
        run_id=new_run_id(),
        kind=RUN_INGEST,
        started_at=started_at,
        finished_at=deps.clock.now(),
        # status 与 CLI 的退出码同一条：有源没读到 ⇒ 非零。
        status=RUN_FAILED if outcome.errors else RUN_OK,
        seen=outcome.read,
        produced=len(collected),
        entries=outcome.ledger.entries,
    )
    outcome.inserted = deps.messages.upsert_messages(collected, run=run)
    outcome.run_id = run.run_id

    return outcome
