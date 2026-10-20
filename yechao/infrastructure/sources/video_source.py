"""视频源 —— 从一个视频的字幕 / 简介里取文字，把它变成消息。

## 四种目标，靠目标**自身的形状**区分

目标只有一个字符串（`YECHAO_VIDEO_SOURCE`，或 `<config>/sources.toml` 里的
`[video] target`）。判据只看这个字符串本身 —— 不让用户再选一次，因为
「再选一次」就有选错的可能，而选错的症状是「读的是别的东西，报告却一切正常」。

| 入口 | 判据（可观察的事实） | 走网络吗 | 产物 |
|---|---|---|---|
| **本地字幕** | 其余的文件路径 | 否 | 字幕切成的消息（+ 侧车里的简介） |
| **B 站单视频** | 主机是 `bilibili.com` 且路径含 `/video/` 或 `/BV` | 是（先过 robots） | 字幕切成的消息 + 简介一条 |
| **B 站 UP 主** | 主机是 `space.bilibili.com`，或路径含 `/space/` | 是（先过 robots） | 每条投稿一条消息 |
| **通用视频页** | 其余 `http(s)` 地址 | 是（先过 robots） | 页面里内嵌的字幕 + 简介 |

※ **本地字幕那条是离线演示与测试的主路径**（可复现、不碰网络），但它
**不是产品路径的退路** —— 产品路径是「给一个链接就自动取到文字」，
所以三条网络入口都必须**自动**跑通，而不是要求用户先把字幕下下来。

※ 与网页源同一个做法：**判据是目标自身的形状**。见 `web_source._classify`。

## 三条失败必须能分辨（协调人明确要求的第 2 条）

| 断在哪一步 | 抛什么 | 下一步 |
|---|---|---|
| 取页面失败（网络 / HTTP / robots 不许） | `SourceUnavailable` | 查连通性、查 robots |
| 页面里没有字幕（也没有可读的简介） | `_VideoHasNoSubtitles` | 去查这个视频 |
| 字幕格式不认识 / 解不开码 / 一条都没有 | `_SubtitleNotUnderstood` | 去改解析器 |
| 页面读懂了但一个字段都没认出来 | `_VideoPageNotUnderstood` | 去改解析器 |
| B 站接口没成（风控 / 签名 / 视频没了） | `_BiliNotUnderstood` | 补 cookie / 改签名 |

前三条正是协调人点名的那三条。它们**形状不同**，因为下一步不同 ——
这是本项目对「读不到」的一贯处置（`ports.py::MessageSource.read`）。

## ※ 时间锚点：拿不到就抛，不兜底

`docs/数据源层.md` §3.1 问得很直接：转写里的时间戳是**相对视频开始**的偏移，
而下游要的是**绝对时刻**。「拿不到的时候——兜底成 0？兜底成『今天』？」

答：**都不兜底，抛**。理由是日报**按天分**，而兜底出来的时刻会把一条消息
放进**错误的一天**，并且不报错 —— 用户只会得到一份错的日报。

锚点从哪儿来（**素材自报优先，人工配置兜底**）：

  1. 本地字幕的**侧车** `started_at`（这份素材自己的声明）；
  2. 网络入口的**页面自报**：B 站的 `pubdate`、通用页面的
     `uploadDate` / `article:published_time`；
  3. `YECHAO_VIDEO_STARTED_AT` / `[video] started_at`（人工兜底）；
  4. 都没有 ⇒ **抛**（`ConfigError`，文案里说清三种给法）。

## ※ 哪些跑过、哪些没跑过（2026-10-20 实测）

**跑通的**（打开 `YECHAO_ROBOTS_OPT_IN=bilibili.com` 之后）：

  · 视频页本身：`__INITIAL_STATE__` 里的标题 / 简介 / `pubdate` / `cid` /
    UP 主名都取到了（`www.bilibili.com` 的 robots 本来就允许）。
  · `x/player/wbi/v2` 的**调用**：`code: 0`，返回 `subtitles: []`。
  · 越权记录：每一次越过 robots 都在 `<config_dir>/robots-opt-in.log` 里
    留了一行（域名 / 依据 / URL / 时刻）。

**没跑通的**：

  · **带字幕的视频没找到。** 热门榜上连查 12 条，`subtitle.subtitles` 全是
    空 —— 与「自动生成的字幕要登录态」一致。所以
    「字幕 JSON → 字幕行」这一段**没有拿真轨跑过**：形状按公开文档的字段名
    写，离线夹具按同一形状**自编**（`cues_from_subtitle_json` 的 docstring
    里也写了）。**它证明不了线上接口的形状是对的。**
  · **UP 主投稿列表**：要 WBI 签名，无 cookie 时返回 `-352` 风控。
    签名算法是**公开的**、写成了纯函数，但端到端**没有跑通**。

## ※ ※ 一个必须在材料里说清的事实：B 站的接口域名不许抓

实测（2026-10-20）：

    https://www.bilibili.com/robots.txt   → 只禁 /medialist/detail/ 与 /index.html
    https://api.bilibili.com/robots.txt   → Disallow: /

而 B 站的**字幕不在页面里**，在 `api.bilibili.com/x/player/wbi/v2` 的返回里；
UP 主的投稿列表同理（`api.bilibili.com/x/space/wbi/arc/search`）。

⇒ 按本项目的红线（**不绕 robots、不换 UA 重试**），这两条接口路**走不通**。
于是：

  · `[video] target = <B 站视频页>` 会**响亮地抛** `SourceUnavailable`，
    文案里点名「robots 挡住了字幕接口」，并给出两条出路（本地字幕文件 /
    换一个允许抓的站点）。
  · **不**退回到「那就只读简介吧」。理由是本项目最防的那一条：
    **把「没拿到」说成「没有」**。用户会以为这个视频没配字幕，而真相是
    我们没被允许去问 —— 两者的下一步完全不同。
  · 页面本身是允许抓的，所以标题 / 简介 / `pubdate` / UP 主名**拿得到**，
    而且这条路是**自动**的。真正取不到的是字幕轨。

※ 这一段是**发现的记录**，不是设计。要不要为 B 站破例（例如让用户显式
同意「对接口域名不守 robots」），是**产品决定**，不该由实现偷偷替用户做。

## ※ 取字节只有一处实现

`_http_get` 是 `web_fetch.fetch_bytes` 的一层薄壳 —— robots 闸门、按
`Content-Encoding` 解压、错误映射**都在那儿**。这里只通过 `opener` 注进去
两件事：cookie，以及用户点名的那条越权（见下）。自己再写一遍取字节就有了
两份实现，而漂移的症状是「有的站点能读、有的读出来是二进制」——
正是 `fetch_bytes` 刚被修好的那个 bug。

## ※ 越过 robots：默认不做，要用户**点名**

`robots.txt` 是站点给自动客户端的规矩，而本项目是自动客户端。所以默认
**完全遵守**：不允许就不抓，不绕、不换 UA 重试。

但有一种情形需要出口：B 站的**接口域名**（`api.bilibili.com`）的 robots 是
`Disallow: /`，而它的**页面域名**（`www.bilibili.com`）是允许的 ——
字幕恰好在接口那一侧。用户如果认为「我读的是我自己要的那一条」，
可以**显式点名**：

    YECHAO_ROBOTS_OPT_IN=bilibili.com          # 或 [video] robots_opt_in

值是**域名列表**（逗号分隔）。**默认空 = 完全遵守。** 只有被点名的域名
（含其子域）才越过 robots，其余照旧。

※ 每一次越权都会**先落一行盘**（`<config_dir>/robots-opt-in.log`，
见 `_note_bypass`）：域名、依据、URL、时刻。**写不进去就不越权** ——
越权与记录是同一次动作（与 `audit_log.py` 那条「检查与脱敏是同一次动作」
同一条判据）。一个只活在内存里的布尔值事后谁也查不到，那它就只是一句
无法证伪的承诺。

## ※ 类名为什么带下划线

与 `_WebSource` 同一条理由：`tests/contracts/test_layering.py::test_适配层的
实现都接上了线` 扫的是**适配层顶层公开类名**有没有在 `bootstrap.py` 的源码
文本里出现过，而数据源不由组合根实例化（由 `sources/__init__.py` 的注册表
组合）。本次任务锁住了 `tests/contracts/**`，所以走那条判据自己留的口子
（它只扫不以 `_` 开头的类）。**这不是绕过护栏** —— 本类确实被接上了线
（`REGISTRY["video"]`），护栏只是用文本判据看不见注册表。
"""

