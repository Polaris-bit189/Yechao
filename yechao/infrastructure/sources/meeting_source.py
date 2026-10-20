"""会议源 —— 读**本机**的会议转写与会议记录，把它们变成消息。

## 「自动」是这一层的硬要求，不是加分项

判据：如果一个源的使用前提是「用户先手动导出文件」，它在真实使用里等于不存在
—— 没有人每天记得去点一次导出，而**漏掉的会没有任何信号**（日报看起来只是
「今天很安静」）。

⇒ 所以输入不是「一个用户必须填的目录」，而是**一条发现链**：

```
1. YECHAO_MEETING_DIR              ← 这一次的意图（临时换一个目录看看）
2. <config>/sources.toml 的 [meeting] dir  ← 长期的意图
3. 自动探测（下面那三步）           ← **用户零操作**的那一条
4. 都没有 ⇒ 抛 ConfigError，并把探测过的位置逐个列出来
```

前两步与 `web_source` 的优先级同一条判据：环境变量是**这一次**的意图，
配置文件是**长期**的意图，临时的不该被长期的覆盖掉。

## 自动探测的三步（判据是「哪个位置**真的有东西可读**」）

| 顺序 | 位置 | 认它的条件 | 读到的是 |
|---|---|---|---|
| 1 | `%USERPROFILE%\\Documents\\WeMeet` | 里面有 `.txt` / `.srt` / `.vtt` | 转写文件（要切） |
| 2 | `%APPDATA%\\Tencent\\WeMeet` 的会议历史库 | 库里有 `historical_meetings_new` 表 | 会议实体（一场会一条） |
| 3 | `%APPDATA%\\DingTalk` / `%LOCALAPPDATA%\\DingTalk` | 目录存在 | 转写文件（要切） |

※ 为什么第 1 步要检查「里面有没有转写文件」而不是「目录在不在」：腾讯会议的
默认本地录制目录就在那儿，而它**放的是 mp4 / m4a，不是文本**。只看目录在不在
的话，一台从没导出过任何东西的机器会稳定地选中一个**永远读不出东西**的位置，
而第 2 步（本机会议历史，真的读得到东西）永远轮不到 —— 那种失败的样子是
「这个源一直是空的」，没有任何东西会报错。

## 三个「读不到」的位置各自卡在哪（实测，2026-10，Windows）

  · **转写正文不在本机。** 腾讯会议把逐字稿放在**云端**，客户端本地只留指针
    （`record_file_id` / `uni_record_id` / `record_*_jump_url`）与计数。客户端
    自带的「智能录制（章节 / 发言人 / 纪要 / 话题）」还是**会员功能**。
    ⇒ 本机能自动拿到的只有**会议这个实体**（主题、起止、时长、发起人、人数），
    逐字稿拿不到 —— 见 `meeting_wemeet.py`。
  · **会内聊天是加密的。** `...\\Global\\Database\\im_db\\<uid>\\message.db` 与
    `files.db` 的文件头不是 `SQLite format 3\\0`（实测前 16 字节是随机字节）。
    那是会内聊天与文件，本来也不是转写；但要读它得先解 SQLCipher，**没有密钥
    这层就到此为止**。
  · **钉钉本机没装。** 所以钉钉那一侧这里只做探测、不做解析 —— 凭空按猜的
    结构去读它，比不读更糟：它会以「读到了」的样子产出错东西。钉钉的导出形态
    由 `meeting_parse.py` 认（`.txt` 的第二种形态），落盘位置由这里探测。

## 输入是**一个目录**（或本机的一个库），不是「一个文件」

「自动」的第二层意思：目录里**新出现的文件，下一次 `ingest` 就被吃进来**。
不需要用户改代码、不需要重启、不需要登记。`read()` 每次现扫目录。

※ **本模块不 import `bootstrap`**：`bootstrap.py` 在模块级 import 本包，
反过来引会造出导入环（`tests/contracts/test_layering.py::test_没有导入环`）。
配置目录的默认值因此从 `_common.py` 来，与组合根那处**必须一致**。

## ※ 类名为什么带下划线

与 `web_source._WebSource` 同一个理由：
`tests/contracts/test_layering.py::test_适配层的实现都接上了线` 扫的是**适配层
顶层公开类名**有没有在组合根源码里出现过，而数据源不由组合根实例化（它由
`sources/__init__.py` 的注册表组合）。下划线前缀走那条判据自己留的口子。
**这不是绕过护栏**：本类确实被接上了线（`REGISTRY["meeting"]`）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yechao.domain.errors import (
    ConfigError,
    SourceFormatError,
    SourceUnavailable,
)
import yechao.infrastructure.sources.meeting_wemeet as meeting_wemeet
from yechao.infrastructure.sources._common import (
    #: 配置目录的环境变量名。**必须与 `bootstrap.Settings.from_env` 一致**。
    CONFIG_DIR_ENV,
    #: 配置目录的默认名。**必须与 `bootstrap.DEFAULT_CONFIG_DIR` 一致**。
    DEFAULT_CONFIG_DIR,
    #: 数据源的配置文件。与 `groups.toml` 等并列放在配置目录下。
    SOURCES_FILE,
    config_dir as _config_dir,
    in_window,
    source_table,
)
from yechao.infrastructure.sources.meeting_parse import (
    #: 源的标识。它在解析模块里定义（每条消息的 `source` 字段用的就是它）。
    NAME,
    #: 认的转写后缀。
    TRANSCRIPT_SUFFIXES,
    #: 「读懂了、这里确实没有会议内容」—— 与「没读懂」必须分得开。
    _NoMeetingsHere,
    #: 一场会的 `group_id`（= 下游视角里的一个「群」）。
    group_id_for,
    #: 侧车 / 文件头里的墙上时间 → Unix 秒。与解析模块**同一个**解析。
    parse_wall_clock,
    parse_transcript,
)

#: 输入目录的来源。
DIR_ENV = "YECHAO_MEETING_DIR"

#: 侧车的后缀。与 `web_source` 的夹具侧车**同名**。
#:
#: ※ 两处各写一遍是这一轮的硬约束造成的（`_common.py` 锁住了）。它必须与
#: `web_fetch.META_SUFFIX` 一致 —— 漂移的症状是「同一份素材，一个源认得它的
#: 侧车、另一个认不得」。
META_SUFFIX = ".meta.json"


# ── 输入 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Input:
    """已经定下来的输入：**从哪儿读**、**它是哪一种**、**为什么是它**。"""

    kind: str  # "dir" / "wemeet"
    path: Path
    where: str  # 这个选择是从哪儿来的（给报错与说明用）


def _settings_table() -> dict[str, Any]:
    """读 `<config_dir>/sources.toml` 的 `[meeting]` 表。没有就返回 `{}`。

    ※ 读法在 `_common.source_table` 里（网页源读的是同一份文件的 `[web]` 表，
    两边必须按同一套规矩读）。这里只负责给出「`[meeting]` 该长什么样」。
    """
    return source_table(
        "meeting",
        hint='  [meeting]\n  dir = "C:/Users/你/Documents/WeMeet"',
    )


def _documents_dir() -> Path | None:
    """用户的「文档」目录。拿不到返回 `None`。

    ※ 也认 `%OneDrive%\\Documents`：企业/教育版 Windows 常把「文档」重定向到
    OneDrive，而客户端跟着走。不认它的话，一台重定向过的机器上第 1 步会
    永远探测不到 —— 症状是「它老是去读会议历史，不读我的转写」。
    """
    for env, tail in (("USERPROFILE", "Documents"), ("OneDrive", "Documents")):
        base = os.environ.get(env)
        if base:
            candidate = Path(base) / tail
            if candidate.is_dir():
                return candidate
    return None


def _has_transcripts(path: Path) -> bool:
    """这个目录里有没有认得出的转写文件。"""
    try:
        return any(
            f.is_file() and f.suffix.lower() in TRANSCRIPT_SUFFIXES
            for f in path.iterdir()
        )
    except OSError:
        return False


def _auto_input() -> _Input | None:
    """**用户零操作**的那一步。探测顺序与判据见模块 docstring 的表。"""
    documents = _documents_dir()
    if documents is not None:
        recording_dir = documents / "WeMeet"
        if _has_transcripts(recording_dir):
            return _Input(
                kind="dir",
                path=recording_dir,
                where=f"自动探测：{recording_dir}（腾讯会议落盘目录里有转写文件）",
            )

    root = meeting_wemeet.wemeet_root()
    if root is not None:
        store = meeting_wemeet.find_meeting_store(root)
        if store is not None:
            return _Input(
                kind="wemeet",
                path=store,
                where=f"自动探测：{store}（腾讯会议本机的会议历史库）",
            )

    for env in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        dingtalk = Path(base) / "DingTalk"
        if dingtalk.is_dir():
            return _Input(
                kind="dir",
                path=dingtalk,
                where=f"自动探测：{dingtalk}（钉钉的数据目录）",
            )
    return None


def resolve_input() -> _Input:
    """把「从哪儿读」定下来。**没配又探测不到就抛，不兜底。**

    ※ 不兜底的理由与 `web_source.resolve_target` 一字不差：兜底到某个位置会让
    `--source meeting` 在什么都没配的情况下「成功」读到别人的东西，而报告
    看起来一切正常。
    """
    table = _settings_table()
    from_env = (os.environ.get(DIR_ENV) or "").strip()
    if from_env:
        return _Input(kind="dir", path=Path(from_env), where=DIR_ENV)

    from_config = str(table.get("dir") or "").strip()
    if from_config:
        return _Input(
            kind="dir", path=Path(from_config), where=f"{SOURCES_FILE} 的 [meeting] dir"
        )

    auto = _auto_input()
    if auto is not None:
        return auto

    documents = _documents_dir()
    probed = [
        str(documents / "WeMeet") if documents else "%USERPROFILE%\\Documents\\WeMeet",
        str(meeting_wemeet.app_data_dir() or "%APPDATA%") + "\\Tencent\\WeMeet",
        "%APPDATA%\\DingTalk",
        "%LOCALAPPDATA%\\DingTalk",
    ]
    raise ConfigError(
        "会议源不知道要读哪儿，自动探测也没在本机找到任何会议落盘位置。"
        "探测过这些地方：\n"
        + "\n".join(f"  · {p}" for p in probed)
        + "\n二选一：\n"
        f"  · 环境变量：{DIR_ENV}=<一个放转写文件的目录>\n"
        f"  · 配置文件：{_config_dir() / SOURCES_FILE} 里写\n"
        f'      [meeting]\n      dir = "一个放转写文件的目录"\n'
        "※ 刻意**没有默认目录**：默认到一个位置上，`--source meeting` 就会在"
        "什么都没配的情况下「成功」读到别的东西，而报告看起来一切正常。"
    )


# ── 目录那一侧 ──────────────────────────────────────────────────────


def _sidecar_anchor(path: Path) -> int | None:
    """同名侧车 `<名>.meta.json` 里的 `started_at`。没有返回 `None`。

    ※ 侧车只给**字幕**（`.srt` / `.vtt`）用：那两种格式里根本没有放会议开始
    时刻的地方。txt 形态自己带文件头，用不上它（但侧车仍然压过文件头 ——
    显式的意图压过隐式的默认，见 `meeting_parse` 模块 docstring）。
    """
    meta_path = path.with_suffix(META_SUFFIX)
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SourceFormatError(
            f"会议侧车 {meta_path.name} 读不出来：{e}。"
            f"侧车是「这场会什么时候开始」的唯一答案（字幕格式自己不带日期），"
            f"它坏了就**不能**猜 —— 猜出来的开始时刻会把整场会放进错误的一天。",
            meta=str(meta_path),
        ) from e
    if not isinstance(meta, dict):
        raise SourceFormatError(
            f"会议侧车 {meta_path.name} 不是一个 JSON 对象。", meta=str(meta_path)
        )
    raw = str(meta.get("started_at") or "").strip()
    if not raw:
        return None
    return parse_wall_clock(raw)


def _transcript_files(directory: Path) -> list[Path]:
    try:
        entries = list(directory.iterdir())
    except OSError as e:
        raise SourceUnavailable(
            f"会议目录读不到：{directory}（{e}）", path=str(directory)
        ) from e
    return sorted(
        (f for f in entries if f.is_file() and f.suffix.lower() in TRANSCRIPT_SUFFIXES),
        key=lambda f: f.name,
    )


def _read_directory(directory: Path, *, since: int | None, until: int | None) -> list[Any]:
    if not directory.is_dir():
        raise SourceUnavailable(
            f"会议目录不存在：{directory}。"
            f"（它要么被删了、要么盘没挂上 —— 这不是「今天没有会」。）",
            path=str(directory),
        )

    files = _transcript_files(directory)
    if not files:
        raise _NoMeetingsHere(
            f"{directory} 里一个转写文件都没有（认的后缀："
            f"{'、'.join(TRANSCRIPT_SUFFIXES)}）。"
            f"**这是「读懂了、这个目录确实没有会议」**，不是解析器坏了 —— "
            f"要去查的是目录选对没有。"
        )

    messages: list[Any] = []
    seen: dict[int, str] = {}
    for path in files:
        try:
            raw = path.read_bytes()
        except OSError as e:
            raise SourceUnavailable(
                f"转写文件读不到：{path}（{e}）", path=str(path)
            ) from e

        # 命名空间 = 文件名（去掉后缀）。见 `meeting_parse` 模块 docstring：
        # 「一场会 = 一个群」，而这场会的身份就是它的文件名 —— 这是用户手上
        # 唯一稳定、可控的那件事。
        namespace = f"meeting:{path.stem}"
        group_id = group_id_for(namespace)
        previous = seen.get(group_id)
        if previous is not None and previous != namespace:
            # 两个不同的会议名撞进同一个群号 ⇒ 它们会被切进同一批，模型会把
            # 一场会的上下文套到另一场（`chunking.py` 的第一条硬约束）。
            # 概率极低，但**撞了必须响**：静默合并的症状是「日报里两场会的内容
            # 混在一起」，而它看起来完全正常。
            raise SourceFormatError(
                f"两场不同的会议算出了同一个群号 {group_id}：{previous} 与 {namespace}。"
                f"它们会被切进同一批，模型会把一场会的上下文套到另一场 —— "
                f"请把其中一个转写文件改个名。",
                group_id=group_id,
            )
        seen[group_id] = namespace

        try:
            messages.extend(
                parse_transcript(
                    raw,
                    suffix=path.suffix.lower(),
                    namespace=namespace,
                    group_id=group_id,
                    anchor_ts=_sidecar_anchor(path),
                )
            )
        except SourceFormatError as e:
            # ※ 带上文件名再抛：一份目录里可能有几十个转写，「有一份读不懂」
            # 必须能直接定位到是哪一份。
            raise type(e)(f"转写文件 {path.name}：{e}") from e

    return [m for m in messages if in_window(m.ts, since, until)]


# ── 源 ──────────────────────────────────────────────────────────────


class _MeetingSource:
    """会议源。实现 `application/ports.py::MessageSource`。

    ※ 它**切**消息（与网页源、邮件源相反）—— 一段几万字的转写当成一条消息，
    下游那一批就只有它一条，而端点会静默截断。切分的判据全在
    `meeting_parse.py`（说话人轮次 + 时间窗口），本类只负责「从哪儿读」。

    ※ 输入在**装配时**定下来（由 `build_meeting_source` 传进来），不是每次
    `read()` 现查环境变量。理由与 `web_source` 相同：报错的位置。见
    `build_meeting_source`。
    """

    name = NAME

    def __init__(self, source_input: _Input) -> None:
        self._input = source_input

    @property
    def where(self) -> str:
        """这次读的是哪儿、为什么是它。给报错与测试用。"""
        return self._input.where

    def read(
        self, *, since: int | None = None, until: int | None = None
    ) -> list[Any]:
        """读一遍。抛（四条都是**响亮**的，没有一条是静默返回空）：

          · `ConfigError`       —— 没配、也探测不到（装配时抛，见工厂）
          · `SourceUnavailable` —— 目录 / 库读不到（不存在、没权限、被锁）
          · `_NotUnderstood`    —— 读到了但没读懂（形态 / 编码 / 时间戳 / 改版）
          · `_NoMeetingsHere`   —— 读懂了，但这里确实没有会议内容

        ※ **时间窗在解析之后才过**。这不是实现细节，是判据：「解析出 0 条」与
        「这段时间里没有会」是两件事 —— 前者是格式问题（去改解析器），后者是
        数据问题（这段时间真的安静）。先解析、再过滤，两者就分得开。
        """
        if self._input.kind == "wemeet":
            messages = meeting_wemeet.read_meetings(self._input.path)
            return [m for m in messages if in_window(m.ts, since, until)]
        return _read_directory(self._input.path, since=since, until=until)


def build_meeting_source() -> _MeetingSource:
    """注册表用的工厂（`REGISTRY["meeting"]`）。

    ※ 它在**装配的时候**就把输入定下来，而不是等 `read()` 时再查。

    判据在退出码上：`interfaces/cli.py` 把 `ConfigError` 归在「用法错」
    （退出码 2，下一步是去改配置/命令行）而把「跑起来之后才失败」归在 1。
    在这里报出来还有一个好处：它发生在**任何一个源开始读之前** —— 要是等
    `read()` 才报，`--source synth,meeting` 会先把合成语料读完并入库，然后才
    说会议源没配，一半的工作已经做完了。
    """
    return _MeetingSource(resolve_input())
