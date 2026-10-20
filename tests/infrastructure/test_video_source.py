"""视频源 —— 切分判据、时间锚点、四种目标形状、以及三种必须能分辨的失败。

## 这个文件为什么一个真实网络请求都不发

判据与 `test_web_source.py` 同一处：抓取会碰网络、会变（站点更新了），
所以它不该出现在测试里；解析只吃字节，同样的字节永远给同样的结果，
所以它必须能被离线测。B 站那一路的网络部分**全部注入了假的 `_http_get`**
—— 它们测的是「拿到这些字节之后怎么处理」，不是「能不能连上网」。
真抓取那一步只能手动跑，它没有自动化用例（**这是刻意的**，写在这里免得
下一个人以为漏了）。

## 样本全部自己编

※ 红线。夹具字幕、临时造的页面、B 站的首屏数据与字幕 JSON **全部是本文件
或本仓库自编**的：虚构校名「青梧」、虚构域名 `example.test` / `*.test`
（RFC 2606 保留，永远不会解析）、虚构人名（取自本项目自己的合成语料
`domain/synth.py`）。编出来的不必像真的，格式对就够。

## B 站那部分的诚实交代

`x/player/wbi/v2` 与投稿列表接口**没有在网络环境里跑通过**（没有 cookie 时
实测返回空字幕 / `-352` 风控）。这里测的是**解析**：喂进去一份按公开字段名
自编的返回，看它吐出什么。**它证明不了线上接口的形状是对的** —— 这一点
写在 `video_source` 的模块 docstring 里，也写在下面的用例注释里。
"""

from __future__ import annotations

import ast
import hashlib
import json
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

from yechao.application import ports
from yechao.domain.errors import ConfigError, SourceError, SourceUnavailable
from yechao.domain.models import PendingMessage
from yechao.infrastructure.sources import (
    bili_parse,
    video_page,
    video_parse,
    video_source,
    web_fetch,
)
from yechao.infrastructure.sources.video_page import (
    _VideoPageNotUnderstood,
    parse_video_page,
)
from yechao.infrastructure.sources.video_parse import (
    MAX_SEGMENT_SECONDS,
    MIN_SEGMENT_SECONDS,
    PAUSE_SECONDS,
    _SubtitleNotUnderstood,
    _VideoHasNoSubtitles,
    build_messages,
    parse_subtitles,
)

# ── 夹具在哪 ────────────────────────────────────────────────────────

夹具目录 = Path(video_source.__file__).resolve().parent / "fixtures"
夹具SRT = 夹具目录 / "qingwu-lecture.srt"
夹具VTT = 夹具目录 / "qingwu-lecture.vtt"

#: 夹具那一份视频的绝对起始时刻（侧车里的 `started_at`，UTC+8 墙上时间）。
夹具起点 = int(
    datetime(2026, 10, 13, 19, 0, 0, tzinfo=timezone.utc).timestamp()
) - 8 * 3600

#: 夹具那一份视频自报的地址（侧车里的 `source_url` 的标识）。
夹具标识 = "https://video.example.test/qingwu/lecture-2026-10-13"


