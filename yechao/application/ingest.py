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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from yechao.domain.errors import SourceError


@dataclass
class IngestDeps:
    messages: Any   # MessageRepository
    sources: list[Any]  # 若干 MessageSource


@dataclass
class IngestOutcome:
    """一次摄取的结果。

    ⚠️ `errors` 与「读到 0 条」**必须分开**：
      · `errors` 非空 ⇒ 有些源**没读到**，退出码要非零
      · `errors` 为空且 `inserted == 0` ⇒ 源**真的没有新消息**
    这两件事如果混成一个「0」，用户与评委都分不出来。
    """

    inserted: int = 0
    read: int = 0
    #: `[(源名, 错误)]`。
    errors: list[tuple[str, str]] = field(default_factory=list)
    #: 每个源读到了几条（**含**读不到的那些，值为 0）。
    per_source: dict[str, int] = field(default_factory=dict)

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
    collected: list[Any] = []

    for src in deps.sources:
        name = getattr(src, "name", type(src).__name__)
        try:
            msgs = src.read(since=since, until=until)
        except SourceError as e:
            # ⚠️ **不中断**，但**必须记账**。见模块 docstring。
            outcome.errors.append((name, str(e)))
            outcome.per_source[name] = 0
            continue
        outcome.per_source[name] = len(msgs)
        outcome.read += len(msgs)
        collected.extend(msgs)

    if collected:
        # ⚠️ 幂等：已存在的按 `msg_id` 跳过。重跑摄取不该产出重复消息
        # （重复消息会导致重复条目，而那是无声的）。
        outcome.inserted = deps.messages.upsert_messages(collected)

    return outcome
