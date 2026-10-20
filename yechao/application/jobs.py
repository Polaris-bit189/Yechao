"""批处理骨架 —— 逐批执行、预算、取消、失败记账。

## 要防什么

**一次失败如果看起来像成功，它比失败本身更危险。**

批量作业有三种「看起来像成功」的失败：

| 看起来像 | 真相 |
|---|---|
| 作业「跑完了」 | 其实是预算耗尽，后面的批根本没跑 |
| 进度 2/N | 第 1 批就抛异常，而 `done` 被加了 1 |
| 「已取消」 | 只是**请求**了取消，正在飞的那批还在跑 |

## `done` 的语义：**最后一条成功批次的序号**

⚠️ 这是前端逐字钉住的契约：「`done` 是「**最后一条成功批次**的序号」，不是
『已完成批次数』（后端 docstring 原话）。照实显示，**别自己加一**——那会
变成另一句假话：第 1 批就抛异常时用户会看到 2/N，像『全成了』」。

⇒ 所以本模块更新 `done` 的**唯一**地方是「一批成功了」。第 n 批成功之后
`done = n + 1`（1-based 序号）。抛异常的批**不更新** `done`。

## 取消是协作式的

Python 没法从外面杀线程，所以「已请求取消」只在**批与批之间**生效：
正在飞的那一批会跑完，它停在下一批。

⚠️ 对**已经结束**的作业，取消请求返回 `(False, 终态)`，**不是**报错
—— 那是「手慢了一步」。前端据此显示「它已经结束了，这一步没赶上」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from yechao.domain.errors import YechaoError

RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"


@dataclass
class BatchResult:
    ok: int = 0
    failed: int = 0
    #: 失败的原因（去重后取前几条）。
    reasons: list[str] = field(default_factory=list)


def run_batches(
    items: list[Any],
    *,
    jobs: Any,
    job_id: str,
    run_one: Callable[[int, Any], None],
    cancelled: Callable[[], bool],
    on_progress: Callable[[int, int], None] | None = None,
) -> BatchResult:
    """逐批跑。

    `run_one(index, item)` 抛异常 ⇒ 这一批算失败，**但不中断**——
    记一笔原因，继续下一批。理由：一个批次的失败（超时、输出不合法）
    不应该让其余批次全部作废。

    ⚠️ **但失败必须记进 `reasons`**，而调用方必须把它带进 `message`
    —— 用户看到的不能只是「跑了 7 批」，而要是「7 批成功、1 批失败，
    原因是 HTTP 503」。
    """
    total = len(items)
    result = BatchResult()
    jobs.update(job_id, total=total)

    for i, item in enumerate(items):
        if cancelled():
            # ⚠️ 取消时**不更新 done** —— 已完成的进度保留，未完成的不假装。
            jobs.update(job_id, state=CANCELLED)
            return result
        try:
            run_one(i, item)
        except YechaoError as e:
            result.failed += 1
            result.reasons.append(f"第 {i + 1} 批：{e}")
            continue
        except Exception as e:  # noqa: BLE001
            # ⚠️ 兜住**所有**异常。一个未预期的异常不该让整个作业崩掉，
            # 而它**必须**被记下来（否则就是「静默跳过」）。
            result.failed += 1
            result.reasons.append(f"第 {i + 1} 批：{type(e).__name__}: {e}")
            continue

        result.ok += 1
        # ⚠️ `done` = **最后一条成功批次的序号**（1-based）。
        # 不在这里 +1 之后再 +1，也不要对失败的批更新它。
        jobs.update(job_id, done=i + 1)
        if on_progress is not None:
            on_progress(i + 1, total)

    state = DONE if result.failed == 0 else FAILED
    jobs.update(job_id, state=state, message=_message(result))
    return result


def _message(r: BatchResult) -> str:
    """进度文案。

    ⚠️ 措辞是**给人看的**，要能一眼分清「全成了」与「有没成的」。
    """
    parts: list[str] = []
    if r.ok:
        parts.append(f"{r.ok} 批完成")
    if r.failed:
        parts.append(f"{r.failed} 批失败")
    if r.reasons:
        head = "；".join(r.reasons[:3])
        more = f"（另有 {len(r.reasons) - 3} 条）" if len(r.reasons) > 3 else ""
        parts.append(f"原因：{head}{more}")
    return "，".join(parts) if parts else "没有可处理的内容"
