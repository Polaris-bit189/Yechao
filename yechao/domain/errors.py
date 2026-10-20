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

    ⚠️ **绝不静默用默认值兜底**。用户改了 `config/groups.toml`，写错了键名，
    而程序默默用了默认 —— 用户会以为设置生效了，直到某天发现群根本没被监视。
    """

    default_message = "配置有问题"


class ValidationError(YechaoError):
    """外部输入不合法（HTTP 参数、CLI 参数）。

    由入口层翻译成 4xx。⚠️ **4xx 不是「服务不可达」** —— 前端的连通性
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

    ⚠️ **它与「这个源本来就没消息」是两件事**，必须形状不同。
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

    ⚠️ 这是数据源层最阴的一种失败：产出为空，而空是所有错误里最不容易被
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
    """⚠️ 输出被**长度上限截断**了。

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
    """⚠️ 有人试图用只读连接写主库。

    这条不是"防御性编程" —— 它是**不变量 ①**「网页侧不得写入主库」的
    执行点。取向是**让数据库本身拒绝**，而不是靠代码自觉：靠自觉的约束
    迟早被静默绕开，而绕开时没有声音。
    """

    default_message = "主库是只读打开的，写操作被拒绝"


# ── 占位 ────────────────────────────────────────────────────────────


class NotWired(YechaoError):
    """功能没接线。

    ⚠️ **占位必须显眼**：没实现的功能显式报错，不静默返回假数据。
    演示时那会被当成造假。

    入口层把它翻译成 **501** —— 501 与 500 的区别是「还没做」与
    「做了但出错」，这两件事对排查者意味着完全不同的方向。
    """

    default_message = "这个功能还没接上"


# ── 批量作业 ────────────────────────────────────────────────────────


class JobCancelled(YechaoError):
    """⚠️ 取消是**协作式**的。

    Python 没法从外面杀线程，所以「已请求取消」只在**批与批之间**生效：
    正在飞的那一批会跑完，它停在下一批。这个错误就是那一批结束时抛出来的
    —— 它带着"停在哪"的信息，因为**部分完成的结果要保留**（取消不该把
    已经落库的东西回滚掉）。
    """

    default_message = "作业已取消"


# ── 失败记账 ────────────────────────────────────────────────────────


class FailureLedger:
    """⚠️ 静默跳过的消息 = 用户以为那天没事。

    所以「跳过了几条、为什么」必须**有地方记**，而且要能被读出来展示、
    被断言。这个薄容器就是那个地方。

    它故意**不是**一个日志：日志可以不被读，而这里的每一条都会进最终
    报告（覆盖率的分母就是靠它算的 —— 见  1）。
    """

    def __init__(self) -> None:
        self._entries: list[tuple[str, str]] = []

    def record(self, kind: str, detail: str = "") -> None:
        self._entries.append((kind, detail))

    @property
    def entries(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._entries)

    def count(self, kind: str) -> int:
        return sum(1 for k, _ in self._entries if k == kind)

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)

    def summary(self) -> dict[str, int]:
        """按种类计数。给报告用。"""
        out: dict[str, int] = {}
        for kind, _ in self._entries:
            out[kind] = out.get(kind, 0) + 1
        return out
