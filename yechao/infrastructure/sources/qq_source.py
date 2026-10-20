"""QQ 数据源 —— 读**本机 QQ** 的群消息。

## 三种目标，**不是二选一的退路**

| 目标 | 走哪条 | 需要人工动作吗 |
|---|---|---|
| 不给目标（默认） | **自动找**本机 QQ 的数据根 → 认出加密库 → 从 QQ 进程内存取钥匙 → 按页解密 → 当 SQLite 读 | 不需要（QQ 开着并登录着就行） |
| 一个目录、或一个 `.db` 文件 | 同上（目录按约定的相对路径找 `nt_msg.db`；`.db` 直接读） | 不需要 |
| 一个 `.json` / `.txt` 文件 | 按**写死的契约**解析（见 `qq_parse.parse_export_json` 的 docstring） | 需要（那份文件得由用户产出） |

※ 三条都留着，理由不同：**前两条是产品路径**（本机装了 QQ 就该不用人工动作）；
最后一条是**可复现路径** —— 别人 clone 这个仓库、机器上没有 QQ 时，也能拿一份
文件把整条流水线跑通（与网页源「夹具入口」同一个用途）。

## 目标从哪来

  1. 环境变量 `YECHAO_QQ_SOURCE` —— 一次性的、临时换一个目标时用
  2. `<config_dir>/sources.toml` 的 `[qq] target` —— 长期配置
  3. 都没配 ⇒ **自动找**（`qq_locate.resolve_account`）

### 为什么最后那一条从「抛」改成了「自动找」

上一版在这里写的是「**刻意不自动发现** `~/Documents/Tencent Files/`」，理由也
写得很足：读了什么要能被指定，否则「连读什么都不知道，后面那三道闸门就都没有
意义」。那条理由现在**仍然成立**，改的是「怎么满足它」：

  · 这条路的全部价值就是**用户不用做任何事**（与 `meeting_wemeet` 自动探测本机
    会议历史库逐字相同）。前提是「先把路径找出来填进配置」的话，它在真实使用里
    等于不存在 —— 而用户要的是「自动提取群消息」。
  · 所以「指定」这一条改由**别的方式**兑现：
      - **本机有多个账号时不挑**，报错并把它们列出来（挑错的症状是「我明明有
        两个号，它只读了其中一个」，而报告看起来完全正常）；
      - **选中了哪个账号写进 `where`**，由报错与报告打出来 —— 读了什么一眼可见；
      - 任何时候都可以用上面那两条把它**钉死**。

⇒ 判据从「不许自动找」变成「**自动找，但不许猜**」。前者防的那件事（静默读到
别的东西）由「不猜 + 说出来」防住，而后者才是用户真正要的。

## ※ 类名为什么带下划线

`tests/contracts/test_layering.py::test_适配层的实现都接上了线` 扫的是**适配层
顶层公开类名**有没有在 `bootstrap.py` 的源码文本里出现过。数据源不由组合根
实例化（它由 `sources/__init__.py` 的注册表组合）—— 与 `_WebSource` /
`_MailSource` 是同一类东西。⇒ 用下划线前缀走那条判据自己留的口子（它只扫不以
`_` 开头的类）。**这不是绕过护栏**：本类确实被接上了线（`REGISTRY["qq"]`），
护栏只是用文本判据看不见注册表。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from yechao.domain.errors import ConfigError, SourceUnavailable
from yechao.infrastructure.sources._common import (
    SOURCES_FILE,
    config_dir,
    in_window,
    source_table,
)
from yechao.infrastructure.sources.qq_ntdb import (
    HEADER_SIZE,
    SQLITE_MAGIC,
    _KeyNotFound,
    looks_like_qqnt_library,
    open_encrypted_library,
    recover_key,
)
#: 源的标识与三种后缀都在解析模块里定义（每条消息的 `source` 字段用的就是它），
#: 这里引用而不再写一遍字面量 —— 两处字面量漂移的症状是「消息自称来自一个不
#: 存在的源」，而它在审计里看不出来。
from yechao.infrastructure.sources.qq_locate import PROFILE_DB, resolve_account
from yechao.infrastructure.sources.qq_parse import (
    COL_TS,
    DB_SUFFIX,
    JSON_SUFFIX,
    LIBRARY_COLUMNS,
    NAME,
    TEXT_SUFFIX,
    _LibraryNotUnderstood,
    parse_export_json,
    parse_export_text,
    parse_library,
)

#: 目标的来源。
TARGET_ENV = "YECHAO_QQ_SOURCE"
#: 分组号。※ QQ 的群号是一个整数，理论上能从库里读出来；但**配置给的优先** ——
#: 见 `parse_library` 里那条判据（「这批消息算哪个群」是一个配置事实）。
GROUP_ID_ENV = "YECHAO_QQ_GROUP_ID"

#: 认的后缀。
SUFFIXES = (JSON_SUFFIX, TEXT_SUFFIX, DB_SUFFIX)

#: 本机 QQ 的消息库在这个相对路径下（`<数据目录>/nt_qq/nt_db/nt_msg.db`）。
#:
#: ※ 它是**约定**，不是猜：本机两个账号的目录下都长这样（报告里写了）。
#: 目录目标先按它找；找不到再退回「目录下唯一的那个 .db」。
#: 消息库的文件名。
NT_MSG_NAME = "nt_msg.db"

#: 目录目标要找的几处，**按顺序**试。
#:
#: ※ 前一处是「直接把 `nt_db` 那一层指过来」的形状，后一处是本机实测的
#: 账号目录形状（`<账号>/nt_qq/nt_db/nt_msg.db`）。两处都没有，才退回
#: 「目录下唯一的那个 .db」。
#: ※ 本机实测：把目标指到 `nt_db` 那一层上是很自然的一件事，而那时按
#: 账号目录的形状拼出来的是 `nt_db/nt_qq/nt_db/nt_msg.db` —— 找不到。
NT_MSG_CANDIDATES = (
    (NT_MSG_NAME,),
    ("nt_qq", "nt_db", NT_MSG_NAME),
)

#: 库里的表名。
#:
#: ※ 它沿用 QQ NT 的表名（`group_msg_table`），**列名**由本源约定 —— 见
#: `qq_parse.parse_library` 的 docstring：真库的列名是数字（`40001` / `40050` /
#: `40800` …），而本机那份库是加密的、列名看不到，所以**不猜**列名。
LIBRARY_TABLE = "group_msg_table"

#: 不给分组号时用哪个。
#:
#: ※ 它**是**一个默认值，而本项目对默认值的态度是「用了要能被看出来」。
#: 能看出它的地方有两处：`--source qq` 的报告会打出源名，以及落库后的
#: `group_id` 就在消息行上。取 820000008 是因为它是本项目**自编**的假群号段
#: （820000001–820000007 已被网页源、邮件源、订阅源等占用）—— 与它们**错开**
#: 是刻意的：两个源的默认分组号撞在一起，症状是「QQ 的消息与别处的消息在同一个
#: 『群』里」，而那一列本来是用来分开它们的。
DEFAULT_GROUP_ID = 820000008


# ── 目标 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Target:
    """一个已经解析好的目标：**从哪儿读**、**它是哪种形态**、**算进哪个分组**。

    ※ `group_id` 可以是 `None` —— 那表示「**用库里那个群号**」。这不是缺省值，
    是**更对的那一个**：真库每一行都自带群号（实测一个账号下有 51 个群），
    全都塞进一个配置来的 id 会让它们在日报里互相串上下文。
    ⇒ 只有**导出文件**（里面没有群号）才必须有一个配置来的 id。

    ※ `namespace` 是 `msg_id` 的命名空间。它与 `group_id` **不是一回事**：
    `group_id` 说「这条消息属于哪个群」，`namespace` 说「这条消息的身份是在
    哪个空间里算的」。同一批数据走导出文件与走消息库要算出**同一批 id**
    （测试钉着这条），所以命名空间取**配置里那个值**或**账号标识**，
    不取路径 —— 路径会变，而 id 一变，重跑摄取就把整个源重写一遍。
    """

    raw: str
    kind: str  # "export-json" / "export-text" / "library"
    group_id: int | None
    namespace: str
    where: str  # 这个目标是从哪儿读出来的（给报错用）


def _account_of(path: Path) -> str:
    """这个库属于哪个账号。

    ※ 判据是**路径的形状**（实测：`<根>/<QQ号>/nt_qq/nt_db/nt_msg.db`），
    不是「目录名像不像数字」：认不出来时回落到文件名 —— 夹具与导出文件
    都走那一条，而它们本来就没有账号。
    """
    parts = path.parts
    if len(parts) >= 4 and parts[-2] == "nt_db" and parts[-3] == "nt_qq":
        return parts[-4]
    return path.stem


def _env_group_id() -> int | None:
    raw = os.environ.get(GROUP_ID_ENV)
    if not raw or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError as e:
        raise ConfigError(
            f"{GROUP_ID_ENV}={raw!r} 不是整数。分组号要是一个整数 —— "
            f"它会写进每条消息的 group_id。"
        ) from e


def _resolve_library_file(path: Path) -> Path:
    """目录目标 → 里面那个消息库文件。

    ※ 两级判据，都是**可观察的事实**：先看约定的相对路径，再退回「目录下唯一的
    那个 `.db`」。**两个都没有、或者有多个** ⇒ 抛，不挑一个 —— 挑错的症状是
    「读的是另一个库」，而它看起来完全正常。
    """
    if path.is_file():
        return path
    tried = [path.joinpath(*parts) for parts in NT_MSG_CANDIDATES]
    for candidate in tried:
        if candidate.is_file():
            return candidate
    found = sorted(p for p in path.glob("*.db") if p.is_file())
    if len(found) == 1:
        return found[0]
    raise SourceUnavailable(
        f"这个目录里找不到唯一的 QQ 消息库：{path}\n"
        f"  先找过：{'、'.join(str(c) for c in tried)}"
        f"  再找过目录下的 *.db，找到 {len(found)} 个"
        f"{'：' + '、'.join(p.name for p in found) if found else ''}。\n"
        f"※ **不挑一个**：挑错的症状是「读的是另一个库」，而它看起来完全正常。"
        f"请把目标直接指到那个库文件上。",
        path=str(path),
    )


def resolve_target() -> _Target:
    """把「从哪儿读」定下来。没配就**自动找**，找不着才抛。

    优先级：环境变量 > 配置文件。理由：环境变量是**这一次**的意图（临时读一份
    夹具看看），配置文件是**长期**的意图；临时的不该被长期的覆盖掉。
    """
    table = source_table(
        "qq",
        hint=(
            '  [qq]\n'
            '  target = "~/Documents/Tencent Files/<QQ号>/nt_qq/nt_db"\n'
            '  group_id = 820000008'
        ),
    )
    from_env = (os.environ.get(TARGET_ENV) or "").strip()
    if from_env:
        raw, where = from_env, TARGET_ENV
    elif str(table.get("target") or "").strip():
        raw, where = str(table["target"]).strip(), f"{SOURCES_FILE} 的 [qq] target"
    else:
        # ※ **没配就自动找**，而不是直接抛。
        #
        # 判据与 `meeting_wemeet`（自动探测本机腾讯会议的会议历史库）逐字相同：
        # 这条路的全部价值就是**用户不用做任何事** —— 如果前提是「先把路径找出来
        # 填进配置」，它在真实使用里等于不存在。
        #
        # ※ 但「自动」不等于「静默」：选中了哪个账号会**写进 `where`**，
        # 由报错与报告打出来（`_Target.where` 的用途就是这个）。
        # ※ 本机有多个账号时**不挑**，`resolve_account` 会抛并列出它们 ——
        # 挑错的症状是「我明明有两个号，它只读了其中一个」，而报告看起来正常。
        try:
            account, account_where = resolve_account()
        except ConfigError as e:
            raise ConfigError(
                f"{e}\n"
                f"※ 也可以**直接指定**，那样就不用自动找：\n"
                f"  · 环境变量：{TARGET_ENV}=<QQ 的数据目录、一个 .db、或一份导出文件>\n"
                f"  · 配置文件：{config_dir() / SOURCES_FILE} 里写\n"
                f'      [qq]\n'
                f'      target = "~/Documents/Tencent Files/<QQ号>/nt_qq/nt_db"'
            ) from e
        raw, where = str(account.db_dir), f"自动探测到 {account.uin}（{account_where}）"

    # ※ `group_id` **只取配置里那个**，没有就是 `None`（= 用库里的群号）。
    # 不给一个「默认分组号」：那会把 51 个群并成一个，而报告看起来完全正常。
    group_id = _env_group_id()
    if group_id is None:
        raw_gid = table.get("group_id")
        if raw_gid is not None:
            try:
                group_id = int(raw_gid)
            except (TypeError, ValueError) as e:
                raise ConfigError(
                    f"{SOURCES_FILE} 的 [qq] group_id={raw_gid!r} 不是整数。"
                ) from e

    return _classify(raw, group_id=group_id, where=where)


def _classify(raw: str, *, group_id: int | None, where: str) -> _Target:
    """按目标**自身的形状**判它属于哪一种形态。见模块 docstring 的表。

    ※ 「是不是目录」与「后缀是什么」都是**用户自己给的、非黑即白的事实** ——
    这里不嗅内容（嗅探是在几种结构之间猜，猜错的症状是「按另一种结构解析，
    产出一些看起来正常的东西」）。
    """
    path = Path(raw).expanduser()
    if path.is_dir():
        path = _resolve_library_file(path)
    # ※ 命名空间：配置给了分组号就用它，否则用**账号标识**（见 `_Target`）。
    namespace = f"qq:{group_id}" if group_id is not None else f"qq:{_account_of(path)}"

    suffix = path.suffix.lower()
    if suffix == JSON_SUFFIX:
        kind = "export-json"
    elif suffix == TEXT_SUFFIX:
        kind = "export-text"
    elif suffix == DB_SUFFIX:
        kind = "library"
    else:
        raise ConfigError(
            f"QQ 源认 {' 与 '.join(SUFFIXES)}，也可以给一个目录"
            f"（里面放消息库），而这个目标两者都不是：{raw}\n"
            f"（目标来自 {where}）\n"
            f"※ 这里**不嗅内容**：嗅探是在几种结构之间猜，猜错的症状是「按另一种"
            f"结构解析，产出一些看起来正常的东西」。后缀与「是不是目录」是用户"
            f"自己给的、非黑即白的事实。"
        )

    # ※ **导出文件必须有一个分组号**：它里面没有群号可读。没配就用那个自编的
    #   默认值 —— 它**用了要能被看出来**（源名会打进报告，`group_id` 就在消息行上）。
    if kind != "library" and group_id is None:
        group_id = DEFAULT_GROUP_ID
        namespace = f"qq:{group_id}"

    return _Target(
        raw=str(path), kind=kind, group_id=group_id, namespace=namespace, where=where
    )


# ── 读 ──────────────────────────────────────────────────────────────


def _read_export_bytes(target: _Target) -> bytes:
    """把导出文件的字节读回来。读不到 ⇒ `SourceUnavailable`（**不返回空**）。

    ※ 只用 `read_bytes()`：**只读**是这条判据的另一半。
    """
    path = Path(target.raw)
    try:
        return path.read_bytes()
    except OSError as e:
        raise SourceUnavailable(
            f"QQ 导出文件读不到：{path}（{e}）。（目标来自 {target.where}）",
            path=str(path),
        ) from e


def _open_plain_sqlite(path: Path) -> sqlite3.Connection:
    """**只读**打开一个明文 SQLite 库。

    ※ `mode=ro` 是 URI 参数，不是「我们自己小心点」—— 由数据库本身拒绝写。
      与不变量 ①「网页侧不得写入主库」同一条取向：靠自觉的约束迟早被静默绕开。
    ※ **不用 `immutable=1`**：那会告诉 SQLite「这个文件不会被别人改」，
      而 QQ 的消息库**正在被写**。用 immutable 的后果是读到撕裂的页，
      而且不会报错。
    """
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _library_connection(path: Path) -> tuple[sqlite3.Connection, object, object]:
    """给一个消息库文件，返回 `(能查它的连接, 钥匙, 参数)`。

    ## 三种库，三种处置

      · 明文 SQLite（魔数 `SQLite format 3\0`）⇒ 只读打开**原文件**，
        钥匙与参数都是 `None`。
      · QQ NT 的加密库（魔数 `SQLite header 3\0` + 头部有 `QQ_NT DB`）
        ⇒ 取钥匙、按页解密，解出来的字节**放进内存库**（`deserialize`）。
        钥匙一并带回来 —— 同一个账号的**资料库要用同一把**（见 `recover_key`）。
      · 别的 ⇒ 抛 `_LibraryNotUnderstood`。

    ※ 解密结果**不落盘**：落一个明文副本就等于把聊天记录写到了另一个地方，
      而这是本项目最不该做的事（红线是「敏感原文不出本机」，这里是「多出一份」）。
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(HEADER_SIZE)
    except OSError as e:
        raise SourceUnavailable(
            f"QQ 消息库读不到：{path}（{e}）", path=str(path)
        ) from e

    if head.startswith(SQLITE_MAGIC):
        return _open_plain_sqlite(path), None, None

    if looks_like_qqnt_library(head):
        found = recover_key(path)          # 取不到钥匙时在这里抛 `_KeyNotFound`
        if found is None:
            raise _KeyNotFound(
                f"读到了 QQ 的加密库，但取不到它的钥匙：{path}\n"
                f"※ 钥匙只在**正在运行的 QQ 进程内存**里（见 qq_key 的说明）。\n"
                f"※ 下一步：先把 QQ 打开并登录，再跑一次。",
                path=str(path),
            )
        key, params = found
        plain = open_encrypted_library(path, key=key, params=params)
        con = sqlite3.connect(":memory:")
        con.deserialize(plain)
        return con, key, params

    raise _LibraryNotUnderstood(
        f"这个文件既不是明文 SQLite，也不是 QQ NT 的加密库：{path}\n"
        f"  前 16 字节：{head[:16]!r}\n"
        f"  ※ 明文库以 {SQLITE_MAGIC!r} 开头；QQ NT 的加密库以 "
        f"b'SQLite header 3\\x00' 开头且头部里有 b'QQ_NT DB'。\n"
        f"  ※ 认不出就抛 —— 猜一个格式的症状是「读出一堆看起来正常的东西」。",
        path=str(path),
    )


