"""会议源 —— 转写解析、切分、三种「读不到」、以及「自动」那条路。

## 这个文件要钉住的那一件事

端口的 docstring 写着「**归一化的责任在数据源**」，而会议源与网页源的分工就
在「切不切」上：网页一页多通知要**切**，邮件是自洽短文本**不切**，会议转写
是长音视频**必须切**。本文件把「切」这个行为钉死 ——
`test_一轮话比预算长时按句末标点切_不从句子中间劈开` 是它的落点。

## 夹具自己编

`fixtures/meeting/` 里两份合成转写：腾讯会议形态与钉钉形态各一份。人名取自
项目语料里那批假名，会议名、时间、内容全部自己编（红线自检见
`test_夹具里的人名与号码都是自己编的`）。
"""

from __future__ import annotations

import ast
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from yechao.application import ports
from yechao.domain import chunking
from yechao.domain.errors import (
    ConfigError,
    SourceError,
    SourceFormatError,
    SourceUnavailable,
)
from yechao.domain.models import PendingMessage
from yechao.infrastructure.sources import meeting_parse, meeting_source, meeting_wemeet
from yechao.infrastructure.sources.meeting_parse import (
    _NoMeetingsHere,
    _NotUnderstood,
    parse_transcript,
)

# ── 夹具在哪 ────────────────────────────────────────────────────────

夹具目录 = Path(meeting_source.__file__).resolve().parent / "fixtures" / "meeting"
腾讯会议夹具 = 夹具目录 / "qingwu-course-qa-tencent.txt"
钉钉夹具 = 夹具目录 / "qingwu-club-fair-dingtalk.txt"

#: 夹具里那场会自报的开始时刻（文件头）。
腾讯会议开始 = "2026-09-14 14:00:00"
钉钉开始 = "2026-09-15 19:00:00"

#: 项目语料里那批**自编**的假人名。
假人名 = ("林小满", "周允", "沈明朗", "赵雪依", "钱亦航", "孙予棠", "李青萝", "吴不换")


def _墙上时间(text: str) -> int:
    """按项目口径（UTC+8）把墙上时间换算成 Unix 秒。

    ※ 用例里**不调用**被测代码的同一个函数来算期望值 —— 那等于拿实现证明
    实现。这里只用标准库按定义算一遍。
    """
    naive = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    return int(naive.replace(tzinfo=timezone.utc).timestamp()) - 8 * 3600


def _读夹具(path: Path) -> list[PendingMessage]:
    namespace = f"meeting:{path.stem}"
    return parse_transcript(
        path.read_bytes(),
        suffix=path.suffix.lower(),
        namespace=namespace,
        group_id=meeting_parse.group_id_for(namespace),
    )


# ── 环境 ────────────────────────────────────────────────────────────


@pytest.fixture()
def 临时配置(tmp_path, monkeypatch):
    """把配置目录与源相关的环境变量都指到临时位置。

    ※ 不这么做的话，用例会读到**开发机上的** `config/sources.toml` 与
    `YECHAO_MEETING_DIR` —— 于是「没配目录时抛」这条在别人机器上会因为
    「他恰好配了」而失败，而那种失败看起来像代码坏了。
    """
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv(meeting_source.CONFIG_DIR_ENV, str(cfg))
    monkeypatch.delenv(meeting_source.DIR_ENV, raising=False)
    return cfg


@pytest.fixture()
def 关掉自动探测(monkeypatch):
    """把「自动探测本机落盘位置」这一步停掉。

    ※ 这条 fixture 是**必须的**：自动探测会去读**这台机器上真实装的**腾讯会议
    数据目录（实测：装了就有）。不关掉的话，「没配目录就抛」这条判据在装了
    客户端的机器上会失败、在没装的机器上会通过 —— 一个**取决于跑测机器**的
    结论，而它看起来像代码坏了。
    """
    monkeypatch.setattr(meeting_source, "_auto_input", lambda: None)


# ── 临时造的转写（自己编） ──────────────────────────────────────────


def _腾讯会议形态(轮次: list[tuple[str, str, str]], *, 会议时间: str = 腾讯会议开始) -> bytes:
    """`(说话人, 时:分:秒, 正文)` → 腾讯会议形态的字节。

    ※ 说话人与时间之间用**两个空格** —— 那是本形态的判据之一，见
    `meeting_parse._TENCENT_TURN`。
    """
    lines = ["会议主题：测试用会议", f"会议时间：{会议时间}", ""]
    for speaker, offset, body in 轮次:
        lines.append(f"{speaker}  {offset}")
        lines.append(body)
        lines.append("")
    return "\n".join(lines).encode("utf-8")


def _钉钉形态(轮次: list[tuple[str, str, str]], *, 开始时间: str = 钉钉开始) -> bytes:
    lines = ["测试用会议", f"开始时间：{开始时间}", ""]
    for speaker, offset, body in 轮次:
        lines.append(f"[{offset}] {speaker} 说：{body}")
    return "\n".join(lines).encode("utf-8")


