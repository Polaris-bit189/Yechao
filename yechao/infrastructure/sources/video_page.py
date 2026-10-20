"""视频页面 → 视频的元信息。**纯函数，不碰 I/O。**

这是**通用**的那条路：不属于任何具体站点的视频页。B 站有它自己的一条
（`bili_parse.py`），因为它的结构化数据在 `window.__INITIAL_STATE__` 里，
而那是它的私有约定，不是网页的通用形状。

## 从这个页面里能拿到什么、为什么是这几样

| 字段 | 从哪儿取 | 判据 |
|---|---|---|
| 标题 | `og:title` → `<title>` | 两个都在时取 `og:title`：它是**给分享卡片用的**，比 `<title>` 少一层站点后缀（「标题 - 某某站」） |
| 简介 | `og:description` → `<meta name="description">` → JSON-LD `description` | 同上 |
| 发布时间 | JSON-LD `uploadDate` / `datePublished` → `article:published_time` → `<time datetime>` | **这是时间锚点**，见 `video_source._anchor_of`。拿不到就抛，不兜底 |
| 讲者 | JSON-LD `author.name` → `og:site_name` | 空串 = 页面没说。**不从正文里猜** |
| 字幕地址 | `<track kind=subtitles>` / `<track kind=captions>` 的 `src` → JSON-LD `caption` / `transcript` → `<link rel=alternate type=text/vtt>` | 没有 = 这个页面没有字幕轨 |

※ **JSON-LD 优先于 meta 标签**：`application/ld+json` 是**结构化**的
（字段有名字、有类型），而 meta 标签是自由文本的约定。结构化数据在的时候
按结构化数据读，比从自由文本里认格式可靠。

## ※ 「这个页面没读懂」与「这个视频没有字幕」是两件事

与 `web_parse.py` 同一套设计：前者是 `_VideoPageNotUnderstood`（下一步是
**去改解析器**），后者**不是异常** —— 它是 `_VideoPage.subtitle_url` 为空串
（下一步是**去查这个视频**）。把两者混成一个「没有字幕」的异常，会让一个
已经改版了的解析器看起来像「这些视频都没配字幕」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

from yechao.domain.errors import SourceFormatError


class _VideoPageNotUnderstood(SourceFormatError):
    """页面读到了，但**一个字段都没认出来**。

    下一步：**去改解析器**（站点改版了），不是去查这个视频有没有字幕。
    """


@dataclass(frozen=True)
class _VideoPage:
    """一个视频页面里能拿到的元信息。**空串 = 页面没给**，不是「取到了空值」。"""

    title: str = ""
    description: str = ""
    #: 页面自报的发布时间（原样保留写法）。空串 = 页面没说 —— 那时时间锚点
    #: 只能来自配置，见 `video_source._anchor_of`。
    published_at: str = ""
    #: 讲者 / 作者。空串 = 页面没说。
    speaker: str = ""
    #: 字幕轨的**绝对**地址。空串 = 这个页面没有字幕轨。
    subtitle_url: str = ""


def _first_json_ld(text: str) -> dict:
    """页面里的第一个 JSON-LD 对象。取不到就返回 `{}`。

    ※ 解析失败**不抛**：页面里常有多个 `ld+json` 块，其中一个坏了不该让
    整页读不了 —— 后面还有 meta 标签兜底。这与「解析出 0 条要抛」不冲突：
    抛的判据是**整页一个字段都没认出来**（见 `parse_video_page`）。
    """
    start = 0
    while True:
        at = text.find("application/ld+json", start)
        if at < 0:
            return {}
        open_at = text.find(">", at)
        close_at = text.find("</script>", open_at)
        if open_at < 0 or close_at < 0:
            return {}
        try:
            data = json.loads(text[open_at + 1 : close_at].strip())
        except ValueError:
            start = at + 1
            continue
        if isinstance(data, list):
            data = next((x for x in data if isinstance(x, dict)), None)
        if isinstance(data, dict):
            return data
        start = at + 1


class _PageScanner(HTMLParser):
    """把页面里**有名字的那几样**收下来。别的都不看。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.og: dict[str, str] = {}
        self.meta: dict[str, str] = {}
        self.title = ""
        self.time_datetime = ""
        self.tracks: list[str] = []
        self.links: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").strip().lower()
            content = a.get("content", "").strip()
            if key and content:
                if key.startswith("og:"):
                    self.og.setdefault(key, content)
                else:
                    self.meta.setdefault(key, content)
        elif tag == "title":
            self._in_title = True
        elif tag == "time" and a.get("datetime"):
            self.time_datetime = self.time_datetime or a["datetime"].strip()
        elif tag == "track":
            kind = a.get("kind", "subtitles").strip().lower()
            # ※ 只认字幕轨。`kind=chapters` / `metadata` 是别的东西，
            # 拿它们当字幕会把「章节名」当成正文。
            if a.get("src") and kind in ("subtitles", "captions"):
                self.tracks.append(a["src"].strip())
        elif tag == "link":
            rel = a.get("rel", "").lower()
            typ = a.get("type", "").lower()
            if a.get("href") and (typ == "text/vtt" or rel == "alternate" and typ.endswith("vtt")):
                self.links.append(a["href"].strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def _json_ld_value(ld: dict, *keys: str) -> str:
    for key in keys:
        value = ld.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _json_ld_author(ld: dict) -> str:
    """JSON-LD 里的作者名。`author` 可能是对象、也可能是对象数组。"""
    author = ld.get("author")
    if isinstance(author, list):
        author = next((x for x in author if isinstance(x, dict)), None)
    if isinstance(author, dict):
        name = author.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    if isinstance(author, str) and author.strip():
        return author.strip()
    return ""


def _json_ld_subtitle_url(ld: dict) -> str:
    """JSON-LD 里的字幕地址。`caption` / `transcript` 可能是串、也可能是数组。"""
    for key in ("caption", "transcript"):
        value = ld.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return item.strip()
                if isinstance(item, dict):
                    url = item.get("url") or item.get("contentUrl")
                    if isinstance(url, str) and url.strip():
                        return url.strip()
    return ""


def parse_video_page(text: str, *, base_url: str) -> _VideoPage:
    """一页视频页面的 HTML → 元信息。**纯函数。**

    抛 `_VideoPageNotUnderstood`：**五个字段一个都没认出来**。那时下一步是
    去改解析器 —— 不是去查这个视频。

    ※ 相对地址（`<track src="/x.vtt">`）用 `base_url` 补成绝对地址。
    不补的话，抓取那一步会拿到一个没有 host 的串，报出来的错会指向
    「URL 不合法」而不是「页面里写的是相对地址」。
    """
    ld = _first_json_ld(text)
    scanner = _PageScanner()
    scanner.feed(text)

    title = _json_ld_value(ld, "name", "headline") or scanner.og.get("og:title", "") or scanner.title.strip()
    description = (
        _json_ld_value(ld, "description")
        or scanner.og.get("og:description", "")
        or scanner.meta.get("description", "")
    )
    published_at = (
        _json_ld_value(ld, "uploadDate", "datePublished", "dateCreated")
        or scanner.meta.get("article:published_time", "")
        or scanner.time_datetime
    )
    speaker = _json_ld_author(ld) or scanner.og.get("og:site_name", "")

    raw_subtitle = ""
    if scanner.tracks:
        raw_subtitle = scanner.tracks[0]
    elif scanner.links:
        raw_subtitle = scanner.links[0]
    else:
        raw_subtitle = _json_ld_subtitle_url(ld)

    page = _VideoPage(
        title=title.strip(),
        description=description.strip(),
        published_at=published_at.strip(),
        speaker=speaker.strip(),
        subtitle_url=urljoin(base_url, raw_subtitle) if raw_subtitle else "",
    )
    if not (page.title or page.description or page.subtitle_url):
        raise _VideoPageNotUnderstood(
            f"{base_url} 这个页面里标题、简介、字幕轨一个都没认出来。"
            f"下一步是去改解析器（站点可能改版了），不是去查这个视频有没有字幕。"
            f"※ 如果这个页面是靠 JavaScript 现渲染出来的，那么**它本来就不该**"
            f"走这条路 —— 抓到的 HTML 里没有内容，不是解析器的问题。",
            url=base_url,
        )
    return page