def _library_rows(con: sqlite3.Connection) -> Iterator[Mapping[str, Any]]:
    """从库里取消息行。表不在 ⇒ `_LibraryNotUnderstood`（**不返回空**）。

    ※ **点名取那几列**，不用 `SELECT *`：真库那张表有 38 列（实测），
    全取回来再丢掉 31 列是白读几倍的字节。
    ※ 按 `40050`（时刻）排序：**有序**让下游「取前后几条做上下文」这件事在源这
    一层就成立，而不是靠下游自己再排一次。
    """
    con.row_factory = sqlite3.Row
    cols = ", ".join(f'"{c}"' for c in LIBRARY_COLUMNS)
    try:
        return con.execute(
            f'SELECT {cols} FROM "{LIBRARY_TABLE}" ORDER BY "{COL_TS}"'
        )
    except sqlite3.DatabaseError as e:
        raise _LibraryNotUnderstood(
            f"库里没有能读的表 {LIBRARY_TABLE!r}：{e}。\n"
            f"※ 认的表名见 qq_source.LIBRARY_TABLE，认的列名见 "
            f"qq_parse.LIBRARY_COLUMNS（它们是 QQ 自己那套**数字列名**）。"
        ) from e


#: 资料库那张表，以及要取的三列（实测：`1000` 是 uid、`1002` 是 QQ 号、
#: `20002` 是昵称）。※ 本机实测覆盖 95% 的群消息（491388 条里 468262 条能查到）。
PROFILE_TABLE = "profile_info_v6"
COL_PROFILE_UID = "1000"
COL_PROFILE_UIN = "1002"
COL_PROFILE_NAME = "20002"


