"""网页通知源 —— 三种入口、两条「0 条」判据、以及夹具。

## 这个文件为什么**一个网络请求都不发**

判据在 `web_fetch` 的模块 docstring 里：抓取会碰网络、会变（站点更新了），
所以它不该出现在测试里；解析只吃字节，同样的字节永远给同样的结果，所以它
必须能被离线测。把两者混在一起，最需要被测试的那部分（解析）反而没人测。

⇒ 本文件里出现的网络相关用例**全部注入了假 opener**，它们测的是
「拿到这些字节之后怎么处理」，不是「能不能连上网」。真抓取那一步只能手动跑，
它没有自动化用例 —— 这是刻意的，写在这里免得下一个人以为漏了。

## 样本全部自己编

※ 红线。夹具页与这里临时造的页面都是**本文件/本仓库自编**的：假校名
「青梧」、假域名 `example.test`、假电话 `17700001003`、假群号 `820000005`。
编出来的不必像真的，格式对就够。
"""

from __future__ import annotations

import ast
import hashlib
import json
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from yechao.application import ports
from yechao.domain.errors import (
    ConfigError,
    SourceError,
    SourceFormatError,
    SourceUnavailable,
)
from yechao.domain.models import PendingMessage
from yechao.infrastructure.sources import REGISTRY, build, web_fetch, web_parse, web_source
from yechao.infrastructure.sources.web_parse import (
    _PageHasNoNotices,
    _PageNotUnderstood,
    parse_page,
)

# ── 夹具在哪 ────────────────────────────────────────────────────────

夹具目录 = Path(web_source.__file__).resolve().parent / "fixtures"
夹具 = 夹具目录 / "qingwu-notices.html"
夹具侧车 = 夹具目录 / "qingwu-notices.meta.json"

#: 夹具那一页自报的站点（侧车里的 `source_url` 的站点部分）。
夹具站点 = "https://notice.example.test"


@pytest.fixture()
def 干净的配置(tmp_path, monkeypatch):
    """把三个环境变量与配置目录都指到临时位置。

    ※ 不这么做的话，用例会读到**开发机上的** `config/sources.toml` 与
    `YECHAO_WEB_SOURCE` —— 于是「没配目标时抛」这条在别人机器上会因为
    「他恰好配了」而失败，而那种失败看起来像代码坏了。
    """
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv(web_source.CONFIG_DIR_ENV, str(cfg))
    monkeypatch.delenv(web_source.TARGET_ENV, raising=False)
    monkeypatch.delenv(web_source.GROUP_ID_ENV, raising=False)
    return cfg


@pytest.fixture()
def 指向夹具(干净的配置, monkeypatch):
    """把目标配成仓库里那份夹具。"""
    monkeypatch.setenv(web_source.TARGET_ENV, str(夹具))
    return 夹具


# ── 临时造的页面（自己编） ──────────────────────────────────────────


def _条目(
    title: str,
    href: str,
    *,
    at: str = "2026-09-14 09:20:00",
    dept: str = "教务处",
    summary: str = "",
) -> str:
    tail = f'<p class="notice-summary">{summary}</p>' if summary else ""
    return (
        f'<li class="notice-item" data-publish-time="{at}" data-department="{dept}">'
        f'<a class="notice-title" href="{href}">{title}</a>{tail}</li>'
    )


def _页(条目们: list[str], *, 容器类名: str = "notice-list") -> bytes:
    body = "".join(条目们)
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>青梧大学 · 通知公告</title></head><body>"
        '<div class="topbar"><ul class="topbar-nav">'
        '<li><a href="/index.html">首页</a></li></ul></div>'
        f'<ul class="{容器类名}">{body}</ul>'
        "</body></html>"
    ).encode("utf-8")


_甲 = _条目("青梧大学图书馆闭馆通知", "/notice/a.html", summary="10 月 1 日闭馆一天。")
_乙 = _条目(
    "青梧大学社团招新",
    "/notice/b.html",
    at="2026-09-15 08:00:00",
    dept="团委",
    summary="报名请加群 820000005。",
)
_丙 = _条目("青梧大学体测安排", "/notice/c.html", at="2026-09-16 12:00:00", dept="体育部")


