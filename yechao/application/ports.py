"""端口 —— 用例层**需要什么**。

## 端口与实现不是「同一个东西的两半」

这一点说得最清楚：

> 端口是「**我需要什么**」，实现是「**外部世界能给我什么**」。

所以本文件的每个 `Protocol` 都是从用例的视角写的：它列出用例**真的会调用**
的方法，不多一个。一个「将来可能有用」的方法不该出现在这里 —— 它会让每个
适配器都必须实现一个没人调用的东西。

## ⚠️ 返回类型为什么是 `Any`

**一个容易踩的坑**：端口里的返回类型写具体的数据类，
就意味着这一层要 import 适配层 —— 那是反向依赖。

**类型精度让位于依赖方向。** 这是刻意的取舍，不是疏忽。

代价：IDE 补全在用例层对返回值不起作用。收益：换存储不动用例，
而且分层护栏能机械地守住这条。

## 为什么用 `Protocol` 而不是抽象基类

`Protocol` 是**结构化**的：适配层不需要 `class SqliteStore(ItemRepository)`
—— 它只要长得像就行。于是适配层**不 import 用例层**，依赖方向天然单向。

用 ABC 的话，适配层得 import 基类才能继承它 —— 那在 Python 里没问题
（同向），但它把「这个类属于哪一层」搅浑了：一个 `SqliteItemStore` 既在
适配层又继承着用例层的类，读代码时说不清它算谁。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


# ── 时钟 ────────────────────────────────────────────────────────────


@runtime_checkable
class Clock(Protocol):
    """取当前时刻。**这是用例层唯一能拿到「现在」的地方。**

    ⚠️ 领域层不许取当前时间（护栏钉这条）。理由：`now()` 让同一段代码在不同
    时刻行为不同 —— 那正是「测试只能测我这一刻跑的那次」的形状。
    """

    def now(self) -> int:
        """当前 Unix 秒。"""
        ...


# ── 仓储 ────────────────────────────────────────────────────────────


@runtime_checkable
class MessageRepository(Protocol):
    """原始消息的存取。"""

    def upsert_messages(self, messages: list[Any]) -> int:
        """写入消息。**已存在的按 `msg_id` 跳过**（幂等）。返回新写入条数。"""
        ...

    def pending_messages(
        self, *, since: int | None = None, until: int | None = None
    ) -> list[Any]:
        """取待抽取的消息。

        ⚠️ **必须跳过已经被软删的**。
        否则用户删掉的条目会在下一轮被重新抽出来。
        """
        ...

    def message_by_id(self, msg_id: int) -> Any | None: ...


@runtime_checkable
class ItemRepository(Protocol):
    """结构化条目的存取。"""

    def insert_items(self, items: list[Any], sources: list[Any]) -> list[int]:
        """**条目 + 来源 + 记账必须在同一个事务里。**

        ⚠️ 分开提交会在崩溃时留下「写了条目、没记账」，下一轮重抽产出重复
        条目，而且无声。

        返回新条目的 id（按输入顺序）。
        """
        ...

    def window_items(self, *, since: int, until: int) -> list[Any]: ...

    def items_with_deadline(self, *, since: int | None = None, until: int | None = None) -> list[Any]:
        """带截止日的条目。日历用。**不分页**。"""
        ...

    def search(self, **filters: Any) -> tuple[list[Any], int]:
        """查条目。返回 `(本页, 总数)`。

        ⚠️ **总数必须有**：前端的翻页按钮依赖它（检索分页要返回总数）。
        """
        ...

    def item_detail(self, item_id: int) -> Any | None: ...

    def set_kind(self, item_id: int, kind: str, *, actor: str) -> int:
        """人工改类目。**返回一个 edit_id**（撤销的键）。"""
        ...

    def soft_delete(self, item_id: int, *, actor: str) -> int: ...

    def counts_by_kind(self) -> dict[str, int]: ...

    def counts_by_person(self) -> dict[int, int]: ...

    def deleted_item_ids(self) -> set[int]:
        """被软删的条目 id。

        ⚠️ 抽取消重时要读它 —— 「删除是软的」意味着「不会在下一轮被抽出来」
        **不自动成立**：抽取管线必须**主动**跳过这些来源消息。
        """
        ...


@runtime_checkable
class EditRepository(Protocol):
    """人工干预的**事件追加**（append-only）。

    ⚠️ 为什么是事件而不是直接改：**撤销需要「原来是什么」**。
    直接改掉之后，撤销就无从下手。
    """

    def append(self, kind: str, payload: dict[str, Any], *, actor: str) -> int:
        """追加一条事件。返回 `edit_id`。"""
        ...

    def last_editable(self) -> Any | None: ...

    def undo(self, edit_id: int | None) -> bool: ...

    def bulk_undo(self, actor: str) -> tuple[bool, int]: ...


@runtime_checkable
class DigestRepository(Protocol):
    def insert_digest(self, digest: Any, item_ids: list[int]) -> int: ...

    def list_digests(self, *, limit: int = 50) -> list[Any]: ...

    def digest_detail(self, digest_id: int) -> Any | None: ...


@runtime_checkable
class JobRepository(Protocol):
    """批量作业的进度与结果。

    ⚠️ `done` 的语义是「**最后一条成功批次**的序号」，**不是**「已完成批次数」
    （前端注释逐字：「照实显示，**别自己加一**——那会变成另一句假话：
    第 1 批就抛异常时用户会看到 2/N，像『全成了』」）。
    """

    def create(self, kind: str, total: int, *, actor: str = "") -> str: ...

    def update(self, job_id: str, **fields: Any) -> None: ...

    def get(self, job_id: str) -> Any | None: ...

    def busy(self) -> bool: ...

    def request_cancel(self, job_id: str) -> tuple[bool, str]: ...


# ── 数据源 ──────────────────────────────────────────────────────────


@runtime_checkable
class MessageSource(Protocol):
    """一个数据源。**形状由接口约束，处理方式不由接口约束。**

    要害在这里：会议转写是「长音视频」必须切，邮件是
    「自洽的短文本」切了反而把它拆散。⇒ **归一化的责任在数据源**，
    不在接口。如果两个源为了迎合同一个下游而做同样的处理，其中一个必然是错的。
    """

    #: 源的标识（`qq` / `meeting` / `mail` / `synth`）。
    name: str

    def read(self, *, since: int | None = None, until: int | None = None) -> list[Any]:
        """读出消息。

        ⚠️ **读不到就抛，不许返回空**。

        「这个群没消息」与「这个群读不到」形状相同是问题的根源 —— 下游看到
        两个空，会给出同一份日报，并用「今天很安静」的口气叙述它。

        ⚠️ 也**解析出 0 条就抛**（`SourceFormatError`）：选择器失效、站点改版、
        编码认错，表现都是「这个源今天没更新」。而**产出为空是所有错误里
        最不容易被注意到的一种**。
        """
        ...


# ── 模型 ────────────────────────────────────────────────────────────


@runtime_checkable
class ModelClient(Protocol):
    """本地模型客户端（OpenAI 兼容）。

    ⚠️ 契约要求实现方自己处理三件事：
      1. 输出可能被 **Markdown 代码块**包住（而且不是每次都包）
      2. 输出可能被**长度截断** —— 这时要抛 `ModelTruncated`，**不是**
         `ModelOutputInvalid`。两者在 HTTP 层看起来一样，但处置完全不同：
         前者要改切批的预算，后者要改提示词或解析。
      3. 「关思考」参数**不同端点认不同的名字** —— 所以 `enable_thinking`
         是三态（`None` = 不发这个字段）。
    """

    def complete(self, *, system: str, user: str, max_tokens: int = 2048) -> str: ...

    @property
    def enable_thinking(self) -> bool | None: ...


# ── 出网审计 ────────────────────────────────────────────────────────


@runtime_checkable
class EgressAudit(Protocol):
    """出网审计归档 —— 「可证伪的隐私」的落点。

    ⚠️ **只存计数与指纹，不存内容。** 存内容就是新造一个泄漏面，而它恰好
    会存下最该保护的那些文本。
    """

    def record(self, entry: Any) -> None: ...

    def read_all(self) -> list[Any]: ...


# ── 配置 ────────────────────────────────────────────────────────────


@runtime_checkable
class ConfigStore(Protocol):
    """配置读写。

    ⚠️ 键名是**对外契约**：`config/groups.toml` 的 `id` / `name` / `tier`。
    """

    def groups(self) -> list[Any]: ...

    def set_groups(self, groups: list[Any]) -> None: ...

    def persons(self) -> list[Any]: ...

    def set_persons(self, persons: list[Any]) -> None: ...

    def categories(self) -> Any: ...

    def set_categories(self, data: Any) -> None: ...

    def llm(self) -> Any: ...

    def set_llm(self, settings: Any) -> None: ...

    def nicknames(self) -> list[str]:
        """已知的成员昵称 —— 姓名脱敏的**名单来源**。"""
        ...