def _profile_maps(
    db_dir: Path, *, key: object = None, params: object = None
) -> tuple[dict[str, int], dict[str, str]]:
    """`(uid → QQ 号, uid → 昵称)`，从同一个目录下的 `profile_info.db` 读。

    ※ **它读不到不算这个源读不到**：资料库只是给消息补「发送者的账号与名字」，
    缺了它消息照样读得出来，只是 `actor_account` 全变成 `None`。
    这是**如实置空**，不是编一个号 —— 把 uid 哈希成一个整数会让下游把那个数字
    当成一个真实的人（见 `qq_parse.parse_library` 的说明）。
    ※ 但「文件在、却打不开」是另一回事：那说明这个目录坏了，**抛**。
    """
    path = db_dir / PROFILE_DB
    if not path.is_file():
        return {}, {}
    try:
        head = path.open("rb").read(HEADER_SIZE)
    except OSError as e:
        raise SourceUnavailable(
            f"QQ 资料库读不到：{path}（{e}）", path=str(path)
        ) from e

    if head.startswith(SQLITE_MAGIC):
        con = _open_plain_sqlite(path)
    elif looks_like_qqnt_library(head):
        # ※ 用**消息库那把**钥匙（同一账号共用一把，见 `recover_key`）。
        con = sqlite3.connect(":memory:")
        con.deserialize(open_encrypted_library(path, key=key, params=params))
    else:
        raise _LibraryNotUnderstood(
            f"资料库既不是明文 SQLite，也不是 QQ NT 的加密库：{path}\n"
            f"  前 16 字节：{head[:16]!r}"
        )
    try:
        rows = con.execute(
            f'SELECT "{COL_PROFILE_UID}", "{COL_PROFILE_UIN}", "{COL_PROFILE_NAME}"'
            f' FROM "{PROFILE_TABLE}"'
        ).fetchall()
    except sqlite3.DatabaseError:
        # ※ 表不在 ⇒ 这一版客户端的资料库换了形状。补不上账号，但**不阻断**
        #   消息读取 —— 与「文件不在」同一条处置。
        return {}, {}
    finally:
        con.close()

    accounts: dict[str, int] = {}
    names: dict[str, str] = {}
    for uid, uin, name in rows:
        if not uid:
            continue
        if isinstance(uin, int) and uin > 0:
            accounts[str(uid)] = uin
        if name:
            names[str(uid)] = str(name)
    return accounts, names


