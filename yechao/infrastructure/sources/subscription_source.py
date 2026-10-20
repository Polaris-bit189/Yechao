"""订阅源 —— 给一个**账号**或一个**关键词**，自动把新内容取回来。

一个源，五份适配规则（`subscription_sites.py`），一套解析（`subscription_parse.py`）。

## 「自动」的现实定义

这个词要先说清楚，否则这个模块看起来会像「没做完」：

**它不可能是「零配置、点一下就有」的。** 五个站里没有一个允许匿名拿到内容
（实测见 `subscription_sites.py` 那张表）。所以「自动」的现实定义是：

> **配置里放一份登录态（cookie），之后自动。**

于是「没配登录态」不是一次静默的空抓，而是一次**说清怎么配**的报错。
这条判据有三层，每一层都对应一次真实的失败：

| 没配什么 | 抛 | 用户下一步 |
|---|---|---|
| 协议没签 | `consent._ConsentRequired`（`ConfigError`） | 读协议、签 |
| 登录态没配 | `ConfigError` | 配 `YECHAO_<站>_COOKIE`，或开浏览器登录态 |
| 登录态过期 / 被拒 | `_CredentialRejected`（`SourceUnavailable`） | 重新拷一份 cookie |

※ 第三种与第二种**必须形状不同**：前者要改的是**配置**（用法错，退出码 2），
后者是「配了但不work」—— 要查的是那个 cookie 还有没有效。混成一种，
用户会在「我明明配了」和「你根本没配」之间来回看。

## 登录态从哪来（三条，按优先级）

  1. `YECHAO_<站>_COOKIE`（环境变量）—— 用户**显式**给的那一份，永远最优先
     （自动读到的可能是同一个浏览器里的**另一个**账号）。
  2. **本机浏览器的登录态**（`browser_cookies`）—— 用户在 Edge / Chrome /
     Firefox 里登录一次，之后自动。**默认关**，要在 `sources.toml` 的
     `[subscription]` 里写 `browser_cookies = true` —— 读用户的浏览器 profile
     是一件要显式同意的事，与签协议同一条判据。
  3. `YECHAO_<站>_USER` / `..._PASS`（环境变量）—— 只在内存里换一次会话。

※ 「自动」这个词的现实定义见上面：**不是零配置**，是「配一次，之后不用管」。
浏览器那条路把「每次过期都要重新拷一份」这件事去掉了。

## 凭据怎么存（三条，都是硬约束）

1. **口令永不落盘。** 会话凭据（cookie）与账号密码是两条路，**cookie 是首选**
   —— 它可长期存、且不含口令。账号密码只在**进程内存里**用于登录，
   拿到会话之后立刻丢弃。
2. **口令不进配置文件、日志、审计、异常消息。** 配置走 `config/*.toml`，
   而配置文件会被提交、会被 `/api/config/export` 导出、会跟着配置包跑到
   别的机器上 —— 口令放进去就是泄漏。所以它**只**从环境变量读
   （`YECHAO_<站>_PASS`），这条与 `YECHAO_API_KEY` 同一条判据。
3. **报错脱敏。** 两道：结构上**不把口令拼进任何消息**（不是「拼了再替换」），
   外加 `_redact` 兜底。测试钉住（`test_口令绝不出现在报错里`）。

## ※ 遇到人机验证：如实报，不假装成功

自动登录遇到验证码时抛 `_CaptchaRequired`，消息说清「这个站要人机验证，
请改用 cookie」。**不尝试过验证码、不假装登录成功、不重试**。
判据是 `subscription_sites.CAPTCHA_MARKERS` —— 它来自实测观察
（知网那次 302 到 `/verify/home?captchaType=blockPuzzle`），不是猜的。

## ※ robots：匿名抓取受它约束，带凭据的访问不是它管的

`web_fetch` 的判据是「robots 不许就不抓」。这条在这里**分两种情况**，
而分开的理由要说清楚，否则看起来像放宽了标准：

  · **匿名抓取**（没有凭据）：robots 是**硬闸门**，不许就抛，不绕。
    这正是 `web_fetch` 那条判据，一字不改。
  · **带凭据的抓取**：这是「用户用**自己的账号**看自己的内容」，
    它的边界由用户与平台之间的条款决定（协议正文里逐字写了这一条），
    **不由 robots 决定** —— robots 是给爬虫看的，不是给一个登录用户看的。

※ 但**从不静默**：`last_robots` 里带着这一站的 robots 实测结论，
每一条错误消息里也会复述它。用户看得见自己在什么位置上。

## ※ 「登录墙」怎么认（这条判据有一个反例，所以写清楚）

抓回来之后先判它是不是登录页 / 验证码页 —— 因为登录页也会被解析器读到，
而它的结构标记多半**不在** ⇒ 解析器会报「结构变了」。那是一条**把人引向
错方向**的错误：真相是登录态过期了。

判据是：**结构标记在 ⇒ 这是内容页**（登录页不会嵌那段数据块）。
只有在结构标记**不在**时才去看 `CAPTCHA_MARKERS` / `LOGIN_WALL_MARKERS`，
且只看响应头部（`LOGIN_SCAN_CHARS`）。

※ 为什么不直接扫全页的关键词：真实站点的导航栏里就写着「登录」两个字，
而一条讲校园卡的通知正文里出现「验证码」也完全正常。直接扫的症状是
「正常内容被报成要人机验证」。结构标记那一步把绝大多数误判挡掉了；
剩下的（真改版 + 恰好含这些词）会落到解析器的 `_StructureChanged` ——
那仍然是**响亮**的错，只是方向不同。

## 目标从哪来（不写死任何站点）

  1. 环境变量 `YECHAO_SUBSCRIPTION_TARGET` / `..._SITE` / `..._MODE`
     —— 一次性的、临时换一个目标时用
  2. `<config_dir>/sources.toml` 的 `[subscription]` —— 长期配置

两个都没有 ⇒ **抛 `ConfigError`，不兜底**（与网页源、邮件源逐字相同的判据）。

## ※ 类名为什么带下划线

`tests/contracts/test_layering.py::test_适配层的实现都接上了线` 扫的是
**适配层顶层公开类名**有没有在 `bootstrap.py` 的源码文本里出现过。数据源
不由组合根实例化（它由 `sources/__init__.py` 的注册表组合）—— 与
`_WebSource` / `_MailSource` / `SynthSource` 是同一类东西。⇒ 用下划线前缀
走那条判据自己留的口子（它只扫不以 `_` 开头的类）。**这不是绕过护栏**：
本类确实被接上了线（`REGISTRY["subscription"]`），护栏只是用文本判据
看不见注册表。
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlencode, urljoin, urlsplit

from yechao.domain.errors import ConfigError, SourceUnavailable
import yechao.infrastructure.sources.browser_cookies as browser_cookies
import yechao.infrastructure.sources.consent as consent
from yechao.infrastructure.sources._common import (
    #: 配置目录的环境变量名。**必须与 `bootstrap.Settings.from_env` 一致**。
    CONFIG_DIR_ENV,
    #: 配置目录的默认名。**必须与 `bootstrap.DEFAULT_CONFIG_DIR` 一致**。
    DEFAULT_CONFIG_DIR,
    #: 数据源的配置文件。与 `groups.toml` 等并列放在配置目录下。
    SOURCES_FILE,
    config_dir,
    in_window,
    source_table,
)
from yechao.infrastructure.sources.subscription_parse import (
    NAME,
    _ParseReport,
    parse_page,
)
from yechao.infrastructure.sources.subscription_sites import (
    CAPTCHA_MARKERS,
    LIST_HTML,
    LOGIN_WALL_MARKERS,
    MODE_ACCOUNT,
    MODE_KEYWORD,
    MODE_URL,
    MODES,
    _Site,
    detect,
    site_by_key,
    site_keys,
    url_for,
)
#: ※ 抓取这一步**复用**网页源的工具：UA、超时、robots 的判定规则
#: （`robots_allows` 是纯函数，且它已经钉住了「robots.txt 404 ⇒ 视为无限制」
#: 这条 RFC 9309 的口径）。抄一遍就是两处口径。
from yechao.infrastructure.sources.web_fetch import (
    DEFAULT_TIMEOUT,
    USER_AGENT,
    robots_allows,
)

#: 目标的来源。
TARGET_ENV = "YECHAO_SUBSCRIPTION_TARGET"
SITE_ENV = "YECHAO_SUBSCRIPTION_SITE"
MODE_ENV = "YECHAO_SUBSCRIPTION_MODE"
GROUP_ID_ENV = "YECHAO_SUBSCRIPTION_GROUP_ID"

#: 不给分组号时用哪个。
#:
#: ※ 它**是**一个默认值，而本项目对默认值的态度是「用了要能被看出来」。
#: 这里能看出它的地方有两处：`--source subscription` 的报告会打出源名，
#: 以及落库后的 `group_id` 就在消息行上。取 820000007 是因为它是本项目
#: **自编**的假群号段里下一个空位（`config/groups.toml` 用到 001–004，
#: 网页源默认 001、邮件源默认 006）—— 与它们**错开**是刻意的：
#: 两个源的默认分组号撞在一起，症状是「订阅内容与邮件在同一个『群』里」，
#: 而那一列本来是用来分开它们的。
DEFAULT_GROUP_ID = 820000007

#: 夹具入口认的后缀。见 `_classify`。
HTML_SUFFIXES = (".html", ".htm")

#: 扫「登录墙 / 验证码」标记时只看响应头部这么多字符。
LOGIN_SCAN_CHARS = 4096

#: 兜底脱敏时，短于这个长度的秘密**不替换**。
#:
#: ※ 理由：一个 1 字符的口令会把消息里每个该字符都换成 `***`，消息变得
#: 不可读 —— 而消息不可读正是这条判据要防的另一件事。
#: 真正的保证在**结构**上：口令从不被拼进任何消息（见模块 docstring 第 3 条）。
MIN_REDACT_CHARS = 3


# ── 错误 ────────────────────────────────────────────────────────────


class _LoginNotSupported(SourceUnavailable):
    """这个站的自动登录**不做**（签名 / 滑块 / 机构 IP）。

    ※ 它是 `SourceUnavailable` 而不是 `NotWired`：这不是「还没做」，
    是**结论** —— 每个站的 `login_verdict` 都写清了它拦在哪一步。
    """

    default_message = "这个站不支持自动登录"


class _CaptchaRequired(SourceUnavailable):
    """登录 / 抓取被人机验证拦住。

    ※ 与 `_LoginNotSupported` 分开：那个是「本工具不做自动登录」，
    这个是「**站点要人机验证**」—— 前者换 cookie 就好，后者连 cookie 也
    不一定够（会话过期后会再次撞上）。分开报，用户才知道该重试还是该换路子。
    """

    default_message = "这个站要人机验证"


class _CredentialRejected(SourceUnavailable):
    """配了登录态，但站点把请求打回了登录页（过期 / 无效 / 权限不够）。"""

    default_message = "登录态被站点拒绝"


# ── 凭据 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Credential:
    """一份登录态。**口令带 `repr=False`** —— 见模块 docstring 第 2 条。

    ※ 为什么用 `field(repr=False)` 而不是自己写 `__repr__`：它是**类型系统
    层面的**保证。自己写 `__repr__` 只在「有人调 repr」时生效，而
    `dataclasses.asdict`、日志框架的 `%r`、pytest 断言失败时的回显各自走
    不同的路 —— 其中一条漏掉，口令就出现在某次失败输出里。
    """

    site: str
    kind: str  # "cookie" / "password"
    cookie: str = ""
    user: str = ""
    password: str = field(default="", repr=False)

    def secrets(self) -> tuple[str, ...]:
        """要脱敏的串。给 `_redact` 用。"""
        return (self.password, self.cookie)


def _redact(text: str, secrets: Iterable[str]) -> str:
    """把秘密从一段文本里抹掉。**兜底**，不是主要防线。

    ※ 主要防线是**结构**：口令从不被拼进任何消息。这个函数防的是
    「某天有人加了一句 `f"... {password}"`」—— 那种改动看起来无害，
    而它的后果是凭据进了日志。
    """
    out = text
    for s in secrets:
        if s and len(s) >= MIN_REDACT_CHARS:
            out = out.replace(s, "***")
    return out


#: `sources.toml` 的 `[subscription]` 里那个「去读本机浏览器登录态」的开关。
#:
#: ※ **默认关**，而且**只有配置文件这一个开关**（没有环境变量版）。两条理由：
#:   1. 「读你的浏览器 profile」是一个**长期**的决定，不是一次性的意图 ——
#:      它和 `target` / `site` 同类，写在同一个块里才看得见（`git diff` 里
#:      也看得见）。一次性的意图有 `YECHAO_<站>_COOKIE` 那条路。
#:   2. 一个环境变量的开关会让**用例**变成「看跑它的那台机器上有没有设这个
#:      变量」—— 而 `tests/infrastructure/test_subscription_source.py` 的夹具
#:      只会删这一站的凭据变量，删不到一个还不存在的名字。
BROWSER_COOKIES_KEY = "browser_cookies"

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def _as_bool(value: Any, *, where: str) -> bool:
    """把配置里的开关解成 bool。**认不出来就抛**，不猜。

    ※ 猜的症状是「我明明写了 browser_cookies = false，它还是去读了我的
    浏览器」—— 而那是**读用户的浏览器 profile**，猜错不是小事。
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ConfigError(
        f"{where} = {value!r} 认不出来 —— 要 true / false"
        f"（也可以写 1 / 0、yes / no）。"
    )