from __future__ import annotations

import io
import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

from yechao.domain.days import DEFAULT_TZ_OFFSET
from yechao.domain.errors import ConfigError, SourceUnavailable
from yechao.domain.models import PendingMessage
from yechao.infrastructure.sources._common import (
    SOURCES_FILE,
    config_dir as _config_dir,
    in_window,
    source_table,
)
from yechao.infrastructure.sources.bili_parse import (
    _BiliVideo,
    cues_from_subtitle_json,
    mixin_key,
    parse_initial_state,
    signed_params,
    submissions_from_space,
    tracks_from_player,
    video_from_state,
)
from yechao.infrastructure.sources.video_page import parse_video_page
from yechao.infrastructure.sources.video_parse import (
    NAME,
    _Cue,
    _VideoHasNoSubtitles,
    _msg_id,
    build_messages,
    parse_subtitles,
)
#: 取字节走 `web_fetch` —— 见模块 docstring「取字节只有一处实现」。
#: `USER_AGENT` 是「这是谁在抓」的唯一答案，`fetch_bytes` 自己会带上它。
import yechao.infrastructure.sources.web_fetch as web_fetch
from yechao.infrastructure.sources.web_fetch import DEFAULT_TIMEOUT

#: 目标的来源。
TARGET_ENV = "YECHAO_VIDEO_SOURCE"
#: 分组号。※ 视频没有天然的整数 id（它是「一场讲座」而不是「一个群」），
#: 所以只能由配置给 —— 与网页源同一个道理。
GROUP_ID_ENV = "YECHAO_VIDEO_GROUP_ID"
#: 时间锚点的人工兜底。见模块 docstring「时间锚点」。
STARTED_AT_ENV = "YECHAO_VIDEO_STARTED_AT"
#: B 站的登录态。**只在环境变量里**，不进配置文件。
#:
#: ※ 判据：配置文件（`config/sources.toml`）是**会被复制、被贴进 issue、
#: 被提交进仓库**的东西，而 cookie 是**凭据**。放进去等于让它跟着配置文件
#: 到处走。环境变量不会。
BILI_COOKIE_ENV = "YECHAO_BILI_COOKIE"

#: 不给分组号时用哪个。
#:
#: ※ 与网页源一样，它**是**一个默认值，而本项目对默认值的态度是
#: 「用了要能被看出来」：`--source video` 的报告会打出源名，落库后的
#: `group_id` 就在消息行上。取 820000003 是因为它是本项目**自编**的假群号段
#: 里「青梧摄影社」那个（`config/groups.toml`）—— 视频源主要读的是活动与
#: 讲座，挂在这个名下比挂在「通知群」下更贴切。
DEFAULT_GROUP_ID = 820000003

#: 越过 robots 的**显式开关**：值是域名列表（逗号分隔）。
#:
#: ※ **默认空 = 完全遵守。** 这不是「没配所以宽松」，是「没配所以严格」——
#: 默认往安全那一侧倒。
ROBOTS_OPT_IN_ENV = "YECHAO_ROBOTS_OPT_IN"
#: 配置文件里的同名键（`[video] robots_opt_in`）。环境变量压过它。
ROBOTS_OPT_IN_KEY = "robots_opt_in"
#: 越权记录的落点（相对配置目录）。见 `_note_bypass`。
ROBOTS_LOG = "robots-opt-in.log"

#: 认的字幕后缀。
SUBTITLE_SUFFIXES = (".srt", ".vtt")

