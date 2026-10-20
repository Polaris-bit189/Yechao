"""QQ 群消息的解析 —— 把「QQ 里的东西」变成消息。

## 两种形态，同一个出口

| 形态 | 从哪来 | 走哪条解析 | 可复现吗 |
|---|---|---|---|
| **导出文件**（`.json` / `.txt`） | 用户自己导出的聊天记录 | `parse_export_json` / `parse_export_text` | 是（一个文件就是全部输入） |
| **消息库**（`.db`） | 本机 QQ 的 `nt_msg.db`，或一份同形的合成库 | `parse_library` | 是（库是文件，读法只读） |

※ 两种形态**都要**，而且**不是二选一的退路**：导出文件是「这台机器上没有 QQ
客户端也能复现」的那条路，消息库是「有客户端时不用人工动作」的那条路。

## ※ 本模块只做解析，不碰 I/O

打开文件、判定加密、取密钥、连进程内存 —— 全在 `qq_source.py`。这一层只吃
**字节**与**行**，所以它能被穷举测试：同一段字节永远给同一批消息。

## msg_id 怎么算（这是本模块最要紧的一条判据）

`_stable_msg_id(namespace, raw_id)` —— 见它的 docstring。要点是：**id 只由
「这条消息在源系统里的身份」决定**，不由「它在这一次读到的东西里排第几」决定。

## ※ 为什么导出的文本格式由本模块**写死**

QQ 自带的「消息管理器 → 导出消息记录」产出的文本格式**随版本变**，而本机
没有一份可核对的样本（这一条在报告里如实写了）。**猜一个格式**的症状是：
猜错时解析出 0 条或者解析出一些看起来正常的东西 —— 后者更糟。

⇒ 所以本源认的是下面这份**写死的契约**（`parse_export_text` 的 docstring 里有
逐字的格式），认不出就抛 `_ExportNotUnderstood`。这是「不猜」的直接后果：
宁可让用户按契约转一道，也不猜一个我没见过的格式。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

from yechao.domain.errors import SourceFormatError
from yechao.domain.models import PendingMessage
#: 切分**复用**会议源那一个函数，不另写一套：切分是同一个问题（一段超预算的
#: 正文 → 几条消息），而预算必须只有一处 —— 两处预算漂移的症状是「源以为自己
#: 切好了、下游仍然报 oversized」，那时已经白花了一次调用。
from yechao.infrastructure.sources.meeting_parse import (
    EFFECTIVE_BUDGET_CHARS,
    split_text,
)

#: 源的标识。每条消息的 `source` 字段用的就是它。
#: ※ 它在**解析模块**里定义一次（`qq_source` 引用它），两处字面量漂移的症状是
#: 「消息自称来自一个不存在的源」，而它在审计里看不出来。
NAME = "qq"

#: 三种目标后缀。
JSON_SUFFIX = ".json"
TEXT_SUFFIX = ".txt"
DB_SUFFIX = ".db"


# ── 两种「读不懂」与两种「本来就没有」 ──────────────────────────────


class _ExportNotUnderstood(SourceFormatError):
    """导出文件读到了，但结构不认识。

    ※ 与「读不到」分开：前者要改**文件**（或按契约重新导出），后者要查路径。
    """


class _ExportHasNoMessages(SourceFormatError):
    """导出文件读懂了，但里面**确实一条消息都没有**。

    ※ 与 `_ExportNotUnderstood` 分开：这一条说明解析器没坏，是这份导出本来就是
    空的（比如按一个没有消息的日期区间导出）。两者的下一步不同 —— 一个去改
    解析器，一个去改导出的区间。
    """


class _LibraryNotUnderstood(SourceFormatError):
    """库读到了，但表/列不认识。"""


class _LibraryHasNoMessages(SourceFormatError):
    """库读懂了，但那张表里**确实一条消息都没有**。"""


# ── msg_id ──────────────────────────────────────────────────────────


def _stable_msg_id(namespace: str, raw_id: str) -> int:
    """由「源标识 + 这条消息在源系统里的身份」算出的稳定 id。

    ## 观察到什么

    `msg_id` 是**幂等键**：同一批消息重复摄取不能产生重复条目。所以这个 id
    必须只由「这条消息本身」决定 —— 由「它在这一次读到的东西里排第几」决定的话，
    同一批消息换一次读法就会换一批 id。

    ## 为什么这么定

    · **不用行号 / 下标**。导出文件的顺序会变（换个日期区间导出、中间删了几条、
      客户端改成按时间倒序），用下标当 id 会让**整批**换一批新 id，于是重跑摄取
      把同一批消息又写一遍 —— 日报里同一条出现两次，而没有任何东西会报错。
      （与网页源「不用列表位置」同一条判据。）

    · **不用时间戳**。群里连着发两条，时间戳可能落在同一秒里。

    · **用 QQ 自己给的消息 id**（`raw_id`）。它在源系统里本来就唯一且稳定，
      重新导出同一段时间拿到的是同一个值 —— 这是最可靠的一种身份。

    · **必须带 `namespace`**。同一个 id 在另一个群、另一台机器上是另一条消息。
      （与网页源「`/notice/1.html` 在两个站点上是两篇不同的文章」同一条判据。）

    · 导出文件里**没有**消息 id 时退化成内容指纹（`_content_fingerprint`）——
      那是**如实承认**「这份导出没有给出稳定标识」，代价写在那里。

    ## 为什么取 63 位

    SQLite 的 INTEGER 是有符号 64 位，右移一位留出符号位。不这么做的话约一半的
    id 会超过 `2**63-1`，症状是插入时报 "Python int too large to convert to
    SQLite INTEGER" —— 在**一半**的输入上出现，像随机故障。

    ## 怎么验证

    `test_同一批消息读两遍算出同一批_id`：同一份夹具读两遍，id 集合相同；
    把文件里两条消息**对调顺序**，id 集合仍然相同。
    """
    digest = hashlib.blake2b(
        f"{namespace}\x00{raw_id}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") >> 1


def _content_fingerprint(
    *, group_id: int, ts: int, sender: str, content: str
) -> str:
    """没有源系统 id 时的兜底身份：**内容指纹**。

    ## 代价（写在前面，因为它是一条真实的限制）

    指纹由「群 + 时刻 + 发送者 + 正文」算出。于是：

      · 同一个人在**同一秒**里发了**两段一模一样**的正文 ⇒ 两条会算成同一个 id，
        第二条被当成重复而跳过。这是**已知的、可接受的**损失：那两条在语义上
        本来就是同一条消息的重复（群里连发两遍）。
      · 反过来，正文**被编辑过**（QQ 的「重新编辑」会改正文但保留原时刻）⇒ 会
        算成两个 id，产出两条。这个方向是安全的（多一条好过少一条）。

    ## 为什么不做成「按行号」

    行号是本函数要避免的那件事本身：它让「同一批消息换一次导出顺序」变成
    「一整批新消息」。
    """
    return "fp:" + hashlib.blake2b(
        f"{group_id}\x00{ts}\x00{sender}\x00{content}".encode("utf-8"),
        digest_size=16,
    ).hexdigest()


# ── 导出文件：JSON ──────────────────────────────────────────────────


def _text(value: Any) -> str:
    """把导出文件里的一个字段变成字符串。

    ※ `None` ⇒ 空串（不是 `"None"`）。写错的话症状是正文里出现字面的 "None"，
    而它看起来像消息内容的一部分。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        raise _ExportNotUnderstood("导出文件里的布尔值不能当字段用")
    if isinstance(value, (int, float)):
        return str(value)
    raise _ExportNotUnderstood(
        f"导出文件里的字段值既不是字符串也不是数字：{type(value).__name__}"
    )