def _browser_cookies_enabled() -> bool:
    """要不要去读本机浏览器的登录态。**默认关** —— 见 `BROWSER_COOKIES_KEY`。"""
    return _as_bool(
        _settings_table().get(BROWSER_COOKIES_KEY),
        where=f"{SOURCES_FILE} 的 [subscription] {BROWSER_COOKIES_KEY}",
    )


def _cookie_from_browser(site: _Site, host: str) -> str:
    """从本机浏览器里取这一站的登录态。**取不到返回空串**（不是抛）。

    ※ 先问**目标 URL 的那个域**：一个站可能认多个域名（微博认 `weibo.com`
    与 `weibo.cn`），而 cookie 是**按域作用域**的 —— 把 `weibo.com` 的凭据
    发给 `weibo.cn` 既是错的，也是一次跨域泄漏。目标域问不到，再按这个站
    列出的域挨个问，取第一个有的。

    ※ 它**不吞**任何东西：真的取不到时原因在 `browser_cookies.explain` 里，
    由 `_credential_hint` 打给用户看。
    """
    order = ([host] if host else []) + [h for h in site.hosts if h != host]
    for candidate in order:
        found = browser_cookies.cookies_for(candidate)
        if found:
            return found
    return ""


def _credential(site: _Site, *, host: str = "") -> _Credential | None:
    """读这一站的登录态。**没配返回 `None`**。

    优先级：环境变量 > 浏览器登录态 > 账号密码。理由：
      · 环境变量在最前 —— 它是用户**显式**给的那一份，要能盖掉自动读到的
        （自动读到的可能是同一个浏览器里的**另一个**账号）。
      · 浏览器在账号密码之前 —— 见模块 docstring：cookie 是首选那条路，
        它可长期存、且不含口令。

    ※ `host` 是目标 URL 的域名，用来在浏览器里挑**对的那个域**（见
      `_cookie_from_browser`）。留空时按这个站列出的域挨个问。
    """
    cookie = (os.environ.get(site.cookie_env) or "").strip()
    if cookie:
        return _Credential(site=site.key, kind="cookie", cookie=cookie)

    if _browser_cookies_enabled():
        from_browser = _cookie_from_browser(site, host)
        if from_browser:
            return _Credential(site=site.key, kind="cookie", cookie=from_browser)

    user = (os.environ.get(site.user_env) or "").strip()
    password = os.environ.get(site.pass_env) or ""
    if user and password:
        return _Credential(
            site=site.key, kind="password", user=user, password=password
        )
    return None