# ── 夹具本身 ────────────────────────────────────────────────────────


def test_夹具解析出四条通知_字段各自落位() -> None:
    """一条通知 = 一条消息，且四个字段各有来源。

    ※ 断言 `sender` 取的是 `data-department` 而**不是** `notice-meta` 那行
    自由文本 —— 后者是「部门 · 日期」拼在一起的一句话，切它是猜。
    """
    raw = 夹具.read_bytes()
    msgs = parse_page(raw, namespace=夹具站点, group_id=820000001)

    assert len(msgs) == 4
    assert [m.sender for m in msgs] == ["教务处", "图书馆", "信息化中心", "青梧摄影社"]
    assert {m.source for m in msgs} == {"web"}
    assert {m.group_id for m in msgs} == {820000001}
    # 网页通知没有账号可言 ⇒ None，不是 0。
    # 用 0 冒充会让它被人物筛选命中（0 会被当成一个真实的账号）。
    assert {m.actor_account for m in msgs} == {None}
    for m in msgs:
        assert isinstance(m, PendingMessage)
        assert m.content.strip()


def test_正文是标题加摘要_两者都在() -> None:
    """标题回答「这是什么」，摘要回答「几点、在哪、怎么办」—— 缺一不可。

    只给标题，下游拿不到办理时间与地点；只给摘要，下游不知道这是什么。
    """
    msgs = parse_page(夹具.read_bytes(), namespace=夹具站点, group_id=820000001)
    first = msgs[0]
    lines = first.content.split("\n")
    assert lines[0] == "2026 年秋季学期补退选课安排"
    assert "补退选课系统将于 9 月 16 日 08:00 开放" in lines[1]


def test_夹具页上的假号码与假群号是自己编的() -> None:
    """※ 红线自检：夹具里**不许**出现真实站点的真实内容。

    这条判据能机械检查的部分只有「用了保留域名与自编号段」；剩下的部分
    （内容是不是抄的）只能靠人看。它至少把「哪天有人把抓来的真页面提交进来」
    变成一次红灯。
    """
    text = 夹具.read_text(encoding="utf-8")
    meta = json.loads(夹具侧车.read_text(encoding="utf-8"))

    assert "example.test" in meta["source_url"], "侧车里的来源必须是保留域名"
    assert meta["source_url"].startswith("https://notice.example.test/")
    # 夹具正文里出现的电话与群号都落在本项目自编的号段上。
    assert "17700001003" in text
    assert "820000005" in text
    # 侧车自己说清了它是手写的、不是抓来的。
    assert meta["fetched_at"] is None
    assert meta["robots"] is None


def test_夹具侧车的指纹与_html_对得上_夹具过期会被发现() -> None:
    """※ 守的是「素材改了但夹具没重新生成」。

    症状：演示出来的数字与实际不符 —— 而那是**评委会看到的差异**，
    比代码里的错更难解释。指纹对不上 ⇒ 要么重新生成夹具，要么承认它被手改过。
    """
    meta = json.loads(夹具侧车.read_text(encoding="utf-8"))
    raw = 夹具.read_bytes()
    assert meta["sha256"] == hashlib.sha256(raw).hexdigest()
    assert meta["bytes"] == len(raw)


# ── 两种「0 条」 ────────────────────────────────────────────────────


def test_没读懂与没有通知必须抛不同的东西() -> None:
    """**这是本模块的核心判据。**

    两者都是「解析出 0 条」，而产出为空是所有错误里最不容易被注意到的一种。
    形状相同的话，运维会拿着「站点今天没发通知」的解释去看一个其实已经改版
    了的解析器 —— 而两者的下一步完全不同（改解析器 / 查站点）。

    ⇒ 分界线是**列表容器**：改版会把它改名或挪走，「今天没发通知」不会动它。
    """
    # 容器改名 ⇒ 没读懂
    with pytest.raises(_PageNotUnderstood) as 改版:
        parse_page(
            _页([_甲], 容器类名="notice-list-v2"),
            namespace=夹具站点,
            group_id=1,
        )
    # 容器在、里面空的 ⇒ 读懂了，只是没有通知
    with pytest.raises(_PageHasNoNotices) as 空页:
        parse_page(_页([]), namespace=夹具站点, group_id=1)

    assert type(改版.value) is not type(空页.value)
    # 两者都仍是 SourceFormatError —— 用例层的处置不变（`ingest` 照旧记账、
    # 照旧非零退出），变的只是人能读到的**下一步**。
    assert isinstance(改版.value, SourceFormatError)
    assert isinstance(空页.value, SourceFormatError)
    assert "改版" in str(改版.value)
    assert "没发通知" in str(空页.value)


