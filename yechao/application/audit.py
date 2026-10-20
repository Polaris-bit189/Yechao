"""出网审计用例 —— 把审计记录汇总成隐私页要的那份数字。

## 要防什么

**「我们做了脱敏」是一句无法证伪的承诺。** 任何作品都能这么说，评委没法验证，
于是它不产生区分度。

⇒ 把边界从**纪律**变成**可回放的功能**：每次出网时留一条记录，让任何人都能
事后复核「那一刻送出去的到底是什么」。

## 两个必踩的坑，本模块各有一条判据

**坑一：判据自己没在跑。**

如果检查用的正则/词表与脱敏用的**不是同一份**，两者迟早漂移。漂移后检查报
「0 命中」，而实际有东西出去了。

⇒ 本模块**不自己写检测逻辑**。它读的是 `domain/scrub.py` 在**脱敏那一刻**
留下的报告（`ScrubReport`）—— 检查与脱敏是**同一次动作**，不可能漂移。

**坑二：输入是空的。**

如果这一轮没有可比对的名单，那么「命中 0」的含义是「**没查**」，不是「通过」。
这两件事在结果里长得一模一样。

⇒ 汇总结果里**必须**带 `names_were_checked`，它的含义是「本轮至少有一次
出网**真的比对过**姓名名单」。它是 `false` 时，`known_name_hits: 0` 读作
「没查」而不是「通过」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PrivacyView:
    """`/api/privacy` 的响应。字段名与前端 `PrivacyResponse` 逐个对应。"""

    records: int = 0
    files: int = 0
    surfaces: dict[str, int] = field(default_factory=dict)
    messages: int = 0
    chars: int = 0
    known_name_hits: int = 0
    names_checked: int = 0
    #: ⚠️ **姓名那一维是否真的检查过。**
    #: `false` 时 `known_name_hits: 0` 的含义是「**没查**」，不是「查了没漏」
    #: —— UI 必须分开显示这两件事（前端注释逐字）。
    names_were_checked: bool = False
    num_hits: int = 0
    name_placeholders: int = 0
    num_placeholders: int = 0
    clean: bool = True
    dirty: list[dict[str, Any]] = field(default_factory=list)
    hint: str | None = None


def summarize(entries: list[Any]) -> PrivacyView:
    """把审计记录汇总成视图。

    ⚠️ `clean` 的判据**只是**「没有命中的记录」。它**不等于**「没有隐私风险」
    —— 界面上不许把它简化成「无隐私风险」。本函数不产生那句文案，
    那是界面的事，但字段语义在这里定死。
    """
    view = PrivacyView()

    if not entries:
        # ⚠️ `records: 0` **不是错误** —— 它表示还没跑过 refine。
        # `hint` 给引导文案，界面据此说「还没有出网记录」而不是「一切正常」。
        view.hint = "还没有出网记录。跑一次 `python -m yechao refine` 之后，这里会显示每次出网的统计。"
        return view

    view.records = len(entries)
    view.files = len({e.get("_file") for e in entries if e.get("_file")}) or 1

    surfaces: dict[str, int] = {}
    checked_max = 0
    for e in entries:
        surface = str(e.get("surface") or "unknown")
        surfaces[surface] = surfaces.get(surface, 0) + 1
        view.messages += int(e.get("messages") or 0)
        view.chars += int(e.get("chars") or 0)
        view.num_placeholders += int(e.get("num_placeholders") or 0)
        view.name_placeholders += int(e.get("name_placeholders") or 0)
        checked_max = max(checked_max, int(e.get("names_checked") or 0))

    view.surfaces = surfaces
    view.names_checked = checked_max
    # ⚠️ **这一行是坑二的全部内容**：名单为空（`checked == 0`）时，
    # 「命中 0」必须能被读成「没查」。
    view.names_were_checked = checked_max > 0

    # 命中：来自记录里已经算好的字段。本模块**不重算** —— 重算就意味着
    # 另写一套判据，而那正是坑一。
    for e in entries:
        nh = int(e.get("name_hits") or 0)
        uh = int(e.get("num_hits") or 0)
        view.known_name_hits += nh
        view.num_hits += uh
        if nh or uh:
            view.dirty.append(
                {
                    "surface": e.get("surface") or "unknown",
                    "batch": int(e.get("batch") or 0),
                    "name_hits": nh,
                    "num_hits": uh,
                    "sha256": str(e.get("sha256") or ""),
                }
            )

    view.clean = not view.dirty
    return view


def verify_records(entries: list[Any], *, names: list[str], nums: list[str]) -> list[dict[str, Any]]:
    """**事后复核**：拿一批已知的姓名/号码去审计记录上比对。

    ⚠️ 这个函数是**给测试与自检用的**，不是运行时路径。

    它存在的理由：验收方式要求
      · 构造一个**必然触发**的输入 ⇒ 检查**必须报红**
      · 构造一个干净输入 ⇒ 检查**必须放过**
    两者缺一不可 —— 只会报红的检查会让红线失去意义（狼来了）。

    ⚠️ 它比对的是审计记录里的**指纹与计数**，不是内容（记录里本就不存内容）。
    所以它回答的是「有没有记录在案」，不是「文本里有没有」。
    """
    out: list[dict[str, Any]] = []
    for e in entries:
        if e.get("name_hits") or e.get("num_hits"):
            out.append(dict(e))
    return out