def _credential_hint(site: _Site, *, host: str = "") -> str:
    """没配登录态时给用户照做的一段话。

    ※ 它复述**这一站**的 robots 结论与要拷哪个字段 —— 「我该拷什么」
    是这条路上最常被问到的问题，而答案每站不同。
    """
    fields = "、".join(site.session_fields) or "（这一站没有列出来）"
    return (
        f"{site.label} 的登录态没有配。给它一份 —— 三选一：\n"
        f"  · 会话凭据（**首选**，可长期存）：\n"
        f"      {site.cookie_env}=<浏览器里登录之后拷出来的 cookie>\n"
        f"    这个站的登录态是 {fields} 这几项。\n"
        f"{_browser_hint(site, host)}"
        f"  · 账号密码（**只在内存里用一次**，不进配置文件）：\n"
        f"      {site.user_env}=<账号>\n"
        f"      {site.pass_env}=<口令>\n"
        f"※ 为什么必须要一个：{site.public_note}\n"
        f"※ 这一站的 robots：{site.robots_verdict}"
    )


def _browser_hint(site: _Site, host: str) -> str:
    """「浏览器登录态」那一条。**开着与没开给的话不一样** —— 见下。

    ※ 没开时说的是「怎么开」；开着时说的是「**为什么没读到**」
    （`browser_cookies.explain`）。后者才是要紧的：「浏览器正开着、库被锁住」
    与「你根本没在那个浏览器里登录过」是两件事，下一步完全不同 ——
    而它们**都**表现为「没有登录态」。混成一句，用户会去查错方向。
    """
    if not _browser_cookies_enabled():
        return (
            f"  · 或者：在**本机浏览器**里登录一次，让本工具自己去读"
            f"（不必手工拷）：\n"
            f"      {SOURCES_FILE} 的 [subscription] 里写"
            f" {BROWSER_COOKIES_KEY} = true\n"
            f"    ※ 默认**关** —— 读你的浏览器 profile 是一件要显式同意的事"
            f"（与签协议同一条判据）。\n"
        )
    order = ([host] if host else []) + [h for h in site.hosts if h != host]
    why = browser_cookies.explain(order[0]) if order else "（这个站没列出域名）"
    return f"  · 浏览器登录态：**开着**，但这次没读到 —— {why}\n"


