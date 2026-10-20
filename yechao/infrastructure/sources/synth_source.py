"""合成数据源 —— 演示用，**离线、确定、不含真人信息**。

## 为什么它是「一步复现」的地基

`python -m yechao demo` **不许需要任何密钥、任何网络、任何外部素材**。
评委只 clone 仓库就要能跑通 —— 这是红线。

⇒ 所以演示数据必须**在仓库里**、**能自己长出来**。

## 它同时是评测集

真实数据没有真值，所以「漏检率」这类指标的分母只能来自
合成数据。`domain/synth.py` 里每条语料都带 `Expect` —— 本模块读的就是它。

## 假号码、假姓名全部自己编

⚠️ 红线之一。它们**不必像真的，格式对就够**
—— 判据是按格式写的，不是按真实性写的。

## 「读不到」与「本来就没有」

本源的 `read()` **永远读得到**（数据在代码里），所以它不会抛。
但它仍然遵守契约的形式：**如果素材为空（有人把 CORPUS 清空了），就抛**
—— 因为那正是「格式坏了」而不是「今天没消息」。
"""

from __future__ import annotations

from typing import Any

from yechao.domain.errors import SourceFormatError
from yechao.domain.synth import CORPUS, GROUPS, PendingMessage

#: 源的标识。
NAME = "synth"


class SynthSource:
    """从 `domain/synth.py` 里的语料读。

    ⚠️ 这个类的 `read()` **不做任何解析** —— 语料本来就是结构化的。
    真实源（会议转写 / 邮件）的读+解比例与这里完全不同，那正是「归一化的
    责任在数据源」的意思：**每个源自己决定怎么把外部世界变成消息**。
    """

    name = NAME

    def __init__(self, corpus: tuple[Any, ...] = CORPUS) -> None:
        self.corpus = corpus

    def read(self, *, since: int | None = None, until: int | None = None) -> list[PendingMessage]:
        if not self.corpus:
            # ⚠️ **解析出 0 条 ⇒ 抛**。
            # 空是所有错误里最不容易被注意到的一种。
            raise SourceFormatError(
                "合成语料是空的 —— 素材生成方式不对，不是『今天没消息』"
            )

        out: list[PendingMessage] = []
        for entry in self.corpus:
            m = entry.msg
            if since is not None and m.ts < since:
                continue
            if until is not None and m.ts >= until:
                continue
            out.append(m)
        return out

    def groups(self) -> list[Any]:
        return list(GROUPS)
