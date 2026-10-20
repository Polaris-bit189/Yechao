"""订阅源 —— **每站一份适配规则**。

## 为什么是「一个源 + 五份规则」，不是五个源

`MessageSource` 的形状对五个站是一样的：给一个账号 / 一个关键词，取回
一批**自洽的短文本**（一条笔记、一条视频、一条微博、一篇论文）。它们的
差别**不在形状上，在页面上** —— 用哪个 cookie 字段、结构标记长什么样、
发布时间藏在哪个键里。

⇒ 所以站与站之间的差别落在这个文件的**数据**里，而不是各写一个源。
反过来说：如果某个站真的连「取什么」都不同（比如它根本没有账号这个概念），
那它就该是另一个源，而不是这个表里多一行。

## ※ 这张表里哪些是**实测**的，哪些是**建模**的

这一点必须写在最前面，因为它决定了这份代码的可信度边界。

**实测**（2026-10-20，单次只读请求，遵守 robots，只记元信息）：

| 站 | robots.txt | 匿名实测 |
|---|---|---|
| 小红书 | `User-agent: *` → `Disallow: /` | **未发起请求** —— robots 全面禁止，所以没抓 |
| 抖音 | 只有具名爬虫组，**没有 `*` 组** | 首页 200 / 72914 字节 / 正文约 4955 字符，**不含内容**；内容接口 `/aweme/v1/web/aweme/post/` → **403**，46 字节 |
| 快手 | `User-agent: *` → `Disallow: /` | **未发起请求**（同上） |
| 微博 | `weibo.com` 与 `m.weibo.cn` 的 `*` 组都是 `Disallow: /`（只放行 `/ads.txt`） | **未发起请求**（同上） |
| 知网 | robots.txt **404** ⇒ 按 RFC 9309 视为无限制 | 首页 200；结果页 **302 → `/verify/home?captchaType=blockPuzzle`**（滑块验证）；详情页 **302 → `login.cnki.net/login/`** |

**建模**（`shape_verified = False`）：每站的**页面结构**（`items_key` /
字段路径 / URL 模板）是按公开可观察的形状**推**出来的，**没有**在真实
网络下逐站验证 —— 因为三个站的 robots 不允许抓、另外两个站匿名拿不到内容。
夹具（`fixtures/subscription-<站>.html`）按**同一份声明**手写，所以
「解析器与夹具」这一对是自洽的；而「解析器与真实站点」这一对**未经验证**。
`shape_verified` 就是为这件事存在的：它是一句能被读出来的诚实声明，
而不是让下一个人以为这些都试过了。

## ※ 反爬绕过：不做，且是刻意的

抖音的内容接口要 `a_bogus` / `X-Bogus` 签名，小红书要 `X-s` / `X-t`，
知网要滑块 —— 这些**都不做**。理由不是「做不到」，是**不该做**：
签名逆向是绕过平台风控，而本项目的取向是「公开能拿的拿到，拿不到的如实报」。
`login_verdict` 那一列就是这条取向的落点：每个站都如实写着它拦在哪一步，
拿不到时抛的是**说清原因**的错误，不是一次假装成功的空抓。

## 时间：`ts` 用内容自己的发布时间

每站的发布时间字段不同（毫秒 / 秒 / 微博那串英文日期 / 知网只有年月），
所以 `time_kind` 声明它属于哪一种。**拿不到就退回「抓取时间」**，
并且那个次数会被数出来（见 `subscription_parse.ParseReport.ts_from_fetch`）——
静默退回会让一整批内容挤到「抓取那一刻」，而日报是按天分的，
那看起来像「今天突然发了这么多」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

from yechao.domain.errors import ConfigError

#: 查询的三种形态。**闭集** —— 每站的 `modes` 是它的子集。
MODE_ACCOUNT = "account"
MODE_KEYWORD = "keyword"
MODE_URL = "url"
MODES = (MODE_ACCOUNT, MODE_KEYWORD, MODE_URL)

#: 列表容器的两种形状。
LIST_SSR_JSON = "ssr_json"  # 页面里嵌一段 JSON（四个社交站都是这个形状）
LIST_HTML = "html_list"  # 静态 HTML 列表（知网的结果列表）

#: 发布时间的四种写法。
TIME_EPOCH_MS = "epoch_ms"  # 毫秒时间戳（小红书 / 快手）
TIME_EPOCH_S = "epoch_s"  # 秒时间戳（抖音）
TIME_WEIBO = "weibo_ctime"  # `Wed Oct 15 12:00:00 +0800 2026`
TIME_DATE_ONLY = "date_only"  # 只有年月日（知网）—— **解析不出就退回抓取时间**

#: 环境变量名的前缀。`XHS` → `YECHAO_XHS_COOKIE` / `YECHAO_XHS_USER` / `YECHAO_XHS_PASS`。
#:
#: ※ 为什么用短前缀而不是站的全名：环境变量是**用户手打**的东西
#: （`export YECHAO_XIAOHONGSHU_COOKIE=...`），而手打的长度直接变成打错的概率。
#: 前缀与 `key` 分开声明，是为了让「站标识」（出现在配置里、要稳定）
#: 与「环境变量前缀」（出现在命令行里、要短）能各自演化。
ENV_PREFIX: dict[str, str] = {
    "xiaohongshu": "XHS",
    "douyin": "DY",
    "kuaishou": "KS",
    "weibo": "WB",
    "cnki": "CNKI",
}

#: 出现这些标记 = 这个站把请求打回了登录页 / 要人机验证。
#:
#: ※ 判据是**实测观察到的跳转目标**（知网那次 302 到 `login.cnki.net/login/`、
#: 以及 `/verify/home?captchaType=blockPuzzle`），不是「猜几个关键词」。
#: 加一条就要能说清它对应哪次观察。
LOGIN_WALL_MARKERS: tuple[str, ...] = (
    "login.cnki.net",
    "verify/home",
    "captcha",
    "验证码",
    "请先登录",
    "请登录",
    "passport.",
)

#: 出现这些标记 = 要**人机验证**。它与「登录页」分开，因为下一步不同：
#: 登录页可以配 cookie 解决，人机验证不行（本工具不做过验证码）。
CAPTCHA_MARKERS: tuple[str, ...] = (
    "captcha",
    "blockPuzzle",
    "滑块",
    "验证码",
    "verify/home",
)


@dataclass(frozen=True)
class _Site:
    """**一个站的适配规则。** 三件事各占一段：拿得到什么、要什么才能更多、抓不到怎么报。"""

    key: str
    label: str
    #: 认这个站的域名。用来从 URL 反推站点（`detect`）。
    hosts: tuple[str, ...]

    # ── 一、不登录能拿到什么 ──

    #: 匿名（无 cookie）实测能拿到的字段。**这是实测结论，不是期望**。
    public_fields: tuple[str, ...]
    #: 匿名实测的结论，一句话。
    public_note: str

    # ── 二、要拿到更多需要什么 ──

    #: cookie 里哪几个字段算「登录态」。
    #:
    #: ※ 它是**判据**而不是文档：`has_credential()` 只判「用户给了 cookie」，
    #: 而这些字段名是给**报错消息**用的 —— 用户问「我该拷哪个」时，
    #: 一句「需要 `web_session` 这一项」比「需要一个 cookie」有用得多。
    session_fields: tuple[str, ...]
    #: 还需要什么（签名 / 机构 IP / 订阅）。写进报错消息。
    more_needs: str
    #: robots.txt 的实测结论。
    robots_verdict: str
    #: 匿名抓取被 robots 允许吗（实测）。
    robots_allows_anonymous: bool

    # ── 三、抓不到时怎么报 ──

    #: 登录流程的实测结论。它决定「给了账号密码之后会得到什么错误」。
    login_verdict: str
    #: 登录端点。**五个站全是 `None`** —— 见 `login_verdict` 与模块 docstring：
    #: 每个站的登录都拦在签名 / 滑块 / 机构 IP 上，所以「用密码自动登录」
    #: 这条路在本项目里**不做**。`None` 不是「忘了填」，是结论。
    login_endpoint: str | None

    # ── 怎么问它 ──

    #: 支持哪几种查询形态。
    modes: tuple[str, ...]
    #: 每种形态对应的 URL 模板（`{target}` 会被替换成**已转义**的查询词）。
    url_templates: dict[str, str] = field(default_factory=dict)

    # ── 怎么解它 ──

    list_kind: str = LIST_SSR_JSON
    #: SSR JSON 的标记。**它是「读懂了没有」的分界线**：改版会把它换掉，
    #: 「这个账号没发新东西」不会动它。
    blob_marker: str = ""
    #: 条目列表在 JSON 里的**键名**（递归找，见 `subscription_parse._find_by_key`）。
    items_key: str = ""
    #: `list`（数组）还是 `mapping`（以 id 为键的对象）。
    items_kind: str = "list"
    #: 各字段**相对一条条目**的路径。`None` = 这个站没有这个字段。
    #:
    #: ※ 「没有标题」是一个**如实的事实**（微博的正文就是全部），不是缺失：
    #: 用正文冒充标题会让下游以为它是一句概括。
    id_path: tuple[str, ...] = ()
    title_path: tuple[str, ...] | None = None
    body_path: tuple[str, ...] = ()
    time_path: tuple[str, ...] = ()
    author_path: tuple[str, ...] | None = None
    time_kind: str = TIME_EPOCH_S
    #: 这一站的**页面结构**是否在真实网络下验证过。见模块 docstring。
    shape_verified: bool = False
    #: HTML 列表（`LIST_HTML`）用的结构标记。
    html_container_class: str = ""
    html_item_tag: str = ""
    html_title_class: str = ""
    html_author_class: str = ""
    html_date_class: str = ""

    # ── 环境变量 ──

    @property
    def cookie_env(self) -> str:
        """会话凭据（cookie）从哪个环境变量读。**首选的那条路。**"""
        return f"YECHAO_{ENV_PREFIX[self.key]}_COOKIE"

    @property
    def user_env(self) -> str:
        """账号。"""
        return f"YECHAO_{ENV_PREFIX[self.key]}_USER"

    @property
    def pass_env(self) -> str:
        """口令。

        ※ 它**只从环境变量读、绝不进配置文件**：配置文件会被提交、会被导出
        （`/api/config/export`）、会跟着配置包跑到别的机器上，而口令是凭据。
        这条与 `YECHAO_API_KEY` 同一条判据（见 `config_models.py` 与 `.env.example`）。
        """
        return f"YECHAO_{ENV_PREFIX[self.key]}_PASS"


#: 五个站的适配规则。**键就是源要用的站标识**（出现在配置与报错里）。
SITES: dict[str, _Site] = {
    # ── 小红书 ──────────────────────────────────────────────────────
    "xiaohongshu": _Site(
        key="xiaohongshu",
        label="小红书",
        hosts=("xiaohongshu.com",),
        public_fields=(),
        public_note=(
            "robots.txt 的 `User-agent: *` 组是 `Disallow: /`，所以**没有发起"
            "任何请求** —— 按本项目的取向，robots 不许就不抓，不绕、不换 UA。"
        ),
        session_fields=("web_session",),
        more_needs=(
            "浏览器里登录之后，从开发者工具里拷 `web_session` 这一项"
            "（它是登录态的凭据）。未登录访问会跳登录页。"
        ),
        robots_verdict="`User-agent: *` → `Disallow: /`（2026-10-20 实测）",
        robots_allows_anonymous=False,
        login_verdict=(
            "登录接口要求 `X-s` / `X-t` 签名，且常见滑块验证 —— **不做**。"
            "请改用 cookie。"
        ),
        login_endpoint=None,
        modes=(MODE_ACCOUNT, MODE_KEYWORD, MODE_URL),
        url_templates={
            MODE_ACCOUNT: "https://www.xiaohongshu.com/user/profile/{target}",
            MODE_KEYWORD: "https://www.xiaohongshu.com/search_result?keyword={target}",
        },
        blob_marker="window.__INITIAL_STATE__=",
        items_key="noteDetailMap",
        items_kind="mapping",
        id_path=("note", "noteId"),
        title_path=("note", "title"),
        body_path=("note", "desc"),
        time_path=("note", "time"),
        author_path=("note", "user", "nickname"),
        time_kind=TIME_EPOCH_MS,
    ),
    # ── 抖音 ────────────────────────────────────────────────────────
    "douyin": _Site(
        key="douyin",
        label="抖音",
        hosts=("douyin.com",),
        public_fields=(),
        public_note=(
            "首页 200 / 72914 字节 / 正文约 4955 字符，**不含任何内容**"
            "（只有 2 个 `<script>`，没有 `RENDER_DATA` 一类的数据块）；"
            "内容接口 `/aweme/v1/web/aweme/post/` 无签名直接 **403**（46 字节）。"
        ),
        session_fields=("sessionid", "ttwid", "msToken"),
        more_needs=(
            "除了 cookie（`sessionid` / `ttwid`），内容接口还要 `a_bogus` "
            "（旧版是 `X-Bogus`）签名 —— 那个**不做**（见模块 docstring）。"
        ),
        robots_verdict=(
            "只有具名爬虫组，**没有 `User-agent: *` 组** ⇒ 按 RFC 9309 与 "
            "`urllib.robotparser` 的口径，我们的 UA 不被禁止（2026-10-20 实测）"
        ),
        robots_allows_anonymous=True,
        login_verdict=(
            "登录要走短信验证码，且登录请求同样要签名 —— **不做**。请改用 cookie。"
        ),
        login_endpoint=None,
        modes=(MODE_ACCOUNT, MODE_KEYWORD, MODE_URL),
        url_templates={
            MODE_ACCOUNT: "https://www.douyin.com/user/{target}",
            MODE_KEYWORD: "https://www.douyin.com/search/{target}",
        },
        blob_marker="window._ROUTER_DATA = ",
        items_key="awemeList",
        items_kind="list",
        id_path=("aweme_id",),
        title_path=None,  # 抖音的正文就是 `desc`，它没有独立的标题
        body_path=("desc",),
        time_path=("create_time",),
        author_path=("author", "nickname"),
        time_kind=TIME_EPOCH_S,
    ),
    # ── 快手 ────────────────────────────────────────────────────────
    "kuaishou": _Site(
        key="kuaishou",
        label="快手",
        hosts=("kuaishou.com",),
        public_fields=(),
        public_note=(
            "robots.txt 的 `User-agent: *` 组是 `Disallow: /` ⇒ **未发起请求**。"
        ),
        session_fields=("did", "kuaishou.server.web_st", "kuaishou.server.web_ph"),
        more_needs=(
            "cookie（`did` 与 `kuaishou.server.web_st`）之外，接口还要 "
            "`__NS_sig3` 一类的签名 —— 那个**不做**。"
        ),
        robots_verdict="`User-agent: *` → `Disallow: /`（2026-10-20 实测）",
        robots_allows_anonymous=False,
        login_verdict="登录要短信验证码与签名 —— **不做**。请改用 cookie。",
        login_endpoint=None,
        modes=(MODE_ACCOUNT, MODE_KEYWORD, MODE_URL),
        url_templates={
            MODE_ACCOUNT: "https://www.kuaishou.com/profile/{target}",
            MODE_KEYWORD: "https://www.kuaishou.com/search/video?searchKey={target}",
        },
        blob_marker="window.__APOLLO_STATE__=",
        items_key="works",
        items_kind="list",
        id_path=("photoId",),
        title_path=None,  # 快手的文案就是正文
        body_path=("caption",),
        time_path=("timestamp",),
        author_path=("userName",),
        time_kind=TIME_EPOCH_MS,
    ),
    # ── 微博 ────────────────────────────────────────────────────────
    "weibo": _Site(
        key="weibo",
        label="微博",
        hosts=("weibo.com", "weibo.cn"),
        public_fields=(),
        public_note=(
            "`weibo.com` 与 `m.weibo.cn` 的 robots `*` 组都是 `Disallow: /`"
            "（只放行 `/ads.txt`）⇒ **未发起请求**。"
        ),
        session_fields=("SUB",),
        more_needs=(
            "cookie 里的 `SUB` 是登录态。未登录访问 `m.weibo.cn` 的接口会"
            "被当成访客（`SUB` / `SSOLoginState` 缺失）。"
        ),
        robots_verdict=(
            "`weibo.com` 与 `m.weibo.cn` 的 `User-Agent: *` 组都是 `Disallow: /`"
            "（2026-10-20 实测）"
        ),
        robots_allows_anonymous=False,
        login_verdict="登录要验证码与 `pincode` 校验 —— **不做**。请改用 cookie。",
        login_endpoint=None,
        modes=(MODE_ACCOUNT, MODE_KEYWORD, MODE_URL),
        url_templates={
            MODE_ACCOUNT: "https://m.weibo.cn/u/{target}",
            MODE_KEYWORD: (
                "https://m.weibo.cn/search?containerid=100103type%3D1%26q%3D{target}"
            ),
        },
        blob_marker="var $render_data = ",
        items_key="cards",
        items_kind="list",
        id_path=("mblog", "id"),
        title_path=None,  # 微博没有标题：正文就是全部
        body_path=("mblog", "text"),
        time_path=("mblog", "created_at"),
        author_path=("mblog", "user", "screen_name"),
        time_kind=TIME_WEIBO,
    ),
    # ── 知网 ────────────────────────────────────────────────────────
    "cnki": _Site(
        key="cnki",
        label="知网",
        hosts=("cnki.net", "cnki.com.cn"),
        public_fields=("标题", "作者", "来源刊", "发表日期（年月日）"),
        public_note=(
            "首页 200。但**结果列表页 302 到 `/verify/home?captchaType=blockPuzzle`"
            "（滑块验证）**，**详情页 302 到 `login.cnki.net/login/`** "
            "（2026-10-20 实测）—— 也就是说，匿名能拿到的只有首页本身，"
            "连检索结果都要先过滑块。"
        ),
        session_fields=("Ecp_ClientId", "Ecp_session", "SID", "cnkiUserKey"),
        more_needs=(
            "登录态（cookie）之外，**全文还要机构订阅或单篇付费** —— "
            "订阅是账号侧的权限，本工具拿不到也不该去拿。"
        ),
        robots_verdict=(
            "`https://www.cnki.net/robots.txt` 返回 **404** ⇒ 按 RFC 9309 "
            "视为没有任何限制（2026-10-20 实测）"
        ),
        robots_allows_anonymous=True,
        login_verdict=(
            "登录页（`login.cnki.net`）之后紧跟着滑块验证与机构 IP 校验 —— "
            "**不做**。请改用 cookie。"
        ),
        login_endpoint=None,
        modes=(MODE_KEYWORD, MODE_URL),
        url_templates={
            MODE_KEYWORD: "https://kns.cnki.net/kns8s/defaultresult/index?kw={target}",
        },
        list_kind=LIST_HTML,
        html_container_class="result-table-list",
        html_item_tag="tr",
        html_title_class="fz14",
        html_author_class="author",
        html_date_class="date",
        time_kind=TIME_DATE_ONLY,
    ),
}


def site_keys() -> tuple[str, ...]:
    """全部站标识（排序后）。给报错文案与文档用。"""
    return tuple(sorted(SITES))


def site_by_key(key: str) -> _Site:
    """按站标识取规则。不认识 ⇒ 抛，**不回落**。

    ※ 不回落是刻意的（与 `sources/__init__.py::build` 逐字相同的判据）：
    回落到某个默认站会让 `site = "xhs"`（拼错了）变成「静默读了小红书」，
    而用户拿到的报告看起来一切正常。
    """
    site = SITES.get(key)
    if site is None:
        raise ConfigError(
            f"不认识的站 {key!r}；可用的站：{'、'.join(site_keys())}"
        )
    return site


def detect(url: str) -> _Site | None:
    """从 URL 反推站点。认不出返回 `None`（**不猜**）。

    ※ 判据是**域名后缀**而不是「URL 里出现过站名」：后者会把
    `https://example.test/redirect?to=xiaohongshu.com` 认成小红书。
    后缀匹配同样要防 `notxiaohongshu.com` —— 所以比的是
    `host == h or host.endswith("." + h)`。
    """
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return None
    for site in SITES.values():
        for h in site.hosts:
            if host == h or host.endswith("." + h):
                return site
    return None


def url_for(site: _Site, mode: str, target: str) -> str:
    """把「站 + 形态 + 目标」翻成一个 URL。

    抛（两条都是**用法错**，不是运行期失败）：
      · 形态不在这一站的 `modes` 里（比如知网没有「账号主页」这个概念）
      · 这一站没有给这个形态的模板
    """
    if mode not in site.modes:
        raise ConfigError(
            f"{site.label} 不支持「{mode}」这种查法；它支持："
            f"{'、'.join(site.modes)}。"
        )
    template = site.url_templates.get(mode)
    if template is None:
        raise ConfigError(
            f"{site.label} 声明支持「{mode}」，但没有配对应的 URL 模板 —— "
            f"这是本工具内部的错（见 subscription_sites.py 的 {site.key} 那一段）。"
        )
    return template.format(target=quote(target, safe=""))


def sites_table() -> list[dict[str, Any]]:
    """给报告用的一张表：每站的边界各占一行。

    ※ 存在的理由：这份结论（公开能拿到什么 / 要什么才能更多 / 抓不到怎么报）
    必须**能被打印出来**，而不是只活在注释里。注释不会出现在任何一次运行的
    输出中，而用户问的恰恰是「为什么这个站读不到」。
    """
    return [
        {
            "key": s.key,
            "label": s.label,
            "modes": list(s.modes),
            "public_fields": list(s.public_fields),
            "public_note": s.public_note,
            "more_needs": s.more_needs,
            "robots": s.robots_verdict,
            "login": s.login_verdict,
            "cookie_env": s.cookie_env,
            "shape_verified": s.shape_verified,
        }
        for s in SITES.values()
    ]
