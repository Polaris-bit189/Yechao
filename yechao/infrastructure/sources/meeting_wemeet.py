"""腾讯会议客户端**在本机留下的会议历史** —— 自动，零操作。

## 这一层要解决的是「用户不用做任何事」

观察到的现象：如果「读会议」的前提是「用户先把转写导出成文件」，那这个源在
真实使用里等于不存在 —— 没有人每天记得去点一次导出。而客户端**本来就会在
本机写东西**：一场会开完，它把这场会记进自己的本地库。⇒ 源应当去读那个。

## 实测到的落盘位置与形状（Windows，2026-10）

| 东西 | 位置 | 形状 |
|---|---|---|
| **会议历史** | `%APPDATA%\\Tencent\\WeMeet\\Global\\Database\\<hash>.db` | **明文 SQLite**，表 `historical_meetings_new` |
| 即将开始的会 | 同一目录下另一个库的 `home_meeting_list_table` | 明文 SQLite |
| 会内聊天 / 文件 | `...\\Global\\Database\\im_db\\<uid>\\message.db`、`files.db` | **加密**（文件头不是 `SQLite format 3\\0`） |
| 客户端配置 | `...\\Global\\Preferences\\*.tv` | 私有二进制（tkv） |
| 本地录制 | 用户配置的目录（客户端默认 `文档\\WeMeet\\<会议号>`） | mp4 / m4a，**不是文本** |

### 库名为什么不能写死

`<hash>.db` 那个文件名是安装时算出来的，**每台机器不一样**（实测：本机是
`7b7673502ee316661967871a748564f4.db`）。⇒ 判据不是「打开哪个文件」，而是
「哪个库里有那张表」—— 扫一遍目录，按表名认。这样换台机器、换个版本都还在。

### 这里读到的**不是逐字稿**，这件事要说清楚

`historical_meetings_new` 有 55 列，其中与「会议内容」有关的几列全是**指针**：

  · `record_file_id` / `uni_record_id` / `record_short_jump_url` /
    `record_complete_jump_url` —— 指向**云端**的录制与转写
  · `has_ai_summary` / `ai_summary_num` / `record_ai_summarize` —— 智能纪要
    的**计数与摘要字段**（本机实测全为 0）

也就是说：**转写正文不在本机**。客户端本地留的是「这场会存在过」这件事
（主题、起止时刻、时长、发起人、参会人数），而逐字稿在腾讯云端，且客户端
自带的「智能录制（章节 / 发言人 / 纪要 / 话题）」是**会员功能**
（本机 `DynamicResource/.../ai_record_exp_config/config_zh.json` 的原文可证）。

⇒ 所以本模块产出的是**会议这个实体**的一条自述，不是切分后的转写。
「一场会一条消息」在这里是天然成立的，不需要按说话人轮次切 —— 但**切分
照旧走 `meeting_parse.split_text`**：预算与时间窗口的判据不该因为正文从哪来
而变，而且正文长度不由我们决定。

### 钉钉：本机没装，无法实测

`%APPDATA%` / `%LOCALAPPDATA%` / `Program Files` / `Program Files (x86)` 与
注册表卸载表里都没有钉钉（实测）。所以这里**没有**钉钉的实现 —— 凭空写一个
「照着猜的结构」去读它，比不写更糟：它会以「读到了」的样子产出错东西。
钉钉那一侧要走的路与腾讯会议同一形状（找到客户端的本地库 → 按表名认），
**接手的下一个人拿到一台装了钉钉的机器就能照这个模块的判据补上**。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from yechao.domain.days import DEFAULT_TZ_OFFSET
from yechao.domain.errors import SourceUnavailable
from yechao.domain.models import PendingMessage
from yechao.infrastructure.sources.meeting_parse import (
    EFFECTIVE_BUDGET_CHARS,
    NAME,
    _NoMeetingsHere,
    _NotUnderstood,
    group_id_for,
    meeting_message_id,
    split_text,
)

#: 客户端的数据根，相对 `%APPDATA%`。
APP_DIR_PARTS = ("Tencent", "WeMeet")
#: 会议历史库所在的子目录，相对数据根。
STORE_DIR_PARTS = ("Global", "Database")

#: 会议历史那张表。
#:
#: ※ **实测**得到的名字（本机客户端版本）。列名同样实测 —— 见
#: `REQUIRED_COLUMNS` 与 `OPTIONAL_COLUMNS` 的分工：**缺了必需的列 ⇒ 抛**
#: （客户端改版了），缺了可选的列只是少一行信息。
MEETINGS_TABLE = "historical_meetings_new"

#: 缺了就报错的列 —— 它们是「一条会议记录」的最小骨架。
REQUIRED_COLUMNS = ("meeting_code", "meeting_id", "meeting_subject", "meeting_begin_time")

#: 有就用、没有就略过的列。
OPTIONAL_COLUMNS = (
    "meeting_end_time",
    "meeting_total_elapsed_time",
    "creator_nickname",
    "participants_count",
)

#: `meeting_begin_time` 的合法区间（Unix 秒）。
#:
#: ※ 定这个区间不是为了「校验数据」，而是为了**在单位变了的时候响**：
#: 实测这个列是**秒**（`1791698400` → 2026-10-11）。若哪天客户端改成毫秒，
#: 直接算出来的时刻会落到五万年后 —— 那会让整场会跑出日报的视野，而且
#: **不报错**。所以这里划一条线：越界 ⇒ 抛，不猜单位。
MIN_PLAUSIBLE_TS = 946_684_800      # 2000-01-01
MAX_PLAUSIBLE_TS = 4_102_444_800    # 2100-01-01


def app_data_dir() -> Path | None:
    """`%APPDATA%`。拿不到返回 `None`（非 Windows / 环境被剥掉时）。"""
    raw = os.environ.get("APPDATA")
    return Path(raw) if raw else None


def wemeet_root() -> Path | None:
    """腾讯会议的数据根。**不存在就返回 `None`**（没装客户端）。"""
    base = app_data_dir()
    if base is None:
        return None
    root = base.joinpath(*APP_DIR_PARTS)
    return root if root.is_dir() else None


def find_meeting_store(root: Path) -> Path | None:
    """在客户端的数据目录里找**带会议历史表**的那个库。找不到返回 `None`。

    ※ 按**表名**认库，不按文件名（见模块 docstring「库名为什么不能写死」）。
    打不开的库（加密的 IM 库就是这一种）直接跳过 —— 它们不是会议历史，
    跳过它们不算「静默吞掉一个错误」：判定标准是**表在不在**，而加密库连
    `sqlite_master` 都读不出来，它本来就不可能是答案。
    """
    store_dir = root.joinpath(*STORE_DIR_PARTS)
    if not store_dir.is_dir():
        return None
    for candidate in sorted(store_dir.glob("*.db")):
        try:
            con = sqlite3.connect(candidate.as_uri() + "?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            hit = con.execute(
                "select 1 from sqlite_master where type='table' and name=?",
                (MEETINGS_TABLE,),
            ).fetchone()
        except sqlite3.Error:
            continue
        finally:
            con.close()
        if hit is not None:
            return candidate
    return None


def _fmt_ts(ts: int) -> str:
    """Unix 秒 → 会议本地时间的墙上时刻。

    ※ 时区取**全项目那一处口径**（`domain.days.DEFAULT_TZ_OFFSET`），不取机器
    本地时区 —— 取机器本地的话同一份数据在评委机器上会显示成另一天。
    """
    local = datetime.fromtimestamp(ts + DEFAULT_TZ_OFFSET, tz=timezone.utc)
    return local.strftime("%Y-%m-%d %H:%M")


def _plausible_ts(raw: object, *, column: str) -> int:
    if not isinstance(raw, int) or not (MIN_PLAUSIBLE_TS <= raw <= MAX_PLAUSIBLE_TS):
        raise _NotUnderstood(
            f"会议历史里 {column} 的值 {raw!r} 不是一个合理的 Unix 秒 —— "
            f"这个列的单位变了（客户端改版？）。**不猜单位**：按毫秒算出来的"
            f"时刻会落到五万年后，那会让整场会跑出日报的视野，而且不报错。",
            column=column,
        )
    return raw


def _compose(row: dict[str, object]) -> str:
    """一场会 → 一条自洽的短文本。

    ※ **不知道的字段就省略**，不填「无」也不填 0：
      · `participants_count` 为 0 时它的含义是「客户端没记」，不是「没人参会」；
      · `meeting_end_time` 早于开始时刻的（会没开完 / 字段没写）省略结束与时长。
    这与 `web_parse` 对 `data-department` 的处置是同一条判据：宁可承认
    「这个源没自报这一项」，也不编一个值出来 —— 编出来的值会被下游当成事实。
    """
    lines: list[str] = []
    subject = str(row.get("meeting_subject") or "").strip()
    if subject:
        lines.append(f"会议主题：{subject}")

    begin = int(row["meeting_begin_time"])  # type: ignore[arg-type]
    lines.append(f"开始：{_fmt_ts(begin)}")

    end = row.get("meeting_end_time")
    if isinstance(end, int) and begin < end <= MAX_PLAUSIBLE_TS:
        lines.append(f"结束：{_fmt_ts(end)}")

    elapsed = row.get("meeting_total_elapsed_time")
    if isinstance(elapsed, int) and elapsed > 0:
        lines.append(f"时长：{max(1, round(elapsed / 60))} 分钟")

    creator = str(row.get("creator_nickname") or "").strip()
    if creator:
        lines.append(f"发起人：{creator}")

    count = row.get("participants_count")
    if isinstance(count, int) and count > 0:
        lines.append(f"参会人数：{count}")

    # ※ 刻意**不**写会议号：它是加入会议的凭据，对日报没有信息价值，
    # 而它会进正文、被脱敏器当号码处理 —— 加它只多一处噪声。
    return "\n".join(lines)


def _meeting_key(row: dict[str, object]) -> str:
    """一场会的命名空间 —— 同一场会的两次开法要落到同一个「群」里。"""
    code = str(row.get("meeting_code") or "").strip()
    if code:
        return f"wemeet:{code}"
    return f"wemeet:{row.get('meeting_id')}"


def read_meetings(
    store: Path, *, budget: int = EFFECTIVE_BUDGET_CHARS
) -> list[PendingMessage]:
    """读一个会议历史库，一场会一条消息。

    抛：
      · `SourceUnavailable` —— 库打不开（被独占锁住 / 权限不够）
      · `_NotUnderstood`    —— 表在但列对不上（客户端改版），或时间戳不像 Unix 秒
      · `_NoMeetingsHere`   —— 表读懂了，但里面一场会都没有
    """
    try:
        con = sqlite3.connect(store.as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise SourceUnavailable(
            f"腾讯会议的会议历史库打不开：{store}（{e}）。"
            f"（客户端正在写它时可能独占 —— 等它闲下来再跑。）",
            path=str(store),
        ) from e

    try:
        columns = [r[1] for r in con.execute(f'pragma table_info("{MEETINGS_TABLE}")')]
        if not columns:
            raise _NotUnderstood(
                f"{store} 里找不到表 {MEETINGS_TABLE} —— 找库的时候它在，"
                f"读的时候不在了？",
                path=str(store),
            )
        missing = [c for c in REQUIRED_COLUMNS if c not in columns]
        if missing:
            raise _NotUnderstood(
                f"{store} 的表 {MEETINGS_TABLE} 少了必需的列 {missing} —— "
                f"客户端改版了，解析器要跟着改。**不猜**：猜出来的映射会把"
                f"另一列的值当成会议时间，而它不报错。",
                path=str(store),
                missing=",".join(missing),
            )

        wanted = [c for c in REQUIRED_COLUMNS + OPTIONAL_COLUMNS if c in columns]
        try:
            rows = con.execute(
                f'select {",".join(wanted)} from "{MEETINGS_TABLE}"'
                f" order by meeting_begin_time"
            ).fetchall()
        except sqlite3.Error as e:
            raise _NotUnderstood(
                f"{store} 的 {MEETINGS_TABLE} 读不出来：{e}", path=str(store)
            ) from e
    finally:
        con.close()

    if not rows:
        raise _NoMeetingsHere(
            f"{store} 的 {MEETINGS_TABLE} 读懂了，结构对得上，但里面一场会都没有。"
            f"**这是「读懂了、本机确实还没有会议记录」**，不是解析器坏了 —— "
            f"要去查的是客户端里是不是真的没开过会。"
        )

    out: list[PendingMessage] = []
    for raw in rows:
        row = dict(zip(wanted, raw))
        begin = _plausible_ts(row.get("meeting_begin_time"), column="meeting_begin_time")
        namespace = _meeting_key(row)
        group_id = group_id_for(namespace)
        sender = str(row.get("creator_nickname") or "").strip()
        # ※ 同一次会的两次开法共用命名空间（同一个「群」），但**键不同**：
        # 键里带上这次开始的时刻，于是两场不同的会各自是一条消息，
        # 而重跑摄取落回同一批 id。
        for i, part in enumerate(split_text(_compose(row), budget=budget)):
            out.append(
                PendingMessage(
                    msg_id=meeting_message_id(namespace, f"{begin}|{i}"),
                    group_id=group_id,
                    ts=begin,
                    sender=sender,
                    content=part.text,
                    # 会议历史里没有账号可言 ⇒ None（用 0 冒充会被人物筛选命中）。
                    actor_account=None,
                    source=NAME,
                )
            )
    return out