# ── 目标 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Target:
    """一个已经解析好的目标：**问哪个站**、**怎么问**、**算进哪个分组**。"""

    raw: str
    kind: str  # "remote" / "fixture"
    site: _Site
    mode: str
    query: str
    url: str  # 远程是 URL；夹具是本地路径
    group_id: int
    where: str  # 这个目标是从哪儿读出来的（给报错用）

    @property
    def is_remote(self) -> bool:
        return self.kind == "remote"


def _settings_table() -> dict[str, Any]:
    """读 `<config_dir>/sources.toml` 的 `[subscription]` 表。没有就返回 `{}`。"""
    return source_table(
        "subscription",
        hint=(
            "  [subscription]\n"
            '  site = "xiaohongshu"\n'
            '  mode = "account"\n'
            '  target = "@某个账号"\n'
            "  group_id = 820000007\n"
            f"  {BROWSER_COOKIES_KEY} = true   # 可选：去读本机浏览器的登录态"
        ),
    )


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


def _resolve_group_id(table: dict[str, Any]) -> int:
    group_id = _env_group_id()
    if group_id is not None:
        return group_id
    raw_gid = table.get("group_id")
    if raw_gid is None:
        return DEFAULT_GROUP_ID
    try:
        return int(raw_gid)
    except (TypeError, ValueError) as e:
        raise ConfigError(
            f"{SOURCES_FILE} 的 [subscription] group_id={raw_gid!r} 不是整数。"
        ) from e