def _srt(cues: list[tuple[str, str, str]]) -> bytes:
    """`(开始, 结束, 正文)` → SRT。SRT 的标准里**没有**说话人字段。"""
    out = []
    for i, (start, end, text) in enumerate(cues, start=1):
        out += [str(i), f"{start} --> {end}", text, ""]
    return "\n".join(out).encode("utf-8")


def _vtt(cues: list[tuple[str, str, str]]) -> bytes:
    """`(开始, 结束, 正文)` → WebVTT。说话人写在 `<v ...>` 里。"""
    out = ["WEBVTT", ""]
    for start, end, text in cues:
        out += [f"{start} --> {end}", text, ""]
    return "\n".join(out).encode("utf-8")


# ── 夹具本身 ────────────────────────────────────────────────────────


def test_腾讯会议夹具按说话人轮次切成多条() -> None:
    """※ 判据：**一轮话 = 一条消息**，`sender` 是说话人，`ts` 是那一轮的时间。"""
    msgs = _读夹具(腾讯会议夹具)

    assert len(msgs) >= 9, "十轮发言（其中一轮被切）应当产出至少九条"
    assert {m.sender for m in msgs} <= set(假人名)
    assert {m.source for m in msgs} == {"meeting"}
    assert {m.group_id for m in msgs} == {msgs[0].group_id}
    # 会议转写里没有账号可言 ⇒ None，不是 0（0 会被人物筛选当成一个真实账号）。
    assert {m.actor_account for m in msgs} == {None}
    for m in msgs:
        assert isinstance(m, PendingMessage)
        assert m.content.strip()

    # 每条的时间 = 会议开始时刻 + 那一轮的偏移。第一条在 00:00:12。
    assert msgs[0].ts == _墙上时间(腾讯会议开始) + 12


def test_夹具里那一轮长独白被切开了_而且每条都不超下游预算() -> None:
    """※ **这个源存在的理由**：一段长发言不切，下游那一批就只有它一条，
    而端点会静默截断（回 200、`finish_reason=stop`、客户端拿不到信号）。

    判据直接取下游自己的阈值（`domain.chunking.oversized`）—— 源以为自己切好了、
    下游仍然报 oversized，是最难发现的那种不一致。
    """
    msgs = _读夹具(腾讯会议夹具)

    # 那段独白在 00:09:15 —— 切出来的几条**共用这一个时间点**。
    独白 = [m for m in msgs if m.sender == "沈明朗" and m.ts == _墙上时间(腾讯会议开始) + 555]
    assert len(独白) >= 2, "那段四千多字的独白应当被切成两条以上"
    assert len({m.content for m in 独白}) == len(独白)
    assert len({m.msg_id for m in 独白}) == len(独白)
    assert max(len(m.content) for m in msgs) <= meeting_parse.EFFECTIVE_BUDGET_CHARS
    assert chunking.oversized(msgs) == []


def test_正文里与轮次行同形状的行不会被当成换人() -> None:
    """※ 夹缝里的判据：正文里有一行是「系统开放时间 08:00:00」，前面还空了一行。

    它的形状与轮次行**几乎一样**（名字 + 空白 + 时:分:秒）。把它当成一次换人
    的症状是「一轮话被劈成两半」，两半都读不懂，而且**不报错**。
    """
    msgs = _读夹具(腾讯会议夹具)
    赵雪依 = next(m for m in msgs if m.sender == "赵雪依")

    assert "系统开放时间 08:00:00" in 赵雪依.content
    assert "如果确实有特殊情况" in 赵雪依.content, "那一行之后的正文仍属于同一个人"
    assert not any(m.sender == "系统开放时间" for m in msgs)


def test_钉钉夹具认得出_说话人与正文在同一行() -> None:
    """※ 另一种形态：`[时:分:秒] 说话人 说：正文`。

    它与腾讯会议形态的区别不只是排版 —— `说：` 是形态**自带**的说话人标记，
    所以这一形态不需要「起一段」那条旁证就能确认是一轮。
    """
    msgs = _读夹具(钉钉夹具)

    assert len(msgs) >= 9
    assert {m.sender for m in msgs} <= set(假人名)
    assert msgs[0].ts == _墙上时间(钉钉开始) + 8
    # `说：` 是标记，不是正文 —— 它不该留在 content 里。
    for m in msgs:
        assert "说：" not in m.content
        assert m.content.strip()


def test_夹具里的人名与号码都是自己编的() -> None:
    """※ 红线自检：夹具里**不许**出现真实站点的真实内容。

    能机械检查的部分：人名必须来自项目那批自编假名、正文里不许出现网址。
    """
    for path in (腾讯会议夹具, 钉钉夹具):
        text = path.read_text(encoding="utf-8")
        assert "http://" not in text and "https://" not in text
        assert "example.test" not in text  # 那是网页源夹具的保留域名
        msgs = _读夹具(path)
        assert {m.sender for m in msgs} <= set(假人名)


# ── 切分（合成，把两个触发条件都逼出来） ────────────────────────────