# ── 源 ──────────────────────────────────────────────────────────────


class _QqSource:
    """QQ 源。实现 `application/ports.py::MessageSource`。

    ※ 它**不切**消息：一条群消息是一个自洽的短文本（与邮件、网页通知同类），
    切了反而把它拆散。这与会议转写必须切**相反** —— 端口那句「形状由接口约束，
    处理方式不由接口约束」说的就是这件事。

    ※ 目标在**装配时**定下来（由 `build_qq_source` 传进来），不是每次 `read()`
    现查环境变量。理由与网页源、邮件源逐字相同：报错要发生在**任何一个源开始读
    之前**，而且「目标没配」是**用法错**（退出码 2），不是「跑起来之后才失败」。
    """

    name = NAME

    def __init__(self, target: _Target) -> None:
        self._target = target

    def read(
        self, *, since: int | None = None, until: int | None = None
    ) -> list[Any]:
        """读一批消息。

        抛（每一条都是**响亮**的，没有一条是静默返回空）：
          · `ConfigError`             —— 没配目标 / 后缀不认（在装配时报）
          · `SourceUnavailable`       —— 读不到（文件不在 / 没权限）
          · `_KeyNotFound`            —— 加密库读到了，但密钥取不到
          · `_ExportNotUnderstood` / `_LibraryNotUnderstood` —— 读到了但没读懂
          · `_ExportHasNoMessages` / `_LibraryHasNoMessages` —— 读懂了，确实没有

        ※ **时间窗在解析之后才过**。这不是实现细节，是判据：
        「解析出 0 条」与「这段时间里没有」是两件事 —— 前者是格式问题（去改
        解析器），后者是数据问题（这段时间真的安静）。先解析、再过滤，两者
        就分得开：**解析出的条数是 0 才抛，窗口内 0 条只是返回空**。
        """
        namespace = self._target.namespace
        if self._target.kind == "export-json":
            messages = parse_export_json(
                _read_export_bytes(self._target),
                namespace=namespace,
                group_id=self._target.group_id,
            )
        elif self._target.kind == "export-text":
            messages = parse_export_text(
                _read_export_bytes(self._target),
                namespace=namespace,
                group_id=self._target.group_id,
            )
        else:
            path = Path(self._target.raw)
            con, key, params = _library_connection(path)
            try:
                # ※ 资料库要用**同一把**钥匙（一个账号一把）—— 所以先开消息库、
                #   拿到钥匙，再读资料库。反过来会让资料库去扫一遍内存。
                accounts, names = _profile_maps(path.parent, key=key, params=params)
                rows = list(_library_rows(con))
            finally:
                con.close()   # ※ 用完就关：不关的话 QQ 的库文件会被占着
            messages = parse_library(
                rows,
                namespace=namespace,
                group_id=self._target.group_id,
                accounts=accounts,
                names=names,
            )
        return [m for m in messages if in_window(m.ts, since, until)]


def build_qq_source() -> _QqSource:
    """注册表用的工厂（`REGISTRY["qq"]`）。

    ※ 它在**装配的时候**就把目标定下来，而不是等 `read()` 时再查 —— 与
    `build_web_source` / `build_mail_source` 同一条判据：`interfaces/cli.py`
    把「源名不认识」与「目标没配」都算作**用法错**（退出码 2，下一步是去改
    配置 / 命令行）。
    """
    return _QqSource(resolve_target())