def _int_field(raw: Mapping[str, Any], key: str, *, where: str) -> int:
    if key not in raw:
        raise _ExportNotUnderstood(f"{where} 缺少字段 {key!r}")
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise _ExportNotUnderstood(f"{where} 的 {key!r} 不是数字")
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise _ExportNotUnderstood(
            f"{where} 的 {key!r}={value!r} 不是整数"
        ) from e


def parse_export_json(
    raw: bytes, *, namespace: str, group_id: int
) -> list[PendingMessage]:
    """解析一份 JSON 导出。

    ## 认的形状（写死，不猜）

    ```json
    {
      "messages": [
        {"msg_id": 1001, "ts": 1750000000, "sender": "教务员周允",
         "account": 17700001002, "content": "..."}
      ]
    }
    ```

    · 顶层也可以**直接是一个数组**（省掉 `"messages"` 这一层）。
    · `msg_id` 可以缺 —— 缺了就走内容指纹（见 `_content_fingerprint`）。
    · `account` 可以缺、可以是 `null` ⇒ **匿名**（`actor_account=None`）。
      不拿 0 冒充「没有账号」：0 在人物筛选里是一个合法值，两者混起来会让
      匿名消息被当成某个具体的人发的。

    ※ **不认别的键名**（`msgId` / `message_id` / `senderNick` …）。理由：认一个
    没见过的键名就是猜，而猜错的症状是「字段全是空、但条数对」—— 那种产出看起来
    是成功的。认不出就抛，报错里给出这份契约。
    """
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise _ExportNotUnderstood(f"JSON 导出读不出来：{e}") from e

    if isinstance(data, dict):
        entries = data.get("messages")
        if entries is None:
            raise _ExportNotUnderstood(
                "JSON 导出的顶层对象里没有 'messages' 这个键。"
                "认的形状见 qq_parse.parse_export_json 的 docstring。"
            )
    elif isinstance(data, list):
        entries = data
    else:
        raise _ExportNotUnderstood(
            f"JSON 导出的顶层既不是对象也不是数组，而是 {type(data).__name__}"
        )

    if not isinstance(entries, list):
        raise _ExportNotUnderstood("'messages' 不是一个数组")
    if not entries:
        raise _ExportHasNoMessages(
            "这份 JSON 导出里一条消息都没有（是空的导出，不是解析器坏了）"
        )

    out: list[PendingMessage] = []
    for i, entry in enumerate(entries):
        where = f"第 {i + 1} 条消息"
        if not isinstance(entry, dict):
            raise _ExportNotUnderstood(f"{where} 不是一个对象")
        ts = _int_field(entry, "ts", where=where)
        sender = _text(entry.get("sender"))
        content = _text(entry.get("content"))
        account_raw = entry.get("account")
        account = None if account_raw is None else _int_field(entry, "account", where=where)
        raw_id = entry.get("msg_id")
        if raw_id is None:
            raw_id = _content_fingerprint(
                group_id=group_id, ts=ts, sender=sender, content=content
            )
        else:
            raw_id = str(raw_id)
        out.append(
            PendingMessage(
                msg_id=_stable_msg_id(namespace, raw_id),
                group_id=group_id,
                ts=ts,
                sender=sender,
                content=content,
                actor_account=account,
                source=NAME,
            )
        )
    return out