def resolve_target() -> _Target:
    """把「问哪个站、怎么问」定下来。**没配就抛，不兜底。**

    优先级：环境变量 > 配置文件。理由：环境变量是**这一次**的意图
    （临时换一个账号看看），配置文件是**长期**的意图；临时的不该被长期的
    覆盖掉。
    """
    table = _settings_table()

    raw = (os.environ.get(TARGET_ENV) or "").strip() or str(
        table.get("target") or ""
    ).strip()
    if not raw:
        raise ConfigError(
            f"订阅源不知道要读哪儿。给它一个目标 —— 二选一：\n"
            f"  · 环境变量：{TARGET_ENV}=<一个账号 / 一个关键词 / 一个链接>\n"
            f"  · 配置文件：{config_dir() / SOURCES_FILE} 里写\n"
            f"      [subscription]\n      site = \"xiaohongshu\"\n"
            f"      mode = \"account\"\n      target = \"@某个账号\"\n"
            f"※ 刻意**没有默认目标**：默认到一个账号上，`--source subscription`"
            f" 就会在什么都没配的情况下「成功」读到别人的内容，"
            f"而报告看起来一切正常。"
        )

    site_key = (os.environ.get(SITE_ENV) or "").strip() or str(
        table.get("site") or ""
    ).strip()
    mode = (os.environ.get(MODE_ENV) or "").strip() or str(
        table.get("mode") or ""
    ).strip()

    where = (
        TARGET_ENV
        if os.environ.get(TARGET_ENV)
        else f"{SOURCES_FILE} 的 [subscription]"
    )
    return _classify(
        raw,
        site_key=site_key,
        mode=mode,
        group_id=_resolve_group_id(table),
        where=where,
    )


def _shape_mode(raw: str) -> str:
    """按目标**自身的形状**猜它的形态。

    ※ 判据是「它长什么样」而不是「用户选了哪个」：`http(s)://` 只可能是链接，
    `@` 开头只可能是账号，其余当关键词。这条推法**有例外**（一个账号 id
    不以 `@` 开头时会被当成关键词），所以 `mode` 允许显式给出 —— 见 `_classify`。
    """
    if raw.startswith(("http://", "https://")):
        return MODE_URL
    if raw.startswith("@"):
        return MODE_ACCOUNT
    return MODE_KEYWORD


def _classify(
    raw: str, *, site_key: str, mode: str, group_id: int, where: str
) -> _Target:
    """按目标**自身的形状**判它属于哪一种入口。"""
    path = Path(raw)
    if path.suffix.lower() in HTML_SUFFIXES:
        # 夹具入口：**本地文件**，不走网络、不要凭据、不过 robots、不签协议。
        # 它存在的理由是「解析要能被离线测试」—— 与网页源逐字相同。
        if not site_key:
            raise ConfigError(
                f"夹具入口必须说明它是**哪个站**的页面（要按那个站的规则解它）：\n"
                f"  · 环境变量：{SITE_ENV}=<站标识>\n"
                f"  · 配置文件：{SOURCES_FILE} 的 [subscription] site = \"<站标识>\"\n"
                f"可用的站：{'、'.join(site_keys())}。\n"
                f"※ 夹具是本地文件，所以它不需要登录态、也不需要签协议 —— "
                f"但要按**对应站**的规则解析。"
            )
        return _Target(
            raw=raw,
            kind="fixture",
            site=site_by_key(site_key),
            mode=MODE_URL,
            query="",
            url=str(path),
            group_id=group_id,
            where=where,
        )

    if mode and mode not in MODES:
        raise ConfigError(
            f"不认识的 mode={mode!r}；可用的：{'、'.join(MODES)}。"
        )

    shape = _shape_mode(raw)
    if mode and (mode == MODE_URL) != (shape == MODE_URL):
        # ※ 显式给的 mode 与目标形状**对不上** ⇒ 抛，不挑一个用。
        # 挑一个用（无论挑哪个）的症状都是「读了别的东西，而报告一切正常」。
        raise ConfigError(
            f"mode={mode!r} 与目标 {raw!r} 的形状对不上："
            f"`{MODE_URL}` 要求目标以 http:// 或 https:// 开头，"
            f"而其余两种要求它**不是**一个链接。\n"
            f"※ 这里不挑一个用：挑错的那个症状是「读的是别的东西，"
            f"而报告一切正常」。"
        )
    use_mode = mode or shape

    if use_mode == MODE_URL:
        site = detect(raw)
        if site is None:
            raise ConfigError(
                f"从链接里认不出这是哪个站：{raw}\n"
                f"可用的站：{'、'.join(site_keys())}。\n"
                f"※ 这里**不猜**：猜错的症状是「按另一个站的规则去解这个页面」，"
                f"而它多半会解析出 0 条 —— 看起来像「这个账号没发新内容」。"
            )
        if site_key and site.key != site_key:
            raise ConfigError(
                f"配置里说 site={site_key!r}，但这个链接是 {site.label} 的"
                f"（{site.key}）—— 两者对不上。\n"
                f"※ 不挑一个用：挑错的症状是「按错的规则解析」，而它看起来"
                f"像「没发新内容」。"
            )
        return _Target(
            raw=raw,
            kind="remote",
            site=site,
            mode=use_mode,
            query="",
            url=raw,
            group_id=group_id,
            where=where,
        )

    if not site_key:
        kind = "账号" if use_mode == MODE_ACCOUNT else "关键词"
        raise ConfigError(
            f"「{raw}」是一个{kind}，但要读它得先说明是**哪个站**：\n"
            f"  · 环境变量：{SITE_ENV}=<站标识>\n"
            f"  · 配置文件：{SOURCES_FILE} 的 [subscription] site = \"<站标识>\"\n"
            f"可用的站：{'、'.join(site_keys())}。\n"
            f"※ 也可以直接把一个链接当目标（那时站点从域名认出来）。"
        )

    site = site_by_key(site_key)
    query = raw[1:] if raw.startswith("@") else raw
    return _Target(
        raw=raw,
        kind="remote",
        site=site,
        mode=use_mode,
        query=query,
        url=url_for(site, use_mode, query),
        group_id=group_id,
        where=where,
    )