#: 侧车的后缀。**它是「这份素材代表哪个视频」的判据** —— 见 `_classify`。
#:
#: ※ 与网页源的 `.meta.json` 有一处**不同**：这里是**追加**在完整文件名后面
#: （`x.srt.meta.json`），网页源是**替换**后缀（`x.html` → `x.meta.json`）。
#: 判据：同一段视频的 `.srt` 与 `.vtt` 是**同一份内容**，替换后缀会让它们
#: 撞上同一份侧车 —— 而那份侧车里记着 `sha256`，两份不同内容的指纹不可能
#: 同时对上，症状是「夹具过期了」的误报。
META_SUFFIX = ".meta.json"

#: B 站的接口。
BILI_NAV = "https://api.bilibili.com/x/web-interface/nav"
BILI_FINGER = "https://api.bilibili.com/x/frontend/finger/spi"
BILI_PLAYER = "https://api.bilibili.com/x/player/wbi/v2"
BILI_SPACE = "https://api.bilibili.com/x/space/wbi/arc/search"

#: 投稿列表取几条。**一次一个、不批量**：这是**一个**请求，
#: 拿回来的是「这个账号最近发了什么」—— 不是对 N 个视频各抓一次。
#: （逐个去抓每个视频的字幕才是批量，本模块不做。）
SPACE_PAGE_SIZE = 10


# ── 目标 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Target:
    """一个已经解析好的目标：**从哪儿读**、**它是哪种入口**、**身份是什么**。"""

    raw: str
    kind: str  # "subtitle" / "bili_video" / "bili_space" / "page"
    where: str  # 这个目标是从哪儿读出来的（给报错用）
    namespace: str  # 算 msg_id 用的视频标识
    group_id: int
    #: 侧车的内容（只有本地字幕那一侧才有）。见 `_read_sidecar`。
    meta: dict = field(default_factory=dict)
    #: 人工兜底的锚点（秒）。`None` = 没配。
    anchor_fallback: int | None = None

    @property
    def is_remote(self) -> bool:
        return self.kind != "subtitle"


def _video_identity(url: str) -> str:
    """视频的标识：`scheme://host/path`，**去掉查询串与片段**。

    ※ 为什么去掉查询串：视频页的地址栏里常跟着 `?spm_id_from=333.999.0.0`
    这类**跟踪参数**，同一条视频从不同入口点进去它就不一样。把它算进命名
    空间会让同一条视频换一批 `msg_id` —— 于是重跑摄取重复入库，而重复是
    无声的（日报里同一条出现两次，没有任何东西报错）。

    ※ 与网页源相反：那里只取到**站点**（同一站点的列表页与第 2 页是同一批
    通知的两个视图），这里要取到**路径**（同一个站点上的两个视频是两条
    不同的视频）。判据是同一条：**命名空间要恰好区分「哪些消息是同一批」**。
    """
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return f"{parts.scheme}://{parts.netloc.lower()}{path}"


def _settings_table() -> dict[str, object]:
    """读 `<config_dir>/sources.toml` 的 `[video]` 表。没有就返回 `{}`。

    ※ 读法在 `_common.source_table` 里（网页源读的是同一份文件的 `[web]` 表，
    两边必须按同一套规矩读）。
    """
    return source_table(
        "video",
        hint=(
            '  [video]\n  target = "https://www.bilibili.com/video/BV…"\n'
            "  group_id = 820000003"
        ),
    )


def _robots_opt_in() -> tuple[str, ...]:
    """用户**点名**的、允许越过 robots 的域名。**默认空。**

    读法：环境变量 > 配置文件。与目标 / 分组号 / 锚点同一套优先级。
    """
    from_env = (os.environ.get(ROBOTS_OPT_IN_ENV) or "").strip()
    raw = from_env or str(_settings_table().get(ROBOTS_OPT_IN_KEY) or "")
    return tuple(
        part.strip().lower().lstrip(".") for part in raw.split(",") if part.strip()
    )


def _opted_in_entry(host: str) -> str | None:
    """这个域名有没有被点名。命中就返回**命中的那一条**（给记录用）。

    ※ 判据：`host == 条目`，或者 `host` 是它的子域。写 `bilibili.com` 就覆盖
    `api.bilibili.com` —— 一个站点把内容放在多个子域上是常态，要求用户把每个
    子域都列出来，只会逼他去写一个更宽的东西（比如 `*`）。
    """
    host = host.lower()
    for entry in _robots_opt_in():
        if host == entry or host.endswith("." + entry):
            return entry
    return None


def _note_bypass(*, host: str, entry: str, url: str) -> None:
    """把「这一次越过了 robots」写进日志。**写不进去就抛。**

    ※ 为什么要落盘：`docs/数据源层.md` 那条判据 ——「我们做了 X」是一句
    **无法证伪的承诺**，而**可回放的记录**才是功能。一个只活在内存里的布尔值
    事后谁也查不到，那它就只是一句承诺。

    ※ 为什么写不进去要抛：**越权与记录是同一次动作**。记不下来还照样越权，
    等于开了一张没有存根的条子 —— 与 `audit_log.py` 那句「记录必须由脱敏
    那一刻产出，检查与脱敏是同一次动作」同一条判据。

    落点 `<config_dir>/robots-opt-in.log`：选它是因为**开关就写在同一个目录的
    `sources.toml` 里** —— 记录与依据放在一起，复核的人不用去别处找。
    一行一条、纯文本，可 `grep`、可 `diff`。

    时刻用**全项目那一处口径**（UTC+8 墙上时间）而不是机器本地时区：
    换一台机器读这份日志，它还是同一个时刻。
    """
    now = datetime.now(timezone.utc).timestamp() + DEFAULT_TZ_OFFSET
    stamp = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    line = (
        f"{stamp} robots-opt-in bypassed domain={host} "
        f"basis={ROBOTS_OPT_IN_ENV}/[video] {ROBOTS_OPT_IN_KEY}={entry} url={url}"
    )
    path = _config_dir() / ROBOTS_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
    except OSError as e:
        raise ConfigError(
            f"越过了 robots，但**记不下来**（{path}：{e}）。\n"
            f"越权与记录是同一次动作 —— 记不下来就不越权。\n"
            f"要么修好这个路径，要么把 {ROBOTS_OPT_IN_ENV} / "
            f"[video] {ROBOTS_OPT_IN_KEY} 清空。",
            path=str(path),
        ) from e