def test_容器在但条目全是空壳时算没读懂() -> None:
    """`<li class="notice-item"></li>` **不是**一条没有标题的通知。

    它是模板残留/占位。而它算「一条」的话，一个改版到只剩空壳的页面会
    「成功」产出 0 条有效消息，却声称读到了 N 条 —— 比直接报错更糟。
    """
    page = _页(['<li class="notice-item"></li>', '<li class="notice-item"></li>'])
    with pytest.raises(_PageNotUnderstood) as ei:
        parse_page(page, namespace=夹具站点, group_id=1)
    assert "标题" in str(ei.value)
    assert not isinstance(ei.value, _PageHasNoNotices)


def test_一条通知缺发布时间时整页报错_而不是静默少一条() -> None:
    """※ 判据：**缺字段不静默跳过那一条**。

    静默跳过一条消息正是本项目明令禁止的形状（用户会以为那天没事）。
    而缺字段最常见的成因是站点改版 —— 那会命中每一条，所以「只坏了一条」
    是例外情形，为它设计一条安静的旁路不划算，代价却是把真问题也吞掉。
    """
    page = _页([_甲, _条目("缺时间的通知", "/notice/x.html", at=""), _乙])
    with pytest.raises(_PageNotUnderstood) as ei:
        parse_page(page, namespace=夹具站点, group_id=1)
    assert "第 2 条" in str(ei.value)


def test_一条通知没有_href_时整页报错() -> None:
    """href 是这条通知在站点里的**身份**，也是幂等键的来源。

    没有它就保证不了「重跑不重复入库」，而重复入库是无声的 ——
    日报里同一条通知出现两次，没有任何东西会报错。
    """
    page = _页([_条目("没有链接的通知", "")])
    with pytest.raises(_PageNotUnderstood) as ei:
        parse_page(page, namespace=夹具站点, group_id=1)
    assert "href" in str(ei.value)


# ── 身份（msg_id） ──────────────────────────────────────────────────


def test_条目顺序变化不改变_msg_id() -> None:
    """※ 位置**不是**身份。

    列表顺序会变：一条通知被置顶之后，它后面每一条的位置都往后挪一格。
    用位置当 id 的话，置顶一条会让整页换一批新 id，于是重跑摄取把它们
    全部重写一遍 —— 日报里同一条通知出现两次，而没有任何东西会报错。
    """
    page = _页([_甲, _乙, _丙])
    换序 = _页([_丙, _甲, _乙])

    前 = {m.msg_id: m.content for m in parse_page(page, namespace=夹具站点, group_id=1)}
    后 = {m.msg_id: m.content for m in parse_page(换序, namespace=夹具站点, group_id=1)}

    assert 前 == 后, "换了个顺序就换了一批 id —— 那会让重跑摄取重复入库"


def test_同一站点换个入口读出来的是同一批_msg_id(指向夹具) -> None:
    """※ 这条钉的是「抓一次落盘、之后离线读」这条路。

    夹具入口的命名空间来自侧车里的 `source_url`，URL 入口的来自那个 URL ——
    两者算出来**必须一样**，否则「先联网抓一次、之后每次离线读夹具」会让库里
    出现两份同样的通知（抓来的那份 id 来自站点，夹具那份 id 来自文件路径）。
    """
    从夹具 = [m.msg_id for m in web_source.build_web_source().read()]

    原始 = 夹具.read_bytes()
    # 直接把同一批字节当成「从那个 URL 抓回来的」来解析。
    从URL = [
        m.msg_id
        for m in parse_page(原始, namespace=web_source._origin("https://notice.example.test/qingwu/index.html"), group_id=1)
    ]

    assert 从夹具 == 从URL


