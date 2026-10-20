"""B 站那条路 —— 页面与接口的**解析**部分。**纯函数，不碰 I/O。**

## 为什么 B 站要单独一个模块

它有三样别处没有的东西，而且都是它的**私有约定**，不是网页的通用形状：

  1. `window.__INITIAL_STATE__` —— 首屏数据直接内联在 HTML 里（一个 JSON）。
     标题、简介、`pubdate`、`cid`、`aid`、UP 主名都在里面。
  2. 字幕**不在页面里**，而在 `x/player/wbi/v2` 接口的返回里（一条轨一个
     `subtitle_url`，指向一个 JSON 文件）。所以「页面上有字幕」这句话在 B 站
     不成立 —— 要**再发一次请求**才知道。
  3. UP 主的投稿列表要 **WBI 签名**，而签名要一对每天轮换的 key。

把这三样塞进通用的页面解析里，通用那条路就会被 B 站的具体形状污染。

## ※ 字幕的可见性：这就是「有的要登录态」的落点

观察到的现象：**没有登录 cookie 时**，`x/player/wbi/v2` 返回的
`data.subtitle.subtitles` 经常是**空数组** —— 人工上传的字幕轨通常能看到，
而**自动生成的字幕（`ai-zh`）要登录态**。所以「空数组」有两个完全不同的
原因：这个视频真的没配字幕，或者**我们没有身份**。

⇒ 处置：把这两件事在**报错文案**里分开（见 `video_source`），并且给一条
`YECHAO_BILI_COOKIE` 的出口 —— 而不是静默返回空。静默返回空正是本项目
最防的那一种失败：用户以为「这个视频没字幕」，其实只是没带 cookie。

## 说话人从哪儿来

B 站的字幕 JSON 里**没有**说话人字段（那是字幕的通用形状，不是它的私有
约定）。所以 `speaker` 取 UP 主名 —— 那是**页面自报的**，不是从正文里猜的。
一段视频里的具体是谁在讲，字幕素材本身没说，本模块不猜。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from yechao.domain.errors import SourceFormatError
from yechao.infrastructure.sources.video_parse import _Cue

#: 首屏数据的那一行。`undefined` 在 JSON 里不是合法值，B 站却会写它 ——
#: 替换成 `null` 再解析（这是**它的**约定，不是我们的宽容）。
_INITIAL_STATE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.+?\})\s*;\s*", re.S)

#: 认中文轨的顺序。第一条命中的就用。
#: ※ `ai-zh` 排在最后：它是机器生成的，错字比人工轨多；两者都有时优先人工。
_PREFERRED_LANGS = ("zh-CN", "zh-Hans", "zh-Hant", "ai-zh")

#: WBI 签名的重排表。它是**公开的**（`img_key` + `sub_key` 按这张表打乱后取
#: 前 32 位得到 `mixin_key`）。写在这里而不是从网上取 —— 取不到的时候
#: 就没有签名，而没有签名的症状是「接口返回 -403」，看起来像被封了。
_MIXIN_KEY_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

#: 签名前要从参数值里**滤掉**的字符。它们会让 `urlencode` 与签名的口径
#: 对不上，而症状是「签名算对了却仍然 -403」。
_FILTER = str.maketrans("", "", "!'()*")


class _BiliNotUnderstood(SourceFormatError):
    """B 站这一路读到了字节，但**没读懂**。

    下一步：**去改解析器**（它的页面结构变了），不是去查这个视频。
    """


@dataclass(frozen=True)
class _BiliTrack:
    """一条字幕轨。`url` 指向那个 JSON 文件（可能是 `//` 开头的协议相对地址）。"""

    lang: str
    label: str
    url: str


@dataclass(frozen=True)
class _BiliVideo:
    bvid: str
    title: str
    description: str
    #: 发布时刻（Unix 秒）。`0` = 页面没给 —— 那时**不能**拿它当锚点。
    pubdate: int
    speaker: str
    tracks: tuple[_BiliTrack, ...] = ()
    aid: int = 0
    cid: int = 0

    def best_track(self) -> _BiliTrack | None:
        """挑一条字幕轨。中文优先，其次第一条。

        ※ 挑不出来时返回 `None`（而不是抛）—— 「这个视频没有字幕」是**数据**
        的问题，不是格式的问题，两者的下一步不同。处置在调用方。
        """
        if not self.tracks:
            return None
        for lang in _PREFERRED_LANGS:
            for track in self.tracks:
                if track.lang == lang:
                    return track
        return self.tracks[0]


# ── 首屏数据 ────────────────────────────────────────────────────────


def parse_initial_state(text: str) -> dict:
    """页面 HTML → `window.__INITIAL_STATE__`。取不到 ⇒ **抛**。

    ※ 取不到**不返回 `{}`**：B 站对**没有 cookie / 疑似机器人**的请求会返回
    一个 200 的**精简页**（没有首屏数据）。返回 `{}` 会让它一路走到
    「这个视频没标题」，而真相是「我们没被当成浏览器」。两者的下一步完全不同。
    """
    m = _INITIAL_STATE.search(text)
    if m is None:
        raise _BiliNotUnderstood(
            "这个页面里没有 window.__INITIAL_STATE__。"
            "常见原因是**服务端返回了精简页**（没有 cookie 或疑似机器人时它"
            "会这么做，而状态码仍然是 200）—— 那时下一步是补 cookie，"
            "不是改解析器。另一种原因是它真的改版了。",
            found=len(text),
        )
    raw = m.group(1).replace("undefined", "null")
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise _BiliNotUnderstood(
            f"window.__INITIAL_STATE__ 不是合法 JSON：{e}", found=len(text)
        ) from e
    if not isinstance(data, dict):
        raise _BiliNotUnderstood("window.__INITIAL_STATE__ 不是一个对象")
    return data


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _tracks(raw: object) -> tuple[_BiliTrack, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[_BiliTrack] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("subtitle_url") or item.get("url") or "").strip()
        if not url:
            continue
        # 接口给的是 `//aisubtitle.hdslb.com/...`（协议相对）。补上 https：
        # 不补的话抓取那一步会说「URL 不合法」，而它其实是页面给的形状。
        if url.startswith("//"):
            url = "https:" + url
        out.append(
            _BiliTrack(
                lang=str(item.get("lan") or "").strip(),
                label=str(item.get("lan_doc") or "").strip(),
                url=url,
            )
        )
    return tuple(out)


def video_from_state(state: dict) -> _BiliVideo:
    """首屏数据 → 一个视频。`videoData` 不在 ⇒ **抛**。"""
    video = state.get("videoData")
    if not isinstance(video, dict):
        raise _BiliNotUnderstood(
            "首屏数据里没有 videoData —— 这个页面不是视频页（可能是番剧、"
            "直播或其它页面）。本源的**单视频**那条路只认 `/video/BV…`。"
        )
    subtitle = video.get("subtitle")
    tracks = _tracks(
        subtitle.get("list") if isinstance(subtitle, dict) else None
    )
    owner = video.get("owner")
    return _BiliVideo(
        bvid=str(video.get("bvid") or "").strip(),
        title=str(video.get("title") or "").strip(),
        description=str(video.get("desc") or "").strip(),
        pubdate=_int(video.get("pubdate")),
        speaker=str((owner or {}).get("name") or "").strip() if isinstance(owner, dict) else "",
        tracks=tracks,
        aid=_int(video.get("aid")),
        cid=_int(video.get("cid")),
    )


def tracks_from_player(payload: dict) -> tuple[_BiliTrack, ...]:
    """`x/player/wbi/v2` 的返回 → 字幕轨。

    ※ `code != 0` ⇒ **抛**。这一条是「不要静默返回空」的落点：接口报错
    （-403 签名不对 / -352 风控 / -404 视频没了）与「这个视频没有字幕」
    必须分开 —— 前者返回空数组的话，用户会以为视频没配字幕。
    """
    code = _int(payload.get("code"))
    if code != 0:
        raise _BiliNotUnderstood(
            f"B 站接口返回 code={code}（{payload.get('message')}）。"
            f"这不是「这个视频没有字幕」，是**这一次请求没成** —— "
            f"-352 是风控（补 cookie），-403 是签名不对。",
            code=code,
        )
    data = payload.get("data")
    subtitle = (data or {}).get("subtitle") if isinstance(data, dict) else None
    return _tracks((subtitle or {}).get("subtitles") if isinstance(subtitle, dict) else None)


def cues_from_subtitle_json(raw: bytes) -> list[_Cue]:
    """B 站字幕 JSON → 字幕行。**纯函数。**

    形状：`{"body": [{"from": 0.0, "to": 4.5, "content": "…"}], …}` ——
    `from` / `to` 是**视频内的偏移秒数**，与本项目 `_Cue` 的口径一致，
    所以这里不需要任何换算。

    ※ 这个形状**未在网络环境验证**（见 `video_source` 的模块 docstring：
    带字幕的视频没找到）。判据来自公开文档与接口的字段名，离线夹具按同一
    形状自编。接手的下一个人应当拿一条真轨对一次。
    """
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise _BiliNotUnderstood(f"字幕 JSON 解不开：{e}") from e
    body = data.get("body") if isinstance(data, dict) else None
    if not isinstance(body, list):
        raise _BiliNotUnderstood("字幕 JSON 里没有 body 数组 —— 形状不认识")

    cues: list[_Cue] = []
    for index, item in enumerate(body):
        if not isinstance(item, dict):
            continue
        text = str(item.get("content") or "").strip()
        if not text:
            # 空白的 cue 是**排版**（B 站用它做停顿），不是一条没说的话。
            # 跳过它不算「静默丢一条消息」。
            continue
        try:
            start = float(item.get("from"))
            end = float(item.get("to"))
        except (TypeError, ValueError) as e:
            raise _BiliNotUnderstood(
                f"第 {index} 条字幕的 from/to 不是数字：{item.get('from')!r}"
                f"/{item.get('to')!r}", index=index
            ) from e
        cues.append(_Cue(start=start, end=end, text=text))
    if not cues:
        raise _BiliNotUnderstood(
            "这条字幕轨的 body 是空的 —— 轨存在但没有内容。"
            "（这不是「这个视频没字幕」：轨已经拿到了。）"
        )
    return cues


# ── UP 主的投稿列表 ─────────────────────────────────────────────────


def submissions_from_space(payload: dict) -> list[_BiliVideo]:
    """`x/space/wbi/arc/search` 的返回 → 一批投稿。**纯函数。**

    ※ 取的是 `data.list.vlist`（投稿的**列表视图**）。只取标题、简介、
    发布时间与 UP 主名 —— **不取字幕**：那是每个视频各抓一次，属于
    「对 N 个视频批量抓取」，本模块不做（见 `video_source` 的说明）。
    """
    code = _int(payload.get("code"))
    if code != 0:
        raise _BiliNotUnderstood(
            f"B 站投稿列表接口返回 code={code}（{payload.get('message')}）。"
            f"-352 是风控 —— 这个接口基本一定要带 cookie，"
            f"见 YECHAO_BILI_COOKIE。",
            code=code,
        )
    data = payload.get("data") or {}
    lst = data.get("list") if isinstance(data, dict) else None
    vlist = lst.get("vlist") if isinstance(lst, dict) else None
    if not isinstance(vlist, list):
        raise _BiliNotUnderstood("投稿列表的返回里没有 data.list.vlist —— 形状不认识")

    out: list[_BiliVideo] = []
    for item in vlist:
        if not isinstance(item, dict):
            continue
        out.append(
            _BiliVideo(
                bvid=str(item.get("bvid") or "").strip(),
                title=str(item.get("title") or "").strip(),
                description=str(item.get("description") or "").strip(),
                pubdate=_int(item.get("created") or item.get("pubdate")),
                speaker=str(item.get("author") or "").strip(),
            )
        )
    if not out:
        # ※ 这个**是**「这个账号没有投稿」，与「接口没成」是两件事：
        # 前者返回空列表是如实的，调用方据此产出 0 条。
        return []
    return out


# ── WBI 签名 ────────────────────────────────────────────────────────


def mixin_key(img_key: str, sub_key: str) -> str:
    """由一对 key 算出 `mixin_key`。**纯函数。**

    做法：把 `img_key + sub_key` 按公开的重排表打乱，取前 32 位。
    """
    raw = img_key + sub_key
    mixed = "".join(raw[i] for i in _MIXIN_KEY_TAB if i < len(raw))
    return mixed[:32]


def signed_params(params: dict[str, object], *, mixin: str, wts: int) -> dict[str, str]:
    """给参数加上 `wts` 与 `w_rid`。**纯函数。**

    步骤（顺序是有讲究的）：
      1. 值里的 `!'()*` 滤掉 —— 它们会让 `urlencode` 与签名的口径对不上；
      2. **按 key 排序** —— 签名算的是排序后的查询串；
      3. 加上 `wts`（时间戳，服务端据此判有效期）；
      4. `w_rid = md5(查询串 + mixin_key)`。

    ※ `quote` 用 `safe=""`：`+` 与 `/` 必须转义，否则服务端重算出来的串
    与我们签的串不一样，而症状是「本地算对了却仍然 -403」。
    """
    clean = {k: str(v).translate(_FILTER) for k, v in params.items()}
    clean["wts"] = str(wts)
    ordered = {k: clean[k] for k in sorted(clean)}
    query = urlencode({k: quote(v, safe="") for k, v in ordered.items()})
    ordered["w_rid"] = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
    return ordered