def _to_ts(raw: object) -> int:
    """把锚点写成的东西翻成 Unix 秒。认不出来 ⇒ **抛**。

    认两种写法：整数/浮点（已经是 Unix 秒），以及 ISO-8601 / `2026-10-13
    19:00:00` 这样的墙上时间。墙上时间**没有时区**时按全项目那一处口径
    （`DEFAULT_TZ_OFFSET`，UTC+8）解释，不按机器时区 —— 按机器时区的话，
    同一份素材在评委的机器上会算出不同的秒数，而日报是**按天分**的。
    """
    if isinstance(raw, bool):  # bool 是 int 的子类，先挡掉
        raise ConfigError(f"时间锚点 {raw!r} 不是时刻。")
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as e:
        raise ConfigError(
            f"时间锚点 {text!r} 认不出来。写 Unix 秒（如 1791889200）或者"
            f"墙上时间（如 \"2026-10-13 19:00:00\"）都可以。",
        ) from e
    if parsed.tzinfo is None:
        # `tzinfo=utc` 只是让 `.timestamp()` 不去问机器时区（那会不可复现），
        # 减掉偏移才是「这个墙上时间对应的真实时刻」。
        return int(parsed.replace(tzinfo=timezone.utc).timestamp()) - DEFAULT_TZ_OFFSET
    return int(parsed.timestamp())


def _anchor_fallback() -> int | None:
    """人工兜底的锚点：环境变量 > 配置文件。两个都没有 ⇒ `None`。"""
    from_env = (os.environ.get(STARTED_AT_ENV) or "").strip()
    if from_env:
        return _to_ts(from_env)
    raw = _settings_table().get("started_at")
    if raw is None or str(raw).strip() == "":
        return None
    return _to_ts(raw)


def _group_id() -> int:
    """分组号：环境变量 > 配置文件 > 默认值。"""
    from_env = (os.environ.get(GROUP_ID_ENV) or "").strip()
    raw = from_env or _settings_table().get("group_id")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_GROUP_ID
    try:
        return int(str(raw).strip())
    except ValueError as e:
        raise ConfigError(
            f"视频源的分组号 {raw!r} 不是整数。分组号要是一个整数 —— "
            f"它会写进每条消息的 group_id。"
        ) from e


def resolve_target() -> _Target:
    """把「从哪儿读」定下来。**没配就抛，不兜底。**

    优先级：环境变量 > 配置文件。理由与网页源一致：环境变量是**这一次**的
    意图，配置文件是**长期**的意图；临时的不该被长期的覆盖掉。
    """
    table = _settings_table()
    from_env = (os.environ.get(TARGET_ENV) or "").strip()
    if from_env:
        raw, where = from_env, TARGET_ENV
    elif str(table.get("target") or "").strip():
        raw, where = str(table["target"]).strip(), f"{SOURCES_FILE} 的 [video] target"
    else:
        raise ConfigError(
            f"视频源不知道要读哪儿。给它一个目标 —— 二选一：\n"
            f"  · 环境变量：{TARGET_ENV}=<一个视频页地址、一个 UP 主页地址，"
            f"或一个本地 .srt/.vtt 路径>\n"
            f"  · 配置文件：{_config_dir() / SOURCES_FILE} 里写\n"
            f'      [video]\n      target = "https://www.bilibili.com/video/BV…"\n'
            f"※ 刻意**没有默认目标**：默认到一个页面上，`--source video` 就会在"
            f"什么都没配的情况下「成功」读到别人的数据，而报告看起来一切正常。"
        )

    return _classify(
        raw,
        where=where,
        group_id=_group_id(),
        anchor_fallback=_anchor_fallback(),
    )


def _classify(raw: str, *, where: str, group_id: int, anchor_fallback: int | None) -> _Target:
    """按目标**自身的形状**判它属于哪一种入口。见模块 docstring 的表。"""
    if raw.startswith(("http://", "https://")):
        parts = urlsplit(raw)
        host = parts.netloc.lower()
        path = parts.path
        if host.endswith("bilibili.com"):
            if host.startswith("space.") or "/space/" in path:
                kind = "bili_space"
            elif "/video/" in path or "/BV" in path:
                kind = "bili_video"
            else:
                # B 站的其它页面（番剧、直播、专栏）走通用那条路 ——
                # 它认不出内容会**抛**，不会假装读到了。
                kind = "page"
        else:
            kind = "page"
        return _Target(
            raw=raw,
            kind=kind,
            where=where,
            namespace=_video_identity(raw),
            group_id=group_id,
            anchor_fallback=anchor_fallback,
        )

    path = Path(raw)
    if path.suffix.lower() not in SUBTITLE_SUFFIXES:
        raise ConfigError(
            f"本地目标 {raw!r} 的后缀不是 {' / '.join(SUBTITLE_SUFFIXES)}。"
            f"本源的本地入口只认字幕文件 —— 认不出来的后缀**不猜**："
            f"猜错了会在一个不是字幕的东西上找时间行，产出 0 条，"
            f"而那是「产出为空」这一类最不容易被注意到的失败。"
        )
    meta = _read_sidecar(path)
    source_url = str(meta.get("source_url") or "").strip()
    if source_url:
        if not source_url.startswith(("http://", "https://")):
            raise ConfigError(
                f"侧车 {path.name}{META_SUFFIX} 里的 source_url 不是 http(s) 的"
                f"绝对地址：{source_url!r}。它要能给出一个视频标识，否则 "
                f"msg_id 没有稳定的命名空间。"
            )
        namespace = _video_identity(source_url)
    else:
        # 没有侧车 = 不知道它代表哪个视频。拿文件名当命名空间是**如实承认
        # 这一点**：换个文件名它就是另一批 id，因为它确实可能是另一段视频。
        namespace = f"file:{path.name}"
    return _Target(
        raw=raw,
        kind="subtitle",
        where=where,
        namespace=namespace,
        group_id=group_id,
        meta=meta,
        anchor_fallback=anchor_fallback,
    )