def test_换一个站点同一批通知就是另一批_msg_id() -> None:
    """`/notice/a.html` 在两个站点上是两篇不同的文章。

    只按 href 算 id 的话，两个站点会撞出同一个 id，而症状是
    「后抓的那个站点的通知被当成已存在，一条都没进库」。
    """
    page = _页([_甲])
    甲站 = [m.msg_id for m in parse_page(page, namespace="https://a.example.test", group_id=1)]
    乙站 = [m.msg_id for m in parse_page(page, namespace="https://b.example.test", group_id=1)]
    assert 甲站 != 乙站


def test_msg_id_落在_sqlite_能装的范围内() -> None:
    """※ 取 63 位而不是 64 位。

    超过 `2**63-1` 的 id 在插入时报 "Python int too large to convert to
    SQLite INTEGER" —— 而那是按哈希值随机发生的，看起来像偶发故障。
    """
    msgs = parse_page(夹具.read_bytes(), namespace=夹具站点, group_id=1)
    for m in msgs:
        assert 0 <= m.msg_id <= 2**63 - 1


# ── 时间 ────────────────────────────────────────────────────────────


def test_时间按站点本地时区解释而不是机器时区() -> None:
    """※ 时区取**全项目那一处口径**（UTC+8），不取机器本地时区。

    取机器本地的话，同一份夹具在评委的机器上会算出不同的秒数，而日报是
    **按天分**的 —— 差 8 小时足以让一条通知落到前一天。那是跨机器才显形、
    且看起来像「数据本来就这样」的差异。

    锚点：2026-09-14 09:20:00（北京时间）= 2026-09-14T01:20:00Z = 1789348800。
    这个字面量是刻意写死的 —— 用同一个公式算出来的期望值只能证明代码自洽，
    证明不了「不依赖机器时区」。
    """
    msgs = parse_page(
        _页([_条目("锚点通知", "/notice/tz.html", at="2026-09-14 09:20:00")]),
        namespace=夹具站点,
        group_id=1,
    )
    assert msgs[0].ts == 1789348800

    # 再钉一次定义：墙上时间按 UTC+8 解释。
    定义 = int(
        datetime(2026, 9, 14, 9, 20, tzinfo=timezone(timedelta(hours=8))).timestamp()
    )
    assert msgs[0].ts == 定义


def test_时间写法不认识时抛_不兜底成今天() -> None:
    """兜底会把一条通知放进错误的一天，而它**不报错**。"""
    page = _页([_条目("时间写法怪的通知", "/notice/x.html", at="9月14日 上午")])
    with pytest.raises(_PageNotUnderstood):
        parse_page(page, namespace=夹具站点, group_id=1)


# ── 时间窗 ──────────────────────────────────────────────────────────


def test_时间窗在解析之后过_窗口内为空不算格式错(指向夹具) -> None:
    """※ 「解析出 0 条」与「这段时间里没有」是两件事。

    前者是格式问题（去改解析器），后者是数据问题（这段时间真的安静）。
    先解析、再过滤，两者就分得开：**解析出的条数是 0 才抛，窗口内 0 条
    只是返回空**。反过来（先过滤再判 0）会让「窗口挑错了」伪装成
    「站点改版了」—— 排查方向完全错。
    """
    src = web_source.build_web_source()

    assert len(src.read()) == 4
    # 窗口把四条全滤掉 ⇒ 空列表，**不抛**。
    assert src.read(since=4102444800, until=4102444801) == []
    # 窗口正好框住第一条。
    covered = src.read(since=1789348800, until=1789348801)
    assert [m.ts for m in covered] == [1789348800]


def test_窗口是半开区间(指向夹具) -> None:
    """`[since, until)`。闭区间会让 00:00:00 那一刻的消息落进两天里。"""
    src = web_source.build_web_source()
    ts = 1789348800
    assert len(src.read(since=ts, until=ts + 1)) == 1
    assert src.read(since=ts + 1, until=ts + 2) == []
    assert src.read(since=ts, until=ts) == []


# ── 编码 ────────────────────────────────────────────────────────────


