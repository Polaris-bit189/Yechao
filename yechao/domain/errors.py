"""错误体系：一个根类 + 几个子类。跨层只抛这些。

## 要防什么

**一次失败如果看起来像成功，它比失败本身更危险。**

具体到本项目的三种形态：

| 看起来像 | 真相 |
|---|---|
| 静默用了默认配置 | 用户以为设置生效了 |
| 静默跳过一条消息 | 用户以为那天没事 |
| 静默返回 0 | 脚本里的 `&&` 继续往下跑 |

所以这里的设计取向是：**错误类型本身要携带「该往哪查」的信息**，而不是
只有一个消息串。调用方据此决定：重试有没有意义（`retryable`）、用户该看
哪一栏（`field`）、这次失败算不算「跑完了」（`fatal`）。

## 为什么用异常而不是返回 `None` / 空集合

空集合与「真的空」形状相同。一个群读不到返回空 ⇒ 下游给出「今天很安静」
口吻的日报 —— 而真相是没读到。形状相同是问题的根源，所以出错就抛。

## 为什么不用 `dataclass` 的 `frozen`

异常对象会被 `raise ... from ...` 串起来、被 `add_note` 挂上下文，
冻结会让这些手法别扭。**值对象**才冻结，错误不是值对象。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class YechaoError(Exception):
    """所有本项目错误的根类。

    跨层只抛这一个族里的东西 —— 于是「这一层可能抛什么」是一个**闭集**，
    可以被穷举。裸 `ValueError` / `KeyError` 漏到边界上，入口层就没法给它
    配一个像样的状态码与文案。
    """

    #: 给用户看的默认文案。子类覆盖它。
    default_message = "夜抄内部错误"

    def __init__(self, message: str | None = None, **context: Any) -> None:
        self.context: dict[str, Any] = context
        super().__init__(message or self.default_message)

    def __str__(self) -> str:
        base = super().__str__()
        if not self.context:
            return base
        detail = "、".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"{base}（{detail}）"


# ── 配置与输入 ──────────────────────────────────────────────────────


class ConfigError(YechaoError):
    """配置读不出来 / 不合法。

    ※ **绝不静默用默认值兜底**。用户改了 `config/groups.toml`，写错了键名，
    而程序默默用了默认 —— 用户会以为设置生效了，直到某天发现群根本没被监视。
    """

    default_message = "配置有问题"


class ValidationError(YechaoError):
    """外部输入不合法（HTTP 参数、CLI 参数）。

    由入口层翻译成 4xx。※ **4xx 不是「服务不可达」** —— 前端的连通性
    状态机据此保持在线。
    """

    default_message = "输入不合法"

    def __init__(self, message: str | None = None, *, field: str = "", **context: Any):
        super().__init__(message, **context)
        self.field = field


class NotFound(YechaoError):
    """要的东西不存在。入口层翻译成 404。"""

    default_message = "找不到"


# ── 数据源 ──────────────────────────────────────────────────────────


class SourceError(YechaoError):
    """数据源读不到。

    ※ **它与「这个源本来就没消息」是两件事**，必须形状不同。
    「读不到」抛这个；「本来就没有」返回空。
    下游看到两个空会给出同一份日报，并用「今天很安静」的口气叙述它。
    """

    default_message = "数据源读不到"

    #: 一个源读不到时，其余源还应不应该继续？
    #: 取 `False` = 这一条失败不阻断其余源 —— 但**必须记账**，由调用方决定
    #: 退出码。
    fatal = False


class SourceFormatError(SourceError):
    """源读到了，但**解析出 0 条**。

    ※ 这是数据源层最阴的一种失败：产出为空，而空是所有错误里最不容易被
    注意到的一种。选择器失效、站点改版、编码认错，表现都是「这个源今天没
    更新」。

    与 `SourceUnavailable`（读不到）分开，是因为两者的下一步不同：
    前者要改解析器，后者要查连通性。
    """

    default_message = "源解析出 0 条（格式不认识？）"
    fatal = True


class SourceUnavailable(SourceError):
    """源根本读不到（文件不在、网络不通、认证过期）。"""

    default_message = "数据源不可用"


# ── 模型 ────────────────────────────────────────────────────────────


class ModelError(YechaoError):
    """调用模型失败。"""

    default_message = "模型调用失败"

    #: 同样参数重试有没有意义。网络抖动 / 5xx 有意义；输出不合法没有 ——
    #: 同一段输入大概率还是那个形状。
    retryable = False


class ModelTruncated(ModelError):
    """※ 输出被**长度上限截断**了。

    它与「模型输出不合法」在 HTTP 层看起来一样吗？——**不一样**：
    截断时 `finish_reason` 是 `length`。所以不解析内容就能先把两者分开。

    分开的价值在处置：截断说明**切批的预算算错了**（要改的是切批），
    而不合法说明提示词或解析要改。混成一个「解析失败」会让你在错的地方找。
    """

    default_message = "模型输出被长度截断（该调切批的预算）"
    retryable = False


class ModelOutputInvalid(ModelError):
    """模型返回了，但内容不合法（不是 JSON / 缺字段 / 摘录对不上源文）。"""

    default_message = "模型输出不合法"


# ── 存储 ────────────────────────────────────────────────────────────


class StorageError(YechaoError):
    """存储层失败。"""

    default_message = "存储失败"


class ReadOnlyViolation(StorageError):
    """※ 有人试图用只读连接写主库。

    这条不是"防御性编程" —— 它是**不变量 ①**「网页侧不得写入主库」的
    执行点。取向是**让数据库本身拒绝**，而不是靠代码自觉：靠自觉的约束
    迟早被静默绕开，而绕开时没有声音。
    """

    default_message = "主库是只读打开的，写操作被拒绝"


# ── 占位 ────────────────────────────────────────────────────────────


class NotWired(YechaoError):
    """功能没接线。

    ※ **占位必须显眼**：没实现的功能显式报错，不静默返回假数据。
    演示时那会被当成造假。

    入口层把它翻译成 **501** —— 501 与 500 的区别是「还没做」与
    「做了但出错」，这两件事对排查者意味着完全不同的方向。
    """

    default_message = "这个功能还没接上"


# ── 批量作业 ────────────────────────────────────────────────────────


class JobCancelled(YechaoError):
    """※ 取消是**协作式**的。

    Python 杀不掉别的线程，所以「已请求取消」只在**批与批之间**生效：
    手上那批会照常跑完，新的批次不再开。这个错误就是那一批结束时抛出来的
    —— 它带着"停在哪"的信息，因为**部分完成的结果要保留**（取消不该把
    已经落库的东西回滚掉）。
    """

    default_message = "作业已取消"


# ── 失败记账 ────────────────────────────────────────────────────────
#
# ## 账的单位是什么（这一段决定了下面所有形状）
#
# 三层问题，两层表：
#
#   一次跑   → 一个**抬头**（`RunRecord`）：跑了什么、什么时候、结果如何
#   一件事   → 一笔**账**（`LedgerEntry`）：这一条消息 / 这一批 / 这一行
#              为什么没成
#
# ※ **为什么不是「一条消息一行」的单层表**：那样「这次跑了什么、什么时候」
# 就要在每一行里重复一遍，而且「一次跑」这个概念没有落点 —— 覆盖率、批数、
# 开始结束时刻这些**属于整轮**的量无处安放。反过来，只记抬头也不成立：
# 「为什么是**这一条**」根本答不出来，而那正是产品承诺（每一条消息都能回答
# 为什么）里的那一条。
#
# ※ **为什么不复用 `jobs` 表**（它看起来也像「一次跑」）：
#   1. `jobs.done` 的语义是**最后一批成功的批号**，前端在轮询它画进度条；
#      refine / ingest / digest 没有「批号」这个概念，塞进去只能填 0，
#      而那个 0 会被读成「一批都没成」。
#   2. `jobs` 上的 `any_job_running()` 是**重排的并发闸**。让一次失败的
#      `refine` 在里面留一行 `running`，症状是「重排按钮点了没反应」——
#      两处代码单看都正常。
#   3. `jobs` 没有「这一条消息为什么」的位置，而那是账的主要用途。
#
# ## ※ 账里**不存原文**
#
# 与 `audit_log.py` 同一条红线：账里只放**计数、标识与原因码**，不放正文。
# 这条不是靠自觉 —— `reason` 有长度上限（`REASON_MAX_CHARS`），两边的
# 数据库都拒绝超长的 reason（SQLite 用 `CHECK`，MySQL 用 `VARCHAR(64)`）。
# 往 `reason` 里塞正文会**报错**，而不是把正文写进磁盘。

#: 一笔账的 `reason` 的长度上限。
#:
#: ※ 定 64 的理由：真正要放进来的东西都很短 —— 判据名（`pure_ack`）、
#: 异常类型名（`ModelOutputInvalid`）、字段名（`deadline_ts,start_ts`）、
#: 标识（`meeting:SourceUnavailable`）。而**任何一段正文**都远超 64。
#: 所以这个上限的作用不是「截断得好看」，而是让「塞正文」这件事**报错**。
REASON_MAX_CHARS = 64

#: 一次跑的结局。**闭集，二值**。
#:
#: ※ 为什么不做成 `ok / partial / failed` 三值：多一档就要多一条判据去定
#: 「多少算 partial」，而那条判据在每个命令上都不一样（预筛丢垃圾算不算
#: 部分失败？日报用机械行兜底算不算？）。二值 + `produced` + 账里的条数，
#: 三个**各自独立**的数合起来已经分得清「全成 / 部分成 / 全没成」——
#: 而它们比一个混起来的枚举更难撒谎。
RUN_OK = "ok"
RUN_FAILED = "failed"

#: 一次跑的 kind。**闭集**：与 `interfaces/cli.py` 的命令名逐字一致。
RUN_REFINE = "refine"
RUN_INGEST = "ingest"
RUN_DIGEST = "digest"


class LedgerEntryTooLong(YechaoError):
    """一笔账的 `reason` 太长了。

    ※ 它几乎总是同一个原因：**有人把正文塞进了 `reason`**。账里不存原文
    是红线，所以这里**报错**，而不是截断 —— 截断会把「账里混进了正文」
    变成一条看不见的失败，而那正是本项目最怕的形状。
    """

    default_message = "账里的 reason 太长了（账只存计数、标识与原因码，不存正文）"


@dataclass(frozen=True)
class LedgerEntry:
    """**一笔账** —— 「这一件事为什么没成」。

    ※ 主体字段（`msg_id` / `group_id` / `batch_no` / `item_id`）是**可选**的，
    因为它们不是同一类主体：一条消息、一批、一条日报行。哪一笔填哪几个，
    由 `kind` 决定（见 `application/extract.py` 顶部那张表）。

    ※ 为什么主体用**列**而不是一个 `subject: str`：主体的用途是**被查**
    （「这条消息为什么没进条目」），而字符串主体既查不快、也拼得错。
    """

    kind: str
    msg_id: int | None = None
    group_id: int | None = None
    batch_no: int | None = None
    item_id: int | None = None
    #: **短码**，不是散文：判据名 / 异常类型名 / 字段名 / 标识。
    #: 多个用 `:` 拼（如 `meeting:SourceUnavailable`）。
    reason: str = ""


@dataclass(frozen=True)
class RunRecord:
    """**一次跑的抬头** —— 跑了什么、什么时候、结果如何。

    ※ 四个计数列里有两列（`model_eligible` / `model_sent`）是**模型那一侧
    才填**的：覆盖率的两半（refine 一定填；digest 只在真的走了模型那条路时填）。
    ingest 填 0，而那个 0 的含义是「这一轮没有模型那一侧的事」——
    所以列名带着 `model_` 前缀。叫 `eligible` / `sent` 的话，`0` 会被读成
    「一条都不够格」，那是另一句话。

    ※ `entries` 挂在抬头里，而不是让调用方分两次写：**账与它解释的那次
    写入必须同一个事务**（见 `ports.ItemRepository.insert_items`）。
    """

    run_id: str
    kind: str
    started_at: int
    finished_at: int
    status: str = RUN_OK
    #: 这一轮**看到**多少条输入（refine: 待处理消息数 / ingest: 从源读到的
    #: 消息数 / digest: 窗口内的条目数）。
    seen: int = 0
    #: 这一轮**产出**多少（refine: 条目数 / ingest: **交给库**的消息条数 /
    #: digest: 日报行数）。
    #:
    #: ※ ingest 那一格不是「其中新写了几条」：后者只有 `upsert_messages`
    #: 知道（它才看得见重复键），而账与消息在同一个事务里，提交之前拿不到
    #: 那个数。取舍是记一个**写入前拿得到**的数，而不是编一个 ——
    #: 「新写了几条」CLI 会打印，库里也有（`messages` 的行数）。
    produced: int = 0
    model_eligible: int = 0
    model_sent: int = 0
    entries: tuple[LedgerEntry, ...] = field(default_factory=tuple)


class FailureLedger:
    """※ 静默跳过的消息 = 用户以为那天没事。

    所以「跳过了几条、为什么」必须**有地方记**，而且要能被读出来展示、
    被断言。这个薄容器就是那个地方。

    它故意**不是**一个日志：日志可以不被读，而这里的每一条都会进最终
    报告，并且会**落库**（`runs` / `run_entries` 两张表）—— 进程退出之后
    它还在，这才是「每一条消息都能回答为什么」这句话的兑现方式。
    """

    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []

    def record(
        self,
        kind: str,
        *,
        msg_id: int | None = None,
        group_id: int | None = None,
        batch_no: int | None = None,
        item_id: int | None = None,
        reason: str = "",
    ) -> None:
        """记一笔。

        ※ `reason` 是**短码**（见 `LedgerEntry`），不是散文 —— 超过
        `REASON_MAX_CHARS` 时这里就报错，而不是等着数据库拒绝：
        错误信息要说清「你塞的是正文」，而不是 SQLite 那句 CHECK 失败。
        """
        if len(reason) > REASON_MAX_CHARS:
            raise LedgerEntryTooLong(
                f"kind={kind} 的 reason 有 {len(reason)} 字"
                f"（上限 {REASON_MAX_CHARS}）"
            )
        self._entries.append(
            LedgerEntry(
                kind=kind, msg_id=msg_id, group_id=group_id,
                batch_no=batch_no, item_id=item_id, reason=reason,
            )
        )

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries)

    def count(self, kind: str) -> int:
        return sum(1 for e in self._entries if e.kind == kind)

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)

    def summary(self) -> dict[str, int]:
        """按种类计数。给报告用。"""
        out: dict[str, int] = {}
        for e in self._entries:
            out[e.kind] = out.get(e.kind, 0) + 1
        return out