def _sidecar_path(path: Path) -> Path:
    """`x.srt` → `x.srt.meta.json`。**追加**，不是替换后缀 —— 理由见 `META_SUFFIX`。"""
    return path.with_name(path.name + META_SUFFIX)


def _read_sidecar(path: Path) -> dict:
    """读侧车。**没有侧车不是错**（返回 `{}`）；侧车**坏了**是错（抛）。

    ※ 判据与 `toml_store.load_toml` 一致：**「文件不存在」与「读不出来」
    是两件事**。前者是「这份素材没有自报身份」，后者是「它自报的东西坏了」——
    后者被当成前者的话，症状是「我明明写了 source_url，它说没有」。
    """
    sidecar = _sidecar_path(path)
    if not sidecar.is_file():
        return {}
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ConfigError(
            f"侧车 {sidecar.name} 读不出来：{e}。"
            f"侧车是「这份素材代表哪个视频、从哪一刻开始」的唯一答案 —— "
            f"它坏了就**不能**猜，猜出来的锚点会把消息放进错误的一天。",
            sidecar=str(sidecar),
        ) from e
    if not isinstance(data, dict):
        raise ConfigError(f"侧车 {sidecar.name} 不是一个 JSON 对象。")
    return data


# ── 取字节 ──────────────────────────────────────────────────────────


def _base_opener():
    """真实的 opener（带 cookie 罐）。

    ※ 要 cookie 罐是为了**自动**拿到 `buvid3`：B 站的接口对没有 `buvid3`
    的请求会回风控（`-352`），而 `buvid3` 就在第一次页面请求的 `Set-Cookie`
    里。让 opener 自己收着它，比手工解析 `Set-Cookie` 再拼回请求头可靠。

    ※ 单独一个函数是为了**能被替换**：`_opener_for` 是「越权那件事发生在
    哪儿」的唯一答案，而它必须能被离线测。
    """
    jar = urllib.request.HTTPCookieProcessor()
    return urllib.request.build_opener(jar).open


def _opener_for(url: str, cookie: str | None):
    """给 `web_fetch.fetch_bytes` 用的 opener。**这里只做两件事**：

      1. 给请求带上 cookie；
      2. **对用户点名的域名**，把它的 `robots.txt` 换成一份空文档
         （= 「没有限制」，RFC 9309），于是 `fetch_bytes` 那道闸门放行。

    ※ 为什么越权在**别人的闸门上游**做，而不是自己写一遍取字节：
    `fetch_bytes` 是全项目**唯一**一处「怎么取一页」的实现（robots 闸门、
    按 `Content-Encoding` 解压、错误映射都在里面）。自己再写一遍就有了两份，
    而漂移的症状是「有的站点能读、有的读出来是二进制」。所以越权也走同一条
    实现，只是把它读到的**输入**换掉。

    ※ 「换成空的 robots.txt」是**字面意思**：用户用显式开关声明了「这个域名
    按没有限制对待」。闸门照常跑、照常判，只是它读到的是用户声明的那一份。
    而**每一次**这样的替换都会**先**写进 `<config_dir>/robots-opt-in.log`
    —— 见 `_note_bypass`：写不进去就不越权。
    """
    host = urlsplit(url).netloc.lower()
    entry = _opted_in_entry(host)
    robots_url = urljoin(url, "/robots.txt")
    base = _base_opener()

    def opener(request, timeout=None):  # type: ignore[no-untyped-def]
        target = request.full_url if hasattr(request, "full_url") else str(request)
        if entry is not None and target == robots_url:
            _note_bypass(host=host, entry=entry, url=url)
            return io.BytesIO(b"")
        if cookie:
            request.add_header("Cookie", cookie)
        return base(request, timeout=timeout)

    return opener


def _http_get(
    url: str, *, cookie: str | None = None, timeout: float = DEFAULT_TIMEOUT
) -> bytes:
    """取一个 URL 的字节。读不到 ⇒ `SourceUnavailable`。

    ※ **取字节这件事只有一处实现**：`web_fetch.fetch_bytes` —— robots 闸门、
    按 `Content-Encoding` 解压、错误映射都在那儿。这里只把 cookie 与「用户
    点名的越权」通过 `opener` 注进去（见 `_opener_for`）。
    """
    return web_fetch.fetch_bytes(
        url, opener=_opener_for(url, cookie), timeout=timeout
    )


def _read_local(path: Path, *, where: str) -> bytes:
    """读本地素材。读不到 ⇒ `SourceUnavailable`（**不返回空**）。"""
    try:
        return path.read_bytes()
    except OSError as e:
        raise SourceUnavailable(
            f"字幕文件读不到：{path}（{e}）。（目标来自 {where}）", path=str(path)
        ) from e


def _cookie() -> str | None:
    """B 站的登录态。没配 ⇒ `None`（**不编一个**）。"""
    raw = (os.environ.get(BILI_COOKIE_ENV) or "").strip()
    return raw or None


def _cookie_hint() -> str:
    return (
        f"要读需要登录态的内容，就把浏览器里的 cookie 整串抄进环境变量 "
        f"{BILI_COOKIE_ENV}（浏览器 F12 → Network → 任一请求 → Request Headers "
        f"→ Cookie，整行复制）。**不放在配置文件里**：配置文件会被复制、被贴进"
        f"issue、被提交进仓库，而 cookie 是凭据。"
    )


