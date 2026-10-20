"""摄取用例 —— 这个文件测的是**接线**，不是某个真源。

## 为什么用假源

真源（群库 / 会议转写 / 邮箱）在这里跑不起来：它们要外部世界。
而「一个源能不能被正确接上」与「那个源长什么样」是**两件事** ——
前者可以在没有真源的情况下被完整验证，后者不能。

⇒ 所以先证明源层接线正确，再往里塞真源。顺序反了会写出
「跑得通但没被验证」的代码：它看起来是绿的，而它绿的原因是**没人跑它**。

## ※ 假源自己必须先守住端口的三条判据

如果假源不守，下面那些接线用例**什么都没证明** —— 它们证明的只是
「一个不守契约的东西被接上了」。所以本文件的前一半是直接钉假源本身的。

端口（`application/ports.py::MessageSource`）的三条：

  1. **读不到就抛**（`SourceUnavailable`），**不许返回空**
  2. **解析出 0 条也抛**（`SourceFormatError`）
  3. **形状由接口约束，处理方式不由接口约束** —— 两个源的归一化方式可以
     完全不同（会议转写要切、邮件不能切），但都得长得像 `MessageSource`

第 1、2 条要分开，是因为两者的下一步不同：一个去查连通性，一个去改解析器。
而「读不到」与「本来就没有」如果形状相同，下游会给出同一份日报，
并用「今天很安静」的口气叙述它。

## 假号码、假群号

※ 红线：**自己编**。下面这些号是按格式编的（群号 820000011 起），
不是从任何地方抄的。编出来的不必像真的，格式对就够。
"""

from __future__ import annotations

import pytest

from yechao.application import ingest as ingest_uc
from yechao.application import ports
from yechao.domain.errors import ConfigError, SourceFormatError, SourceUnavailable
from yechao.domain.models import PendingMessage
from yechao.infrastructure.persistence import sqlite_store
from yechao.infrastructure.persistence.sqlite_store import SqliteStore
from yechao.infrastructure.runtime.clock import FixedClock


# ── 两个假源：同样的端口，**相反的处理方式** ─────────────────────────


class 会议转写源:
    """会议转写 —— **长音视频**，一段转写必须切成多条。

    ※ 切是**这个源自己的决定**，不是接口的要求：一整场会的转写直接丢给
    模型会超出上下文，而端点在超长时**静默截断**（回 200、只报截断后的数）。
    所以切批的责任在这里，长度预算由这个源负责。
    """

    name = "meeting"

    def __init__(
        self,
        transcript: str | None,
        *,
        group_id: int = 820000011,
        first_msg_id: int = 9001,
    ) -> None:
        self._transcript = transcript
        self._group_id = group_id
        self._first = first_msg_id

    def read(self, *, since: int | None = None, until: int | None = None) -> list[PendingMessage]:
        if self._transcript is None:
            # 判据 1：读不到 ⇒ 抛。**不许**返回空。
            raise SourceUnavailable("会议转写读不到：音频文件不在")
        blocks = [b.strip() for b in self._transcript.split("\n---\n") if b.strip()]
        if not blocks:
            # 判据 2：读到了、但解析出 0 段 ⇒ 也抛。
            # 「这场会没人说话」与「转写格式变了」在这里长得一样，
            # 而产出为空是所有错误里最不容易被注意到的一种。
            raise SourceFormatError("会议转写解析出 0 段（转写格式变了？）")
        out: list[PendingMessage] = []
        for i, block in enumerate(blocks):
            ts = 1000 + i
            if since is not None and ts < since:
                continue
            if until is not None and ts >= until:
                continue
            out.append(
                PendingMessage(
                    msg_id=self._first + i,
                    group_id=self._group_id,
                    ts=ts,
                    sender="转写",
                    content=block,
                )
            )
        return out


class 邮件源:
    """邮件 —— **自洽的短文本**，一封就是一条，**切了反而把它拆散**。

    ※ 它与 `会议转写源` 的差别正是判据 3 想说的：两者归一化方式**相反**
    （一个切、一个不切），而它们对下游来说长得一样。
    如果为了迎合同一个下游而让两者做同样的处理，其中一个必然是错的。
    """

    name = "mail"

    def __init__(
        self,
        mails: list[dict] | None,
        *,
        group_id: int = 820000012,
        first_msg_id: int = 9101,
    ) -> None:
        self._mails = mails
        self._group_id = group_id
        self._first = first_msg_id

    def read(self, *, since: int | None = None, until: int | None = None) -> list[PendingMessage]:
        if self._mails is None:
            raise SourceUnavailable("邮件源读不到：IMAP 连不上")
        out: list[PendingMessage] = []
        for i, mail in enumerate(self._mails):
            body = str(mail.get("body") or "").strip()
            if not body:
                # 空正文的邮件是**这封邮件没内容**，不是「整个源读不到」——
                # 所以它被跳过，而不是让整个源抛。
                continue
            ts = 2000 + i
            if since is not None and ts < since:
                continue
            if until is not None and ts >= until:
                continue
            out.append(
                PendingMessage(
                    msg_id=self._first + i,
                    group_id=self._group_id,
                    ts=ts,
                    sender=str(mail.get("from") or "发件人"),
                    content=body,
                )
            )
        if not out:
            raise SourceFormatError("邮件源解析出 0 条（一封都没解出来）")
        return out