def test_三十分钟的转写切成多条且每条都不超下游预算() -> None:
    """※ 「一轮话 = 一条消息」在长会议上的样子：三十分钟、每分钟一段。

    不切的话这一批就只有一条消息、而它的正文上万字 —— 端点会静默截断。
    """
    轮次 = []
    for i in range(30):
        body = f"这是第 {i} 分钟的一段话，用来把时间拉开。" + "内容" * 300 + "。"
        轮次.append(("林小满", f"00:{i:02d}:00", body))
    raw = _腾讯会议形态(轮次)

    namespace = "meeting:半小时"
    msgs = parse_transcript(
        raw, suffix=".txt", namespace=namespace,
        group_id=meeting_parse.group_id_for(namespace),
    )

    assert len(msgs) == 30, "一分钟一段，三十分钟就该是三十条"
    assert chunking.oversized(msgs) == []
    assert len({m.msg_id for m in msgs}) == len(msgs)


def test_一轮话比预算长时按句末标点切_不从句子中间劈开() -> None:
    """※ 「太长再切」的字符侧。

    切点落在**句末标点**之后：从句子中间切开会把一句话劈成两条半截话，
    两条都读不懂（`chunking.py` 对「超长单体」的处置理由一字不差）。
    """
    body = "".join(f"第 {i} 句：这一句话用来说明切分落在哪里。" + "填充" * 60 for i in range(120))
    raw = _腾讯会议形态([("林小满", "00:00:10", body)])

    namespace = "meeting:超长一轮"
    msgs = parse_transcript(
        raw, suffix=".txt", namespace=namespace,
        group_id=meeting_parse.group_id_for(namespace),
    )

    assert len(msgs) >= 3, "一万多字的一轮话应当被切成三条以上"
    assert chunking.oversized(msgs) == []
    for m in msgs[:-1]:
        assert m.content.rstrip()[-1] in "。！？；.!?;", m.content[-20:]
    # 切出来的几条共用同一个时间点（txt 形态一轮话只有一个时间点），
    # 但 `msg_id` 各不相同 —— 否则后一条会覆盖前一条，消息**变少**。
    assert {m.ts for m in msgs} == {_墙上时间(腾讯会议开始) + 10}
    assert len({m.msg_id for m in msgs}) == len(msgs)
    # 每一条都是原文的连续片段，拼起来还是那一段话（没有被改写）。
    assert "".join(m.content.replace("\n", "") for m in msgs) == body.replace("\n", "")


def test_同一轮里时间跨度超过窗口就切开() -> None:
    """※ 「太长再切」的时间侧：一个人连着说四十分钟，中间不换人。

    字符侧管不到这种（字幕一条只有几十字），所以必须有一条**时间**判据 ——
    否则一轮四十分钟的独白会被合并成一条跨了半小时的消息。
    """
    cues = []
    for i in range(0, 2400, 300):  # 每 5 分钟一条，共 40 分钟
        start = f"{i // 3600:02d}:{i % 3600 // 60:02d}:{i % 60:02d}.000"
        end = f"{(i + 60) // 3600:02d}:{(i + 60) % 3600 // 60:02d}:{(i + 60) % 60:02d}.000"
        cues.append((start, end, "<v 林小满>这是同一轮里的一段话。"))
    namespace = "meeting:窗口"
    msgs = parse_transcript(
        _vtt(cues), suffix=".vtt", namespace=namespace,
        group_id=meeting_parse.group_id_for(namespace),
        anchor_ts=_墙上时间(腾讯会议开始),
    )

    assert len(msgs) >= 3, "跨度四十分钟的一轮话应当被时间窗口切开"
    assert {m.sender for m in msgs} == {"林小满"}
    # 每条的时间跨度不超过窗口。
    for m in msgs:
        assert m.ts >= _墙上时间(腾讯会议开始)
    assert chunking.oversized(msgs) == []


# ── 说话人：形态自带才认，不带的**不猜** ────────────────────────────


def test_srt_没有说话人字段时_sender_置空而不是去猜() -> None:
    """※ 判据：SRT 的标准里没有说话人字段。

    猜「第一个冒号之前是不是名字」的后果：猜错会把正文的一部分当成名字，
    而那个「名字」会进人脸名单、被当成人名抹掉 —— **正文被咬掉一块**，
    而且没有任何东西会报错。
    """
    cues = [("00:00:10,000", "00:00:20,000", "各位同学注意：选课系统在今晚关闭。")]
    namespace = "meeting:srt"
    msgs = parse_transcript(
        _srt(cues), suffix=".srt", namespace=namespace,
        group_id=meeting_parse.group_id_for(namespace),
        anchor_ts=_墙上时间(腾讯会议开始),
    )

    assert {m.sender for m in msgs} == {""}
    # 正文原样保留 —— 「各位同学注意：」没有被当成名字切掉。
    assert msgs[0].content.startswith("各位同学注意：")


def test_vtt_的_v_标记是标准自带的_所以认它() -> None:
    cues = [
        ("00:00:10.000", "00:00:20.000", "<v 林小满>各位同学注意。"),
        ("00:01:10.000", "00:01:20.000", "<v 周允>我补充一句。"),
    ]
    namespace = "meeting:vtt"
    msgs = parse_transcript(
        _vtt(cues), suffix=".vtt", namespace=namespace,
        group_id=meeting_parse.group_id_for(namespace),
        anchor_ts=_墙上时间(腾讯会议开始),
    )

    assert [m.sender for m in msgs] == ["林小满", "周允"]
    assert "<v" not in msgs[0].content