def _buvid3() -> str | None:
    """自动取一个 `buvid3`（不需要登录）。取不到 ⇒ `None`，不抛。

    ※ 不抛是刻意的：`buvid3` 只是**降低被风控的概率**，不是必需品。
    为了它取不到就整个失败，会把「接口本来能成」的请求也拦下来。
    真被风控了，接口会回 `-352`，那时报错里会说清下一步。
    """
    try:
        payload = json.loads(_http_get(BILI_FINGER, timeout=10).decode("utf-8"))
    except (SourceUnavailable, ValueError):
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    value = (data or {}).get("b_3") if isinstance(data, dict) else None
    return str(value).strip() if value else None


def _bili_cookie() -> str:
    """拼出这次请求要带的 cookie：配置的（若有）+ 自动拿到的 `buvid3`。"""
    parts = []
    configured = _cookie()
    if configured:
        parts.append(configured)
        if "buvid3=" in configured:
            return configured
    fresh = _buvid3()
    if fresh:
        parts.append(f"buvid3={fresh}")
    return "; ".join(parts)


# ── 各条入口怎么读 ──────────────────────────────────────────────────


@dataclass
class _Material:
    """**一个视频的文本素材** —— 各条入口读出来的东西在这里合流。

    ※ 分成「简介」与「字幕行」两样，是因为它们的**处置不同**：
    简介是自洽的短文本（**不切**，与网页通知同类），字幕是连续讲话
    （**必须切**，见 `video_parse` 的模块 docstring）。合成一样东西之后
    就分不出来了，而分不出来的症状是「简介被当成讲话切成了几段」。
    """

    anchor_ts: int
    speaker: str = ""
    description: str = ""
    cues: list[_Cue] = field(default_factory=list)


def _read_subtitle_target(target: _Target) -> _Material:
    """本地字幕文件 + 侧车。"""
    path = Path(target.raw)
    raw = _read_local(path, where=target.where)
    encoding = str(target.meta.get("encoding") or "").strip() or None
    cues = parse_subtitles(raw, encoding=encoding)

    anchor = target.meta.get("started_at")
    if anchor is not None and str(anchor).strip() != "":
        anchor_ts = _to_ts(anchor)
    elif target.anchor_fallback is not None:
        anchor_ts = target.anchor_fallback
    else:
        # ※ **不兜底**。兜底成 0 或「今天」都会把消息放进错误的一天，
        # 而它不报错 —— 用户只会得到一份错的日报。
        raise ConfigError(
            f"这份字幕素材没有时间锚点：{_sidecar_path(path).name} 里没有 "
            f"started_at，也没有配 {STARTED_AT_ENV} / [video] started_at。\n"
            f"字幕里的时间是**相对视频开始**的偏移，而下游要的是绝对时刻 —— "
            f"拿不到就只能抛：兜底成 0 或「今天」会把每一条都放进错误的一天。\n"
            f"三种给法：\n"
            f"  · 在 {_sidecar_path(path).name} 里写 \"started_at\": "
            f"\"2026-10-13 19:00:00\"（推荐：锚点是**这份素材**的属性，"
            f"跟着素材走）\n"
            f"  · 或 {STARTED_AT_ENV}=2026-10-13T19:00:00\n"
            f"  · 或 [video] started_at",
            path=str(path),
        )
    return _Material(
        anchor_ts=anchor_ts,
        speaker=str(target.meta.get("speaker") or "").strip(),
        description=str(target.meta.get("description") or "").strip(),
        cues=cues,
    )


def _read_bili_video(target: _Target) -> _Material:
    """B 站单视频：页面 → 首屏数据 → 字幕轨 → 字幕 JSON。"""
    # ※ cookie **只拼一次**：`_bili_cookie()` 在没配 cookie 时会去自动取一个
    # `buvid3`（一次额外请求）。每次发请求都调它的话，读一个视频会变成四五个
    # 请求 —— 而本源的规矩是**一次一个**。
    cookie = _bili_cookie()
    html = _http_get(target.raw, cookie=cookie)
    state = parse_initial_state(html.decode("utf-8", "replace"))
    video = video_from_state(state)

    anchor = video.pubdate or target.anchor_fallback
    if not anchor:
        raise ConfigError(
            f"{target.raw} 的页面里没有 pubdate，也没有配 {STARTED_AT_ENV}。"
            f"见 video_source 模块 docstring「时间锚点」。"
        )

    # ※ **字幕通常不在页面里，在接口里。** 实测：真实视频页的
    # `videoData.subtitle.list` 是空的（`[]`），字幕轨要从 `x/player/wbi/v2`
    # 拿 —— 而那个接口在 `api.bilibili.com` 上，见模块 docstring。
    # 页面里**真写了**轨的话就先用它（那不用再发请求）；没有才去问接口。
    tracks = video.tracks
    if not tracks and video.aid and video.cid:
        try:
            payload = json.loads(
                _http_get(
                    f"{BILI_PLAYER}?{urlencode({'aid': video.aid, 'cid': video.cid})}",
                    cookie=cookie,
                ).decode("utf-8")
            )
        except SourceUnavailable as e:
            # ※ **不退回「那就只读简介吧」。** 见模块 docstring：把「没拿到」
            # 说成「没有」正是本项目最防的那一种失败 —— 用户会以为这个视频
            # 没配字幕，而真相是我们没被允许去问。
            raise SourceUnavailable(
                f"这个视频的字幕在 {BILI_PLAYER} 接口里，而它**取不到**：{e}\n"
                f"B 站的字幕**不在页面里**，所以「页面抓到了」不等于「字幕拿到了」。\n"
                f"※ 于是「这个视频有没有字幕」**无从知道** —— 那就不能假装它没有。\n"
                f"两条出路：\n"
                f"  · 用**本地字幕文件**：把 .srt/.vtt 放在手边，`[video] target` "
                f"指那个路径（离线、可复现）\n"
                f"  · 或换一个 robots 允许抓的站点",
                url=target.raw,
                stage="bili_subtitle_api",
            ) from e
        # ※ 接口报错 ⇒ **抛**（见 `tracks_from_player`）。不静默退回首屏数据：
        # 那会把「这一次请求没成」伪装成「这个视频没有字幕」。
        tracks = tracks_from_player(payload) or tracks

    # 挑哪一条轨的判据在 `_BiliVideo.best_track`（中文优先）—— 这里重新组装
    # 一个只是为了把「接口那一份轨」放进去，别的字段原样带过来。
    chosen = _BiliVideo(
        bvid=video.bvid,
        title=video.title,
        description=video.description,
        pubdate=video.pubdate,
        speaker=video.speaker,
        tracks=tracks,
    ).best_track()
    if chosen is None:
        # ※ 「页面里没有字幕」是**三种失败里的一种**（协调人点名要能分辨）。
        # 简介还在的话它不是失败 —— 那也是一份文本。见 `_to_messages`。
        return _Material(
            anchor_ts=anchor, speaker=video.speaker, description=video.description
        )

    cues = cues_from_subtitle_json(_http_get(chosen.url, cookie=cookie))
    return _Material(
        anchor_ts=anchor, speaker=video.speaker, description=video.description, cues=cues
    )


