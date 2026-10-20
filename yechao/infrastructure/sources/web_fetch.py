"""抓取 —— **只负责把字节拿下来并落盘**。解析不在这里。

## 为什么「抓」与「解」要分成两个模块

`docs/数据源层.md` §2.5 把夹具当成「可复现的最后一道保险」。而要让这道保险
成立，得先让**解析能被离线测试**：

  · 抓取必须碰网络、必须有网才跑得动、结果还会变（站点更新了）——
    所以它**不该出现在测试里**；
  · 解析只吃字节，同样的字节永远给同样的结果 —— 所以它**必须能被离线测**。

把两者写在一个函数里，测试要么去连网（不稳、也不该），要么整块不测（于是
解析这个最需要被测试的部分反而没人测）。⇒ 分开，`web_parse.py` 只吃字节。

## 夹具是怎么来的

    抓取（本模块）→ 落成一份**原始 HTML** + 一份**侧车**（`*.meta.json`）
    解析（`web_parse.py`）→ 消息

存**原始素材**而不是解析后的结果，理由：解析器改进了，旧夹具还能重放。
存解析结果的话，解析器一变夹具就废了，得重新联网抓 —— 而「联网」正是夹具
要消灭的东西。体积也站在这一边：一页 HTML 几 KB，解析结果里的字段比它还多。

## ※ 抓不到时**不写空夹具**

「抓失败」与「抓到了、只是页面是空的」形状相同（都是 0 字节），而空夹具会
**覆盖掉一份好的夹具**，并且看起来像「抓到了，只是站点没内容」——
正是 `docs/数据源层.md` §2.6 说的那类「产出为空」的失败。
⇒ 本模块的顺序是**先全部拿到、再落盘**：任何一步失败都在写盘之前抛出去。

## 红线：只碰公开的、robots.txt 允许的页面

`robots.txt` 在**抓取之前**读（见 `_assert_robots_allows`）。不允许就不抓，
不绕、不换 UA 重试。这条不是建议 —— 需要登录、需要绕反爬的站点一律不做。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

from yechao.domain.errors import SourceFormatError, SourceUnavailable

#: 自报身份。※ 带一个能让人找到出处的串 —— 被站点管理员看见时，
#: 「这是谁在抓」应当是能回答的。
USER_AGENT = "yechao-ingest/0.1 (campus-notice-digest demo; contact: example.test)"

#: 单次请求超时（秒）。不设的话一个卡住的连接会挂住整个摄取。
DEFAULT_TIMEOUT = 15.0

#: 侧车的后缀。**它同时是「这是不是一份夹具」的判据** —— 见 `web_source._classify`。
META_SUFFIX = ".meta.json"


# ── robots ──────────────────────────────────────────────────────────


def robots_allows(robots_text: str, url: str, *, user_agent: str) -> bool:
    """给定 `robots.txt` 的**正文**，判断能不能抓 `url`。**纯函数。**

    ※ 空字符串 ⇒ 允许全部。这不是「宽容」，是 RFC 9309 的规定：**没有
    robots.txt（或它取不到、返回 4xx）就是没有限制**。把它当成「禁止」会让
    一个不写 robots.txt 的站点永远抓不了，而那是对规范的误读。

    ※ 反过来，**取不到 robots.txt 的原因如果不是 4xx（网络错误、5xx、
    403），本函数不会被调用** —— 那种情况由 `_assert_robots_allows` 直接
    判为「不抓」。判据：不确定就不抓。理由是一个站点把 robots.txt 临时挂掉
    通常意味着它在维护或封禁，而「读不到规则 = 没有规则」在那种时刻恰恰是
    最不该采取的解读。
    """
    parser = RobotFileParser()
    parser.parse(robots_text.splitlines())
    return parser.can_fetch(user_agent, url)


def _assert_robots_allows(
    url: str, *, opener: object, user_agent: str, timeout: float
) -> None:
    """抓之前的闸门。不允许就抛，**不绕**。"""
    robots_url = urljoin(url, "/robots.txt")
    request = urllib.request.Request(robots_url, headers={"User-Agent": user_agent})
    try:
        with opener(request, timeout=timeout) as resp:  # type: ignore[operator]
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            # RFC 9309：4xx ⇒ 视为没有任何限制。
            text = ""
        else:
            raise SourceUnavailable(
                f"{robots_url} 读不到（HTTP {e.code}）—— 读不到规则就不抓。",
                robots_url=robots_url,
                status=e.code,
            ) from e
    except urllib.error.URLError as e:
        raise SourceUnavailable(
            f"{robots_url} 读不到（{e.reason}）—— 读不到规则就不抓。",
            robots_url=robots_url,
        ) from e

    if not robots_allows(text, url, user_agent=user_agent):
        raise SourceUnavailable(
            f"robots.txt 不允许抓取 {url}。本模块**不绕** robots："
            f"不换 UA、不换路径、不重试。要读这个站，请换一个允许抓的入口。",
            url=url,
            robots_url=robots_url,
        )


# ── 取字节 ──────────────────────────────────────────────────────────


def fetch_bytes(
    url: str,
    *,
    user_agent: str = USER_AGENT,
    timeout: float = DEFAULT_TIMEOUT,
    opener: object = None,
) -> bytes:
    """抓一页，返回**原始字节**。读不到 ⇒ `SourceUnavailable`。

    ※ 返回**字节**而不是 `str`：解码是解析那一侧的事（`web_parse.decode_html`），
    而它要能看见「页面声明了什么编码」。在这里先解一次会把编码声明丢掉，
    然后在解析侧再按默认编码解一次 —— 两处口径，漂移的症状是「有些页面乱码」。

    `opener` 只为测试留的注入口（默认走 `urllib.request.urlopen`）。
    """
    op = opener or urllib.request.urlopen
    _assert_robots_allows(url, opener=op, user_agent=user_agent, timeout=timeout)

    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with op(request, timeout=timeout) as resp:  # type: ignore[operator]
            return resp.read()
    except urllib.error.HTTPError as e:
        raise SourceUnavailable(
            f"抓 {url} 失败：HTTP {e.code}", url=url, status=e.code
        ) from e
    except urllib.error.URLError as e:
        raise SourceUnavailable(f"抓 {url} 失败：{e.reason}", url=url) from e


# ── 落盘 ────────────────────────────────────────────────────────────


def fetch_to_file(
    url: str,
    dest: str | Path,
    *,
    user_agent: str = USER_AGENT,
    timeout: float = DEFAULT_TIMEOUT,
    opener: object = None,
    note: str = "",
) -> dict:
    """抓一次，落成 `dest` 与它的侧车 `<dest 去后缀>.meta.json`。

    ※ **顺序是有意的**：先抓、再校验、最后才写。任何一步失败都在写盘之前
    抛出去 —— 于是「抓不到」永远不会留下一个空的 `dest`（见模块 docstring）。

    ※ 落盘走**先写临时文件再改名**（`os.replace` 同盘内原子）。
    直接覆盖的话，写到一半失败会把上一份**好的**夹具毁成半截，而半截的 HTML
    仍然能被解析器读出一部分 —— 它看起来像「站点少发了几条」。

    返回侧车的内容（也写进了磁盘），给调用方打印用。
    """
    raw = fetch_bytes(
        url, user_agent=user_agent, timeout=timeout, opener=opener
    )
    if not raw:
        # ※ 0 字节**不是**一份夹具。它是「抓到了但什么都没拿到」——
        # 而那与「页面是空的」形状相同。写下去会覆盖掉好的那份。
        raise SourceUnavailable(
            f"{url} 抓回来是 0 字节 —— 不当成一份夹具写盘。"
            f"（空夹具会覆盖掉上一份好的，且看起来像「站点没内容」）",
            url=url,
        )

    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    try:
        tmp.write_bytes(raw)
        os.replace(tmp, path)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise SourceUnavailable(f"夹具写不进 {path}：{e}", dest=str(path)) from e

    meta = {
        "fixture": path.name,
        "source_url": url,
        "fetched_at": int(time.time()),
        "bytes": len(raw),
        # ※ 这份指纹是**落盘那一刻**这一页的样子。它的用途是发现
        # 「HTML 被改过但侧车没跟着重新生成」—— 素材换了而夹具没换，
        # 演示出来的数字就与实际不符，而那是评委会看到的差异。
        "sha256": hashlib.sha256(raw).hexdigest(),
        "robots": "allowed",
        "user_agent": user_agent,
        "note": note,
    }
    meta_path = path.with_suffix(META_SUFFIX)
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return meta


# ── 抓取这一步的入口 ────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """`python -m yechao.infrastructure.sources.web_fetch <url> <dest.html>`

    ※ 这**不是产品入口** —— 产品入口是 `python -m yechao ingest`，它只读
    **已经落盘的东西**。这个入口是「造夹具」那一步用的，跑一次就够，
    之后演示与测试都不再需要网络。

    它刻意不挂在 `cli.py` 上：`ingest` 是「读源 → 入库」，而这一步只
    「把字节拿下来」，不碰库、不产出消息。混进同一个命令会让
    `ingest` 在不该碰网络的时候碰网络。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("用法：python -m yechao.infrastructure.sources.web_fetch <url> <dest.html>")
        print("     抓一次公开页面，落成 dest.html 与它的侧车 dest.meta.json。")
        return 2

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        # 中文 Windows 控制台是 GBK。改不了就算了 —— 它只影响打印，不影响抓取。
        pass

    url, dest = args
    try:
        meta = fetch_to_file(url, dest)
    except SourceUnavailable as e:
        print(f"抓不到：{e}")
        return 1
    except SourceFormatError as e:
        print(f"抓到了但不对：{e}")
        return 1

    print(f"已落盘 {dest}")
    print(f"  来源      {meta['source_url']}")
    print(f"  字节      {meta['bytes']}")
    print(f"  sha256    {meta['sha256']}")
    print(f"  robots    {meta['robots']}")
    print("下一步：把它当夹具读 ——")
    print(f"  YECHAO_WEB_SOURCE={dest} python -m yechao ingest --source web")
    return 0


if __name__ == "__main__":  # pragma: no cover - 手动跑的入口
    raise SystemExit(main())