def test_按页面声明的编码解_gbk_也认() -> None:
    """编码声明在页面里，就该按它解 —— 不靠猜。"""
    body = _页([_条目("青梧大学校运会报名", "/notice/gbk.html")])
    text = body.decode("utf-8").replace('charset="utf-8"', 'charset="gbk"')
    msgs = parse_page(text.encode("gbk"), namespace=夹具站点, group_id=1)
    assert msgs[0].content.startswith("青梧大学校运会报名")


def test_编码声明与字节不符时抛_而不是产出乱码() -> None:
    """※ 判据：**不换一个编码重试**。

    按错的编码解通常**解不出错**（GBK 的字节能被 latin-1 / cp1252 照单全收），
    于是你得到一页乱码，而乱码能一路走完解析、「成功」产出 N 条乱码消息 ——
    那是「产出为空」的镜像：**产出垃圾**。两种都不可见，所以都不接受。
    """
    body = _页([_条目("青梧大学校运会报名", "/notice/mix.html")])
    text = body.decode("utf-8").replace('charset="utf-8"', 'charset="gbk"')
    with pytest.raises(_PageNotUnderstood) as ei:
        乱码 = text.encode("utf-8")  # 声明 GBK，实际 UTF-8
        parse_page(乱码, namespace=夹具站点, group_id=1)
    assert "gbk" in str(ei.value).lower()


def test_没有声明编码时按_utf8_解() -> None:
    body = _页([_条目("青梧大学体测补测", "/notice/nometa.html")])
    text = body.decode("utf-8").replace('<meta charset="utf-8">', "")
    msgs = parse_page(text.encode("utf-8"), namespace=夹具站点, group_id=1)
    assert len(msgs) == 1


# ── 三种入口 ────────────────────────────────────────────────────────


def test_三种入口按目标自身的形状区分(tmp_path, 干净的配置) -> None:
    """URL / 夹具 / 本地 HTML —— 判据是**可观察的事实**，不是用户声明。

    ※ 夹具与本地 HTML 的唯一区别是「知不知道它代表哪个站点」。知道就能算出
    与直接读那个 URL 相同的 id；不知道就只能拿文件名兜底。非黑即白，正好
    可以机械判断 —— 所以它成为分界。
    """
    裸文件 = tmp_path / "随便下的页面.html"
    裸文件.write_bytes(_页([_甲]))

    url = web_source._classify(
        "https://notice.example.test/qingwu/index.html", group_id=1, where="t"
    )
    assert (url.kind, url.namespace, url.is_remote) == ("url", 夹具站点, True)

    夹 = web_source._classify(str(夹具), group_id=1, where="t")
    assert (夹.kind, 夹.namespace, 夹.is_remote) == ("fixture", 夹具站点, False)

    裸 = web_source._classify(str(裸文件), group_id=1, where="t")
    assert 裸.kind == "file"
    assert 裸.namespace == "file:随便下的页面.html"
    assert not 裸.is_remote


def test_夹具入口真的会去读侧车(tmp_path, 干净的配置) -> None:
    """※ 侧车坏了就**抛**，不猜。

    猜出来的命名空间会让同一批通知换一批 id，于是重跑摄取重复入库。
    """
    page = tmp_path / "x.html"
    page.write_bytes(_页([_甲]))
    (tmp_path / "x.meta.json").write_text("{ 这不是 JSON", encoding="utf-8")

    with pytest.raises(SourceFormatError) as ei:
        web_source._classify(str(page), group_id=1, where="t")
    assert "侧车" in str(ei.value)