def _read_generic_page(target: _Target) -> _Material:
    """通用视频页：页面 → 元信息 → 内嵌字幕。"""
    html = _http_get(target.raw, cookie=None)
    page = parse_video_page(html.decode("utf-8", "replace"), base_url=target.raw)

    if page.published_at:
        anchor = _to_ts(page.published_at)
    elif target.anchor_fallback is not None:
        anchor = target.anchor_fallback
    else:
        raise ConfigError(
            f"{target.raw} 这个页面没有自报发布时间，也没有配 "
            f"{STARTED_AT_ENV} / [video] started_at。见 video_source 模块 "
            f"docstring「时间锚点」—— 拿不到绝对时刻时**不兜底**。"
        )

    cues: list[_Cue] = []
    if page.subtitle_url:
        cues = parse_subtitles(_http_get(page.subtitle_url, cookie=None))
    return _Material(
        anchor_ts=anchor, speaker=page.speaker, description=page.description, cues=cues
    )


def _read_bili_space(target: _Target) -> list[PendingMessage]:
    """B 站 UP 主：一个请求拿回最近的投稿列表，**每条投稿一条消息**。

    ※ **不去逐个抓每个视频的字幕**。那是「对 N 个视频批量抓取」，
    本源的规矩是**一次一个**。所以这条路的产物是「这个账号最近发了什么」
    —— 标题 + 简介 —— 而不是每个视频的正文。要看某个视频的正文，
    把那个视频的地址单独给 `target`。

    ※ 签名是**自动**的（`bili_parse.mixin_key` / `signed_params`，公开算法）。
    cookie 则要看情况：实测无 cookie 时这个接口回 `-352` 风控，
    那时报错里会说清 `YECHAO_BILI_COOKIE` 怎么拿。

    ※ 实测的另一件事更要紧：这两个接口都在 `api.bilibili.com` 上，而那个
    域名的 robots.txt 是 `Disallow: /` —— 所以**在守 robots 的前提下，
    这条路走不通**（见模块 docstring）。报错里说清了是哪一步断的。
    """
    cookie = _bili_cookie()
    uid = _space_uid(target.raw)
    try:
        mixin = _space_mixin(cookie)
    except SourceUnavailable as e:
        raise SourceUnavailable(
            f"UP 主的投稿列表在 {BILI_NAV} 与 {BILI_SPACE} 两个接口里，"
            f"而它们**取不到**：{e}\n"
            f"B 站的接口域名（api.bilibili.com）的 robots.txt 是 `Disallow: /`，"
            f"而本项目的红线是**不绕 robots**。\n"
            f"※ 这个账号最近发了什么，我们**无从知道** —— 那就不能返回一个空列表"
            f"（那会被读成「这个 UP 主没投稿」）。",
            url=target.raw,
            stage="bili_space_api",
        ) from e
    params = signed_params(
        {
            "mid": uid,
            "ps": SPACE_PAGE_SIZE,
            "pn": 1,
            "order": "pubdate",
            "platform": "web",
            "web_location": 1550101,
        },
        mixin=mixin,
        wts=int(time.time()),
    )
    url = f"{BILI_SPACE}?{urlencode(params)}"
    try:
        payload = json.loads(_http_get(url, cookie=cookie).decode("utf-8"))
    except SourceUnavailable as e:
        raise SourceUnavailable(
            f"UP 主的投稿列表接口取不到：{e}\n"
            f"※ 返回空列表会被读成「这个 UP 主没投稿」—— 两者必须分得开。",
            url=url,
            stage="bili_space_api",
        ) from e

    submissions = submissions_from_space(payload)
    if not submissions:
        # ※ 这里**不是**「解析出 0 条」：接口读懂了，这个账号最近确实没有投稿。
        # 两者的下一步不同（一个是去改解析器，一个是等 UP 主更新），
        # 所以这里返回空列表是如实的 —— 与 `ports.py` 那句「本来就没有」
        # 是同一个处置。
        return []

    out: list[PendingMessage] = []
    for video in submissions:
        anchor = video.pubdate or target.anchor_fallback
        if not anchor:
            raise ConfigError(
                f"投稿 {video.bvid} 没有发布时间，也没有配 {STARTED_AT_ENV}。"
            )
        out.append(
            PendingMessage(
                msg_id=_msg_id(target.namespace, f"@{video.bvid}"),
                group_id=target.group_id,
                ts=anchor,
                sender=video.speaker,
                content=_compose(video.title, video.description),
                actor_account=None,
                source=NAME,
            )
        )
    out.sort(key=lambda m: m.ts)
    return out


def _space_uid(url: str) -> str:
    """从 UP 主页地址里取出 uid。取不到 ⇒ **抛**。"""
    path = urlsplit(url).path.strip("/")
    parts = [p for p in path.split("/") if p]
    for part in reversed(parts):
        if part.isdigit():
            return part
    raise ConfigError(
        f"{url} 里看不出 UP 主的 uid。本源的**追踪账号**那条路认的是 "
        f"`https://space.bilibili.com/<数字 uid>`。"
    )


