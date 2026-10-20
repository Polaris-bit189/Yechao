"""端到端 —— **整条链路一起跑**，而不是各层各测各的。

## 这个文件补的是哪一层

仓库里已有 500+ 条用例，但它们几乎全是**单层**的：domain 的判据、
application 的用例、persistence 的仓储、interfaces 的路由，各自测各自的。

而项目自己的交接文档记了三次踩过的坑，**全都是「每一层单看都对、连起来错」**：

  1. 提示词模板没渲染，模型照样吐合法 JSON
     （类目判据那一整块是字面量 `{categories}`，模型在猜）
  2. 解析器截尾逻辑把自己的合法输出咬坏（模型完全正常，条目 0 条）
  3. 日报素材里没给编号，而提示词要求「照抄编号」
     ⇒ 每行回连失败、全部退化成机械行，**而有兜底，看不出来**

这三条有一个共同形状：**产出看起来是成功的**。所以它们不能被「某个函数
返回得对」的用例抓住 —— 只有**跑一遍真流程、看实际产出**才看得见。

## 这一条链路

```
demo（合成语料） → ingest（网页源 + 离线夹具） → refine（假模型）
   → digest（假模型） → serve（真 HTTP） → 页面/接口能读到东西
```

## 桩的取向：**按提示词契约行事**，不是往库里塞数据

`_StubModel` 读的是**真的发出去的提示词**：
  · 从 system 里按渲染格式抓出类目 slug（抓不到就记下来，由用例断言 ——
    这正是「模板没渲染」那条失败的形状）
  · 从 user 里按 `[msg_id] 发送者: 正文` 的格式抓出消息
  · 产出的 `quote` 是**脱敏后正文里的连续片段**，所以它能在原文里逐字找到
  · 输出**永远带 Markdown 围栏与一句前置话**（模拟「模型不是每次都给裸
    JSON」的真实行为）

※ 如果桩是直接往库里塞条目，那么「提示词 → 模型 → 解析」这一段就没被
测到 —— 而上面那三条坑全都长在这一段里。

## 为什么这里自己抄了一份 HTTP 客户端

`tests/interfaces/test_api_contract.py` 里有一个 `Client` 类。这里**不
import 它**：它是**另一个测试文件的内部工具**，不是产品代码。跨测试文件
import 内部工具会把两个文件的改动绑在一起 —— 那边为了自己的用例改一下
`Client`，这边就会以「与我无关」的方式红。抄一份小的（二十来行），
换来的是这个文件**自己完整**。

## 样本全部自己编

假域名 `example.test`、假群号 820000001 段、假手机号 1770000100X。
下面的网页夹具是**本文件自己造的**，不是抓来的。

## 为什么用模块级的夹具

整套后端现在约 13 秒。每一步都重建一遍链路会把这里变成第二个 13 秒。
所以链路**只跑一次**（`scope="module"`），每个用例钉的是它上面**不同的接缝**。
唯一的例外是软删那一条 —— 它会改状态，所以放在**最后**，并在结尾撤销掉。
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from yechao.bootstrap import Settings, make_app
from yechao.infrastructure.llm.openai_client import OpenAIClient
from yechao.infrastructure.persistence import sqlite_store
from yechao.infrastructure.persistence.sqlite_store import SqliteStore
from yechao.infrastructure.runtime.audit_log import ALLOWED_KEYS, PREFIX, SUFFIX
from yechao.interfaces import cli
from yechao.interfaces.api import create_app


# ══════════════════════════════════════════════════════════════════
# 一、自造的网页夹具（离线入口）
# ══════════════════════════════════════════════════════════════════
#
# ※ 这里走的是「夹具」入口（HTML + 同名侧车），不是 URL 入口 ——
# 整条链路必须**一个网络请求都不发**。判据在 `web_source` 的模块 docstring：
# 夹具与本地 HTML 的唯一区别就是「知不知道它代表哪个站点」，而侧车回答的
# 正是这一问。

#: 夹具自报的站点。※ `example.test` 是保留域名，不是任何真实站点。
网页站点 = "https://notice.example.test"

#: 网页消息落进哪个群。与合成语料共用 820000001 段 —— 这正是要验的：
#: 两个源的 `msg_id` 不能撞。
网页群号 = 820000001

网页夹具名 = "qingwu-notices.html"


def _网页条目(标题: str, href: str, *, 时间: str, 部门: str, 摘要: str = "") -> str:
    tail = f'<p class="notice-summary">{摘要}</p>' if 摘要 else ""
    return (
        f'<li class="notice-item" data-publish-time="{时间}" data-department="{部门}">'
        f'<a class="notice-title" href="{href}">{标题}</a>{tail}</li>'
    )


def _网页(条目们: list[str]) -> bytes:
    body = "".join(条目们)
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        "<title>青梧大学 · 通知公告</title></head><body>"
        '<div class="topbar"><ul class="topbar-nav">'
        '<li><a href="/index.html">首页</a></li></ul></div>'
        f'<ul class="notice-list">{body}</ul>'
        "</body></html>"
    ).encode("utf-8")


#: 两条通知。时间落在 2026-10-18 —— 与合成语料的某一天重叠，
#: 这样它们会进同一篇日报（否则「网页源也走到日报」这件事没被验到）。
#: ※ 日期写法用 `10月18日`（不夹空格）：`domain/times.py` 认这一种。
网页条目 = [
    _网页条目(
        "青梧大学图书馆闭馆通知",
        "/notice/close.html",
        时间="2026-10-18 10:00:00",
        部门="图书馆",
        摘要="10月18日全天闭馆，请提前归还借阅图书。",
    ),
    _网页条目(
        "青梧大学社团招新",
        "/notice/club.html",
        时间="2026-10-18 10:30:00",
        部门="团委",
        摘要="报名截止到10月20日，请有意者尽快联系。",
    ),
]


# ══════════════════════════════════════════════════════════════════
# 二、模型桩
# ══════════════════════════════════════════════════════════════════

#: 从**渲染后**的系统提示词里抓类目 slug 的形状。
#: `prompts.render_categories` 的输出是 `- `slug`（中文名）：判据`。
#: ※ 刻意不用「抓所有反引号里的东西」—— 提示词里还有 `` `kind` `` /
#: `` `deadline` `` 这类字段名，一起抓进来就分不清「渲染了」与「没渲染」。
_SLUG_LINE = re.compile(r"(?m)^- `([A-Za-z0-9_\-]+)`（")

#: 消息块的开头：`[msg_id] 发送者: 正文`。
_MSG_HEAD = re.compile(r"(?m)^\[(\d+)\] ")

#: 日报素材的每一行：`- [item_id] [kind] 标题`。
_DIGEST_LINE = re.compile(r"(?m)^- \[(\d+)\] \[([^\]]*)\] (.*)$")

#: 源文里的日期 / 日期+时刻写法。
_DATE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_DATETIME = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2}):(\d{2})")

#: 脱敏占位符，或换行 —— 摘录不能跨过它们。
_QUOTE_BREAK = re.compile(r"<[^>]*>|\n")

#: 两套提示词各自的**标志句**。
#: ※ 不能拿「system 里有没有『日报』两个字」来分辨 —— 抽取的系统提示词
#: 里也有一处「这个值会进日历、进日报」。那个判据会把两路接反，而接反的
#: 症状是抽取永远产出空数组（看着像「模型没找到条目」）。
_EXTRACT_MARK = "校园群聊的整理助手"
_DIGEST_MARK = "校园日报的编辑"

#: 类目判据。※ **桩自己的**简易规则，与产品里的判据无关 ——
#: 它只需要「稳定、可预期」，好让用例能钉住某一条消息落进哪个类目。
_KIND_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("recruit", ("招聘", "招新", "应聘", "投递", "勤工")),
    ("claim", ("捡到", "认领", "失主", "丢失")),
    ("trade", ("求购", "出二手", "转让", "闲置")),
    ("todo", ("截止", "报名", "提交", "选课", "缴费", "缴清", "充值", "领取", "办理")),
    ("attend", ("集合", "摆摊", "举行", "入场", "体育馆", "报告厅")),
)


def _摘录(正文: str) -> str | None:
    """从**脱敏后**的正文里取一段当摘录。

    ※ 判据是「它必须能在**原文**里逐字找到」：占位符（`<号码>` /
    `<姓名>`）只在脱敏后的文本里，原文里没有。所以摘录只能是
    **不含占位符的连续片段**。换行同理（`build_extract_user` 用换行分块）。

    取最长的那一段，短于 8 字就不要 —— 太短的片段会在别的消息里也命中，
    回连就指到错的那条上了。
    """
    best = ""
    for part in _QUOTE_BREAK.split(正文):
        part = part.strip()
        if len(part) > len(best):
            best = part
    # 门槛取 4 而不是提示词里写的 8：这条桩只需要「能拿回一段可回连的
    # 摘录」。门槛太高时，脱敏会把一条消息切成两截短片段
    # （例如 `收款账号 <号码> 请核对。`），于是模型**正确地**对整批回 []
    # —— 而 `extract.run` 把「这一批没有候选」记成整批失败。那件事的
    # 形状由 `test_整批失败只来自只含上下文的批次` 钉住。
    # 4 字在**同一批内**已经足够唯一，而 `match_quote` 的判据是逐字包含。
    if len(best) < 4:
        return None
    return best[:30]


def _标题(正文: str) -> str:
    """标题是**桩自己的话**，不是摘录 —— 取正文第一段的前 16 字。"""
    first = _QUOTE_BREAK.split(正文)[0].strip()
    return first[:16] or "条目"


def _类目(正文: str, slugs: tuple[str, ...]) -> str:
    for slug, words in _KIND_RULES:
        if slug in slugs and any(w in 正文 for w in words):
            return slug
    # 落不到任何一条规则上时，用**渲染出来的**第一个 slug（与提示词里
    # `example_kind` 的取值口径一致）—— 保证产出的永远是一个**真 slug**。
    return slugs[0] if slugs else ""


def _截止日(正文: str) -> str | None:
    m = _DATE.search(正文)
    return f"{m.group(1)}月{m.group(2)}日" if m else None


def _开始时刻(正文: str) -> str | None:
    m = _DATETIME.search(正文)
    if not m:
        return None
    return f"{m.group(1)}月{m.group(2)}日 {m.group(3)}:{m.group(4)}"


def _消息块(user: str) -> list[tuple[int, str]]:
    """把提示词里的 `[msg_id] 发送者: 正文` 拆开。

    返回 `[(msg_id, 正文)]`。※ 正文可能含换行（网页通知是「标题\\n摘要」），
    所以按**下一个消息头**切，而不是按行切。
    """
    parts = _MSG_HEAD.split(user)
    out: list[tuple[int, str]] = []
    for i in range(1, len(parts), 2):
        msg_id = int(parts[i])
        body = parts[i + 1].rstrip("\n")
        _sender, _, text = body.partition(": ")
        out.append((msg_id, text))
    return out


class _StubModel:
    """一个**按提示词契约行事**的模型桩。

    ※ 它记下自己**看到了什么**（system / user / 抓到的 slug），
    好让用例能直接断言「提示词渲染对了没有」「日报素材带编号了没有」——
    这两件事是那三条坑里最容易被静默跳过的一环。
    """

    def __init__(self) -> None:
        self.extract_systems: list[str] = []
        self.extract_users: list[str] = []
        self.digest_systems: list[str] = []
        self.digest_users: list[str] = []
        #: 每次抽取调用里抓到的 slug 元组。
        self.slugs_seen: list[tuple[str, ...]] = []

    def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        if _DIGEST_MARK in system:
            self.digest_systems.append(system)
            self.digest_users.append(user)
            # 前置一句废话 + 围栏：模拟「模型不是每次都把 JSON 裸着给」。
            return "好的，以下是结果：\n```json\n" + self._日报(user) + "\n```"
        if _EXTRACT_MARK not in system:
            # 不猜：认不出这是哪一路时立刻响，而不是回一段空数组 ——
            # 空数组的症状是「模型没找到条目」，与真正的病因
            # （提示词换了、桩没跟上）方向完全不同。
            raise AssertionError("认不出的系统提示词：" + repr(system[:80]))
        self.extract_systems.append(system)
        self.extract_users.append(user)
        # 抽取这一路永远给**围栏包住**的输出 —— 裸 JSON 才是例外。
        return "```json\n" + self._抽取(system, user) + "\n```"

    # ── 抽取 ────────────────────────────────────────────────────

    def _抽取(self, system: str, user: str) -> str:
        slugs = tuple(_SLUG_LINE.findall(system))
        self.slugs_seen.append(slugs)
        rows: list[dict[str, object]] = []
        for _msg_id, 正文 in _消息块(user):
            quote = _摘录(正文)
            if quote is None:
                # 正文里全是占位符（例如只剩「收款账号 <号码> 请核对。」）——
                # 拿不出一段能在原文里逐字找到的摘录，就不为它产出候选。
                continue
            rows.append(
                {
                    "kind": _类目(正文, slugs),
                    "title": _标题(正文),
                    "quote": quote,
                    "detail": None,
                    "deadline": _截止日(正文),
                    "start": _开始时刻(正文),
                    "place": None,
                    "amount": None,
                }
            )
        return json.dumps(rows, ensure_ascii=False)

    # ── 日报 ────────────────────────────────────────────────────

    def _日报(self, user: str) -> str:
        rows = [
            {
                "item_id": int(m.group(1)),
                # ※ 这个前缀是**标记**：机械行是 `标题 · 地点 · 群名`，
                # 永远不会长这样。所以「正文里每一行都带它」就等于
                # 「每一行都是模型写的，一行都没退化成机械行」。
                "line": f"今天要处理：{m.group(3).strip()}",
            }
            for m in _DIGEST_LINE.finditer(user)
        ]
        return json.dumps(rows, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════
# 三、HTTP 客户端（自己抄一份，见模块 docstring）
# ══════════════════════════════════════════════════════════════════


class Client:
    """极简 HTTP 客户端。只用标准库 —— 与产品同一条判据：依赖越少越好。"""

    def __init__(self, base: str) -> None:
        self.base = base

    def req(self, method: str, path: str, body=None):
        data = None
        headers: dict[str, str] = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        r = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                return resp.status, _json_or_none(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, _json_or_none(e.read())

    def get(self, p):
        return self.req("GET", p)

    def post(self, p, b=None):
        return self.req("POST", p, b if b is not None else {})

    def delete(self, p):
        return self.req("DELETE", p)


def _json_or_none(raw: bytes):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", errors="replace")


def _空闲端口() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ══════════════════════════════════════════════════════════════════
# 四、配置与语料
# ══════════════════════════════════════════════════════════════════

#: 群配置。※ `members` 是**姓名脱敏的名单来源** —— 不写它，
#: `names_checked` 永远是 0，而隐私页会把那个 0 显示成「查过、没漏」。
#: 号码与昵称都是本文件自己编的（与合成语料的群号段一致）。
群配置 = """\
[[group]]
id = 820000001
name = "青梧大学2027届通知群"
tier = "high"
enabled = true
members = ["2027届辅导员助理林小满", "教务员周允", "辅导员沈明朗"]