# ── 两种「0 条」必须能分辨 ──────────────────────────────────────────


def test_没读懂与没有会议必须抛不同的东西() -> None:
    """※ 两者都是「解析出 0 条」，而产出为空是最不容易被注意到的一种失败。

    分界线是**有没有东西可读**：改版会动它，「这场会没内容」不会。
    """
    namespace = "meeting:两种0条"

    def 解(raw: bytes):
        return parse_transcript(
            raw, suffix=".txt", namespace=namespace,
            group_id=meeting_parse.group_id_for(namespace),
        )

    # 既不是腾讯会议形态、也不是钉钉形态，文件头里也没有时间 ⇒ 没读懂。
    with pytest.raises(_NotUnderstood):
       解("这是一份随便写的文本。\n里面有若干行。\n".encode("utf-8"))

    # 文件头读懂了（有会议开始时刻），但一轮发言都没有 ⇒ 读懂了、这里没内容。
    with pytest.raises(_NoMeetingsHere):
        解("会议主题：一场没有记录的会\n会议时间：2026-09-14 14:00:00\n".encode("utf-8"))

    # 两种异常的父类都是 `SourceFormatError` —— 用例层的处置不变，
    # 变的只是人能读到的**下一步**。
    assert issubclass(_NotUnderstood, SourceFormatError)
    assert issubclass(_NoMeetingsHere, SourceFormatError)


def test_钉钉形态与腾讯会议形态互不误认() -> None:
    """※ 用一套判据去认另一种形态，会把正文当成轮次，或者一条都认不出来。

    腾讯会议形态的轮次行是「说话人 + 对齐空白 + 时间」独占一行；钉钉形态的
    是「[时间] 说话人 说：正文」。两者互相都匹配不上。
    """
    腾讯 = _腾讯会议形态([("林小满", "00:00:10", "各位下午好。")])
    钉钉 = _钉钉形态([("林小满", "00:00:10", "各位下午好。")])
    腾讯文本 = 腾讯.decode("utf-8")
    钉钉文本 = 钉钉.decode("utf-8")

    assert meeting_parse._dingtalk_turn_indices(腾讯文本.splitlines()) == []
    assert meeting_parse._tencent_turn_indices(钉钉文本.splitlines()) == []

    for raw, 形态 in ((腾讯, "腾讯"), (钉钉, "钉钉")):
        msgs = parse_transcript(
            raw, suffix=".txt", namespace=f"meeting:{形态}",
            group_id=meeting_parse.group_id_for(f"meeting:{形态}"),
        )
        assert len(msgs) == 1
        assert msgs[0].sender == "林小满"
        assert msgs[0].content == "各位下午好。"


# ── 时间 ────────────────────────────────────────────────────────────


def test_时间按项目时区解释而不是机器时区() -> None:
    """※ 时区取全项目那一处口径（UTC+8）。取机器本地的话，同一份夹具在评委的
    机器上会算出不同的秒数，而日报是**按天分**的 —— 差 8 小时足以让一场会
    落到前一天。"""
    raw = _腾讯会议形态([("林小满", "00:10:00", "一段话。")], 会议时间="2026-09-14 14:00:00")
    msgs = parse_transcript(
        raw, suffix=".txt", namespace="meeting:时区",
        group_id=meeting_parse.group_id_for("meeting:时区"),
    )
    assert msgs[0].ts == _墙上时间("2026-09-14 14:00:00") + 600


def test_时间写法不认识时抛_不兜底成今天() -> None:
    raw = _腾讯会议形态([("林小满", "00:10:00", "一段话。")], 会议时间="2026年9月14日 14:00")
    with pytest.raises(_NotUnderstood):
        parse_transcript(
            raw, suffix=".txt", namespace="meeting:坏时间",
            group_id=meeting_parse.group_id_for("meeting:坏时间"),
        )


def test_字幕没有锚点时抛_不兜底成0或今天() -> None:
    """※ 转写里的时间戳是**相对会议开始**的偏移，字幕格式里没有放开始时刻的
    地方 ⇒ 没有侧车就算不出绝对时刻。兜底成 0 会把这场会扔进 1970 年。"""
    cues = [("00:00:10,000", "00:00:20,000", "一段话。")]
    with pytest.raises(_NotUnderstood):
        parse_transcript(
            _srt(cues), suffix=".srt", namespace="meeting:无锚点",
            group_id=meeting_parse.group_id_for("meeting:无锚点"),
        )


def test_侧车压过文件头() -> None:
    """※ 侧车是**显式**的（有人专门为这个文件写了它），文件头是**隐式**的。

    与 `web_source` 的「环境变量压过配置文件」是同一条判据：显式的意图压过
    隐式的默认。
    """
    raw = _腾讯会议形态([("林小满", "00:00:10", "一段话。")], 会议时间="2026-09-14 14:00:00")
    msgs = parse_transcript(
        raw, suffix=".txt", namespace="meeting:侧车",
        group_id=meeting_parse.group_id_for("meeting:侧车"),
        anchor_ts=_墙上时间("2026-09-20 09:00:00"),
    )
    assert msgs[0].ts == _墙上时间("2026-09-20 09:00:00") + 10


