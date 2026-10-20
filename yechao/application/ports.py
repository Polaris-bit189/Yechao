"""端口 —— 用例层**需要什么**。

## 端口与实现不是「同一个东西的两半」

这一点说得最清楚：

> 端口是「**我需要什么**」，实现是「**外部世界能给我什么**」。

所以本文件的每个 `Protocol` 都是从用例的视角写的：它列出用例**真的会调用**
的方法，不多一个。一个「将来可能有用」的方法不该出现在这里 —— 它会让每个
适配器都必须实现一个没人调用的东西。

## ※ 仓储端口还覆盖了**入口层的调用**（这一条是补上去的）

「端口 = 用例需要什么」这句话有一个**机械的后果**：入口层（`interfaces/api.py`）
拿到的也是同一个 `App.store`，它也调仓储的方法（`sources_of` / `bulk_set_kind`
/ `get_kv`…）。如果那些方法不在端口里，那么：

· 一个**只按端口写**的适配器（比如 MySQL 那个）会在某个 GET 上
  `AttributeError` —— 而那一刻是**运行时**，不是导入时；
· 更糟的是它**只在换后端时才出现**：本地跑 SQLite 一切正常。

⇒ 判据是**调用点**，不是「这一层叫什么」。凡是应用真的会调到的仓储方法，
都在这里。所以「不多一个」这条纪律仍然成立，只是「应用」的范围包含
入口层 —— 它们跑在同一个进程里，用的是同一个对象。

## ※ 返回类型为什么是 `Any`

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

from typing import Any, Iterable, Protocol, runtime_checkable


# ── 时钟 ────────────────────────────────────────────────────────────


@runtime_checkable
class Clock(Protocol):
    """取当前时刻。**这是用例层唯一能拿到「现在」的地方。**

    ※ 领域层不许取当前时间（护栏钉这条）。理由：`now()` 让同一段代码在不同
    时刻行为不同 —— 那正是「测试只能测我这一刻跑的那次」的形状。
    """

    def now(self) -> int:
        """当前 Unix 秒。"""
        ...


# ── 仓储 ────────────────────────────────────────────────────────────


@runtime_checkable
class MessageRepository(Protocol):
    """原始消息的存取。"""

    def upsert_messages(self, messages: list[Any], *, run: Any = None) -> int:
        """写入消息。**已存在的按 `msg_id` 跳过**（幂等）。返回新写入条数。

        ※ `run` 是一次跑的抬头（`domain.errors.RunRecord`），带上它时
        **这一轮的账与消息在同一个事务里** —— 见 `ItemRepository.insert_items`
        的注释（同一条判据，三个写入方法各落一次）。
        """
        ...

    def pending_messages(
        self, *, since: int | None = None, until: int | None = None
    ) -> list[Any]:
        """取窗口内的消息（按 `ts` 过滤）。

        ※ **去重不在这里**：哪些消息已经处理过，由 `covered_msg_ids()`
        回答 —— 它把**被软删条目的来源**也算作已处理，所以删掉的条目
        不会被下一轮抽回来。两个方法各答一件事，调用方两样都要问。
        """
        ...

    def message_by_id(self, msg_id: int) -> Any | None: ...

    def message_count(self) -> int:
        """库里一共多少条消息。

        ※ 报告里报的是**这一次真的读到的**那一批，而这个数报的是**库里
        现在有多少** —— 两个数不一样（前者含本次新写的）。CLI 两个都报，
        因为「写入 3 条，共 12 条」与「写入 3 条，共 3 条」讲的是不同的事。
        """
        ...


@runtime_checkable
class ItemRepository(Protocol):
    """结构化条目的存取。"""

    def insert_items(
        self, items: list[Any], sources: list[Any], *, run: Any = None
    ) -> list[int]:
        """**条目、来源、记账三件事要落在同一个事务里。**

        ※ 分开提交会在崩溃时留下「写了条目、没记账」，下一轮重抽产出重复
        条目，而且无声。

        ※ `run` 是一次跑的抬头（`domain.errors.RunRecord`），它的 `entries`
        就是这一轮的账。带上它时，**抬头 + 账 + 条目 + 来源四样一起提交**。

        ※ **为什么写账的口子开在数据写入方法上、而不是另开一个
        `record_run`**：因为「同一个事务」这件事没法在用例层表达 ——
        用例层不碰连接（`sqlite_store.transaction` 是适配层的私有工具），
        它只能通过**一次调用**来表达「这些必须一起成」。所以「账」是
        那次调用的一部分。反过来说：如果账能单独写，那它就不再与条目
        同生共死了，而那正是这条判据要防的。

        ※ `items` 允许为空 —— 「这一轮一条都没产出」也是一个结果，
        而它恰恰是最需要留下证据的那种（用户会问「我跑了 refine，
        怎么什么都没有」）。调用方因此**总是**调这个方法一次。

        返回新条目的 id（按输入顺序）。
        """
        ...

    def window_items(self, *, since: int, until: int) -> list[Any]: ...

    def items_with_deadline(self, *, since: int | None = None, until: int | None = None) -> list[Any]:
        """带截止日的条目。日历用。**不分页**。"""
        ...

    def search(self, **filters: Any) -> tuple[list[Any], int]:
        """查条目。返回 `(本页, 总数)`。

        ※ **总数必须有**：前端的翻页按钮依赖它（检索分页要返回总数）。
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

        ※ 抽取消重时要读它 —— 「删除是软的」意味着「不会在下一轮被抽出来」
        **不自动成立**：抽取管线必须**主动**跳过这些来源消息。
        """
        ...

    def covered_msg_ids(self) -> set[int]:
        """已经被引用过的消息 id —— **含被软删的条目**。

        ※ 这个方法是**补进端口**的，理由值得留：`extract.run` 一直在调
        `deps.items.covered_msg_ids()`，而端口里**没有它** —— 也就是说
        一个只按端口写的适配器会在 `refine` 跑到一半时 `AttributeError`。

        为什么会漏：端口的初稿是从「仓储看起来该有什么」写的，不是从
        「调用方实际调了什么」抄的。判据是**调用点**：`extract.py` 里那
        一行就是契约，端口必须逐字包含它。

        为什么在 `ItemRepository` 而不是 `MessageRepository`：它问的是
        「哪些消息**已经产出过条目**」—— 主语是条目，数据来源是
        `item_sources`。调用点写的也是 `deps.items`。
        """
        ...

    def judged_msg_ids(self, *, kinds: Iterable[str]) -> set[int]:
        """账里已经**判完**的消息 id。

        `kinds` 是「哪些结局算判完」—— **由调用方给**，不在这儿写死：
        那个集合是**产品判断**（一次网络抖动算不算判完？），而它只有一处
        定义（`application/extract.py::JUDGED_KINDS`）。写死在这里就是两处，
        漂移的症状是「某类消息每一轮都被重判一遍」，而它不报错。

        ※ 为什么要有这个方法：`covered_msg_ids()` 只认「产出过条目」的消息。
        被预筛刷掉的、模型看过却没抽出东西的，永远不算处理过 ⇒ 每一轮都把
        全库重读重筛。实测（本机 QQ 库 49.2 万条）一轮 150 秒，其中 114 秒
        花在重新预筛那 49 万条上，而第二轮只新写了 21 条。
        """
        ...

    def sources_of(self, item_id: int) -> list[Any]:
        """这条条目挂着的来源消息（含摘录）。

        ※ 返回的是**字典**而不是领域对象：它是「原始消息 + 摘录」的
        拼接视图，没有对应的领域值对象，而给它现造一个只会多一层。
        """
        ...

    def all_items(self, *, include_deleted: bool = False) -> list[Any]:
        """全量条目。`metrics` 与 `reorder` 用。**不分页**。"""
        ...

    def item_count(self) -> int:
        """可见条目数（不含软删）。"""
        ...

    def bulk_set_kind(
        self, item_ids: list[int], kind: str, *, actor: str = "", ts: int = 0
    ) -> tuple[str, int]:
        """批量改类目。返回 `(actor, 改了几条)`。

        ※ `actor` 是**撤销的凭据** —— 一次提交只写一个 actor，
        服务端靠它把这一批认成同一批（见 `EditRepository.bulk_undo`）。
        """
        ...


@runtime_checkable
class EditRepository(Protocol):
    """人工干预的**事件追加**（append-only）。

    ※ 为什么是事件而不是直接改：**撤销需要「原来是什么」**。
    直接改掉之后，撤销就无从下手。
    """

    def append_edit(self, kind: str, payload: dict[str, Any], *, actor: str) -> int:
        """追加一条事件。返回 `edit_id`。

        ※ 名字与实现（`SqliteStore.append_edit`）**逐字一致**。原来这里叫
        `append`，而实现叫 `append_edit` —— 端口与实现对不上名，调用方
        写哪个都是错的：写 `append` 是 AttributeError，写 `append_edit`
        又不在契约里（IDE 不给补全、静态检查也说没有）。

        为什么改端口而不是改实现：`append` 太泛，与 `list.append` 同一个
        形状 —— 一个仓储上有个裸的 `append`，读代码的人第一反应是「往列表
        里加东西」。`append_edit` 把「追加的是一条人工干预事件」说清楚了。
        两边只留一个名字，不再留同义方法（同义方法正是这类错位的来源）。
        """
        ...

    def last_editable(self) -> Any | None: ...

    def undo(self, edit_id: int | None) -> bool: ...

    def bulk_undo(self, actor: str) -> tuple[bool, int]: ...


@runtime_checkable
class DigestRepository(Protocol):
    def insert_digest(
        self, digest: Any, item_ids: list[int], *, run: Any = None
    ) -> int:
        """写日报。`run` 的语义与 `insert_items` 逐字相同（同事务）。"""
        ...

    def list_digests(self, *, limit: int = 50) -> list[Any]: ...

    def digest_detail(self, digest_id: int) -> Any | None: ...


@runtime_checkable
class JobRepository(Protocol):
    """批量作业的进度与结果。

    ※ `done` 记的是**最后一批跑成功的批号**，**不是**已经成功了几批。

    显示时用原值、不要 +1 —— 加一就成了另一种谎报：第 1 批就抛异常时
    进度会显示成 2/N，看着像全做完了。

    ※ **方法名与实现逐字一致**（`create_job` / `update` / `get_job` /
    `any_job_running` / `request_cancel`）。原来这里写的是 `create` /
    `update` / `get` / `busy` —— 名字对不上实现，于是 `reorder.run` 一跑就
    `AttributeError: 'SqliteStore' object has no attribute 'busy'`，
    **整个重排模块没有一个测试**，所以这个错位一直没被发现。

    判据（为什么是「端口改、实现不改」）：
      · `any_job_running` / `create_job` / `get_job` 这三个名字，接口层
        （`interfaces/api.py`）已经在用，一共 6 处调用。改实现的名字要动
        接口层，而端口只有一个用例（`reorder`）在读 —— **改代价小的那一边**。
      · `any_job_running` 也比 `busy` 更明确：「忙」是什么忙？作业在跑才是
        它要问的。
      · `update` 保留**裸名**：`run_batches`（用例层的批处理骨架）按
        `jobs.update(...)` 调，那是它的契约。实现里原本叫 `update_job`，
        已改名为 `update`（`api.py` 没用到它，所以改名不波及接口层）。
      · 只留一套名字，不留同义方法 —— 两边各留一个（`busy` + `any_job_running`）
        正是这次错位的来源。
    """

    def create_job(
        self, kind: str, total: int, *, actor: str = "", ts: int = 0
    ) -> str: ...

    def reclaim_stale(self, *, now: int, older_than: int) -> int:
        """把**显然已经死掉**的 `running` 作业收尾，返回收了几条。

        ※ 为什么需要：`state` 只在 `run_batches` **活着**的时候被写，而
        取消只置旗标。进程被 kill / OOM / 断电之后，那一行永远是
        `running` ⇒ `any_job_running()` 永远为真 ⇒ 重排被永久挡住，
        产品内没有任何恢复路径。

        判据是「开始得太久、而没有任何进展」—— 阈值由调用方给（它有时钟），
        这一层只负责执行。`started_at = 0` 的旧行**不碰**（那是没记时间的
        历史数据，不能当成「1970 年开始的作业」）。
        """

    def update(self, job_id: str, **fields: Any) -> None:
        """更新进度字段。`run_batches` 按这个名字调。"""
        ...

    def get_job(self, job_id: str) -> Any | None: ...

    def any_job_running(self) -> bool:
        """有没有作业在跑。`reorder` 用它挡并发（409 的判据）。"""
        ...

    def request_cancel(self, job_id: str) -> tuple[bool, str]: ...

    def cancel_requested(self, job_id: str) -> bool:
        """这个作业被**请求**取消了吗。

        ※ 与 `request_cancel` 是**两件事**：那个是「发出请求」，这个是
        「读到请求」。取消是协作式的 —— 请求只置一个旗标，真正停下来
        要等 `run_batches` 在批与批之间读到它。批处理骨架（`reorder`）
        靠这个方法判断该不该停。
        """
        ...


@runtime_checkable
class RunLedgerRepository(Protocol):
    """**读**失败记账（`runs` / `run_entries` 两张表）。

    ## 为什么这个端口上只有读

    写账的口子**不在**这里，而在那三个数据写入方法的 `run=` 参数上
    （`upsert_messages` / `insert_items` / `insert_digest`）。理由：
    账必须与它解释的那次写入**同事务**，而「同事务」在用例层唯一的表达方式
    就是「一次调用」。给它单独一个 `record_run`，就等于给了调用方一条
    「先写数据、再写账」的路 —— 而那条路上崩溃时会留下「写了条目、
    没记账」，且无声。**不留这条路，比写一条注释说「别这么用」可靠。**

    ## ※ 抬头与账是两次查询，不是一次

    `recent_runs` 返回的 `RunRecord.entries` 是**空元组** —— 它答的是
    「最近跑过哪些」这个问题，而账要按需取（`entries_of_run`）。
    把两者合成一次查询的代价是：列 20 条抬头就要把它们的账全捞出来，
    而绝大多数调用只想要那一行摘要。

    ## 读路径用只读连接

    `serve` 那一侧拿的是只读连接（不变量 ①）—— 这个方法必须只发 `SELECT`。
    """

    def recent_runs(self, *, limit: int = 20) -> list[Any]:
        """最近的若干次跑，**最新的在前**。

        ※ 排序是 `started_at DESC`，同一秒内按 `run_id` 兜底（任意但稳定）。
        「最近一次」因此有唯一的定义 —— 而 `run_id` 是随机的，所以
        **不要**把同秒的两条顺序当成有意义的事实。
        """
        ...

    def get_run(self, run_id: str) -> Any | None:
        """按 id 取一次跑的抬头。**没有这一次跑时返回 `None`**。

        ※ `None` 与「这一轮一笔账都没有」**必须分得开**：前者是
        「这个 run_id 库里没有」（多半是抄错了），后者是「这一轮全成了」。
        两者混成一个空列表的话，`ledger --run 打错的id` 会显示
        「这一轮没有账」—— 一句听起来像「一切正常」的假话。
        """
        ...

    def entries_of_run(self, run_id: str) -> list[Any]:
        """这一次跑的**全部**账，按记录顺序。"""
        ...

    def entries_of_message(self, msg_id: int, *, limit: int = 50) -> list[Any]:
        """**这一条消息**的账，最新的在前。

        ※ 这个方法是「每一条消息都能回答为什么」那句话的直接落点：
        一条消息为什么没进条目，答案就在这几行里（预筛丢弃 / 摘录对不上 /
        闸门清掉字段 / 整批失败 / 没跑到）。返回 `[]` 的含义是
        「这条消息**没有任何一笔账**」—— 那通常意味着它进了条目，
        或者它压根没被这一轮的窗口看到（两件事要分开看 `runs.seen`）。
        """
        ...


@runtime_checkable
class KvRepository(Protocol):
    """键值杂项。

    ※ 它是**有意的杂物抽屉**，不是「先放这儿以后再整理」：类目撤销栈
    （`api._snapshot_categories`）存的就是一份 JSON 快照，它既不是条目、
    也不是日报，更不是配置（配置走 TOML，用户可读可编辑）。给它现造一个
    领域概念只会让读代码的人去找一个不存在的语义。
    """

    def get_kv(self, key: str) -> str | None: ...

    def set_kv(self, key: str, value: str) -> None: ...


@runtime_checkable
class StorageBackend(Protocol):
    """存储后端**自描述** —— `/api/health` 的第二问。

    ※ 为什么要问「用的是什么」而不只问「通不通」：
    「连不上」这三个字在 SQLite 与 MySQL 上指向**完全不同**的下一步
    （一个是「跑没跑过 demo」，一个是「安全组 / 连接串」）。
    只报通不通，排查的人得先猜自己在连什么。
    """

    def backend_name(self) -> str:
        """`sqlite` / `mysql`。"""
        ...

    def ping(self) -> tuple[bool, str]:
        """`(通不通, 说明)`。**不抛** —— 探活失败要能被报出来。"""
        ...


@runtime_checkable
class Cache(Protocol):
    """缓存 —— 云上是 Redis，本机是一个**可观测**的空实现。

    ※ 这里的 `stats()` 是本端口存在的**主要理由**之一，见下。

    ## 为什么缓存要有端口，而不是直接调 redis

    与仓储同一个理由：用例层不该知道背后是什么。但缓存还多一层 ——
    **它有一个「关掉」的状态**，而那个状态必须能被看见。

    ## `stats()` 的契约：`None` 与 `0` 不是一回事

    | 字段 | `none` 后端 | `redis` 后端（没查过） | `redis` 后端（查过没命中） |
    |---|---|---|---|
    | `enabled` | `False` | `True` | `True` |
    | `hit_rate` | `None` | `None` | `0.0` |

    ※ **第一列与第三列不能合并成同一个数。** 「没启用」是一个配置事实，
    「启用了但一次没命中」是一个**故障信号**（该去查 key 或 TTL）。
    把前者报成 `0%`，就等于把「没开」说成「开了但没用」——
    而后者会让人去查一个根本不存在的问题。

    判据：`None` 的语义是「这个问题在这里不适用」，`0` 的语义是
    「适用，值是零」。两者混起来的失败**看起来像成功**：报告里一切正常。
    """

    def backend_name(self) -> str:
        """`redis` / `none`。"""
        ...

    def get(self, key: str) -> str | None:
        """读。**未命中返回 `None`，不抛** —— 缓存的契约是「拿不到就去算」。"""
        ...

    def set(self, key: str, value: str, *, ttl: int | None = None) -> None:
        """写。`ttl=None` 用后端自己的默认值。"""
        ...

    def delete(self, key: str) -> None: ...

    def stats(self) -> dict[str, Any]:
        """见类 docstring 的那张表。"""
        ...

    def ping(self) -> tuple[bool, str]:
        """`(通不通, 说明)`。**不抛**。"""
        ...


# ── 数据源 ──────────────────────────────────────────────────────────


@runtime_checkable
class MessageSource(Protocol):
    """一个数据源。**形状由接口约束，处理方式不由接口约束。**

    要害在这里：会议转写是「长音视频」必须切，邮件是
    「自洽的短文本」切了反而把它拆散。⇒ **归一化的责任在数据源**，
    不在接口。如果两个源为了迎合同一个下游而做同样的处理，其中一个必然是错的。
    """

    #: 源的标识（`chat` / `meeting` / `mail` / `synth`）。
    name: str

    def read(self, *, since: int | None = None, until: int | None = None) -> list[Any]:
        """读出消息。

        ※ **读不到就抛，不许返回空**。

        「这个群没消息」和「这个群读不到」长得一样，这正是问题所在 —— 下游拿到
        两个空，会给出同一份日报，并用「今天很安静」的口气叙述它。

        ※ 也**解析出 0 条就抛**（`SourceFormatError`）：选择器失效、站点改版、
        编码认错，表现都是「这个源今天没更新」。而**产出为空是所有错误里
        最不容易被注意到的一种**。
        """
        ...


# ── 模型 ────────────────────────────────────────────────────────────


@runtime_checkable
class ModelClient(Protocol):
    """本地模型客户端（OpenAI 兼容）。

    ※ 契约要求实现方自己处理三件事：
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

    ※ **只存计数与指纹，不存内容。** 存内容就是新造一个泄漏面，而它恰好
    会存下最该保护的那些文本。
    """

    def record(self, entry: Any) -> None: ...

    def read_all(self) -> list[Any]: ...


# ── 配置 ────────────────────────────────────────────────────────────


@runtime_checkable
class ConfigStore(Protocol):
    """配置读写。

    ※ 键名是**对外契约**：`config/groups.toml` 的 `id` / `name` / `tier`。
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