_转写 = "第一段：明天下午三点开会。\n---\n第二段：地点改到三号楼。\n---\n第三段：请带笔记本。"


# ── 先钉假源：它自己守不守契约 ──────────────────────────────────────


def test_会议转写源读不到时必须抛_不许返回空() -> None:
    """判据 1。返回空的后果：下游会以为「这场会没内容」。"""
    with pytest.raises(SourceUnavailable):
        会议转写源(None).read()


def test_会议转写源解析出_0_条时也必须抛() -> None:
    """判据 2。**空是错的形状，不是「没有」的形状。**"""
    with pytest.raises(SourceFormatError):
        会议转写源("").read()
    with pytest.raises(SourceFormatError):
        会议转写源("\n---\n\n---\n").read()


def test_邮件源读不到时必须抛_不许返回空() -> None:
    with pytest.raises(SourceUnavailable):
        邮件源(None).read()


def test_邮件源解析出_0_条时也必须抛() -> None:
    with pytest.raises(SourceFormatError):
        邮件源([]).read()
    # 全是空正文 ⇒ 解出来 0 条 ⇒ 同样是「格式坏了」，不是「今天没邮件」
    with pytest.raises(SourceFormatError):
        邮件源([{"body": "   "}, {"body": ""}]).read()


def test_两个源的归一化方式相反_但都满足端口() -> None:
    """判据 3：**形状由接口约束，处理方式不由接口约束**。

    会议转写切成 3 条，邮件 1 封就是 1 条 —— 两者对下游完全一样。
    """
    会议 = 会议转写源(_转写)
    邮件 = 邮件源([{"from": "教务", "body": "选课系统明天关闭。"}])

    # 端口是 `runtime_checkable` 的 `Protocol`：这里查的是「长得像不像」，
    # 而不是「有没有继承谁」—— 适配层不需要 import 用例层。
    assert isinstance(会议, ports.MessageSource)
    assert isinstance(邮件, ports.MessageSource)

    assert len(会议.read()) == 3, "会议转写必须被切"
    assert len(邮件.read()) == 1, "邮件不该被切 —— 切了反而把它拆散"

    # 两者的产物形状相同（都是 PendingMessage），这就是端口约束的那部分
    for m in 会议.read() + 邮件.read():
        assert isinstance(m, PendingMessage)


def test_时间窗被透传给源() -> None:
    """`ingest.run(since=..., until=...)` 要**真的**传下去。

    ※ 这条钉的是一个容易漏的形态：用例层收了 `since`/`until` 却只把它们
    留在签名里。症状是「时间窗设了没用」，而它**不报错** —— 读回来的
    消息多了，看起来像「这段时间本来就有这么多」。
    """
    src = 会议转写源(_转写)
    assert len(src.read(since=1001, until=1002)) == 1


# ── 接线：`ingest.run` 有没有真的走源层 ────────────────────────────


@pytest.fixture()
def store(tmp_path):
    """真仓储。※ 这里用真的而不是桩：这条链要一路走到 SQLite，
    「写进去了」才是可断言的事实。"""
    db = tmp_path / "t.db"
    sqlite_store.init_db(db)
    return SqliteStore(sqlite_store.connect(db))


def _deps(*, messages, sources) -> ingest_uc.IngestDeps:
    """装一个依赖包。

    ※ 时钟是**必给**的（摄取要拿它给账打时间戳），而这里用 `FixedClock`
    而不是真时钟：判据是「测试不许依赖跑用例的那一刻是几号」。
    """
    return ingest_uc.IngestDeps(
        messages=messages, sources=sources, clock=FixedClock(ts=1792333200)
    )


def test_读到的消息真的落库(store) -> None:
    deps = _deps(messages=store, sources=[会议转写源(_转写)])
    outcome = ingest_uc.run(deps)

    assert outcome.inserted == 3
    assert outcome.read == 3
    assert outcome.per_source == {"meeting": 3}
    assert outcome.ok
    assert store.message_count() == 3, "报告说写了 3 条，库里就得有 3 条"


def test_读到的消息就是入库的那一批(store) -> None:
    """※ `outcome.messages` 存在的理由：`demo` 的报告要拿**同一批**去算
    预筛与每群条数。让它自己再取一份，就是「两次读碰巧一样」。"""
    deps = _deps(messages=store, sources=[会议转写源(_转写)])
    outcome = ingest_uc.run(deps)

    assert [m.msg_id for m in outcome.messages] == [9001, 9002, 9003]
    assert sum(outcome.per_source.values()) == len(outcome.messages)