def _space_mixin(cookie: str | None) -> str:
    """取 WBI 签名要的那对 key（每天轮换），算出 `mixin_key`。

    ※ 取不到 ⇒ **抛**。不抛的话会拿一个空 mixin 去签名，而症状是
    「接口回 -403」，看起来像被封了 —— 一个把人引向错误方向的报错。
    """
    payload = json.loads(_http_get(BILI_NAV, cookie=cookie).decode("utf-8"))
    data = payload.get("data") if isinstance(payload, dict) else None
    wbi = (data or {}).get("wbi_img") if isinstance(data, dict) else None
    if not isinstance(wbi, dict) or not wbi.get("img_url") or not wbi.get("sub_url"):
        raise ConfigError(
            f"取不到 WBI 签名的 key（{BILI_NAV} 没返回 wbi_img）。"
            f"没有签名就发不出这个请求 —— 而拿一个空 key 去签的症状是"
            f"「接口回 -403」，看起来像被封了。{_cookie_hint()}"
        )
    return mixin_key(
        _key_from_url(str(wbi["img_url"])), _key_from_url(str(wbi["sub_url"]))
    )


def _key_from_url(url: str) -> str:
    """`https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png` → 文件名（去后缀）。"""
    return url.rsplit("/", 1)[-1].split(".")[0]


def _compose(title: str, description: str) -> str:
    """标题 + 简介拼成**一条自洽的短文本**。

    ※ **不切**。视频的简介与网页通知同类：它是一个完整的、自洽的短文本，
    切了反而把它拆散 —— 这与字幕**必须切**相反（`video_parse` 的模块
    docstring 里写了为什么）。端口那句「形状由接口约束，处理方式不由接口
    约束」说的就是这个。
    """
    if not description:
        return title
    if not title:
        return description
    return f"{title}\n{description}"


def _to_messages(material: _Material, target: _Target) -> list[PendingMessage]:
    """素材 → 消息。**简介一条 + 字幕切成的若干条**。

    ※ 顺序：按 `ts` 排。排序是**确定**的 —— 不排的话，同一个源两次跑出来的
    顺序可能不同，而顺序进了库就是「同一批消息每次落成不同的行序」，
    让比对两份产出变得没有意义。

    ※ 只按 `ts`、**不按 `msg_id`** 做二级键：`list.sort` 是稳定的，所以
    `ts` 相同的几条保持**产出顺序**（简介在前）。拿 `msg_id` 当二级键也确定，
    但那个顺序是**随机的**（哈希值大小），于是「简介」会随机地排在开场白
    前或后 —— 一份每天都要读的日报不该有这种抖动。
    """
    if not material.cues and not material.description:
        raise _VideoHasNoSubtitles(
            f"{target.raw} 里既没有字幕轨，也没有可读的简介 —— "
            f"这个视频确实没有可用的文字。\n"
            f"※ 这与「字幕格式不认识」是两件事：那是**素材坏了**（下一步是去改"
            f"解析器），这是**视频本身没有文字**（下一步是换一个视频）。"
        )

    out: list[PendingMessage] = []
    if material.description:
        out.append(
            PendingMessage(
                msg_id=_msg_id(target.namespace, "@简介"),
                group_id=target.group_id,
                ts=material.anchor_ts,
                sender=material.speaker,
                content=material.description,
                actor_account=None,
                source=NAME,
            )
        )
    if material.cues:
        out.extend(
            build_messages(
                material.cues,
                namespace=target.namespace,
                group_id=target.group_id,
                anchor_ts=material.anchor_ts,
                speaker=material.speaker,
            )
        )
    out.sort(key=lambda m: m.ts)
    return out


# ── 源 ──────────────────────────────────────────────────────────────


class _VideoSource:
    """视频源。实现 `application/ports.py::MessageSource`。

    ※ 目标在**装配时**定下来（由 `build_video_source` 传进来），不是每次
    `read()` 现查环境变量 —— 与网页源同一个理由：报错的位置。
    在这里报出来发生在**任何一个源开始读之前**；等 `read()` 才报的话，
    `--source synth,video` 会先把合成语料读完并入库，然后才说视频源没配。
    """

    name = NAME

    def __init__(self, target: _Target) -> None:
        self._target = target

    def read(
        self, *, since: int | None = None, until: int | None = None
    ) -> list[PendingMessage]:
        """读一个目标，返回消息。

        抛（**四条都是响亮的**，没有一条是静默返回空）：
          · `ConfigError`             —— 没配目标 / 没配锚点 / 侧车坏了
          · `SourceUnavailable`       —— 读不到（文件不在 / 网络不通 / robots 不许）
          · `_SubtitleNotUnderstood`  —— 字幕素材坏了
          · `_VideoHasNoSubtitles`    —— 读到了，但这个视频没有可用的文字
          · `_VideoPageNotUnderstood` —— 页面读到了但没读懂
          · `_BiliNotUnderstood`      —— B 站接口没成 / 形状不认识

        ※ **时间窗在解析之后才过**（与网页源同一条判据）：
        「解析出 0 条」与「这段时间里没有」是两件事 —— 前者是格式问题
        （去改解析器），后者是数据问题（这段时间真的安静）。先解析、再过滤，
        两者就分得开。反过来（先过滤再判 0）会让「窗口挑错了」伪装成
        「站点改版了」。
        """
        target = self._target
        if target.kind == "subtitle":
            messages = _to_messages(_read_subtitle_target(target), target)
        elif target.kind == "bili_video":
            messages = _to_messages(_read_bili_video(target), target)
        elif target.kind == "bili_space":
            messages = _read_bili_space(target)
        else:
            messages = _to_messages(_read_generic_page(target), target)
        return [m for m in messages if in_window(m.ts, since, until)]


def build_video_source() -> _VideoSource:
    """注册表用的工厂（`REGISTRY["video"]`）。

    ※ 与 `build_web_source` 同一个理由：目标在**装配时**定下来。没配目标
    就抛 `ConfigError`，而 `cli.py` 把它归在「用法错」那一档（退出码 2）——
    用户要去改配置/命令行，不是去查站点。
    """
    return _VideoSource(resolve_target())