# ── 导出文件：文本 ──────────────────────────────────────────────────

#: 文本导出的字段分隔符。
TEXT_DELIMITER = "\t"
#: 文本导出的注释前缀。以它开头的行整行忽略。
TEXT_COMMENT = "#"


def parse_export_text(
    raw: bytes, *, namespace: str, group_id: int
) -> list[PendingMessage]:
    """解析一份文本导出。

    ## 认的形状（写死，不猜）

    一行一条消息，四个字段用 **TAB** 分隔：

    ```
    <ts>\\t<sender>\\t<account>\\t<content>
    ```

    · `<ts>`：Unix **秒**（整数）。不认「2026-10-05 12:30」这类墙上时间 ——
      它要一个时区才能变成时刻，而**取哪个时区是猜**（导出的人在哪台机器上、
      机器设的什么时区都不知道）。要墙上时间的导出，请先自己转成 Unix 秒。
    · `<sender>`：群昵称原文。
    · `<account>`：QQ 号；**留空表示匿名**。
    · `<content>`：正文。**不切**（一条群消息是一个自洽的短文本）——
      与会议转写必须切**相反**，端口那句「形状由接口约束、处理方式不由接口
      约束」说的就是这件事。
    · 以 `#` 开头的行是注释；空行忽略。
    · 字段数不是 4 ⇒ 抛（**不猜**：少一个字段时「把正文当成账号」这种错会
      静默产出看起来正常的东西）。

    ※ 为什么是 TAB 而不是逗号：群消息正文里逗号太常见，而 TAB 不会出现在
    群昵称里。选一个**正文里不会出现**的分隔符，就不需要引号与转义 —— 而
    转义规则正是「看起来对、边界上错」的高发区。

    ※ 这份契约**由本项目定义**，不是 QQ 的导出格式：QQ 的导出格式随版本变，
    而本机没有可核对的样本（报告里如实写了）。猜一个没见过的格式，猜错的
    症状是「解析出 0 条」或者「解析出一些看起来正常的东西」—— 后者更糟。
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise _ExportNotUnderstood(f"文本导出不是 UTF-8：{e}") from e

    out: list[PendingMessage] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.startswith(TEXT_COMMENT):
            continue
        fields = line.split(TEXT_DELIMITER)
        if len(fields) != 4:
            raise _ExportNotUnderstood(
                f"第 {lineno} 行有 {len(fields)} 个字段，应当是 4 个"
                f"（ts / sender / account / content，用 TAB 分隔）"
            )
        ts_raw, sender, account_raw, content = fields
        try:
            ts = int(ts_raw.strip())
        except ValueError as e:
            raise _ExportNotUnderstood(
                f"第 {lineno} 行的 ts={ts_raw!r} 不是整数（要 Unix 秒）"
            ) from e
        account_raw = account_raw.strip()
        if account_raw:
            try:
                account: int | None = int(account_raw)
            except ValueError as e:
                raise _ExportNotUnderstood(
                    f"第 {lineno} 行的 account={account_raw!r} 不是整数"
                    f"（匿名请**留空**，不要写别的字）"
                ) from e
        else:
            account = None
        out.append(
            PendingMessage(
                msg_id=_stable_msg_id(
                    namespace,
                    _content_fingerprint(
                        group_id=group_id, ts=ts, sender=sender, content=content
                    ),
                ),
                group_id=group_id,
                ts=ts,
                sender=sender,
                content=content,
                actor_account=account,
                source=NAME,
            )
        )

    if not out:
        raise _ExportHasNoMessages(
            "这份文本导出里一条消息都没有（是空的导出，不是解析器坏了）"
        )
    return out


# ── 消息库 ──────────────────────────────────────────────────────────


#: 真库那张表的列名 —— 它们是**号码，不是名字**（实测，见下）。
COL_ROW_ID = "40001"
COL_GROUP_INT = "40030"
COL_GROUP_STR = "40021"
COL_TS = "40050"
COL_UID = "40020"
COL_CARD = "40090"
COL_BODY = "40800"

#: 取哪几列、按什么顺序取。※ **不用 `SELECT *`**：真库那张表有 38 列，
#: 全取回来再丢掉 31 列，白读几倍的字节；而列名写在这里一次，
#: 写错一个字符的症状是 `KeyError`（响亮），不是读到一列别的数字（无声）。
LIBRARY_COLUMNS = (
    COL_ROW_ID,
    COL_GROUP_INT,
    COL_GROUP_STR,
    COL_TS,
    COL_UID,
    COL_CARD,
    COL_BODY,
)

#: 段的字段号。
FIELD_SEGMENT = 40800
FIELD_SEG_TYPE = 45002
FIELD_SEG_TEXT = 45101
FIELD_SEG_MENTION = 47423

#: 段的类型 → 记号。**只标已知的**，未知的一律 `[其他]`。
#:
#: ※ 猜错标签比不标更糟：「这条消息里写着 [图片]，而它其实是个文件」，
#: 而下游会把记号当事实读。所以这里只收实测过、能指认的类型
#: （6 万条样本里：文字 51980、表情 12273、图片 4586、`@` 6298、文件 882、
#: 语音 42、视频 129，其余是卡片/小程序/链接一类的结构化段）。
SEGMENT_MARKS = {
    2: "[表情]",
    3: "[文件]",
    4: "[语音]",
    5: "[视频]",
    6: "[文件]",
    8: "[图片]",
    10: "[小程序]",
    14: "[链接]",
    16: "[卡片]",
    17: "[卡片]",
}
#: 认不出类型的段用这个。
UNKNOWN_MARK = "[其他]"
#: 文字段与 `@` 段 —— 这两个要单独处理（一个取正文，一个要填名字）。
SEG_TEXT = 1
SEG_MENTION = 7


def _varint(blob: bytes, at: int) -> tuple[int, int]:
    """读一个 protobuf varint，返回 `(值, 下一个位置)`。"""
    value = 0
    shift = 0
    while at < len(blob):
        byte = blob[at]
        at += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, at
        shift += 7
        if shift > 63:
            break
    raise ValueError("varint 读到头了")


def fields(blob: bytes | None) -> list[tuple[int, int, Any]]:
    """把一个 protobuf 消息摊成 `[(字段号, 线类型, 值)]`。

    ※ 只认 varint（0）与长度分隔（2）两种线类型 —— 另外两种在 QQ 的消息体里
    没出现过，而**读不懂就停**比猜下去好：猜下去的症状是「读出一个看起来正常
    但错位的字段」。

    ※ 值：varint 是 `int`，长度分隔是 `bytes`。
    """
    if not blob:
        return []
    out: list[tuple[int, int, Any]] = []
    at = 0
    while at < len(blob):
        try:
            tag, at = _varint(blob, at)
        except ValueError:
            break
        field_no, wire = tag >> 3, tag & 7
        if field_no == 0:
            break
        if wire == 0:
            try:
                value, at = _varint(blob, at)
            except ValueError:
                break
            out.append((field_no, wire, value))
        elif wire == 2:
            try:
                length, at = _varint(blob, at)
            except ValueError:
                break
            if length < 0 or at + length > len(blob):
                break
            out.append((field_no, wire, blob[at : at + length]))
            at += length
        else:
            break
    return out


def compose_content(body: bytes | None) -> str:
    """一段消息体 → 正文。**一个文字段都没有 ⇒ 空串**（调用方据此跳过）。

    ※ 段按**原顺序**拼，不排序、不合并：段序就是显示序，而正文里「先图后字」
    与「先字后图」读起来不一样。段之间不插换行 —— 一条消息在屏幕上是连着读的。
    """
    parts: list[str] = []
    for field_no, wire, value in fields(body):
        if field_no != FIELD_SEGMENT or wire != 2 or not isinstance(value, bytes):
            continue
        seg_type = 0
        text = ""
        mention = ""
        for seg_field, seg_wire, seg_value in fields(value):
            if seg_field == FIELD_SEG_TYPE and seg_wire == 0:
                seg_type = int(seg_value)
            elif seg_field == FIELD_SEG_TEXT and seg_wire == 2:
                text = seg_value.decode("utf-8", "replace")
            elif seg_field == FIELD_SEG_MENTION and seg_wire == 2:
                mention = seg_value.decode("utf-8", "replace")
        if seg_type == SEG_TEXT and text:
            parts.append(text)
        elif seg_type == SEG_MENTION:
            parts.append(f"[@{mention}]" if mention else "[@]")
        else:
            parts.append(SEGMENT_MARKS.get(seg_type, UNKNOWN_MARK))
    return "".join(parts).strip()


def parse_library(
    rows: Iterable[Mapping[str, Any]],
    *,
    namespace: str,
    group_id: int | None,
    accounts: Mapping[str, int] | None = None,
    names: Mapping[str, str] | None = None,
) -> list[PendingMessage]:
    """把消息库里的行变成消息。认的是**真库那套数字列名**。

    ## 为什么现在是数字列名（上一版是自编的名字）

    上一版认的是 `msg_id` / `ts` / `sender` / `content` 这种名字，理由写在当时
    的 docstring 里：「本机那份库是加密的，列名看不到，所以**不猜**」。

    那条理由**已经不成立了** —— 钥匙取到之后，真库看得很清楚。而它当时的实际
    后果是：**这个源读不了任何一份真的 QQ 库**，只能读一份按自编名字造的库。
    ⇒ 现在认真的：

    | 列 | 是什么 | 实测依据 |
    |---|---|---|
    | `40001` | 消息 id，**全表唯一** | 491388 行 / 491388 个不同值 |
    | `40030` | 群号（整数） | 有 37606 行的它是 0，那一批回落 `40021` |
    | `40021` | 群号（字符串形态） | 与 `40030` 对得上时用它兜底 |
    | `40050` | 发送时刻，**Unix 秒** | 最大值落在「今天」 |
    | `40020` | 发送者 uid（`u_` 开头的 22 字符串，**不是** QQ 号） | |
    | `40090` | 发送者的**群名片** | 44% 的行它是空串 |
    | `40800` | 消息体，protobuf | 见 `compose_content` |

    ## 账号从哪儿来

    `40020` 是**字符串** uid，而 `actor_account` 是**整数**契约。把那个串哈希成
    一个整数是**编一个不存在的账号** —— 那个数字会被下游当成一个真实的人，
    而人物筛选会拿它去比。⇒ 真号从 `profile_info.db` 的 `profile_info_v6`
    取（列 `1000` 是 uid、`1002` 是 QQ 号），由调用方查好传进 `accounts`。
    **查不到的那 5% 如实置 `None`**：「这个源不知道它的账号」与「它的账号是 0」
    是两件事，而后者会被当成一个真号。

    ## 两条归一化

      · **纯非文字的消息不进这个源**（实测：4 千条样本里 1272 条一个文字段都
        没有）。它们没有可读正文，而下游要的是文本 —— 与 `web_parse` 跳过
        「不是通知的 li」同一条判据：结构告诉你的，不是失败。
      · **超预算的长正文在这里切**（实测：中位 23 字、p99 是 144 字，但有 20 条
        超过 4500 字，最长 10666）。下游是按**条数**切批的，一条一万字的正文
        独占一批，而端点会**静默截断**。切分复用会议源那一个函数，预算只有一处。

    ※ `group_id` 参数给定时**以它为准**（配置文件里写的那个）；不给就用**行里
    那个群号** —— 一个账号里有 51 个群（实测），全都塞进一个 group_id 会让它们
    在日报里互相串上下文。
    """
    accounts = accounts or {}
    names = names or {}
    out: list[PendingMessage] = []
    for row in rows:
        missing = [c for c in LIBRARY_COLUMNS[:4] if c not in row.keys()]
        if missing:
            raise _LibraryNotUnderstood(
                f"消息库那张表里缺这些列：{'、'.join(missing)}。\n"
                f"※ 认的列名见 qq_parse.LIBRARY_COLUMNS —— 它们是 QQ 自己那套"
                f"**数字列名**，不是自编的名字。"
            )
        content = compose_content(row[COL_BODY])
        if not content:
            continue
        gid = group_id if group_id is not None else _group_of(row)
        uid = str(row[COL_UID] or "")
        parts = _split(content)
        for index, text in enumerate(parts):
            # ※ **没切的时候原文里不带 `:0`** —— 导出那条路用的是裸的 id，
            #   两条路必须算出同一批 `msg_id`（测试钉着这条）。带了的话，
            #   同一批数据走导出与走库会得到两套 id，于是重跑摄取把整个源
            #   重写一遍，而没有任何东西会报错。
            raw_id = (
                str(row[COL_ROW_ID]) if len(parts) == 1 else f"{row[COL_ROW_ID]}:{index}"
            )
            out.append(
                PendingMessage(
                    msg_id=_stable_msg_id(namespace, raw_id),
                    group_id=gid,
                    ts=int(row[COL_TS]),
                    sender=_sender_of(row, uid, names),
                    content=text,
                    actor_account=accounts.get(uid),
                    source=NAME,
                )
            )

    if not out:
        raise _LibraryHasNoMessages(
            "消息库那张表里一条消息都没有（是空的库，不是解析器坏了）"
        )
    return out


def _split(content: str) -> list[str]:
    """超预算才切。切分预算与会议源**同一处**（见模块 docstring）。"""
    if len(content) <= EFFECTIVE_BUDGET_CHARS:
        return [content]
    return [part.text for part in split_text(content, budget=EFFECTIVE_BUDGET_CHARS)]


def _group_of(row: Mapping[str, Any]) -> int:
    """这一行属于哪个群。整数群号优先，为 0 才回落到字符串那一列。

    ※ 回落是必要的：实测 491388 行里有 37606 行的 `40030` 是 0，而它们的
    `40021` 有值。不回落的症状是「三万多条消息全挤进『群 0』」。
    """
    raw = row[COL_GROUP_INT]
    if raw:
        return int(raw)
    try:
        return int(str(row[COL_GROUP_STR] or "").strip() or 0)
    except ValueError:
        return 0


def _sender_of(row: Mapping[str, Any], uid: str, names: Mapping[str, str]) -> str:
    """这条消息的发送者显示名。

    ※ 顺序：**群名片**（`40090`，群里显示的那个名字，常带「22-3班学习委员」
    这类角色前缀）> 资料库里的昵称 > 空串。名片优先是因为下游拿 `sender`
    判断「这条是不是官方发的」，而群名片正是那个判断的依据
    （见 `domain/models.py::PendingMessage` 对 `sender` 的说明）。
    """
    return str(row[COL_CARD] or "").strip() or names.get(uid, "")