@pytest.fixture()
def 干净的环境(tmp_path, monkeypatch):
    """把配置目录与四个环境变量都指到临时位置。

    ※ 不这么做的话，用例会读到**开发机上的** `config/sources.toml` 与
    `YECHAO_VIDEO_SOURCE` —— 于是「没配目标时抛」这条在别人机器上会因为
    「他恰好配了」而失败，而那种失败看起来像代码坏了。
    """
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv("YECHAO_CONFIG_DIR", str(cfg))
    for name in (
        video_source.TARGET_ENV,
        video_source.GROUP_ID_ENV,
        video_source.STARTED_AT_ENV,
        video_source.ROBOTS_OPT_IN_ENV,
        video_source.BILI_COOKIE_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    return cfg


def _读(目标, monkeypatch, **窗口):
    """配好目标，造一个源，读一遍。"""
    monkeypatch.setenv(video_source.TARGET_ENV, str(目标))
    return video_source.build_video_source().read(**窗口)


# ── 自己编的字幕（给切分判据用） ────────────────────────────────────


def _字幕(行: list[tuple[float, float, str]]) -> bytes:
    """`(起点, 终点, 正文)` 列表 → 一份 SRT 的字节。"""
    out = []
    for i, (start, end, text) in enumerate(行, 1):
        out.append(str(i))
        out.append("%s --> %s" % (_srt时间(start), _srt时间(end)))
        out.append(text)
        out.append("")
    return ("\n".join(out) + "\n").encode("utf-8")


def _srt时间(秒: float) -> str:
    ms = int(round(秒 * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def _消息(行: list[tuple[float, float, str]], *, 锚点: int = 0):
    """一份字幕 → 消息。走的是与源同一条路（`parse_subtitles` + `build_messages`）。"""
    return build_messages(
        parse_subtitles(_字幕(行)),
        namespace="test:切分",
        group_id=1,
        anchor_ts=锚点,
    )


def _全部正文(消息) -> str:
    return "".join(m.content for m in 消息)


# ── 夹具本身 ────────────────────────────────────────────────────────


def test_夹具解析出简介一条加字幕五段(干净的环境, monkeypatch) -> None:
    """一条视频 → 一条简介 + 五段讲话。

    ※ 五段是**切出来的**：整段字幕 463 秒，按停顿切成 5 段（第 4 段没有停顿，
    是被上限切开的）。一条字幕行一条消息的话会是 123 条 —— 那个数字本身
    就说明「一条字幕行没有信息量」这件事被处理了。
    """
    msgs = _读(夹具SRT, monkeypatch)
    assert len(msgs) == 6, [m.content[:12] for m in msgs]
    assert all(isinstance(m, PendingMessage) for m in msgs)
    assert {m.source for m in msgs} == {"video"}
    assert {m.group_id for m in msgs} == {video_source.DEFAULT_GROUP_ID}
    assert {m.sender for m in msgs} == {"赵雪依"}
    assert all(m.actor_account is None for m in msgs), "视频里没有账号可言，不许用 0 冒充"


def test_两份格式_srt_与_vtt_产出完全一样(干净的环境, monkeypatch) -> None:
    """※ 这是**同一个视频**的两种字幕格式，所以它们必须给出同一批消息。

    「同一个文件按两种格式解析出不同的条数」是一个无人察觉的可能 ——
    而它的后果是「换一份字幕就把整批条目重写一遍」。所以解析器只写一个
    （`_scan_cues`），SRT 与 VTT 的差别（时间戳分隔符、序号行、头块）
    全都在「含 `-->` 的块才是字幕」这一条判据之下。
    """
    a = _读(夹具SRT, monkeypatch)
    b = _读(夹具VTT, monkeypatch)
    assert [(m.msg_id, m.ts, m.sender, m.content) for m in a] == [
        (m.msg_id, m.ts, m.sender, m.content) for m in b
    ]


def test_每一条消息的_ts_是这段字幕在视频里的时间(干净的环境, monkeypatch) -> None:
    """※ 钉死的那条性质：`ts` 是**字幕在视频里的时间**，不是抓取时间。

    判据：日报按天分，而视频可能是上周录的。用抓取时间会让它落在**今天**
    —— 一份「上周三的讲座出现在今天」的日报，读者不会觉得哪里不对。

    ⇒ 断言 `ts` 落在 **2026-10-13 19:00** 那一段，而不是「现在」。
    """
    msgs = _读(夹具SRT, monkeypatch)
    # 简介那一条的 ts 就是视频的发布时刻；各段的 ts 是它加上段首偏移。
    assert min(m.ts for m in msgs) == 夹具起点
    assert max(m.ts for m in msgs) == 夹具起点 + 438, "最后一段从 438 秒处开始"

    现在 = int(time.time())
    assert all(abs(m.ts - 现在) > 86400 for m in msgs), "有一条落在了今天 —— 用了抓取时间"

    当天 = datetime.fromtimestamp(夹具起点 + 8 * 3600, tz=timezone.utc)
    assert (当天.year, 当天.month, 当天.day) == (2026, 10, 13)


def test_每条消息的边界都落在一句完整的话的末尾(干净的环境, monkeypatch) -> None:
    """※ 切分的核心性质：**边界在句末，不在时间线上**。

    切在时间线上的代价：条目必须能**逐字回连**到源文（`quote`），而切碎了
    回连就失败、整条被丢弃 —— 而丢弃是无声的。
    """
    for m in _读(夹具SRT, monkeypatch):
        if "\n" in m.content:
            continue  # 简介那一条是自洽短文本，不参与切分
        assert m.content.rstrip().endswith(("。", "！", "？")), m.content[-20:]


def test_一条字幕行不会被切成一条消息(干净的环境, monkeypatch) -> None:
    """※ 现象：真实字幕里大量出现「对」「然后」「这个」。

    它们单独成条的话，每一条都没有信息量 —— 而下游会拿它们去抽条目，
    抽不出来，还要占一份预算。
    """
    msgs = _读(夹具SRT, monkeypatch)
    字幕 = [m for m in msgs if "\n" not in m.content]
    assert min(len(m.content) for m in 字幕) > 20, [m.content for m in 字幕]


def test_简介是一条不切的完整消息(干净的环境, monkeypatch) -> None:
    """※ 简介与网页通知同类：自洽短文本，**不切**（与字幕必须切相反）。"""
    简介 = [m for m in _读(夹具SRT, monkeypatch) if "\n" in m.content]
    assert len(简介) == 1
    assert 简介[0].ts == 夹具起点
    assert "报名截止" in 简介[0].content


def test_切分不丢字(干净的环境, monkeypatch) -> None:
    """把各段正文拼起来，应当等于原文去掉换行。丢了字是最难发现的一种错。"""
    cues = parse_subtitles(夹具SRT.read_bytes())
    原文 = "".join(c.text for c in cues)
    产出 = "".join(
        m.content for m in _读(夹具SRT, monkeypatch) if "\n" not in m.content
    )
    assert 产出 == 原文


def test_夹具侧车的指纹与字幕对得上(干净的环境, monkeypatch) -> None:
    """※ 夹具过期怎么发现：素材改了但忘了重新生成侧车 —— 演示的数字就与实际
    不符，而那是评委会看到的差异。指纹把这件事变成一条会红的用例。
    """
    for 字幕 in (夹具SRT, 夹具VTT):
        侧车 = json.loads(
            Path(str(字幕) + video_source.META_SUFFIX).read_text(encoding="utf-8")
        )
        assert 侧车["sha256"] == hashlib.sha256(字幕.read_bytes()).hexdigest()
        assert 侧车["source_url"] == 夹具标识
        assert 侧车["started_at"] == "2026-10-13 19:00:00"


def test_夹具里的假域名是保留域(干净的环境) -> None:
    """※ 红线：样本自己编。`example.test` 是 RFC 2606 保留的，永远不会解析。"""
    侧车 = json.loads(
        Path(str(夹具SRT) + video_source.META_SUFFIX).read_text(encoding="utf-8")
    )
    assert "example.test" in 侧车["source_url"]
    assert "青梧" in 侧车["description"]


# ── 切分判据 ────────────────────────────────────────────────────────


def test_切点要同时满足停顿与句末与够长() -> None:
    """※ 这条用例是切分判据的**全部**：

      · c1 之后停 5 秒 —— 有停顿，但这一段才 25 秒（不够长）⇒ **不切**；
      · c2 是「丙。」—— 句末，但紧接着就有下一句 ⇒ **不切**；
      · c5 之后停 4 秒，且这一段已经 55 秒 ⇒ **切**，而且切在句末。

    只按停顿切会把「甲，乙，」切成一条（没有信息量）；只按句末切会把
    每一句都切成一条（太碎）；只按时间切会切在句子中间（回连失败）。
    """
    行 = [
        (0.0, 10.0, "甲甲甲甲甲，"),
        (15.0, 25.0, "乙乙乙乙乙，"),   # 前面停 5 秒 —— 不够长，不切
        (25.3, 35.0, "丙丙丙丙丙。"),   # 句末 —— 但下一句紧接着，不切
        (35.3, 45.0, "丁丁丁丁丁，"),
        (45.3, 55.0, "戊戊戊戊戊。"),   # 到这里 55 秒
        (59.0, 69.0, "己己己己己，"),   # 前面停 4 秒 ⇒ 在 c5 之后切
        (69.3, 79.0, "庚庚庚庚庚，"),
        (79.3, 89.0, "辛辛辛辛辛。"),
    ]
    msgs = _消息(行)
    assert [m.content for m in msgs] == [
        "甲甲甲甲甲，乙乙乙乙乙，丙丙丙丙丙。丁丁丁丁丁，戊戊戊戊戊。",
        "己己己己己，庚庚庚庚庚，辛辛辛辛辛。",
    ]
    assert [m.ts for m in msgs] == [0, 59], "段首偏移"


def test_没有停顿也没有标点时由上限兜底切开() -> None:
    """※ 自动生成的字幕常常既没有句末标点、也没有够长的停顿。

    那时只能靠上限。它**不是**为了「切得好看」，是因为端点会**静默截断**
    —— 超预算的输入回 200、`finish_reason=stop`、`usage` 只报截断后的数，
    客户端拿不到任何信号。所以预算必须在这里就保证。
    """
    行 = [(i * 3.3, i * 3.3 + 3.0, "甲乙丙丁戊己庚辛壬癸") for i in range(100)]
    msgs = _消息(行)
    assert len(msgs) >= 2, "一句都没切开 —— 上限没起作用"
    # 每一条都不能超过上限太多（至多多出一句 —— 上限是「到了就必须切」，
    # 不是「不许超过」：切在句末比切在预算线上更重要）。
    每行字数 = len("甲乙丙丁戊己庚辛壬癸")
    for m in msgs:
        assert len(m.content) <= (MAX_SEGMENT_SECONDS / 3.3 + 2) * 每行字数, len(m.content)
    assert _全部正文(msgs) == "".join(t for _, _, t in 行), "切分丢了字"


def test_上限是硬的下限是软的() -> None:
    """※ 下限只是**不切**的条件（攒不够就不切），不是「切出来凑够它」。

    所以一段可以短于下限 —— 视频结束了就收尾，不该为了凑够 45 秒去等下一句。
    """
    行 = [(0.0, 5.0, "甲甲甲甲甲。"), (5.3, 10.0, "乙乙乙乙乙。")]
    msgs = _消息(行)
    assert len(msgs) == 1
    assert msgs[0].ts == 0


def test_句末标点后面跟着引号也算说完() -> None:
    """`「就这样。」` 与 `就这样。` 一样，都是说完了。"""
    行 = [(0.0, 10.0, "「甲甲甲甲甲。」"), (10.3, 20.0, "乙乙乙乙乙。")]
    msgs = _消息(行)
    assert len(msgs) == 1


def test_分号不算句末() -> None:
    """※ `；` 分开的是同一句话里的两个分句 —— 在它上面切会切出半个句子。

    这条用例把它钉住：两段用分号连起来的文字，只要没有停顿与句号，
    就不该被切开。
    """
    行 = [(0.0, 10.0, "甲甲甲甲甲；"), (10.3, 20.0, "乙乙乙乙乙；")]
    assert len(_消息(行)) == 1


def test_停顿的门槛是可观察的常量() -> None:
    """※ 判据本身（「换话题时说话人会停」）是代理，`PAUSE_SECONDS` 是它的
    可观测量。调它不需要改结构 —— 所以它得是一个能被找到的常量。"""
    assert PAUSE_SECONDS > 0
    assert MIN_SEGMENT_SECONDS < MAX_SEGMENT_SECONDS


# ── 解字幕（纯函数） ────────────────────────────────────────────────


def test_srt_与_vtt_两种时间戳写法都认() -> None:
    srt = "1\n00:00:01,500 --> 00:00:03,000\n甲甲甲甲甲。\n"
    vtt = "WEBVTT\n\n00:00:01.500 --> 00:00:03.000\n甲甲甲甲甲。\n"
    a = parse_subtitles(srt.encode("utf-8"))
    b = parse_subtitles(vtt.encode("utf-8"))
    assert [(c.start, c.end, c.text) for c in a] == [(1.5, 3.0, "甲甲甲甲甲。")]
    assert [(c.start, c.end, c.text) for c in b] == [(1.5, 3.0, "甲甲甲甲甲。")]


def test_vtt_允许省略小时位() -> None:
    vtt = "WEBVTT\n\n01:02.500 --> 01:05.000\n甲甲甲甲甲。\n"
    cues = parse_subtitles(vtt.encode("utf-8"))
    assert cues[0].start == 62.5


def test_vtt_的头块被跳过() -> None:
    """`WEBVTT` / `NOTE` / `STYLE` 都不含 `-->`，所以一句话就分开了。"""
    vtt = (
        "WEBVTT\n\nNOTE 这是给翻译看的备注\n第二行\n\n"
        "STYLE\n::cue { color: red }\n\n"
        "00:00:01.000 --> 00:00:02.000\n甲甲甲甲甲。\n"
    )
    cues = parse_subtitles(vtt.encode("utf-8"))
    assert [c.text for c in cues] == ["甲甲甲甲甲。"]


def test_vtt_的说话人标记被当成说话人_srt_没有就置空() -> None:
    """※ `docs/数据源层.md` §3.1 问「说话人拿不到时你猜不猜？」—— **不猜**。

    VTT 的标准里**声明了**说话人（`<v 名字>`），所以用它不算猜；
    SRT 的标准里没有，所以 SRT 字幕的说话人一律取不到（置空）。
    """
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v 赵雪依>甲甲甲甲甲。\n"
    assert parse_subtitles(vtt.encode("utf-8"))[0].voice == "赵雪依"

    srt = "1\n00:00:01,000 --> 00:00:02,000\n甲甲甲甲甲。\n"
    assert parse_subtitles(srt.encode("utf-8"))[0].voice == ""


def test_一段里有两个说话人时不算单一说话人() -> None:
    """跨了两个人的发言，硬取一个会让「谁说的」变成错的。"""
    cues = parse_subtitles(
        (
            "WEBVTT\n\n"
                "00:00:01.000 --> 00:00:02.000\n<v 赵雪依>甲甲甲甲甲。\n\n"
                "00:00:02.300 --> 00:00:03.000\n<v 钱亦航>乙乙乙乙乙。\n"
        ).encode("utf-8")
    )
    msgs = build_messages(cues, namespace="n", group_id=1, anchor_ts=0, speaker="视频讲者")
    assert msgs[0].sender == "视频讲者"


def test_一条_cue_里的多行是排版换行() -> None:
    """字幕的换行是**显示**的切分，不是内容里的换行。"""
    srt = "1\n00:00:01,000 --> 00:00:02,000\n甲甲甲甲甲，\n乙乙乙乙乙。\n"
    cues = parse_subtitles(srt.encode("utf-8"))
    assert cues[0].text == "甲甲甲甲甲，乙乙乙乙乙。"


def test_中文之间不补空格_英文之间补() -> None:
    """※ 补错空格的后果：下游的**逐字回连**失败（条目必须能逐字在源文里找到）。

    中文的字幕换行落在词的中间（「今天我们要讲」+「的是动态规划」），
    拼回去时补一个空格就再也找不到原句了；英文里正好相反。
    """
    中文 = parse_subtitles(
        "1\n00:00:01,000 --> 00:00:02,000\n今天我们要讲\n的是动态规划。\n".encode("utf-8")
    )
    assert 中文[0].text == "今天我们要讲的是动态规划。"

    英文 = parse_subtitles(
        "1\n00:00:01,000 --> 00:00:02,000\nhello\nworld.\n".encode("utf-8")
    )
    assert 英文[0].text == "hello world."

    中英 = parse_subtitles(
        "1\n00:00:01,000 --> 00:00:02,000\n报名截止到 10 月 8 日。\n".encode("utf-8")
    )
    assert 中英[0].text == "报名截止到 10 月 8 日。"


def test_html_实体被还原() -> None:
    """`&amp;` 在 VTT 里是**文字**，不是排版。"""
    cues = parse_subtitles("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n甲 &amp; 乙。\n".encode())
    assert cues[0].text == "甲 & 乙。"


def test_只有标签没有文字的_cue_不算一条() -> None:
    """它是**排版**残留，不是一条没说的话 —— 跳过它不算静默丢消息。"""
    vtt = (
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:02.000\n<i></i>\n\n"
        "00:00:02.300 --> 00:00:03.000\n甲甲甲甲甲。\n"
    )
    assert [c.text for c in parse_subtitles(vtt.encode("utf-8"))] == ["甲甲甲甲甲。"]

    # 而**一条真 cue 都没有**时仍然要抛：一份没有文字的字幕是**素材坏了**，
    # 不是「这场讲座没内容」—— 那是 `_VideoHasNoSubtitles` 管的事。
    with pytest.raises(_SubtitleNotUnderstood):
        parse_subtitles(
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<i></i>\n".encode()
        )


def test_空字幕抛_而不是返回空() -> None:
    """判据 2 的前一半：**解析出 0 条 ⇒ 抛**。

    返回空的后果：下游会以为「这场讲座没内容」。
    """
    with pytest.raises(_SubtitleNotUnderstood):
        parse_subtitles(b"")
    with pytest.raises(_SubtitleNotUnderstood):
        parse_subtitles("WEBVTT\n\nNOTE 只有备注\n".encode("utf-8"))


def test_时间行认不出来时整份报错_而不是跳过那一条() -> None:
    """静默跳过一条字幕 = 用户以为视频里没说过那句话。"""
    with pytest.raises(_SubtitleNotUnderstood):
        parse_subtitles(
            "1\n00:00:01,000 --> 00:00:02,000\n甲甲甲甲甲。\n\n"
            "2\n00:00:03,000 --> 一会儿\n乙乙乙乙乙。\n".encode("utf-8")
        )


def test_按_utf8_解不开时抛_而不是换一个编码重试() -> None:
    """用错编码「成功」解出来的是一份乱码，而乱码会照常走完切分。"""
    with pytest.raises(_SubtitleNotUnderstood):
        parse_subtitles("1\n00:00:01,000 --> 00:00:02,000\n甲甲甲甲甲。\n".encode("gbk"))


def test_侧车可以声明编码() -> None:
    """声明与「程序猜一个」是两件事。"""
    raw = "1\n00:00:01,000 --> 00:00:02,000\n甲甲甲甲甲。\n".encode("gbk")
    assert parse_subtitles(raw, encoding="gbk")[0].text == "甲甲甲甲甲。"


# ── msg_id 的稳定性 ─────────────────────────────────────────────────


def test_前面多一条字幕不会让后面的段全部换_id() -> None:
    """※ 幂等键的性质：**不用段的序号**。

    序号取决于切分常量与前面的内容 —— 前面少一段，后面每一段的序号都
    往前挪一格，于是重跑摄取把它们**全部重写一遍**（`msg_id` 是幂等的
    键），日报里同一条出现两次，而没有任何东西会报错。用**偏移**则
    只有被挪动的边界附近会变。
    """
    行 = [
        (0.0, 10.0, "甲甲甲甲甲，"),
        (15.0, 25.0, "乙乙乙乙乙，"),
        (25.3, 35.0, "丙丙丙丙丙。"),
        (35.3, 45.0, "丁丁丁丁丁，"),
        (45.3, 55.0, "戊戊戊戊戊。"),
        (59.0, 69.0, "己己己己己，"),
        (69.3, 79.0, "庚庚庚庚庚，"),
        (79.3, 89.0, "辛辛辛辛辛。"),
    ]
    原 = _消息(行)
    后半 = _消息(行[5:])  # 只留后面那一段
    assert [m.ts for m in 原] == [0, 59]
    assert [m.ts for m in 后半] == [59]
    # 同一条消息在两份素材里的**序号**不同（1 与 0），但 id 相同 ——
    # 这正是「id 跟偏移走、不跟序号走」。
    assert 后半[0].msg_id == 原[1].msg_id
    # 而偏移变了 id 就得变（否则两条不同的东西会撞同一个幂等键）。
    平移 = _消息([(s + 100, e + 100, t) for s, e, t in 行])
    assert 平移[1].msg_id != 原[1].msg_id


def test_同一个视频换一种格式是同一批_msg_id(干净的环境, monkeypatch) -> None:
    a = _读(夹具SRT, monkeypatch)
    b = _读(夹具VTT, monkeypatch)
    assert [m.msg_id for m in a] == [m.msg_id for m in b]


def test_换一个视频就是另一批_msg_id(tmp_path, 干净的环境, monkeypatch) -> None:
    """`/lecture-a` 与 `/lecture-b` 是两条不同的视频 —— 只按内容算 id 会撞车。"""
    import shutil

    for 名字, 标识 in (("a.srt", "https://video.test/x/a"), ("b.srt", "https://video.test/x/b")):
        shutil.copy(夹具SRT, tmp_path / 名字)
        Path(str(tmp_path / 名字) + video_source.META_SUFFIX).write_text(
            json.dumps(
                {"source_url": 标识, "started_at": "2026-10-13 19:00:00"}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
    a = _读(tmp_path / "a.srt", monkeypatch)
    b = _读(tmp_path / "b.srt", monkeypatch)
    assert [m.msg_id for m in a] != [m.msg_id for m in b]
    assert [m.ts for m in a] == [m.ts for m in b]


def test_msg_id_落在_sqlite_能装的范围内(干净的环境, monkeypatch) -> None:
    """SQLite 的 INTEGER 是有符号 64 位。超了会在插入时报
    "Python int too large to convert to SQLite INTEGER"。"""
    for m in _读(夹具SRT, monkeypatch):
        assert 0 <= m.msg_id <= 2**63 - 1


# ── 三种失败必须能分辨 ──────────────────────────────────────────────


def test_读不到时抛_SourceUnavailable_不返回空(tmp_path, 干净的环境, monkeypatch) -> None:
    """断在**第一步**（取素材）。返回空的后果：下游以为「这场讲座没内容」。"""
    with pytest.raises(SourceUnavailable):
        _读(tmp_path / "没有这个文件.srt", monkeypatch)


def test_素材坏了时抛_SubtitleNotUnderstood(tmp_path, 干净的环境, monkeypatch) -> None:
    """断在**第三步**（字幕格式）。下一步是去改解析器。"""
    坏 = tmp_path / "坏.srt"
    坏.write_text("这不是一份字幕\n", encoding="utf-8")
    with pytest.raises(_SubtitleNotUnderstood):
        _读(坏, monkeypatch)


def test_页面里没有字幕也没有简介时抛_VideoHasNoSubtitles(
    干净的环境, monkeypatch
) -> None:
    """断在**第二步**（页面里没有字幕）。下一步是去查这个视频，不是改解析器。"""
    monkeypatch.setattr(
        video_source,
        "_http_get",
        _假的取字节(页面=_b站页(desc="", tracks=[]), 播放器=_播放器返回()),
    )
    monkeypatch.setenv(
        video_source.TARGET_ENV, "https://www.bilibili.com/video/BV1yechao0001"
    )
    with pytest.raises(_VideoHasNoSubtitles):
        video_source.build_video_source().read()


def test_三种失败的形状互不相同(干净的环境) -> None:
    """※ 判据：**下一步不同 ⇒ 异常类型不同**。

    形状相同会让运维拿着「这个视频没字幕」的解释去看一个其实已经坏了的
    解析器 —— 而那是「产出为空」这一类最不容易被注意到的失败。
    """
    三种 = {SourceUnavailable, _VideoHasNoSubtitles, _SubtitleNotUnderstood}
    assert len(三种) == 3
    assert all(issubclass(e, SourceError) for e in 三种)
    assert not issubclass(_VideoHasNoSubtitles, _SubtitleNotUnderstood)
    assert not issubclass(_SubtitleNotUnderstood, _VideoHasNoSubtitles)
    assert _SubtitleNotUnderstood.fatal and _VideoHasNoSubtitles.fatal


def test_报错文案里说清了下一步(干净的环境, monkeypatch) -> None:
    """※ 「失败要响」不只是抛出去 —— 文案要能让人知道**往哪儿走**。"""
    monkeypatch.setattr(
        video_source,
        "_http_get",
        _假的取字节(页面=_b站页(desc="", tracks=[]), 播放器=_播放器返回()),
    )
    monkeypatch.setenv(
        video_source.TARGET_ENV, "https://www.bilibili.com/video/BV1yechao0001"
    )
    with pytest.raises(_VideoHasNoSubtitles) as e:
        video_source.build_video_source().read()
    文案 = str(e.value)
    assert "字幕" in 文案
    assert "解析器" in 文案, "没说清「这不是格式问题」"


# ── 目标形状与配置 ──────────────────────────────────────────────────


def test_四种目标按自身形状区分(干净的环境) -> None:
    """※ 判据是**目标自身的形状**，不是让用户再选一次 —— 「再选一次」就有
    选错的可能，而选错的症状是「读的是别的东西，报告却一切正常」。"""
    def 判(raw):
        return video_source._classify(
            raw, where="测试", group_id=1, anchor_fallback=None
        ).kind

    assert 判("D:/某处/讲座.srt") == "subtitle"
    assert 判("D:/某处/讲座.vtt") == "subtitle"
    assert 判("https://www.bilibili.com/video/BV1yechao0001") == "bili_video"
    assert 判("https://space.bilibili.com/123456") == "bili_space"
    assert 判("https://www.bilibili.com/space/123456") == "bili_space"
    assert 判("https://某视频站.test/watch/1") == "page"
    assert 判("https://www.bilibili.com/bangumi/play/ep1") == "page"


def test_没配目标时抛_不静默兜底(干净的环境) -> None:
    """兜底到某个默认页面会让 `--source video` 在什么都没配的情况下
    「成功」读到一份别人的数据，而报告看起来一切正常。"""
    with pytest.raises(ConfigError):
        video_source.resolve_target()


def test_配置文件里的_target_也能用(干净的环境, monkeypatch) -> None:
    (干净的环境 / "sources.toml").write_text(
        '[video]\ntarget = "%s"\ngroup_id = 820000009\n' % 夹具SRT.as_posix(),
        encoding="utf-8",
    )
    src = video_source.build_video_source()
    assert src._target.kind == "subtitle"
    assert src._target.group_id == 820000009
    assert len(src.read()) == 6


def test_环境变量压过配置文件(干净的环境, monkeypatch) -> None:
    (干净的环境 / "sources.toml").write_text(
        '[video]\ntarget = "D:/不存在的.srt"\n', encoding="utf-8"
    )
    monkeypatch.setenv(video_source.TARGET_ENV, str(夹具SRT))
    assert len(video_source.build_video_source().read()) == 6


def test_不认的后缀不猜(干净的环境, monkeypatch) -> None:
    monkeypatch.setenv(video_source.TARGET_ENV, "D:/某处/讲座.txt")
    with pytest.raises(ConfigError):
        video_source.resolve_target()


def test_分组号能被覆盖且默认值看得见(干净的环境, monkeypatch) -> None:
    monkeypatch.setenv(video_source.TARGET_ENV, str(夹具SRT))
    assert {m.group_id for m in video_source.build_video_source().read()} == {
        video_source.DEFAULT_GROUP_ID
    }
    monkeypatch.setenv(video_source.GROUP_ID_ENV, "820000009")
    assert {m.group_id for m in video_source.build_video_source().read()} == {820000009}
    monkeypatch.setenv(video_source.GROUP_ID_ENV, "不是数字")
    with pytest.raises(ConfigError):
        video_source.resolve_target()


def test_配置目录的口径与组合根一致(tmp_path, monkeypatch) -> None:
    """※ `video_source` **不能** import `bootstrap`（会造出导入环），所以
    配置目录的默认值在 `_common.py` 里重新算了一次。代价是「默认值有两处」
    —— 这条判据把两边钉住。"""
    import yechao.bootstrap as bootstrap
    from yechao.bootstrap import Settings
    from yechao.infrastructure.sources import _common

    assert _common.DEFAULT_CONFIG_DIR == bootstrap.DEFAULT_CONFIG_DIR

    monkeypatch.setenv(_common.CONFIG_DIR_ENV, str(tmp_path / "改过的配置目录"))
    assert _common.config_dir() == Settings.from_env().config_dir

    monkeypatch.delenv(_common.CONFIG_DIR_ENV)
    assert _common.config_dir() == Settings.from_env().config_dir


# ── 时间锚点 ────────────────────────────────────────────────────────


def test_没有锚点时抛_不兜底成今天(tmp_path, 干净的环境, monkeypatch) -> None:
    """※ `docs/数据源层.md` §3.1：「拿不到的时候——兜底成 0？兜底成『今天』？」

    都不兜底。兜底出来的时刻会把每一条消息放进**错误的一天**，而且不报错
    —— 用户只会得到一份错的日报。
    """
    import shutil

    shutil.copy(夹具SRT, tmp_path / "无锚点.srt")
    monkeypatch.setenv(video_source.TARGET_ENV, str(tmp_path / "无锚点.srt"))
    with pytest.raises(ConfigError) as e:
        video_source.build_video_source().read()
    文案 = str(e.value)
    assert "started_at" in 文案 and video_source.STARTED_AT_ENV in 文案


def test_锚点可以从配置兜底(tmp_path, 干净的环境, monkeypatch) -> None:
    import shutil

    shutil.copy(夹具SRT, tmp_path / "无锚点.srt")
    monkeypatch.setenv(video_source.TARGET_ENV, str(tmp_path / "无锚点.srt"))
    monkeypatch.setenv(video_source.STARTED_AT_ENV, "2026-10-13 19:00:00")
    msgs = video_source.build_video_source().read()
    assert min(m.ts for m in msgs) == 夹具起点


def test_侧车自报的锚点压过人工配置(tmp_path, 干净的环境, monkeypatch) -> None:
    """※ 锚点是**这份素材**的属性，不是运行环境的属性 —— 允许人工覆盖会让
    同一份素材在不同机器上算出不同的 ts，而日报是按天分的。"""
    monkeypatch.setenv(video_source.TARGET_ENV, str(夹具SRT))
    monkeypatch.setenv(video_source.STARTED_AT_ENV, "2030-01-01 00:00:00")
    assert min(m.ts for m in video_source.build_video_source().read()) == 夹具起点


def test_墙上时间按_UTC_加_8_解释而不是机器时区(干净的环境) -> None:
    """※ 取机器时区的话，同一份夹具在评委的机器上会算出不同的秒数，
    而日报是**按天分**的 —— 差 8 小时足以让一条消息落到**前一天**。"""
    assert video_source._to_ts("2026-10-13 19:00:00") == 夹具起点
    # 带时区的 ISO 写法按它自己声明的时区算。
    assert video_source._to_ts("2026-10-13T19:00:00+08:00") == 夹具起点
    assert video_source._to_ts("2026-10-13T11:00:00Z") == 夹具起点
    # 整数就是 Unix 秒。
    assert video_source._to_ts(1791889200) == 1791889200
    with pytest.raises(ConfigError):
        video_source._to_ts("昨天下午")


# ── 时间窗 ──────────────────────────────────────────────────────────


def test_时间窗在解析之后过_窗口内为空不算格式错(干净的环境, monkeypatch) -> None:
    """※ 「解析出 0 条」与「这段时间里没有」是两件事：前者是格式问题
    （去改解析器），后者是数据问题（这段时间真的安静）。

    先过滤再判 0 会让「窗口挑错了」伪装成「站点改版了」。
    """
    assert _读(夹具SRT, monkeypatch, since=夹具起点 + 10**6, until=夹具起点 + 10**6 + 1) == []


def test_窗口是半开区间(干净的环境, monkeypatch) -> None:
    """※ 半开是刻意的：相邻两天用 `[d1, d2)` 与 `[d2, d3)` 拼起来不重不漏。
    闭区间会让 00:00:00 那一刻的消息同时落进两天里。"""
    全部 = _读(夹具SRT, monkeypatch)
    边界 = 夹具起点 + 80
    # 边界那一刻的消息（第二段，偏移正好 80）落在**后**半天，不在前半天。
    前半 = _读(夹具SRT, monkeypatch, until=边界)
    后半 = _读(夹具SRT, monkeypatch, since=边界)
    assert len(前半) == 2, "简介与第一段的 ts 都是起点，都在前半"
    assert len(前半) + len(后半) == len(全部), "两半拼起来不重不漏"
    assert all(m.ts < 边界 for m in 前半)
    assert all(m.ts >= 边界 for m in 后半)


# ── 端口形状与纯函数 ────────────────────────────────────────────────


def test_视频源满足端口形状(干净的环境, monkeypatch) -> None:
    monkeypatch.setenv(video_source.TARGET_ENV, str(夹具SRT))
    src = video_source.build_video_source()
    assert isinstance(src, ports.MessageSource)
    assert src.name == "video"


def test_工厂造出来的源名字是_video(干净的环境, monkeypatch) -> None:
    """※ 注册表那一行由组合处加（本次任务不许改 `sources/__init__.py`），
    所以这里不判「在不在注册表里」，只判工厂本身是接得上线的形状：
    无参可调用、造出来的东西有 `name`、能 `read`。"""
    monkeypatch.setenv(video_source.TARGET_ENV, str(夹具SRT))
    src = video_source.build_video_source()
    assert src.name == video_parse.NAME == "video"
    assert callable(video_source.build_video_source)
    assert src.read()


def test_video_不在默认源里_所以_demo_仍然离线() -> None:
    """※ 默认源要在**离线、无配置**的情况下可用（`demo` 那条路的地基），
    而视频源需要一个目标、还可能要走网络。"""
    from yechao.infrastructure.sources import DEFAULT_SOURCES

    assert "video" not in DEFAULT_SOURCES
    assert DEFAULT_SOURCES == ("synth",)


def test_解析模块不碰_IO() -> None:
    """※ 「抓取与解析分开」这条要求，机械可检查的那一半在这里。

    解析模块一旦 import 了 `urllib.request` / `pathlib` / `os`，它就不再是
    纯函数了 —— 而「同样的字节永远给同样的结果」正是它值得被离线测试的理由。

    ※ 与 `test_web_source.py::test_解析模块不碰_IO` 有一处**不同**：那一条
    禁的是顶层包名，所以连 `urllib.parse` 也一起禁了。这里按**点分模块名**
    判 —— `urllib.parse` 是纯字符串处理（拼相对地址要它），禁掉它只会逼着人
    手写一个更差的 URL 拼接。禁的是**发请求的那几个**。
    """
    不许 = {"socket", "http", "os", "pathlib", "io", "time", "tempfile", "shutil", "gzip"}
    不许的_urllib = {"urllib.request", "urllib.error", "urllib.response"}

    for 模块 in (video_parse, video_page, bili_parse):
        树 = ast.parse(Path(模块.__file__).read_text(encoding="utf-8"))
        导入: set[str] = set()
        for node in ast.walk(树):
            if isinstance(node, ast.Import):
                导入.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                导入.add(node.module)
        # ※ 空转自检：扫到空集合的断言恒为真 —— 它在报告里是绿的，
        # 却没有守住任何东西。三个模块加起来至少有十几个 import。
        assert len(导入) >= 5, f"{模块.__name__} 只扫到 {len(导入)} 个 import —— 判据等于没在守"
        碰了 = {m for m in 导入 if m.split(".")[0] in 不许 or m in 不许的_urllib}
        assert not 碰了, f"{模块.__name__} 碰了 I/O：{sorted(碰了)}"


def test_取字节走的是_web_fetch_那一处实现(monkeypatch) -> None:
    """※ 不许有两份「怎么取一页」。

    改了 `fetch_bytes`（比如它那次「按 `Content-Encoding` 解压」的修复）
    就该同时改到本模块 —— 两份实现漂移的症状是「有的站点能读、有的读出来
    是二进制」，而那正是它刚被修好的那个 bug。

    ※ 这里**不**断言 `_assert_robots_allows is …`：robots 闸门现在由
    `fetch_bytes` 自己调用，本模块不再碰它 —— 断言一个不再存在的耦合
    只会把实现细节钉死。
    """
    见到: dict[str, object] = {}

    def 假fetch(url, *, opener=None, timeout=None):  # type: ignore[no-untyped-def]
        见到["url"] = url
        见到["opener"] = opener
        return b"ok"

    monkeypatch.setattr(web_fetch, "fetch_bytes", 假fetch)
    assert video_source._http_get("https://x.test/a", cookie="k=v") == b"ok"
    assert 见到["url"] == "https://x.test/a"
    assert callable(见到["opener"]), "cookie 与越权都要从 opener 注进去"


# ── 取字节：只有一处实现，越权是显式的 ──────────────────────────────


class _假响应:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.headers: dict[str, str] = {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_假响应":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _假的网(表: dict[str, bytes]):
    """一个假的「真实 opener」。表里没有的 URL ⇒ 断言失败（**不许真发请求**）。"""
    见到的: list[str] = []

    def base(request, timeout=None):  # type: ignore[no-untyped-def]
        url = request.full_url if hasattr(request, "full_url") else str(request)
        见到的.append(url)
        assert url in 表, "没预料到的请求：" + url
        return _假响应(表[url])

    base.见到的 = 见到的  # type: ignore[attr-defined]
    return base


def _一个站点(*, 禁止: bool = True, 正文: bytes = b"<html>ok</html>") -> dict[str, bytes]:
    robots = b"User-agent: *\nDisallow: /\n" if 禁止 else b"User-agent: *"
    return {"https://x.test/robots.txt": robots, "https://x.test/a": 正文}


def test_默认完全遵守_robots(干净的环境, monkeypatch) -> None:
    """※ 默认空 = **完全遵守**。默认往安全那一侧倒。"""
    monkeypatch.setattr(video_source, "_base_opener", lambda: _假的网(_一个站点()))
    with pytest.raises(SourceUnavailable):
        video_source._http_get("https://x.test/a")
    assert not (干净的环境 / video_source.ROBOTS_LOG).exists(), "没越权就不该有记录"


def test_点名之后那个域名才越过(干净的环境, monkeypatch) -> None:
    """※ 开关是**按域名**的，不是全局的。"""
    monkeypatch.setenv(video_source.ROBOTS_OPT_IN_ENV, "x.test")
    monkeypatch.setattr(video_source, "_base_opener", lambda: _假的网(_一个站点()))
    assert video_source._http_get("https://x.test/a") == b"<html>ok</html>"


def test_点名之外的域名仍然守_robots(干净的环境, monkeypatch) -> None:
    """※ 点名一个域名**不该顺手放开别的**。"""
    monkeypatch.setenv(video_source.ROBOTS_OPT_IN_ENV, "另一个.test")
    monkeypatch.setattr(video_source, "_base_opener", lambda: _假的网(_一个站点()))
    with pytest.raises(SourceUnavailable):
        video_source._http_get("https://x.test/a")
    assert not (干净的环境 / video_source.ROBOTS_LOG).exists()


def test_越权会留一行可复查的记录(干净的环境, monkeypatch) -> None:
    """※ 「可复查」是这条功能的全部意义。

    一个只活在内存里的布尔值事后谁也查不到 —— 那它就只是一句无法证伪的
    承诺。所以每一次越权都要落一行盘，写清**域名、依据、URL、时刻**。
    """
    monkeypatch.setenv(video_source.ROBOTS_OPT_IN_ENV, "x.test")
    monkeypatch.setattr(video_source, "_base_opener", lambda: _假的网(_一个站点()))
    video_source._http_get("https://x.test/a")
    记录 = (干净的环境 / video_source.ROBOTS_LOG).read_text(encoding="utf-8")
    assert 记录.count(chr(10)) == 1, "一次越权一行"
    assert "robots-opt-in" in 记录
    assert "domain=x.test" in 记录
    assert video_source.ROBOTS_OPT_IN_ENV in 记录, "记录里要写清**依据**"
    assert "https://x.test/a" in 记录
    assert "+08:00" in 记录, "时刻用全项目那一处口径，不跟机器时区跑"


def test_记录写不下去时就不越权(干净的环境, monkeypatch) -> None:
    """※ 越权与记录是**同一次动作** —— 记不下来就不越权。

    与 `audit_log.py` 那句「记录必须由脱敏那一刻产出，检查与脱敏是同一次
    动作」同一条判据：开一张没有存根的条子，等于没有记录。
    """
    文件 = 干净的环境.parent / "其实是个文件"
    文件.write_text("x", encoding="utf-8")
    monkeypatch.setenv("YECHAO_CONFIG_DIR", str(文件 / "config"))
    monkeypatch.setenv(video_source.ROBOTS_OPT_IN_ENV, "x.test")
    monkeypatch.setattr(video_source, "_base_opener", lambda: _假的网(_一个站点()))
    with pytest.raises(ConfigError) as e:
        video_source._http_get("https://x.test/a")
    assert "记不下来" in str(e.value)


def test_点名一个域名覆盖它的子域(干净的环境, monkeypatch) -> None:
    """※ 判据：`host == 条目` 或它是条目的子域。

    一个站点把内容放在多个子域上是常态（B 站的内容在 `www`，接口在 `api`）。
    要求用户把每个子域都列出来，只会逼他去写一个更宽的东西。
    """
    monkeypatch.setenv(video_source.ROBOTS_OPT_IN_ENV, "bilibili.com")
    assert video_source._opted_in_entry("api.bilibili.com") == "bilibili.com"
    assert video_source._opted_in_entry("bilibili.com") == "bilibili.com"
    # 别被「长得像」的域名骗了。
    assert video_source._opted_in_entry("evil-bilibili.com") is None
    assert video_source._opted_in_entry("www.bilibili.com.evil.test") is None


def test_开关也能写在配置文件里_环境变量压过它(干净的环境, monkeypatch) -> None:
    (干净的环境 / "sources.toml").write_text(
        '[video]\nrobots_opt_in = "a.test, B.test"\n',
        encoding="utf-8",
    )
    assert video_source._robots_opt_in() == ("a.test", "b.test"), "大小写归一、空格剥掉"
    monkeypatch.setenv(video_source.ROBOTS_OPT_IN_ENV, "c.test")
    assert video_source._robots_opt_in() == ("c.test",)


def test_cookie_会被带进请求(干净的环境, monkeypatch) -> None:
    """※ B 站的接口要 cookie，而注入点与越权是**同一个**地方（opener）。"""
    见到: list[object] = []

    def base(request, timeout=None):  # type: ignore[no-untyped-def]
        见到.append(request)
        return _假响应(b"<html>ok</html>")

    monkeypatch.setattr(video_source, "_base_opener", lambda: base)
    op = video_source._opener_for("https://x.test/a", "SESSDATA=abc")
    with op(urllib.request.Request("https://x.test/a")):
        pass
    assert 见到 and 见到[0].get_header("Cookie") == "SESSDATA=abc"


def test_抓取失败时抛_SourceUnavailable(干净的环境, monkeypatch) -> None:
    """※ `404` 的 robots.txt 是「没有限制」（RFC 9309），不是「不许抓」；
    而页面本身的 5xx 是 `SourceUnavailable`。两者由 `fetch_bytes` 分。
    """

    def base(request, timeout=None):  # type: ignore[no-untyped-def]
        url = request.full_url
        if url.endswith("/robots.txt"):
            raise urllib.error.HTTPError(url, 404, "no robots", {}, None)
        raise urllib.error.HTTPError(url, 500, "boom", {}, None)

    monkeypatch.setattr(video_source, "_base_opener", lambda: base)
    with pytest.raises(SourceUnavailable):
        video_source._http_get("https://x.test/a")


# ── 通用视频页 ──────────────────────────────────────────────────────


def _通用页(**kw) -> str:
    ld = {
        "@type": "VideoObject",
        "name": kw.get("title", "青梧摄影社招新宣讲"),
        "description": kw.get("desc", "十月十三日晚上七点，学生活动中心。"),
        "uploadDate": kw.get("date", "2026-10-13T19:00:00+08:00"),
        "author": {"@type": "Person", "name": "赵雪依"},
    }
    if kw.get("subtitle"):
        ld["caption"] = kw["subtitle"]
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<script type="application/ld+json">%s</script>'
        "</head><body><h1>甲甲甲甲甲</h1></body></html>"
        % json.dumps(ld, ensure_ascii=False)
    )


def test_通用页从结构化数据里取元信息() -> None:
    page = parse_video_page(_通用页(), base_url="https://某视频站.test/watch/1")
    assert page.title == "青梧摄影社招新宣讲"
    assert page.speaker == "赵雪依"
    assert page.published_at == "2026-10-13T19:00:00+08:00"
    assert video_source._to_ts(page.published_at) == 夹具起点


def test_通用页的相对字幕地址被补成绝对地址() -> None:
    """※ 不补的话，抓取那一步会说「URL 不合法」，而真相是页面里写的是相对地址。"""
    页 = (
        '<html><head><title>甲</title></head><body>'
        '<video><track kind="subtitles" src="/subs/a.vtt"></video></body></html>'
    )
    page = parse_video_page(页, base_url="https://某视频站.test/watch/1")
    assert page.subtitle_url == "https://某视频站.test/subs/a.vtt"


def test_通用页一个字段都认不出来时抛() -> None:
    """下一步是去改解析器，不是去查这个视频。"""
    with pytest.raises(_VideoPageNotUnderstood):
        parse_video_page("<html><body><div>什么都没有</div></body></html>", base_url="https://x.test/")


# ── B 站：页面与首屏数据 ────────────────────────────────────────────


def _b站页(**kw) -> bytes:
    video = {
        "bvid": kw.get("bvid", "BV1yechao0001"),
        "aid": kw.get("aid", 111),
        "cid": kw.get("cid", 222),
        "title": kw.get("title", "青梧摄影社招新宣讲"),
        "desc": kw.get("desc", "十月十三日晚上七点，学生活动中心三零二。"),
        "pubdate": kw.get("pubdate", 1791889200),
        "owner": {"name": kw.get("owner", "青梧摄影社")},
        "subtitle": {"list": kw.get("tracks", [])},
    }
    state = json.dumps({"videoData": video}, ensure_ascii=False).replace("null", "undefined")
    return (
        "<html><head><title>甲甲甲甲甲</title></head><body><script>"
        "window.__INITIAL_STATE__=%s;(function(){var s;})();"
        "</script></body></html>" % state
    ).encode("utf-8")


def test_首屏数据解析出标题简介发布时间与_up_主() -> None:
    state = bili_parse.parse_initial_state(_b站页().decode("utf-8"))
    video = bili_parse.video_from_state(state)
    assert video.bvid == "BV1yechao0001"
    assert video.title == "青梧摄影社招新宣讲"
    assert video.pubdate == 1791889200, "pubdate 本身就是 Unix 秒，不用换算"
    assert video.speaker == "青梧摄影社"
    assert (video.aid, video.cid) == (111, 222)


def test_首屏数据取不到时抛_而不是返回空字典() -> None:
    """※ 取不到**不返回 `{}`**：B 站对疑似机器人的请求会返回一个 200 的
    **精简页**（没有首屏数据）。返回 `{}` 会让它一路走到「这个视频没标题」，
    而真相是「我们没被当成浏览器」—— 两者的下一步完全不同。"""
    with pytest.raises(bili_parse._BiliNotUnderstood):
        bili_parse.parse_initial_state("<html><body>什么都没有</body></html>")


def test_协议相对的字幕地址被补成_https() -> None:
    """接口给的是 `//aisubtitle.hdslb.com/…`。不补的话抓取那一步会说
    「URL 不合法」，而它其实是接口给的形状。"""
    state = bili_parse.parse_initial_state(
        _b站页(
            tracks=[{"lan": "zh-CN", "lan_doc": "中文（中国）", "subtitle_url": "//a.test/x.json"}]
        ).decode("utf-8")
    )
    track = bili_parse.video_from_state(state).best_track()
    assert track is not None and track.url == "https://a.test/x.json"


def test_挑字幕轨时中文优先() -> None:
    state = bili_parse.parse_initial_state(
        _b站页(
            tracks=[
                {"lan": "en-US", "lan_doc": "English", "subtitle_url": "https://a.test/en.json"},
                {"lan": "ai-zh", "lan_doc": "中文（自动生成）", "subtitle_url": "https://a.test/ai.json"},
                {"lan": "zh-CN", "lan_doc": "中文（中国）", "subtitle_url": "https://a.test/zh.json"},
            ]
        ).decode("utf-8")
    )
    assert bili_parse.video_from_state(state).best_track().url == "https://a.test/zh.json"


def test_接口报错时不静默返回空轨() -> None:
    """※ 这一条是「不要静默返回空」的落点：接口报错（-352 风控 / -403 签名）
    与「这个视频没有字幕」必须分开 —— 前者返回空数组的话，用户会以为
    视频没配字幕。"""
    with pytest.raises(bili_parse._BiliNotUnderstood) as e:
        bili_parse.tracks_from_player({"code": -352, "message": "风控校验失败"})
    assert "-352" in str(e.value)


def test_字幕_json_解析出字幕行() -> None:
    """※ **未在网络环境验证**：没有 cookie 时实测拿不到带字幕的视频，
    所以这个形状是照公开字段名写的，夹具自编。它证明不了线上接口是对的。"""
    raw = json.dumps(
        {
            "body": [
                {"from": 0.0, "to": 3.2, "location": 2, "content": "嗯，大家好。"},
                {"from": 3.5, "to": 6.0, "location": 2, "content": "  "},
                {"from": 6.3, "to": 9.0, "location": 2, "content": "我是赵雪依。"},
            ]
        },
        ensure_ascii=False,
    ).encode("utf-8")
    cues = bili_parse.cues_from_subtitle_json(raw)
    assert [(c.start, c.end, c.text) for c in cues] == [
        (0.0, 3.2, "嗯，大家好。"),
        (6.3, 9.0, "我是赵雪依。"),
    ]


def test_字幕轨存在但_body_是空的时候抛() -> None:
    """这不是「这个视频没字幕」：轨已经拿到了。"""
    with pytest.raises(bili_parse._BiliNotUnderstood):
        bili_parse.cues_from_subtitle_json(b'{"body": []}')


# ── B 站：WBI 签名与投稿列表 ────────────────────────────────────────


def test_wbi_签名的机械性质() -> None:
    """※ 端到端**没有跑通**（没有 cookie，实测 -352）。这里只钉机械性质：
    值里的 `!'()*` 被滤掉、参数按 key 排序、`w_rid` 是 `md5(查询串 + mixin)`。
    """
    mixin = bili_parse.mixin_key("a" * 32, "b" * 32)
    assert len(mixin) == 32
    assert set(mixin) <= {"a", "b"}
    assert mixin != "a" * 32

    签过 = bili_parse.signed_params(
        {"mid": "1", "ps": 10, "order": "pubdate", "keyword": "a!b(c)"},
        mixin=mixin,
        wts=1700000000,
    )
    assert 签过["wts"] == "1700000000"
    assert "!" not in 签过["keyword"] and "(" not in 签过["keyword"]
    assert len(签过["w_rid"]) == 32
    参与签名的 = list(签过)[:-1]
    assert 参与签名的 == sorted(参与签名的), "参数要按 key 排序"
    assert list(签过)[-1] == "w_rid", "w_rid 最后才算得出来，排在最后"


def test_投稿列表解析成一批投稿() -> None:
    payload = {
        "code": 0,
        "data": {
            "list": {
                "vlist": [
                    {
                        "bvid": "BV1yechao0002",
                        "title": "青梧摄影社外拍记录",
                        "description": "上周三的外拍。",
                        "created": 1791975600,
                        "author": "青梧摄影社",
                    }
                ]
            }
        },
    }
    subs = bili_parse.submissions_from_space(payload)
    assert [(s.bvid, s.pubdate, s.speaker) for s in subs] == [
        ("BV1yechao0002", 1791975600, "青梧摄影社")
    ]


def test_投稿列表接口报错时不返回空() -> None:
    with pytest.raises(bili_parse._BiliNotUnderstood) as e:
        bili_parse.submissions_from_space({"code": -352, "message": "风控校验失败"})
    assert "cookie" in str(e.value).lower()


def test_投稿列表真的为空时返回空列表() -> None:
    """※ 这与「接口没成」是两件事：前者是**这个账号最近没发**，后者是
    **这一次请求没成**。两者的下一步不同，所以形状必须不同。"""
    assert bili_parse.submissions_from_space({"code": 0, "data": {"list": {"vlist": []}}}) == []


def test_UP_主地址取不出_uid_时抛() -> None:
    with pytest.raises(ConfigError):
        video_source._space_uid("https://space.bilibili.com/")


# ── B 站：把页面那一路整个走一遍（网络注入假的） ────────────────────


def _假的取字节(
    *,
    页面: bytes | None = None,
    播放器: str | None = None,
    字幕: str | None = None,
    字幕地址: str | None = None,
):
    """按 URL 分派的假 `_http_get`。**一个真实请求都不发。**

    ※ 注入点选在 `_http_get` 而不是 `_opener`：这一层测的是「拿到这些字节
    之后怎么处理」，而 `_opener` 那一层（解 gzip、robots）有自己的用例。
    """
    默认页 = 页面 if 页面 is not None else _b站页()

    def 取(url, *, cookie=None, timeout=None):  # type: ignore[no-untyped-def]
        if url == video_source.BILI_FINGER:
            return json.dumps({"code": 0, "data": {"b_3": "x" * 46}}).encode("utf-8")
        if url.startswith(video_source.BILI_PLAYER):
            assert 播放器 is not None, "没预料到的请求：" + url
            return 播放器.encode("utf-8")
        if 字幕地址 is not None and url == 字幕地址:
            assert 字幕 is not None
            return 字幕.encode("utf-8")
        if url.startswith("https://www.bilibili.com/video/"):
            return 默认页
        raise AssertionError("没预料到的请求：" + url)

    return 取


def _播放器返回(*轨: dict[str, str]) -> str:
    return json.dumps({"code": 0, "data": {"subtitle": {"subtitles": list(轨)}}})


def test_B站单视频那一路_从页面一路读到字幕(干净的环境, monkeypatch) -> None:
    """※ 网络部分注入了假的 `_http_get` —— 测的是「拿到这些字节之后怎么处理」。

    **它证明不了线上接口的形状是对的**：没有 cookie 时实测拿不到带字幕的视频
    （自动生成的字幕要登录态）。真抓取只能手动跑。

    ※ 这条用例还钉住一件事：**字幕在接口里，不在页面里**。页面那份
    `videoData.subtitle.list` 是空的（实测真实视频页就是空的），字幕轨是
    从 `x/player/wbi/v2` 拿的。
    """
    页面地址 = "https://www.bilibili.com/video/BV1yechao0001"
    字幕地址 = "https://a.test/sub.json"
    字幕 = json.dumps(
        {
            "body": [
                {"from": 0.0, "to": 3.0, "content": "嗯，大家好。"},
                {"from": 3.3, "to": 6.0, "content": "我是赵雪依。"},
            ]
        },
        ensure_ascii=False,
    )
    monkeypatch.setattr(
        video_source,
        "_http_get",
        _假的取字节(
            页面=_b站页(tracks=[]),
            播放器=_播放器返回(
                {"lan": "zh-CN", "lan_doc": "中文（中国）", "subtitle_url": 字幕地址}
            ),
            字幕=字幕,
            字幕地址=字幕地址,
        ),
    )
    monkeypatch.setenv(video_source.TARGET_ENV, 页面地址)
    msgs = video_source.build_video_source().read()

    assert [m.content for m in msgs] == [
        "十月十三日晚上七点，学生活动中心三零二。",  # 简介：自洽短文本，不切
        "嗯，大家好。我是赵雪依。",                    # 字幕：切成段
    ]
    assert all(m.sender == "青梧摄影社" for m in msgs)
    assert [m.ts for m in msgs] == [1791889200, 1791889200]
    assert msgs[0].msg_id != msgs[1].msg_id, "简介与第一段不许撞 id"


def test_B站页面没有字幕时用简介(干净的环境, monkeypatch) -> None:
    """※ 「页面里没有字幕」**不是**异常 —— 简介也是一份文本。

    它只有在**两者都没有**的时候才变成 `_VideoHasNoSubtitles`。
    """
    monkeypatch.setattr(
        video_source,
        "_http_get",
        _假的取字节(播放器=_播放器返回()),
    )
    monkeypatch.setenv(
        video_source.TARGET_ENV, "https://www.bilibili.com/video/BV1yechao0001"
    )
    msgs = video_source.build_video_source().read()
    assert [m.content for m in msgs] == ["十月十三日晚上七点，学生活动中心三零二。"]


def test_B站接口报错时不退回首屏数据(干净的环境, monkeypatch) -> None:
    """※ 退回去会把「这一次请求没成」伪装成「这个视频没有字幕」。"""
    monkeypatch.setattr(
        video_source,
        "_http_get",
        _假的取字节(播放器=json.dumps({"code": -352, "message": "风控校验失败"})),
    )
    monkeypatch.setenv(
        video_source.TARGET_ENV, "https://www.bilibili.com/video/BV1yechao0001"
    )
    with pytest.raises(bili_parse._BiliNotUnderstood):
        video_source.build_video_source().read()

# ── 被 robots 挡住时：抛，而不是假装「没有」 ─────────────────────────


def test_字幕接口被挡住时抛_而不是假装没有字幕(干净的环境, monkeypatch) -> None:
    """※ 这是本文件里最要紧的一条。

    实测（2026-10-20）：`api.bilibili.com/robots.txt` 是 `Disallow: /`，
    而 B 站的字幕**不在页面里**、在接口里。于是「页面抓到了」不等于
    「字幕拿到了」。

    这时**不能**退回「那就只读简介吧」—— 用户会以为这个视频没配字幕，
    而真相是我们没被允许去问。两者的下一步完全不同。
    """
    页面地址 = "https://www.bilibili.com/video/BV1yechao0001"

    def 取(url, *, cookie=None, timeout=None):  # type: ignore[no-untyped-def]
        if url == video_source.BILI_FINGER:
            return json.dumps({"code": 0, "data": {"b_3": "x"}}).encode("utf-8")
        if url.startswith("https://www.bilibili.com/video/"):
            return _b站页().decode("utf-8").encode("utf-8")
        raise SourceUnavailable("robots.txt 不允许抓取 " + url)

    monkeypatch.setattr(video_source, "_http_get", 取)
    monkeypatch.setenv(video_source.TARGET_ENV, 页面地址)
    with pytest.raises(SourceUnavailable) as e:
        video_source.build_video_source().read()

    文案 = str(e.value)
    assert "robots" in 文案
    assert "字幕" in 文案
    assert "本地字幕文件" in 文案, "没说清出路"
    assert "无从知道" in 文案, "把「没拿到」说成了「没有」"


def test_投稿列表接口被挡住时抛_而不是返回空列表(干净的环境, monkeypatch) -> None:
    """返回空列表会被读成「这个 UP 主没投稿」—— 两者必须分得开。"""

    def 取(url, *, cookie=None, timeout=None):  # type: ignore[no-untyped-def]
        raise SourceUnavailable("robots.txt 不允许抓取 " + url)

    monkeypatch.setattr(video_source, "_http_get", 取)
    monkeypatch.setenv(video_source.TARGET_ENV, "https://space.bilibili.com/123456")
    with pytest.raises(SourceUnavailable):
        video_source.build_video_source().read()