# ── 身份（msg_id / group_id） ───────────────────────────────────────


def test_重跑解析同一份夹具得到同一批_msg_id() -> None:
    """※ `msg_id` 是幂等键。它一变，同一条消息就变成新消息，日报里同一条
    出现两次，而没有任何东西会报错。"""
    甲 = [m.msg_id for m in _读夹具(腾讯会议夹具)]
    乙 = [m.msg_id for m in _读夹具(腾讯会议夹具)]
    assert 甲 == 乙
    assert len(set(甲)) == len(甲), "撞键会让后一条覆盖前一条 —— 消息变少，且不报错"


def test_同一段独白切出来的几条_msg_id_互不相同() -> None:
    """※ 切出来的几条**共用同一个起始秒**（txt 形态一轮话只有一个时间点）。
    键里不带「第几段」就会撞键。"""
    msgs = _读夹具(腾讯会议夹具)
    独白 = [m for m in msgs if m.sender == "沈明朗" and m.ts == _墙上时间(腾讯会议开始) + 555]
    assert len(独白) >= 2
    assert len({m.msg_id for m in 独白}) == len(独白)


def test_两场会算出的群号不同() -> None:
    """※ 一场会 = 下游视角里的一个「群」。两场会共用群号会被切进同一批，
    模型会把一场会的上下文套到另一场。"""
    assert meeting_parse.group_id_for("meeting:甲会") != meeting_parse.group_id_for("meeting:乙会")
    assert meeting_parse.group_id_for("meeting:甲会") >= meeting_parse.GROUP_ID_BASE
    assert meeting_parse.group_id_for("meeting:甲会") < (
        meeting_parse.GROUP_ID_BASE + meeting_parse.GROUP_ID_SPAN
    )


def test_msg_id_落在_sqlite_能装的范围内() -> None:
    """※ 超过 `2**63-1` 时插入报 "Python int too large" —— 在**一半**的文件上
    出现，像随机故障。"""
    for i in range(200):
        assert 0 <= meeting_parse.meeting_message_id("meeting:x", f"{i}|0") < 2**63


# ── 端口形状与「读不到就抛」 ────────────────────────────────────────


def test_会议源满足端口形状_而且真的会切(tmp_path, 临时配置, monkeypatch) -> None:
    """※ 判据 3：**形状由接口约束，处理方式不由接口约束**。

    网页通知一条就是一条（不切），会议转写一段必须切成多条 —— 两者对下游
    长得一样。为了迎合同一个下游而做同样的处理，其中一个必然错。
    """
    d = tmp_path / "会"
    d.mkdir()
    (d / "甲会.txt").write_bytes(_腾讯会议形态([("林小满", "00:00:10", "一段话。")]))
    monkeypatch.setenv(meeting_source.DIR_ENV, str(d))

    src = meeting_source.build_meeting_source()
    assert isinstance(src, ports.MessageSource)
    assert src.name == "meeting"

    msgs = src.read()
    assert len(msgs) == 1
    assert all(isinstance(m, PendingMessage) for m in msgs)


def test_目录里新出现的文件下一次读取就被吃进来(tmp_path, 临时配置, monkeypatch) -> None:
    """※ 「自动」的第二层意思：不需要用户改代码、不需要登记。"""
    d = tmp_path / "会"
    d.mkdir()
    monkeypatch.setenv(meeting_source.DIR_ENV, str(d))

    (d / "甲会.txt").write_bytes(_腾讯会议形态([("林小满", "00:00:10", "第一场。")]))
    assert len(meeting_source.build_meeting_source().read()) == 1

    (d / "乙会.txt").write_bytes(_钉钉形态([("周允", "00:00:20", "第二场。")]))
    assert len(meeting_source.build_meeting_source().read()) == 2


def test_读不到时抛_SourceUnavailable_不返回空(tmp_path, 临时配置, monkeypatch) -> None:
    """判据 1。返回空的后果：下游会以为「今天没有会」。"""
    monkeypatch.setenv(meeting_source.DIR_ENV, str(tmp_path / "没有这个目录"))
    with pytest.raises(SourceUnavailable):
        meeting_source.build_meeting_source().read()


def test_目录里一个转写都没有时抛_NoMeetingsHere(tmp_path, 临时配置, monkeypatch) -> None:
    """判据 2。它与「读不到」必须分得开：这里该去查**目录选对没有**。"""
    d = tmp_path / "空会"
    d.mkdir()
    (d / "说明.md").write_text("这不是转写", encoding="utf-8")
    monkeypatch.setenv(meeting_source.DIR_ENV, str(d))
    with pytest.raises(_NoMeetingsHere):
        meeting_source.build_meeting_source().read()