def test_一个源读不到不阻断其余源_但必须记账(store) -> None:
    """※ **这是本模块的核心判据。**

    一个群读不到不该让其余群的消息也进不了库 —— 那会把一次局部失败放大成
    全量失败。但**失败必须留下痕迹**：静默继续的话，用户看到的是
    「今天只有这些」，而不是「今天有一个源没读到」。

    ⇒ 断言三件事：其余源照样落库、失败被记在 `errors` 里、`ok` 为假
    （由调用方据此决定退出码）。
    """
    deps = _deps(
        messages=store,
        sources=[会议转写源(None), 邮件源([{"from": "教务", "body": "明天停课。"}])],
    )
    outcome = ingest_uc.run(deps)

    assert store.message_count() == 1, "一个源读不到，其余源的消息也得进库"
    assert outcome.inserted == 1
    assert outcome.per_source == {"meeting": 0, "mail": 1}
    assert [name for name, _ in outcome.errors] == ["meeting"]
    assert not outcome.ok, "有源读不到却报 ok —— 那就是「失败看起来像成功」"


def test_全部源都读不到时_一条也不写且不谎称成功(store) -> None:
    deps = _deps(
        messages=store, sources=[会议转写源(None), 邮件源(None)]
    )
    outcome = ingest_uc.run(deps)

    assert store.message_count() == 0
    assert outcome.inserted == 0
    assert len(outcome.errors) == 2
    assert not outcome.ok
    # ※ `read == 0` 与「errors 非空」必须同时成立且**分开可读**：
    # 只看 `read` 的话，「没读到」与「本来就没有」是同一个 0。
    assert outcome.read == 0


def test_解析出_0_条也算失败_而不是今天没消息(store) -> None:
    """判据 2 在**接线层**的样子：源抛了，`errors` 里就得有它。

    这条与上一条分开写，是因为两者的下一步不同（改解析器 vs 查连通性），
    而它们在这里的**形状**是一样的 —— 都是 `errors` 里的一条。
    """
    deps = _deps(messages=store, sources=[邮件源([])])
    outcome = ingest_uc.run(deps)

    assert [name for name, _ in outcome.errors] == ["mail"]
    assert "0 条" in outcome.errors[0][1]
    assert not outcome.ok


def test_两个源的消息合并进同一批(store) -> None:
    deps = _deps(
        messages=store,
        sources=[会议转写源(_转写), 邮件源([{"from": "教务", "body": "明天停课。"}])],
    )
    outcome = ingest_uc.run(deps)

    assert outcome.read == 4
    assert outcome.inserted == 4
    assert outcome.per_source == {"meeting": 3, "mail": 1}
    assert store.message_count() == 4


def test_重跑摄取不产生重复消息(store) -> None:
    """※ 幂等。重复消息会导致重复条目，而那是**无声的**：
    日报里同一条通知出现两次，没有任何东西会报错。"""
    deps = _deps(messages=store, sources=[会议转写源(_转写)])

    first = ingest_uc.run(deps)
    second = ingest_uc.run(deps)

    assert first.inserted == 3
    assert second.inserted == 0, "重跑又写了一遍 —— 那是重复条目的源头"
    # ※ 阴性对照：第二次**确实读到了**（不是「源没消息」），只是没重复写。
    # 少了这一条，一个「永远什么都不读」的实现也能让上面那句通过。
    assert second.read == 3
    assert store.message_count() == 3


# ── 源的选择：名字 → 实现，只有一处答案 ──────────────────────────────


def test_默认源在注册表里并且真的能造出来() -> None:
    """※ 这条守的是「写了但没接」的镜像：**接上了但选不出来**。

    注册表是「有哪些源」的唯一答案。它空掉、或者默认源名打错一个字母，
    症状是 `ingest` 直接报「不认识的数据源」—— 而那时用户手里一个能用的
    源都没有。
    """
    from yechao.infrastructure.sources import DEFAULT_SOURCES, REGISTRY, build

    assert DEFAULT_SOURCES, "默认源是空的 —— `ingest` 会一条都读不到"
    for name in DEFAULT_SOURCES:
        assert name in REGISTRY, f"默认源 {name!r} 不在注册表里"
    built = build(DEFAULT_SOURCES)
    assert [type(s).__name__ for s in built] == ["SynthSource"]
    for s in built:
        assert isinstance(s, ports.MessageSource)


def test_源名不认识时抛_不回落() -> None:
    """※ **不回落**是刻意的。

    回落到默认源会让 `--source meeting`（还没实现）变成「静默读了合成语料」：
    用户拿到一份看起来成功的报告，而里面一条真消息都没有。
    这类失败比报错危险得多 —— 报错至少让人知道要去改什么。
    """
    from yechao.infrastructure.sources import build

    with pytest.raises(ConfigError) as ei:
        build(["meeting"])
    # 报错要说清**有哪些能用的**，否则用户只能去读源码
    assert "synth" in str(ei.value)