[[group]]
id = 820000003
name = "青梧摄影社"
tier = "normal"
enabled = true
members = ["社长赵雪依", "外联钱亦航"]

[[group]]
id = 820000002
name = "青梧计科2203班"
tier = "normal"
enabled = true
members = ["班长孙予棠", "学习委员李青萝"]

[[group]]
id = 820000004
name = "青梧闲置转让"
tier = "low"
enabled = true
members = ["老买家吴不换"]
"""

#: ※ 端点写的是一个**不会被连上**的地址（9 是 discard 端口）。
#: 之所以必须有这一段，是因为 `refine` 会先查 `llm.configured`，
#: 没配就**拒绝跑**（退出码 2）—— 那是产品行为，不该被测试绕过。
#: 而真正的调用被 `OpenAIClient.complete` 的桩截住了，网络一次都不走。
模型配置 = """\
[llm]
base_url = "http://127.0.0.1:9/v1/chat/completions"
model = "stub-model"
"""


def _写网页夹具(dir_: Path) -> Path:
    """把自造的 HTML 与侧车写到临时目录，返回 HTML 路径。"""
    raw = _网页(网页条目)
    html = dir_ / 网页夹具名
    html.write_text(raw.decode("utf-8"), encoding="utf-8")
    html.with_suffix(".meta.json").write_text(
        json.dumps(
            {
                "fixture": 网页夹具名,
                "source_url": f"{网页站点}/notices.html",
                "fetched_at": None,
                "bytes": len(raw),
                "sha256": "",
                "robots": None,
                "user_agent": "yechao-tests/0.1",
                "note": "本仓库测试自造的夹具，不是抓来的",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return html


# ══════════════════════════════════════════════════════════════════
# 五、模块级夹具：整条链路跑一次
# ══════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def 链路(tmp_path_factory):
    """demo → ingest(web) → refine → digest → serve，全部走真入口。

    ※ 只有**模型**是桩，其余一律是真的：真 CLI、真仓储、真脱敏、真核验、
    真审计落盘、真 HTTP。
    """
    base = tmp_path_factory.mktemp("e2e")
    data = base / "data"
    cfg = base / "config"
    data.mkdir()
    cfg.mkdir()

    # 预写配置 —— 这样 `make_app` 的「首启从模板复制」不会触发，
    # 群名单（members）才在。
    (cfg / "groups.toml").write_text(群配置, encoding="utf-8")
    (cfg / "settings.toml").write_text(模型配置, encoding="utf-8")
    html = _写网页夹具(base)

    mp = pytest.MonkeyPatch()
    mp.setenv("YECHAO_DATA_DIR", str(data))
    mp.setenv("YECHAO_CONFIG_DIR", str(cfg))
    mp.setenv("YECHAO_DB", str(data / "yechao.db"))
    mp.delenv("YECHAO_DB_URL", raising=False)
    mp.delenv("YECHAO_REDIS_URL", raising=False)
    # 网页源的三个开关。目标走**夹具**入口（HTML + 同名侧车）——
    # 这条路一个网络请求都不发。
    mp.setenv("YECHAO_WEB_SOURCE", str(html))
    mp.setenv("YECHAO_WEB_GROUP_ID", str(网页群号))

    stub = _StubModel()

    def _complete(self, *, system: str, user: str, max_tokens: int = 2048) -> str:
        return stub.complete(system=system, user=user, max_tokens=max_tokens)

    mp.setattr(OpenAIClient, "complete", _complete)

    # ── 第 1 步：demo（合成语料，离线）──────────────────────────
    assert cli.main(["demo"]) == 0, "demo 没跑成"

    # ── 第 2 步：ingest（网页源 + 离线夹具）────────────────────
    assert cli.main(["ingest", "--source", "web"]) == 0, "网页源摄取没跑成"

    # ── 第 3 步：refine（假模型）───────────────────────────────
    #
    # 退出码必须是 0。演示语料里确实存在「只含上下文邻居」的批次（预筛丢掉的
    # 「好的」「+++」被 `prefilter.with_context` 补回来当上下文），而模型
    # 对它们回 [] 是**正确的** —— 那样的批次不算失败。
    # 判据见 `test_只含上下文邻居的批次不算失败`。
    code = cli.main(["refine"])
    assert code == 0, f"refine 回了 {code}（这一步该当全成）"

    # ── 第 4 步：digest（假模型）───────────────────────────────
    assert cli.main(["digest"]) == 0, "digest 没跑成"

    # ── 第 5 步：serve（真 HTTP）───────────────────────────────
    st = Settings.from_env()
    serve_app = make_app(settings=st)
    api = create_app(serve_app)
    port = _空闲端口()
    server = _Uvicorn(api, port)
    server.start()

    yield SimpleNamespace(
        data=data,
        cfg=cfg,
        db=st.db_path,
        stub=stub,
        app=serve_app,
        client=Client(f"http://127.0.0.1:{port}"),
        #: 只读连接：读库用它与产品同一条路（不变量 ① 的同一侧）。
        reader=SqliteStore(sqlite_store.open_readonly(st.db_path)),
    )

    server.stop()
    serve_app.close()
    mp.undo()


class _Uvicorn:
    """在线程里起一个真 uvicorn，等它真的起来再返回。

    ※ 不起真 uvicorn 长跑（那要几十秒）；但也不能用 TestClient ——
    那会绕开路由注册顺序、状态码、序列化这三样**只在过一遍 HTTP 时
    才显形**的东西（`test_api_contract.py` 的模块 docstring 说的就是这件事）。
    """

    def __init__(self, api, port: int) -> None:
        import uvicorn

        self._server = uvicorn.Server(
            uvicorn.Config(api, host="127.0.0.1", port=port, log_level="critical")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        for _ in range(200):
            if self._server.started:
                return
            time.sleep(0.02)
        raise AssertionError("HTTP 服务没起来")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


# ══════════════════════════════════════════════════════════════════
# 六、用例
# ══════════════════════════════════════════════════════════════════


# ── 接缝：提示词渲染 → 模型 ─────────────────────────────────────────


def test_系统提示词里的类目表是渲染过的而不是模板字面量(链路) -> None:
    """**接缝：`prompts.build_extract_system` → 真正发出去的提示词。**

    ※ 这条钉的是那三条坑里的第一条。模板没渲染时，发出去的提示词里
    类目判据那一整块是一个字面量 `{categories}` —— 而**模型照样会回一段
    形状完全正确的 JSON**，字段齐全、摘录能回连、语法合法。唯一的区别是
    类目判据完全没有依据，它在猜。

    这个失败靠读输出发现不了、靠单层测试也发现不了（形状是对的）。
    所以判据只能是「**真的发出去的那一份**里有什么」—— 也就是桩看到的那一份。
    """
    systems = 链路.stub.extract_systems
    assert systems, "refine 一次都没调模型 —— 抽取那一步根本没走到模型"

    for i, system in enumerate(systems):
        assert "{categories}" not in system, (
            f"第 {i + 1} 次抽取的提示词里类目表是**模板字面量** —— "
            f"`build_extract_system` 没有被调用（模型在猜类目）"
        )
        assert "{example_kind}" not in system, (
            f"第 {i + 1} 次抽取的提示词里示例的 kind 是模板字面量"
        )

    seen = 链路.stub.slugs_seen
    assert seen and all(s == seen[0] for s in seen), (
        f"各批次抓到的类目 slug 不一致：{seen}（类目表是用户可编辑的数据，"
        f"但同一次跑里不该变）"
    )
    assert seen[0], (
        "提示词里**一个类目 slug 都没抓到**（渲染格式变了？）—— "
        "抽取那一段的 `- `slug`（中文名）：判据` 是 `render_categories` 的契约"
    )

    expected = tuple(c.slug for c in 链路.app.config.categories().categories)
    assert set(seen[0]) == set(expected), (
        f"提示词里的 slug 与配置里的类目对不上：\n"
        f"  提示词 {seen[0]}\n  配置   {expected}"
    )


def test_抽取出来的条目带正确的类目_slug(链路) -> None:
    """**接缝：模型给的 kind → `resolve_kind` → 落库的 `items.kind`。**

    ※ 两种隐蔽的错都在这里：
      · 模型回填**中文标签**而不是 slug
      · 模型**自造**一个 slug
    而 `resolve_kind` 对未知 slug **不报错**（它照原样展示），
    于是界面上会出现一个没人认识的类目，而系统一声不吭。

    ⇒ 判据是「库里每一条的 `kind` 都在配置的 slug 集合里」，且
    「某条具体的消息落进了它该去的类目」。
    """
    expected = {c.slug for c in 链路.app.config.categories().categories}
    items = 链路.reader.all_items()
    assert items, "库里一条条目都没有 —— refine 那一步没产出东西"

    bad = sorted({it.kind for it in items} - expected)
    assert not bad, (
        f"库里的 kind 有配置里不存在的值：{bad}。"
        f"配置里的 slug 是 {sorted(expected)} —— "
        f"中文名或自造 slug 都会落在这里，而 `resolve_kind` 不会报错"
    )

    # 逐条回连到源消息，钉住**具体某条**的类目。
    by_msg: dict[int, str] = {}
    for it in items:
        for src in 链路.reader.sources_of(it.item_id):
            by_msg.setdefault(int(src["msg_id"]), it.kind)

    # 选课那条（合成语料 msg 12）：「要办的事」，有截止日。
    assert by_msg.get(12) == "todo", (
        f"「选课系统将于10月31日 23:59 关闭」应当落进 `todo`，"
        f"实际是 {by_msg.get(12)!r}（msg 12 没有条目？）"
    )
    # 招聘那条（msg 20）：「招人」。
    assert by_msg.get(20) == "recruit", (
        f"「校内勤工助学岗位招聘」应当落进 `recruit`，实际是 {by_msg.get(20)!r}"
    )


def test_截止日过了闸门才留下(链路) -> None:
    """**接缝：模型填的日期串 → `_resolve_ts` → `verify.gate` → 落库字段。**

    ※ 两个方向都要有：
      · 源文里**有**那个月日 ⇒ 留下（否则「每一个截止日都被清掉」）
      · 源文里**没有** ⇒ 清成 None

    这一条只看第一个方向 —— 第二个方向由 `tests/domain/test_verify.py` 穷举。
    这里要的是「整条链上它没有被某一步顺手清掉」。
    """
    items = 链路.reader.all_items()
    by_msg: dict[int, object] = {}
    for it in items:
        for src in 链路.reader.sources_of(it.item_id):
            by_msg.setdefault(int(src["msg_id"]), it)

    item = by_msg.get(12)
    assert item is not None, "msg 12（选课通知）没有产出条目"
    assert item.deadline_ts is not None, (
        "源文里明明写着「10月31日」，闸门却把截止日清掉了 —— "
        "多半是解析与核验用了两个不同的时区口径"
    )
    assert item.deadline_supported is True, (
        "`deadline_ts` 有值但 `deadline_supported` 是假 —— "
        "跨字段不变量（`deadline_ts is None` ⇔ `deadline_supported` 为假）被破坏"
    )


def test_跨群转发的同一件事被合并成一个条目(链路) -> None:
    """**接缝：`_merge_forwarded` → `insert_items` 的来源分组。**

    ※ 合成语料里 msg 22/23/24 是**三个群转发同一条通知**。它们必须合成
    一条条目、`source_count = 3`，而三条来源**都留着**。

    这条曾经断过：`insert_items` 假设「`sources[i]` 属于 `items[i]`」，
    合并之后来源比条目多，它当场报「调用方把两个列表配错了」—— 而调用方
    没配错，是契约本身错了。
    """
    best = None
    for it in 链路.reader.all_items():
        mids = {int(s["msg_id"]) for s in 链路.reader.sources_of(it.item_id)}
        if {22, 23, 24} <= mids:
            best = it
            break

    assert best is not None, (
        "三个群转发的那条通知没有合并成一条条目（msg 22/23/24 各自成了条目？）"
    )
    assert best.source_count == 3, (
        f"合并后 `source_count` 应当是 3，实际 {best.source_count} —— "
        f"来源数与条目数在合并之后对不上了"
    )
    assert len(链路.reader.sources_of(best.item_id)) == 3, (
        "合并把来源弄丢了 —— 用户点进去应当能看到三个群都说过"
    )


def test_只含上下文邻居的批次不算失败(链路) -> None:
    """**接缝：`prefilter.with_context` 补进来的邻居 → `extract.run` 的记账。**

    `with_context` 的 docstring 写着「补进来的邻居**只作上下文**，不产出条目
    —— 调用方要能把它们区分开」。这句话曾经**没有落在代码里**：某一批里
    全是邻居时，模型回 `[]` 是**正确的**，却被打成
    `batch_failed(parse_empty)` ⇒ `python -m yechao refine` 退出码 1。
    演示语料里必现（那两批是「好的」与「顶」被邻居规则捞回来的）。

    判据现在是：**这一批里有没有一条是预筛保留的**。
      · 有 ⇒ 模型该产出却没产出，是真失败
      · 没有 ⇒ 它本来就不该产出，这一批跑成了

    这条用例把那个判据钉成可观察的事实：**全程零条 `batch_failed`**；
    并且证明这份语料里**确实存在**「被邻居捞回来的纯应答」—— 否则上面那条
    「零失败」的断言恒为真，看着绿而已。
    """
    from yechao.domain import prefilter

    runs = [r for r in 链路.reader.recent_runs(limit=50) if r.kind == "refine"]
    assert runs, "库里没有 refine 那一次的抬头"
    run = runs[0]

    failed = [
        e for e in 链路.reader.entries_of_run(run.run_id) if e.kind == "batch_failed"
    ]
    assert not failed, (
        "refine 记了整批失败："
        f"{[(e.msg_id, e.reason) for e in failed[:3]]} —— "
        "只含上下文邻居的批次不该算失败（模型对这样一批回 [] 是对的）"
    )

    # 空转自检：这份语料里**真的**有「被邻居捞回来的纯应答」吗？
    msgs = 链路.reader.pending_messages()
    assert msgs, "读不到消息 —— 这条用例在空转"
    tiers = {g.id: g.tier for g in 链路.app.config.groups()}
    kept, _dropped = prefilter.prescreen(msgs, tiers=tiers)
    kept_ids = {m.msg_id for m in kept}
    with_ctx = prefilter.with_context(kept, list(msgs))
    neighbours = [m for m in with_ctx if m.msg_id not in kept_ids]
    assert neighbours, (
        "这份语料里一条上下文邻居都没有 —— 上面那条「零失败」的断言"
        "没有覆盖到它要覆盖的那条路径"
    )


# ── 接缝：源 → ingest → 仓储 ────────────────────────────────────────


def test_网页源的消息真的进库且_msg_id_不与合成源冲突(链路) -> None:
    """**接缝：`web_source`（夹具入口）→ `ingest.run` → `messages` 表。**

    ※ 三个都要钉：
      · 消息**真的进库**（不是「读了但没写」）
      · `msg_id` 与合成源**不冲突**（撞了会让一条覆盖另一条，而且无声）
      · 落库的 `source` 字段是 `web`（不是默认值 —— 一条记错的来源
        在审计里看不出来，因为它格式完全正常）
    """
    msgs = 链路.reader.pending_messages()
    web = [m for m in msgs if m.source == "web"]
    synth = [m for m in msgs if m.source == "synth"]

    assert len(web) == len(网页条目), (
        f"网页源应当读到 {len(网页条目)} 条通知，库里只有 {len(web)} 条"
    )
    assert synth, "合成语料的消息不在库里 —— demo 那一步没写进去"

    web_ids = {m.msg_id for m in web}
    synth_ids = {m.msg_id for m in synth}
    assert not (web_ids & synth_ids), (
        f"网页源与合成源的 msg_id 撞了：{sorted(web_ids & synth_ids)} —— "
        f"撞上会让一条覆盖另一条，而 `INSERT OR IGNORE` 一声不吭"
    )

    # 网页源的群号来自 YECHAO_WEB_GROUP_ID（不是网页里来的）。
    assert {m.group_id for m in web} == {网页群号}
    # 网页通知没有账号可言 ⇒ None，用 0 冒充会让它被人物筛选命中。
    assert {m.actor_account for m in web} == {None}


def test_网页源的通知也走到了日报里(链路) -> None:
    """**接缝：网页源的消息 → refine → digest。**

    ※ 这一条防的是「源读进来了，但下游只处理了合成语料」那种半截接线：
    库里两条来源都有，而日报里只有一条来源的条目。
    """
    from yechao.infrastructure.sources.web_parse import wall_clock_to_ts

    digests = 链路.client.get("/api/digests")[1]["digests"]
    assert digests, "一篇日报都没有"

    day = 链路.app.clock.day_of(wall_clock_to_ts("2026-10-18 10:00:00"))
    found = next((d for d in digests if d["day"] == day), None)
    assert found is not None, f"没有 {day} 的日报（网页通知的发布时间就落在这一天）"

    _st, detail = 链路.client.get(f"/api/digests/{found['digest_id']}")
    groups = {i["group_id"] for i in detail["items"]}
    assert 网页群号 in groups, (
        f"{day} 的日报里没有网页源那个群的条目 —— 网页消息没走完 refine→digest"
    )


# ── 接缝：日报素材 → 模型 → 回连 → 渲染 ────────────────────────────


def test_日报素材里带了编号_所以每一行都能回连到条目(链路) -> None:
    """**接缝：`build_model_input` 的编号 → 提示词 → `parse_digest_lines` 的回连。**

    ※ 这条钉的是那三条坑里的第三条。素材里**没给编号**而提示词要求
    「照抄编号」时，模型没有一个可以照抄的数，回填出来的编号永远对不上
    `{it.item_id for it in items}` —— 每一行都被丢掉、**全部退化成机械行**。

    而那个失败**是看不见的**：机械行兜底让日报照样有内容，报告上一切正常。
    ⇒ 所以判据必须落在**产出**上：正文里每一行都得是模型写的那一句。
    """
    users = 链路.stub.digest_users
    assert users, "digest 一次都没调模型 —— 日报那一步没走到模型"

    numbered = [u for u in users if _DIGEST_LINE.search(u)]
    assert numbered, (
        "发给模型的日报素材里**一行编号都没有** —— "
        "`build_model_input` 没写 `- [item_id] [kind] 标题`，"
        "而提示词要求照抄编号 ⇒ 每一行都会回连失败"
    )

    digests = 链路.client.get("/api/digests")[1]["digests"]
    with_items = [d for d in digests if d["item_count"] > 0]
    assert with_items, "每一篇日报都是空的 —— refine 与 digest 之间断了"

    for d in with_items:
        _st, detail = 链路.client.get(f"/api/digests/{d['digest_id']}")
        bullets = [
            ln for ln in detail["body_md"].splitlines() if ln.startswith("- ")
        ]
        assert bullets, f"{d['day']} 的日报正文里一条要点都没有"
        mechanical = [ln for ln in bullets if "今天要处理：" not in ln]
        assert not mechanical, (
            f"{d['day']} 的日报里有 {len(mechanical)}/{len(bullets)} 行**退化成"
            f"机械行**（没有模型版本）。机械行是 `标题 · 地点 · 群名`，"
            f"而桩写的每一行都以「今天要处理：」开头。\n"
            f"前两行：{mechanical[:2]}"
        )


def test_日报引用的条目都能在接口上取到(链路) -> None:
    """**接缝：`insert_digest` 的 `item_digests` → `digest_detail` → HTTP。**

    ※ 日报正文里写着一件事、点进去却取不到那条条目 —— 这是「两边都正常、
    合起来错」的典型。判据是「正文里的行数 == 详情里能取到的条目数」。
    """
    digests = 链路.client.get("/api/digests")[1]["digests"]
    for d in digests:
        if d["item_count"] == 0:
            continue
        st, detail = 链路.client.get(f"/api/digests/{d['digest_id']}")
        assert st == 200
        bullets = [
            ln for ln in detail["body_md"].splitlines() if ln.startswith("- ")
        ]
        assert len(detail["items"]) == len(bullets), (
            f"{d['day']}：正文里 {len(bullets)} 行，"
            f"`digest_detail` 只给回 {len(detail['items'])} 条 —— "
            f"`item_digests` 没把引用写全"
        )
        for i in detail["items"]:
            st2, one = 链路.client.get(f"/api/items/{i['item_id']}")
            assert st2 == 200, f"日报引用了条目 {i['item_id']}，但详情接口取不到"
            assert one["item_id"] == i["item_id"]


# ── 接缝：出网边界 → 审计归档 ───────────────────────────────────────


def test_出网审计有记录而且记录里不含正文(链路) -> None:
    """**接缝：`scrub` 那一刻 → `JsonlAuditLog` → 磁盘。**

    ※ 两半都要：
      · **有记录**（否则「我们做了脱敏」是一句无法证伪的承诺）
      · **不含正文**（否则审计本身就是新造的一个泄漏面，
        而它恰好会存下最该保护的那些文本）

    判据是机械的：把归档文件当**纯文本**读，正文里任何一段都不该出现在里面。
    """
    files = sorted(链路.data.glob(f"{PREFIX}*{SUFFIX}"))
    assert files, f"没有出网审计归档（{链路.data} 下没有 {PREFIX}*{SUFFIX}）"

    raw = "\n".join(f.read_text(encoding="utf-8") for f in files)
    records = [json.loads(ln) for ln in raw.splitlines() if ln.strip()]
    assert records, "审计文件在，但一条记录都没有"

    surfaces = {r.get("surface") for r in records}
    assert "refine" in surfaces, f"没有 refine 的出网记录（只有 {surfaces}）"
    assert "digest" in surfaces, f"没有 digest 的出网记录（只有 {surfaces}）"

    # 键必须落在白名单里 —— 白名单而不是黑名单：黑名单会在有人加新字段时
    # 静默放行，而如果那个新字段是内容，它就一路写进了磁盘。
    for r in records:
        extra = set(r) - set(ALLOWED_KEYS) - {"_file", "_bad_lines"}
        assert not extra, f"审计记录里有白名单外的键：{sorted(extra)}"
        assert r.get("sha256"), f"记录里没有指纹：{r}"

    # 正文一个字都不许出现在归档里。
    for it in 链路.reader.all_items()[:5]:
        assert it.title not in raw, f"审计归档里出现了条目正文：{it.title!r}"
    for 片段 in ("选课系统将于", "校内勤工助学岗位招聘", "请提前归还借阅图书"):
        assert 片段 not in raw, f"审计归档里出现了源文片段：{片段!r}"

    # ※ 名单为空时「命中 0」的含义是「没查」。配置里写了 members，
    # 所以这里必须**真的比对过** —— 否则那个 0 会被读成「查了没漏」。
    checked = [int(r.get("names_checked") or 0) for r in records]
    assert max(checked) > 0, (
        "所有审计记录的 `names_checked` 都是 0 —— 姓名那一维**根本没查**，"
        "而不是「查了没漏」（群配置里的 members 没被读进来？）"
    )


# ── 接缝：网页 → 只读连接 → JSON ────────────────────────────────────


def test_接口能读到东西(链路) -> None:
    """**接缝：仓储 → `view.item_out` → JSON → HTTP。**

    ※ 这一条是「整条链路真的通」的最后一道：前四步都对了，但接口层
    拿不到东西（路由写错、只读连接打不开、序列化炸了），用户看到的还是
    一个空页面。
    """
    st, body = 链路.client.get("/api/items?limit=200")
    assert st == 200, f"/api/items 回了 {st}"
    assert body["total"] > 0, "/api/items 一条都没有"
    assert body["items"], "total 有值但 items 是空的"

    one = body["items"][0]
    assert one["kind"] in {c.slug for c in 链路.app.config.categories().categories}
    assert one["kind_label"], "`kind_label` 是空的 —— 后端没拼展示名"
    assert one["group_name"], "`group_name` 是空的"

    st, cats = 链路.client.get("/api/categories")
    assert st == 200 and cats["categories"], "/api/categories 空的"

    st, digests = 链路.client.get("/api/digests")
    assert st == 200 and digests["digests"], "/api/digests 空的"

    st, cal = 链路.client.get("/api/calendar")
    assert st == 200, "/api/calendar 打不开"

    st, health = 链路.client.get("/api/health")
    assert st == 200, f"/api/health 回了 {st}：{health}"
    assert health["database"]["ok"] is True, f"库探活不过：{health['database']}"

    st, privacy = 链路.client.get("/api/privacy")
    assert st == 200 and privacy["records"] > 0, f"隐私页看不到出网记录：{privacy}"


def test_搜索能搜到条目(链路) -> None:
    """**接缝：`insert_items` 的 FTS 增量写 → `search(q=)` → HTTP。**

    ※ 这条曾经断过：`rebuild_fts()` 写好了、有实现，但**全仓没有调用者**，
    于是 `q=` 对任何查询都回 **200 + 空列表**（不报错）—— 而搜索框是
    评委第一个会点的地方。

    这里在**端到端**这一层再钉一次：条目是 refine 写进去的（不是测试直接插的）。
    """
    _st, listing = 链路.client.get("/api/items?limit=200")
    target = next(
        (i for i in listing["items"] if len(i["title"]) >= 4), listing["items"][0]
    )
    word = target["title"][:4]

    st, found = 链路.client.get(f"/api/items?q={urllib.parse.quote(word)}")
    assert st == 200, f"搜索回了 {st}"
    assert found["total"] >= 1, (
        f"搜「{word}」一条都没搜到，而标题里就有它 —— "
        f"`insert_items` 没写 `items_fts`（搜索框静默失效）"
    )
    assert any(i["item_id"] == target["item_id"] for i in found["items"])


# ── 接缝：软删（**会改状态，所以放最后**）───────────────────────────


def test_软删的条目从列表搜索日报详情里同时消失(链路) -> None:
    """**接缝：`soft_delete` 的事件 → 各条读路径的口径是否一致。**

    ※ 不变量 ② 说：删掉之后它不再出现，但那一行还在库里。而「界面上」有**三条**
    读路径：列表、搜索、日报详情。它们各自写自己的 SQL，所以**会漂**：
    日报详情那一处就漏过 `i.deleted = 0`，于是同一个条目在列表里看不见、
    在日报里还在 —— 两个读路径给出相反的答案，而**都不报错**。

    ※ 这一条**会改状态**，所以它在文件末尾，并在结尾撤销掉。
    """
    digests = 链路.client.get("/api/digests")[1]["digests"]
    with_items = [d for d in digests if d["item_count"] > 0]
    assert with_items, "前提不成立：没有非空的日报"

    # 挑一条同时出现在日报详情里、且标题够长能搜的条目。
    target = None
    for d in with_items:
        _st, detail = 链路.client.get(f"/api/digests/{d['digest_id']}")
        cand = next((i for i in detail["items"] if len(i["title"]) >= 4), None)
        if cand is not None:
            target = (d, cand)
            break
    assert target is not None, "前提不成立：日报详情里没有可用的条目"

    digest, item = target
    item_id = item["item_id"]
    word = item["title"][:4]

    # 前提：删之前三条路都看得见它。
    _st, before = 链路.client.get(f"/api/items?q={urllib.parse.quote(word)}")
    assert any(i["item_id"] == item_id for i in before["items"]), (
        f"前提不成立：删之前搜索就找不到条目 {item_id}"
    )
    _st, one = 链路.client.get(f"/api/items/{item_id}")
    assert one["item_id"] == item_id

    st, body = 链路.client.delete(f"/api/items/{item_id}")
    assert st == 200 and "edit_id" in body, f"删除回了 {st}：{body}"

    _st, listing = 链路.client.get("/api/items?limit=200")
    assert all(i["item_id"] != item_id for i in listing["items"]), (
        f"软删之后列表里还看得见条目 {item_id}"
    )

    st2, detail2 = 链路.client.get(f"/api/items/{item_id}")
    assert st2 == 404, f"软删之后详情接口回了 {st2}（应当是 404）"

    _st, after = 链路.client.get(f"/api/items?q={urllib.parse.quote(word)}")
    assert all(i["item_id"] != item_id for i in after["items"]), (
        f"软删之后搜索里还搜得到条目 {item_id} —— 搜索那条路漏了 deleted 判据"
    )

    _st, ddetail = 链路.client.get(f"/api/digests/{digest['digest_id']}")
    assert all(i["item_id"] != item_id for i in ddetail["items"]), (
        f"软删之后日报详情里还列着条目 {item_id} —— "
        f"`digest_detail` 漏了 `i.deleted = 0`，与列表/搜索口径不一致"
    )

    # ── 收尾：撤销，把状态还回去（后面的用例不该看到这条被删）──
    st3, undone = 链路.client.post("/api/undo", {"edit_id": None})
    assert st3 == 200 and undone["undone"] is True, f"撤销没成功：{undone}"
    _st, restored = 链路.client.get("/api/items?limit=200")
    assert any(i["item_id"] == item_id for i in restored["items"]), (
        "撤销之后条目没有回来 —— 事件里的「原来是什么」没被用上"
    )