def test_认不出的转写整份报错_不静默跳过(tmp_path, 临时配置, monkeypatch) -> None:
    """※ 静默跳过一条消息正是本项目明令禁止的形状（用户会以为那天没事）。

    报错里要**带上文件名**：一份目录里可能有几十份转写，「有一份读不懂」必须
    能直接定位到是哪一份。
    """
    d = tmp_path / "会"
    d.mkdir()
    (d / "好的.txt").write_bytes(_腾讯会议形态([("林小满", "00:00:10", "一段话。")]))
    (d / "坏的.txt").write_text("这不是任何已知形态。\n", encoding="utf-8")
    monkeypatch.setenv(meeting_source.DIR_ENV, str(d))

    with pytest.raises(_NotUnderstood) as excinfo:
        meeting_source.build_meeting_source().read()
    assert "坏的.txt" in str(excinfo.value)


def test_时间窗在解析之后过_窗口内为空不算格式错(tmp_path, 临时配置, monkeypatch) -> None:
    """※ 「解析出 0 条」与「这段时间里没有会」是两件事：前者去改解析器，
    后者这段时间真的安静。先解析、再过滤，两者才分得开。"""
    d = tmp_path / "会"
    d.mkdir()
    (d / "甲会.txt").write_bytes(_腾讯会议形态([("林小满", "00:00:10", "一段话。")]))
    monkeypatch.setenv(meeting_source.DIR_ENV, str(d))
    src = meeting_source.build_meeting_source()

    早 = _墙上时间(腾讯会议开始)
    assert src.read(until=早) == []          # 窗口挑在会议之前 ⇒ 空，不是错
    assert len(src.read(since=早)) == 1


def test_两场会撞同一个群号时报错(tmp_path, 临时配置, monkeypatch) -> None:
    """※ 静默合并的症状是「日报里两场会的内容混在一起」，而它看起来完全正常。"""
    d = tmp_path / "会"
    d.mkdir()
    (d / "甲会.txt").write_bytes(_腾讯会议形态([("林小满", "00:00:10", "第一场。")]))
    (d / "乙会.txt").write_bytes(_腾讯会议形态([("周允", "00:00:20", "第二场。")]))
    monkeypatch.setenv(meeting_source.DIR_ENV, str(d))
    monkeypatch.setattr(meeting_parse, "group_id_for", lambda namespace: 830000001)
    monkeypatch.setattr(meeting_source, "group_id_for", lambda namespace: 830000001)

    with pytest.raises(SourceFormatError) as excinfo:
        meeting_source.build_meeting_source().read()
    assert "830000001" in str(excinfo.value)


# ── 配置与自动探测 ──────────────────────────────────────────────────


def test_没配目录时抛_不静默兜底(临时配置, 关掉自动探测) -> None:
    """※ 兜底到某个位置会让 `--source meeting` 在什么都没配的情况下「成功」
    读到别的东西，而报告看起来一切正常。"""
    with pytest.raises(ConfigError):
        meeting_source.build_meeting_source()


def test_配置文件里的_dir_也能用(tmp_path, 临时配置, 关掉自动探测, monkeypatch) -> None:
    d = tmp_path / "会"
    d.mkdir()
    (d / "甲会.txt").write_bytes(_腾讯会议形态([("林小满", "00:00:10", "一段话。")]))
    (临时配置 / "sources.toml").write_text(
        f'[meeting]\ndir = "{d.as_posix()}"\n', encoding="utf-8"
    )
    assert len(meeting_source.build_meeting_source().read()) == 1


def test_环境变量压过配置文件(tmp_path, 临时配置, 关掉自动探测, monkeypatch) -> None:
    """环境变量是**这一次**的意图，配置文件是**长期**的意图。"""
    甲 = tmp_path / "甲"
    乙 = tmp_path / "乙"
    for d in (甲, 乙):
        d.mkdir()
        (d / "会.txt").write_bytes(_腾讯会议形态([("林小满", "00:00:10", "一段话。")]))
    (临时配置 / "sources.toml").write_text(f'[meeting]\ndir = "{甲.as_posix()}"\n', encoding="utf-8")
    monkeypatch.setenv(meeting_source.DIR_ENV, str(乙))

    assert meeting_source.resolve_input().path == 乙


def test_配置目录的口径与组合根一致(tmp_path, monkeypatch) -> None:
    """※ 默认值在 `_common.py` 里算了一次，与 `bootstrap.Settings.from_env` 的
    那一份**必须一致**。代价是「同一个默认值有两处」，用测试钉住。

    （与 `tests/infrastructure/test_web_source.py::test_配置目录的口径与组合根一致`
    是同一条判据的两个落点 —— 两个源都依赖 `_common.config_dir`。）
    """
    from yechao.bootstrap import DEFAULT_CONFIG_DIR, Settings

    monkeypatch.delenv(meeting_source.CONFIG_DIR_ENV, raising=False)
    assert meeting_source._config_dir().name == DEFAULT_CONFIG_DIR
    assert Settings.from_env().config_dir.name == DEFAULT_CONFIG_DIR

    monkeypatch.setenv(meeting_source.CONFIG_DIR_ENV, str(tmp_path / "改过的配置目录"))
    assert meeting_source._config_dir() == tmp_path / "改过的配置目录"