def test_侧车里的_source_url_必须是绝对地址(tmp_path, 干净的配置) -> None:
    page = tmp_path / "y.html"
    page.write_bytes(_页([_甲]))
    (tmp_path / "y.meta.json").write_text(
        json.dumps({"source_url": "notice.example.test"}, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(SourceFormatError):
        web_source._classify(str(page), group_id=1, where="t")


# ── 配置 ────────────────────────────────────────────────────────────


def test_没配目标时抛_不静默兜底(干净的配置) -> None:
    """※ 兜底到某个默认页面上，`--source web` 就会在什么都没配的情况下
    「成功」读到别人的数据，而报告看起来一切正常。"""
    with pytest.raises(ConfigError) as ei:
        web_source.resolve_target()
    # 报错要说清**怎么配**，否则用户只能去读源码。
    assert web_source.TARGET_ENV in str(ei.value)
    assert "sources.toml" in str(ei.value)


def test_配置文件里的_target_也能用(tmp_path, 干净的配置) -> None:
    """两种入口都要能用：环境变量（临时）与配置文件（长期）。"""
    # ※ `as_posix()`：TOML 的基本字符串里反斜杠是转义字符，Windows 路径直接
    # 写进去会解析失败（`Unescaped '\\' in a string`）—— 而那会被报成「配置文件坏了」，
    # 与「路径写对了」在报错里长得不一样。
    (干净的配置 / web_source.SOURCES_FILE).write_text(
        f'[web]\ntarget = "{夹具.as_posix()}"\ngroup_id = 820000042\n',
        encoding="utf-8",
    )
    t = web_source.resolve_target()
    # ※ 用 `Path` 比而不比字符串：配置里写的是正斜杠，而 `str(夹具)`
    # 在 Windows 上是反斜杠—— 两者指向同一个文件。
    assert Path(t.raw) == 夹具
    assert t.kind == "fixture"
    assert t.group_id == 820000042


def test_环境变量压过配置文件(tmp_path, 干净的配置, monkeypatch) -> None:
    """环境变量是**这一次**的意图，配置文件是**长期**的意图 ——
    临时的不该被长期的覆盖掉。"""
    (干净的配置 / web_source.SOURCES_FILE).write_text(
        f'[web]\ntarget = "{夹具.as_posix()}"\n', encoding="utf-8"
    )
    monkeypatch.setenv(web_source.TARGET_ENV, "https://notice.example.test/x.html")
    assert web_source.resolve_target().kind == "url"


def test_分组号的默认值可以看出来且能被覆盖(指向夹具, monkeypatch) -> None:
    assert web_source.resolve_target().group_id == web_source.DEFAULT_GROUP_ID
    monkeypatch.setenv(web_source.GROUP_ID_ENV, "820000042")
    assert web_source.resolve_target().group_id == 820000042


def test_分组号写错时抛(指向夹具, monkeypatch) -> None:
    monkeypatch.setenv(web_source.GROUP_ID_ENV, "八二零")
    with pytest.raises(ConfigError):
        web_source.resolve_target()


def test_配置目录的口径与组合根一致(tmp_path, monkeypatch) -> None:
    """※ 本模块**不能** import `bootstrap`（会造出导入环），所以 `config_dir`
    的默认值在这里重新算了一次。代价是「默认值有两处」—— 这条判据把两边钉住。

    它抓的是那种「改了组合根却漏了这一处」的漂移，症状是
    「配置文件放在 `--config-dir` 指定的地方，网页源却看不见它」。
    """
    import yechao.bootstrap as bootstrap
    from yechao.bootstrap import Settings

    assert web_source.DEFAULT_CONFIG_DIR == bootstrap.DEFAULT_CONFIG_DIR

    monkeypatch.setenv(web_source.CONFIG_DIR_ENV, str(tmp_path / "改过的配置目录"))
    assert web_source._config_dir() == Settings.from_env().config_dir

    monkeypatch.delenv(web_source.CONFIG_DIR_ENV)
    assert web_source._config_dir() == Settings.from_env().config_dir


# ── 端口形状与注册表 ────────────────────────────────────────────────


def test_网页源满足端口形状且不切分(指向夹具) -> None:
    """※ 判据 3：**形状由接口约束，处理方式不由接口约束**。

    网页通知与邮件同类（一条就是一条自洽短文本，**不切**），而会议转写必须切
    —— 两者对下游长得一样。为了迎合同一个下游而做同样的处理，其中一个必然错。
    """
    src = web_source.build_web_source()
    assert isinstance(src, ports.MessageSource)
    assert src.name == "web"

    msgs = src.read()
    assert len(msgs) == 4, "四条通知就该是四条消息 —— 多一条都说明它切了"
    assert all(isinstance(m, PendingMessage) for m in msgs)


def test_读不到时抛_SourceUnavailable_不返回空(tmp_path, 干净的配置, monkeypatch) -> None:
    """判据 1。返回空的后果：下游会以为「这个站点今天没更新」。"""
    monkeypatch.setenv(web_source.TARGET_ENV, str(tmp_path / "没有这个文件.html"))
    with pytest.raises(SourceUnavailable):
        web_source.build_web_source().read()


def test_注册表里有_web_且能造出来(指向夹具) -> None:
    """※ 守的是「接上了但选不出来」：注册表是「有哪些源」的唯一答案。

    它需要一个目标才能造出来 —— 工厂在装配时就把目标定下来（见 `build_web_source`）。
    """
    assert "web" in REGISTRY
    built = build(["web"])
    assert [type(s).__name__ for s in built] == ["_WebSource"]
    assert isinstance(built[0], ports.MessageSource)


def test_没配目标时选中网页源就报配置错(干净的配置) -> None:
    """※ 判据在**退出码**上：`cli.py` 把 `build()` 抛的 `ConfigError`
    归在「用法错」（退出码 2），而把「跑起来之后才失败」归在 1。

    两者的下一步不同：前者去改配置/命令行，后者去查站点。
    要是等 `read()` 才报，用户看到的就是一个「跑起来了但读不到」的错，
    而真相是他压根没配。

    它同时保证了报错发生在**任何一个源开始读之前**。
    """
    with pytest.raises(ConfigError):
        build(["web"])


def test_web_不在默认源里_所以_demo_仍然离线() -> None:
    """※ 默认源要在**离线、无配置**的情况下可用（`demo` 那条路的地基），
    而网页源需要一个目标、还可能要走网络。

    默认带上它的症状：`python -m yechao demo` 在一台没配过任何东西的机器上
    直接报错（或者更糟 —— 静默读了别的东西）。
    """
    from yechao.infrastructure.sources import DEFAULT_SOURCES

    assert "web" not in DEFAULT_SOURCES
    assert DEFAULT_SOURCES == ("synth",)


def test_解析模块不碰_IO() -> None:
    """※ 「抓取与解析分开」这条要求，机械可检查的那一半在这里。

    解析模块一旦 import 了 `urllib` / `pathlib` / `os`，它就不再是纯函数了 ——
    而「同样的字节永远给同样的结果」正是它值得被离线测试的理由。
    """
    tree = ast.parse(Path(web_parse.__file__).read_text(encoding="utf-8"))
    顶层包: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            顶层包.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            顶层包.add(node.module.split(".")[0])

    不许 = {"urllib", "socket", "http", "os", "pathlib", "io", "time", "tempfile", "shutil"}
    assert not (顶层包 & 不许), f"解析模块碰了 I/O：{sorted(顶层包 & 不许)}"


# ── 抓取：只测「拿到字节之后怎么办」，不发一个请求 ──────────────────


class _假响应:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_假响应":
        return self

    def __exit__(self, *_: object) -> bool:
        return False


def _假opener(页面: bytes, *, robots: bytes | None = b"", robots_状态: int = 200):
    """造一个 opener：`robots.txt` 与页面各返回预设的字节。

    `robots_状态` 非 200 时，请求 `robots.txt` 抛对应的 `HTTPError`。
    """

    def op(request, timeout=None):
        url = request.full_url
        if url.endswith("/robots.txt"):
            if robots_状态 != 200:
                raise urllib.error.HTTPError(url, robots_状态, "x", {}, None)
            assert robots is not None
            return _假响应(robots)
        return _假响应(页面)

    return op


def test_robots_不允许就不抓() -> None:
    """※ 红线：只碰 robots.txt 允许的页面。不绕、不换 UA、不重试。"""
    assert web_fetch.robots_allows("", "https://notice.example.test/a.html", user_agent="x")
    assert not web_fetch.robots_allows(
        "User-agent: *\nDisallow: /", "https://notice.example.test/a.html", user_agent="x"
    )
    assert web_fetch.robots_allows(
        "User-agent: *\nDisallow: /private/",
        "https://notice.example.test/a.html",
        user_agent="x",
    )

    with pytest.raises(SourceUnavailable) as ei:
        web_fetch.fetch_bytes(
            "https://notice.example.test/a.html",
            opener=_假opener(b"<html></html>", robots=b"User-agent: *\nDisallow: /"),
        )
    assert "robots" in str(ei.value)


def test_robots_读不到时保守不抓() -> None:
    """※ 4xx 是「没有限制」（RFC 9309），**5xx / 网络错误不是**。

    把 5xx 也当成「没有限制」的话，一个正在维护或封禁的站点会在最不该抓的
    时刻被抓 —— 判据是**不确定就不抓**。
    """
    # 404 ⇒ 视为没有 robots.txt ⇒ 抓。
    assert (
        web_fetch.fetch_bytes(
            "https://notice.example.test/a.html",
            opener=_假opener(b"<html>ok</html>", robots_状态=404),
        )
        == b"<html>ok</html>"
    )
    # 500 ⇒ 读不到规则 ⇒ 不抓。
    with pytest.raises(SourceUnavailable) as ei:
        web_fetch.fetch_bytes(
            "https://notice.example.test/a.html",
            opener=_假opener(b"<html>ok</html>", robots_状态=500),
        )
    assert "读不到规则就不抓" in str(ei.value)


def test_抓回来是_0_字节时不写空夹具(tmp_path) -> None:
    """※ 空夹具会**覆盖掉一份好的**，而且看起来像「抓到了，只是站点没内容」
    —— 正是「产出为空」那类失败。"""
    dest = tmp_path / "夹具.html"
    with pytest.raises(SourceUnavailable):
        web_fetch.fetch_to_file(
            "https://notice.example.test/a.html", dest, opener=_假opener(b"")
        )
    assert not dest.exists(), "抓失败却留下了一份（空的）夹具"
    assert not (tmp_path / "夹具.meta.json").exists()


def test_抓取失败不留下半截夹具(tmp_path) -> None:
    """※ 落盘走「先写临时文件再改名」。

    直接覆盖的话，写到一半失败会把上一份**好的**夹具毁成半截，而半截的 HTML
    仍然能被解析器读出一部分 —— 它看起来像「站点少发了几条」。
    """
    dest = tmp_path / "夹具.html"
    dest.write_bytes(b"<html>good</html>")

    def op(request, timeout=None):
        if request.full_url.endswith("/robots.txt"):
            raise urllib.error.URLError("dns")
        raise AssertionError("robots 都读不到就不该再去抓页面")

    with pytest.raises(SourceUnavailable):
        web_fetch.fetch_to_file("https://notice.example.test/a.html", dest, opener=op)

    assert dest.read_bytes() == b"<html>good</html>", "上一份好的夹具被毁了"
    assert not (tmp_path / "夹具.html.part").exists()


def test_抓成功时落盘并写下侧车(tmp_path) -> None:
    """夹具是**数据**：原始 HTML + 一份记着来源与指纹的侧车。"""
    dest = tmp_path / "抓来的.html"
    page = _页([_甲])
    meta = web_fetch.fetch_to_file(
        "https://notice.example.test/qingwu/index.html", dest, opener=_假opener(page)
    )

    assert dest.read_bytes() == page
    assert meta["sha256"] == hashlib.sha256(page).hexdigest()
    assert meta["robots"] == "allowed"

    侧车 = json.loads((tmp_path / "抓来的.meta.json").read_text(encoding="utf-8"))
    assert 侧车["source_url"] == "https://notice.example.test/qingwu/index.html"
    # 侧车一落盘，这份文件立刻就是「夹具入口」了 —— 抓与解在这里接上。
    t = web_source._classify(str(dest), group_id=1, where="t")
    assert (t.kind, t.namespace) == ("fixture", 夹具站点)


def test_网页源读到的错误都在_SourceError_这一族里(干净的配置, monkeypatch) -> None:
    """※ 跨层只抛 `YechaoError` 那一族 —— 「这一层可能抛什么」是个**闭集**。

    裸 `OSError` / `ValueError` 漏到边界上，入口层就没法给它配一个像样的
    状态码与文案（它会以 traceback 的样子出现，而那看起来像「程序坏了」）。
    """
    monkeypatch.setenv(web_source.TARGET_ENV, "https://notice.example.test/a.html")

    def op(request, timeout=None):
        raise urllib.error.URLError("dns")

    monkeypatch.setattr(web_fetch.urllib.request, "urlopen", op)
    with pytest.raises(SourceError):
        web_source.build_web_source().read()