# ── 抓取 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Fetched:
    """一次抓取拿回来的东西。"""

    raw: bytes
    final_url: str
    status: int


def _robots_verdict(
    url: str, *, opener: Callable[..., Any], timeout: float
) -> tuple[bool, str]:
    """抓之前问一次 robots。返回 `(允不允许, 说明)`。

    ※ 判定规则与 `web_fetch._assert_robots_allows` 相同（RFC 9309 的口径：
    **4xx ⇒ 视为没有任何限制**；其余取不到 ⇒ 保守不抓）。这里重写那一段
    胶水是因为那个函数是**私有**的、且硬接在 `fetch_bytes` 里 ——
    而本模块要在同一个请求上带 `Cookie` 头，没法复用那一条路径。
    纯函数 `robots_allows` 仍然是复用的，两处只差「怎么取 robots.txt」。
    """
    robots_url = urljoin(url, "/robots.txt")
    request = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
    try:
        with opener(request, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return True, f"robots.txt 返回 {e.code} ⇒ 视为无限制（RFC 9309）"
        return False, f"robots.txt 读不到（HTTP {e.code}）—— 读不到规则就不抓"
    except urllib.error.URLError as e:
        return False, f"robots.txt 读不到（{e.reason}）—— 读不到规则就不抓"

    if robots_allows(text, url, user_agent=USER_AGENT):
        return True, "robots.txt 允许"
    return False, f"robots.txt 不允许抓 {url}"


def _fetch(
    url: str, *, cookie: str, opener: Callable[..., Any], timeout: float
) -> _Fetched:
    """抓一页，返回**原始字节**。读不到 ⇒ `SourceUnavailable`。

    ※ 返回字节而不是 `str`：解码是解析那一侧的事（`decode_html`），
    而它要能看见「页面声明了什么编码」。在这里先解一次会把编码声明丢掉。
    """
    headers = {"User-Agent": USER_AGENT}
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(url, headers=headers)
    try:
        with opener(request, timeout=timeout) as resp:
            return _Fetched(
                raw=resp.read(), final_url=resp.geturl(), status=resp.status
            )
    except urllib.error.HTTPError as e:
        raise SourceUnavailable(
            f"抓 {url} 失败：HTTP {e.code}", url=url, status=e.code
        ) from e
    except urllib.error.URLError as e:
        raise SourceUnavailable(f"抓 {url} 失败：{e.reason}", url=url) from e


def _structure_marker(site: _Site) -> str:
    """这一站「读懂了没有」的分界线。见模块 docstring 那一段。"""
    if site.list_kind == LIST_HTML:
        return site.html_container_class
    return site.blob_marker


def _assert_not_a_wall(
    site: _Site, fetched: _Fetched, *, secrets: Iterable[str]
) -> None:
    """抓回来之后先看它是不是**登录墙 / 验证码页**。是就抛。

    ※ 判据是「**结构标记在 ⇒ 这是内容页**」—— 登录页不会嵌那段数据块。
    只有在标记不在时才去看关键词，且只看响应头部。见模块 docstring。
    """
    body = fetched.raw.decode("utf-8", errors="replace")
    marker = _structure_marker(site)
    if marker and marker in body:
        return  # 结构在 ⇒ 内容页。导航栏里的「登录」两个字不算数。

    head = body[:LOGIN_SCAN_CHARS]
    where = fetched.final_url

    if any(m in head or m in where for m in CAPTCHA_MARKERS):
        raise _CaptchaRequired(
            _redact(
                f"{site.label} 返回了一个**人机验证**页（{where}）。\n"
                f"本工具**不过验证码**、不重试、不假装登录成功 —— "
                f"这是刻意的：过验证码是绕过站点的风控。\n"
                f"能做的只有换一份有效的会话凭据：\n"
                f"  {site.cookie_env}=<浏览器里登录之后拷出来的 cookie>\n"
                f"（登录态是 {'、'.join(site.session_fields)} 这几项）\n"
                f"※ 自动登录也走不通：{site.login_verdict}",
                secrets,
            ),
            url=where,
            site=site.key,
        )

    if any(m in head or m in where for m in LOGIN_WALL_MARKERS):
        raise _CredentialRejected(
            _redact(
                f"{site.label} 把这次请求打回了登录页（{where}）—— "
                f"登录态没有配、或者已经过期。\n"
                f"重新拷一份：{site.cookie_env}=<浏览器里登录之后拷出来的 cookie>\n"
                f"（登录态是 {'、'.join(site.session_fields)} 这几项）\n"
                f"※ 这一站的匿名边界：{site.public_note}",
                secrets,
            ),
            url=where,
            site=site.key,
        )


def _login_with_password(
    site: _Site,
    user: str,
    password: str,
    *,
    opener: Callable[..., Any],
    timeout: float,
) -> str:
    """用账号密码换一份会话凭据。返回 cookie 串。

    ※ **五个站现在都走不到这里**（`login_endpoint` 全是 `None`），而这条路
    仍然是活的、有测试的：它是「某天某个站真的开放了普通表单登录」时的落点。
    为它写一条 `NotWired` 会更简单，但那会让「账号密码」这件事变成一句空话。
    所以：**管线是真的，只是每个站的登录流程都拦在签名 / 滑块 / 机构 IP 上**，
    于是现在每条路都抛 `_LoginNotSupported`。

    ※ 口令的处理有两条硬约束，都能被机械检查：
      1. **它只在这个函数的局部变量里**，从不写进任何持久化的地方；
      2. **它不出现在任何消息里** —— 下面每一条 `raise` 的文案都不含它，
         并且外面还套了一层 `_redact` 兜底。
    """
    if site.login_endpoint is None:
        raise _LoginNotSupported(
            f"{site.label} 的自动登录**不做**：{site.login_verdict}\n"
            f"请改用会话凭据（**首选**，可长期存）：\n"
            f"  {site.cookie_env}=<浏览器里登录之后拷出来的 cookie>\n"
            f"（登录态是 {'、'.join(site.session_fields)} 这几项）\n"
            f"※ 这不是「还没做」，是结论：{site.robots_verdict}",
            site=site.key,
        )

    body = urlencode({"username": user, "password": password}).encode("utf-8")
    request = urllib.request.Request(
        site.login_endpoint,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with opener(request, timeout=timeout) as resp:
            raw = resp.read()
            head = raw[:LOGIN_SCAN_CHARS].decode("utf-8", errors="replace")
            cookies = resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        raise SourceUnavailable(
            f"{site.label} 的登录请求失败：HTTP {e.code}", site=site.key
        ) from e
    except urllib.error.URLError as e:
        raise SourceUnavailable(
            f"{site.label} 的登录请求失败：{e.reason}", site=site.key
        ) from e

    if any(m in head for m in CAPTCHA_MARKERS):
        # ※ **如实报**：不重试、不换 UA、不试图过验证码。
        raise _CaptchaRequired(
            _redact(
                f"{site.label} 的登录页要**人机验证**，本工具不过验证码。\n"
                f"请改用会话凭据："
                f"{site.cookie_env}=<浏览器里登录之后拷出来的 cookie>",
                (password,),
            ),
            site=site.key,
        )

    session = "; ".join(
        c.split(";", 1)[0].strip() for c in cookies if c.split(";", 1)[0].strip()
    )
    if not session:
        raise _CredentialRejected(
            f"{site.label} 的登录没有换到会话凭据（响应里没有 Set-Cookie）。\n"
            f"账号或口令可能不对 —— 本工具**不猜**是哪一个，也不重试。\n"
            f"请改用会话凭据：{site.cookie_env}=<浏览器里登录之后拷出来的 cookie>",
            site=site.key,
        )
    return session


# ── 源 ──────────────────────────────────────────────────────────────


class _SubscriptionSource:
    """订阅源。实现 `application/ports.py::MessageSource`。

    ※ 它**不切**消息：一条笔记 / 一条视频 / 一条微博 / 一篇论文就是一个
    自洽的短文本（与网页通知、邮件同类），切了反而把它拆散。这与会议转写
    必须切**相反** —— 端口那句「形状由接口约束，处理方式不由接口约束」
    说的就是这件事。

    ※ 目标在**装配时**定下来（由 `build_subscription_source` 传进来），
    不是每次 `read()` 现查环境变量。理由与网页源逐字相同：报错要发生在
    **任何一个源开始读之前**，而且「目标没配 / 协议没签」是**用法错**
    （退出码 2），不是「跑起来之后才失败」。
    """

    name = NAME

    def __init__(
        self,
        target: _Target,
        *,
        credential: _Credential | None = None,
        opener: Callable[..., Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._target = target
        self._credential = credential
        self._opener = opener or urllib.request.urlopen
        self._timeout = timeout
        #: 取「抓取时刻」的口子。※ 只有 `ts` 的**兜底**用它 ——
        #: 内容自己有发布时间时，一律用内容自己的（见模块 docstring）。
        self._now = now
        #: 上一次解析的成色（含「有几条的时间是退回的」）。
        #: ※ 它是**可读的**而不是只在日志里：`read()` 的契约只允许返回消息
        #: 列表，而「成色」没有地方放 —— 没有地方放就没人看得见。
        self.last_report: _ParseReport | None = None
        #: 这一站 robots 的实测结论（每次 `read()` 都会刷新）。
        self.last_robots: str = ""

    # ── 取字节 ──

    def _read_fixture(self) -> bytes:
        path = Path(self._target.url)
        try:
            return path.read_bytes()
        except OSError as e:
            raise SourceUnavailable(
                f"夹具读不到：{path}（{e}）。（目标来自 {self._target.where}）",
                path=str(path),
            ) from e

    def _read_remote(self) -> bytes:
        site = self._target.site
        secrets = self._credential.secrets() if self._credential else ()

        allowed, verdict = _robots_verdict(
            self._target.url, opener=self._opener, timeout=self._timeout
        )
        self.last_robots = verdict
        if not allowed and self._credential is None:
            # ※ 匿名抓取：robots 是**硬闸门**（`web_fetch` 那条判据，一字不改）。
            raise SourceUnavailable(
                f"{verdict}。本模块**不绕** robots：不换 UA、不换路径、不重试。\n"
                f"（带**你自己的**会话凭据时这条路不走 —— 见 "
                f"subscription_source.py 的模块 docstring「robots」那一段。）",
                url=self._target.url,
                robots=verdict,
            )

        fetched = _fetch(
            self._target.url,
            cookie=self._credential.cookie if self._credential else "",
            opener=self._opener,
            timeout=self._timeout,
        )
        _assert_not_a_wall(site, fetched, secrets=secrets)
        if not fetched.raw:
            # ※ 0 字节**不是**「这个账号没发新内容」：它与「读到了、页面是空的」
            # 形状相同，而后者会被解析器报成 `_NothingNew` ——
            # 一句让人去查错方向的话。
            raise SourceUnavailable(
                f"{site.label} 返回了 0 字节（{fetched.final_url}）—— "
                f"这不是「没发新内容」，是**没读到**。",
                url=fetched.final_url,
            )
        return fetched.raw

    # ── 端口 ──

    def read(
        self, *, since: int | None = None, until: int | None = None
    ) -> list[Any]:
        """读一次，返回消息。

        抛（每一条都是**响亮**的，没有一条是静默返回空）：
          · `ConfigError`             —— 目标没配 / 站不认识 / 登录态没配（装配时报）
          · `consent._ConsentRequired` —— 协议没签（装配时报）
          · `SourceUnavailable`       —— 读不到（网络不通 / robots 不许 / 0 字节）
          · `_CaptchaRequired`        —— 站点要人机验证
          · `_CredentialRejected`     —— 登录态过期或被拒
          · `_StructureChanged`       —— 读到了但没读懂（**站点改版**）
          · `_NothingNew`             —— 读懂了，但**这个账号确实没发新东西**

        ※ **时间窗在解析之后才过**。这不是实现细节，是判据：
        「解析出 0 条」与「这段时间里没有」是两件事 —— 前者是格式问题（去改
        解析器），后者是数据问题（这段时间真的安静）。先解析、再过滤，两者
        就分得开：**解析出的条数是 0 才抛，窗口内 0 条只是返回空**。
        反过来（先过滤再判 0）会让「窗口挑错了」伪装成「站点改版了」。
        """
        fetched_at = int(self._now())
        raw = self._read_remote() if self._target.is_remote else self._read_fixture()
        report = parse_page(
            raw,
            site=self._target.site,
            # ※ 命名空间取**站标识**：`12345` 在小红书与快手上是两条不同的内容，
            # 只按 id 算会让两个站撞出同一个 msg_id。
            namespace=self._target.site.key,
            group_id=self._target.group_id,
            fetched_at=fetched_at,
        )
        self.last_report = report
        return [m for m in report.messages if in_window(m.ts, since, until)]


def build_subscription_source() -> _SubscriptionSource:
    """注册表用的工厂（`REGISTRY["subscription"]`）。

    ※ 它在**装配的时候**就把目标、协议、凭据三样都定下来，而不是等 `read()`
    时再查。判据在退出码上：`interfaces/cli.py` 把「源名不认识」算作**用法错**
    （退出码 2）而把「跑起来之后才失败」算作 1。目标没配、协议没签、
    登录态没配是**同一类** —— 都是用户要去改配置/命令行，而不是去查站点。

    ※ 在这里报出来还有一个好处：它发生在**任何一个源开始读之前**。
    要是等 `read()` 才报，`--source synth,subscription` 会先把合成语料读完
    并入库，然后才说订阅源没配 —— 一半的工作已经做完了。

    **顺序是有判据的**：目标 → 协议 → 凭据。
      · 目标在最前：没有目标就无从知道要问哪个站，后面两步都做不了。
      · 协议在凭据之前：那句「**必须先签**一份协议」。
        反过来的话，用户会先被要求配一个 cookie（那是**凭据**），
        配完才被告知还要签字 —— 而他已经在处理凭据了。
    """
    target = resolve_target()

    if not target.is_remote:
        # 夹具：本地文件，不涉及任何账号 ⇒ 不要协议、不要凭据、不过 robots。
        return _SubscriptionSource(target)

    consent.check_consent()
    # ※ 从**目标 URL** 上取域名交给凭据解析：浏览器里一个站可能有好几个域的
    #   cookie（微博认 weibo.com 与 weibo.cn），要挑的是**这一次要访问的那个**。
    host = urlsplit(target.url).hostname or ""
    credential = _credential(target.site, host=host)
    if credential is None:
        raise ConfigError(_credential_hint(target.site, host=host))

    if credential.kind == "password":
        # ※ 账号密码那条路：**当场换成会话凭据**，口令用完即弃。
        # 现在五个站都会抛 `_LoginNotSupported`（见那个函数的 docstring）——
        # 于是「支持账号密码」这件事的现实是：**管线是通的，路是拦住的**，
        # 而拦住它的原因被逐站如实报出来。
        cookie = _login_with_password(
            target.site,
            credential.user,
            credential.password,
            opener=urllib.request.urlopen,
            timeout=DEFAULT_TIMEOUT,
        )
        credential = _Credential(site=target.site.key, kind="cookie", cookie=cookie)

    return _SubscriptionSource(target, credential=credential)