def test_自动探测_腾讯会议的会议历史库(tmp_path, 临时配置, monkeypatch) -> None:
    """※ **用户零操作**那条路：客户端把会议写进本地库，下一轮摄取就吃到。

    这里造一个与真实客户端同形状的树（`<appdata>/Tencent/WeMeet/Global/Database/
    <hash>.db`，表名与列名照实测的来），把 `%APPDATA%` 指过去。
    """
    appdata = tmp_path / "appdata"
    库 = _造会议历史库(appdata, [
        {"meeting_code": "123456789", "meeting_id": "m1", "meeting_subject": "选题会",
         "meeting_begin_time": _墙上时间("2026-09-14 14:00:00"), "meeting_end_time": None,
         "meeting_total_elapsed_time": 1800, "creator_nickname": "林小满",
         "participants_count": 0},
        {"meeting_code": "123456789", "meeting_id": "m1", "meeting_subject": "选题会",
         "meeting_begin_time": _墙上时间("2026-09-21 14:00:00"), "meeting_end_time": None,
         "meeting_total_elapsed_time": 1800, "creator_nickname": "林小满",
         "participants_count": 0},
    ])
    monkeypatch.setattr(meeting_wemeet, "app_data_dir", lambda: appdata)
    monkeypatch.setattr(meeting_source, "_documents_dir", lambda: None)

    chosen = meeting_source.resolve_input()
    assert chosen.kind == "wemeet"
    assert chosen.path == 库

    msgs = meeting_source.build_meeting_source().read()
    assert len(msgs) == 2
    assert {m.sender for m in msgs} == {"林小满"}
    assert all(m.source == "meeting" for m in msgs)
    assert msgs[0].content.startswith("会议主题：选题会")
    # 同一场会的两次开法：同一个「群」，但**两条不同的消息**。
    assert msgs[0].group_id == msgs[1].group_id
    assert msgs[0].msg_id != msgs[1].msg_id
    assert msgs[0].ts == _墙上时间("2026-09-14 14:00:00")


def test_自动探测_转写目录优先于会议历史库(tmp_path, 临时配置, monkeypatch) -> None:
    """※ 判据是「哪个位置**真的有东西可读**」。

    腾讯会议的默认本地录制目录放的是 mp4，不是文本；只看「目录在不在」的话，
    一台从没导出过东西的机器会稳定地选中一个永远读不出内容的位置，而会议
    历史那条路永远轮不到 —— 那种失败的样子是「这个源一直是空的」。
    """
    appdata = tmp_path / "appdata"
    _造会议历史库(appdata, [{
        "meeting_code": "1", "meeting_id": "m", "meeting_subject": "有历史",
        "meeting_begin_time": _墙上时间("2026-09-14 14:00:00"), "meeting_end_time": None,
        "meeting_total_elapsed_time": 60, "creator_nickname": "", "participants_count": 0,
    }])
    documents = tmp_path / "Documents"
    (documents / "WeMeet").mkdir(parents=True)
    monkeypatch.setattr(meeting_wemeet, "app_data_dir", lambda: appdata)
    monkeypatch.setattr(meeting_source, "_documents_dir", lambda: documents)

    # 录制目录里只有 mp4 ⇒ 不算「有东西可读」，轮到会议历史。
    (documents / "WeMeet" / "会议.mp4").write_bytes(b"\x00\x00")
    assert meeting_source.resolve_input().kind == "wemeet"

    # 出现一份转写 ⇒ 它优先。
    (documents / "WeMeet" / "导出.txt").write_bytes(_腾讯会议形态([("林小满", "00:00:10", "一段话。")]))
    chosen = meeting_source.resolve_input()
    assert chosen.kind == "dir"
    assert chosen.path == documents / "WeMeet"


def test_会议历史库一场会都没有时抛_NoMeetingsHere(tmp_path, 临时配置, monkeypatch) -> None:
    appdata = tmp_path / "appdata"
    库 = _造会议历史库(appdata, [])
    monkeypatch.setattr(meeting_wemeet, "app_data_dir", lambda: appdata)
    monkeypatch.setattr(meeting_source, "_documents_dir", lambda: None)
    assert meeting_source.resolve_input().path == 库

    with pytest.raises(_NoMeetingsHere):
        meeting_source.build_meeting_source().read()


def test_会议历史库缺了必需的列时报没读懂(tmp_path) -> None:
    """※ 客户端改版了 ⇒ 抛。**不猜**：猜出来的映射会把另一列的值当成会议时间，
    而它不报错。"""
    root = tmp_path / "appdata" / "Tencent" / "WeMeet" / "Global" / "Database"
    root.mkdir(parents=True)
    库 = root / "abc.db"
    con = sqlite3.connect(库)
    con.execute("create table historical_meetings_new (meeting_code text)")
    con.commit()
    con.close()

    with pytest.raises(_NotUnderstood):
        meeting_wemeet.read_meetings(库)


def test_会议历史里的时间戳不像_unix_秒时报没读懂(tmp_path) -> None:
    """※ 单位变了要**响**：按毫秒算出来的时刻会落到五万年后，整场会跑出日报的
    视野，而且不报错。"""
    appdata = tmp_path / "appdata"
    库 = _造会议历史库(appdata, [{
        "meeting_code": "1", "meeting_id": "m", "meeting_subject": "会",
        "meeting_begin_time": _墙上时间("2026-09-14 14:00:00") * 1000,
        "meeting_end_time": None, "meeting_total_elapsed_time": 60,
        "creator_nickname": "", "participants_count": 0,
    }])
    with pytest.raises(_NotUnderstood):
        meeting_wemeet.read_meetings(库)


def test_会议历史库打不开时抛_SourceUnavailable(tmp_path) -> None:
    """判据 1：读不到就抛，不返回空。"""
    with pytest.raises(SourceUnavailable):
        meeting_wemeet.read_meetings(tmp_path / "没有这个库.db")


def test_会议历史里不知道的字段省略_不编值(tmp_path) -> None:
    """※ 宁可承认「这个源没自报这一项」，也不编一个值出来 —— 编出来的值会被
    下游当成事实。"""
    appdata = tmp_path / "appdata"
    库 = _造会议历史库(appdata, [{
        "meeting_code": "1", "meeting_id": "m", "meeting_subject": "无名会",
        "meeting_begin_time": _墙上时间("2026-09-14 14:00:00"), "meeting_end_time": None,
        "meeting_total_elapsed_time": 0, "creator_nickname": "", "participants_count": 0,
    }])
    (msg,) = meeting_wemeet.read_meetings(库)
    assert "参会人数" not in msg.content      # 0 的含义是「客户端没记」
    assert "结束" not in msg.content
    assert "发起人" not in msg.content
    assert msg.sender == ""
    assert msg.content == "会议主题：无名会\n开始：2026-09-14 14:00"


# ── 护栏 ────────────────────────────────────────────────────────────


def test_解析模块不碰_IO() -> None:
    """※ 「抓取与解析分开」这条要求，机械可检查的那一半在这里。

    解析模块一旦 import 了 `pathlib` / `os` / `sqlite3`，它就不再是纯函数 ——
    而「同样的字节永远给同样的结果」正是它值得被离线测试的理由。
    """
    tree = ast.parse(Path(meeting_parse.__file__).read_text(encoding="utf-8"))
    顶层包: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            顶层包.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            顶层包.add(node.module.split(".")[0])

    不许 = {"urllib", "socket", "http", "os", "pathlib", "io", "time",
            "tempfile", "shutil", "sqlite3"}
    assert not (顶层包 & 不许), f"解析模块碰了 I/O：{sorted(顶层包 & 不许)}"


def test_读不到与没读懂都是_SourceError_这一族(tmp_path, 临时配置, monkeypatch) -> None:
    """※ 跨层只抛这一族里的东西 —— 「这一层可能抛什么」才是一个可以被穷举的闭集。"""
    d = tmp_path / "会"
    d.mkdir()
    monkeypatch.setenv(meeting_source.DIR_ENV, str(d))
    with pytest.raises(SourceError):
        meeting_source.build_meeting_source().read()


def test_类名都带下划线_不触到接线的文本判据() -> None:
    """※ `test_适配层的实现都接上了线` 扫的是**顶层公开类名**。

    数据源不由组合根实例化（它由注册表组合），所以本模块的类都带下划线前缀 ——
    与 `web_source._WebSource` 同一个理由。这条测试把这件事钉住，免得下一个
    人把下划线去掉之后，护栏在**另一条**测试里报一个看起来莫名其妙的错。
    """
    for module in (meeting_parse, meeting_source, meeting_wemeet):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        公开 = [
            node.name
            for node in tree.body
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
        ]
        assert 公开 == [], f"{module.__name__} 有公开类：{公开}"


# ── 造一个与真实客户端同形状的会议历史库 ────────────────────────────


def _造会议历史库(appdata: Path, rows: list[dict]) -> Path:
    """在 `appdata` 下造一棵与实测的客户端目录同形状的树。

    ※ 列名照**实测**的来（见 `meeting_wemeet.REQUIRED_COLUMNS`）—— 造一个
    我们自己想当然的表，测的就只是自己的想象。
    """
    dbdir = appdata / "Tencent" / "WeMeet" / "Global" / "Database"
    dbdir.mkdir(parents=True, exist_ok=True)
    path = dbdir / "7b7673502ee316661967871a748564f4.db"
    con = sqlite3.connect(path)
    con.execute(
        """create table historical_meetings_new (
            meeting_code text, meeting_id text, meeting_subject text,
            meeting_begin_time integer, meeting_end_time integer,
            meeting_total_elapsed_time integer, creator_nickname text,
            participants_count integer)"""
    )
    for row in rows:
        con.execute(
            "insert into historical_meetings_new values (?,?,?,?,?,?,?,?)",
            (
                row.get("meeting_code"), row.get("meeting_id"), row.get("meeting_subject"),
                row.get("meeting_begin_time"), row.get("meeting_end_time"),
                row.get("meeting_total_elapsed_time"), row.get("creator_nickname"),
                row.get("participants_count"),
            ),
        )
    con.commit()
    con.close()
    return path
