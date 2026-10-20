"""端到端指标 —— 「你的东西到底好不好用」。

## 要防什么

**「引用匹配率」「闸门拦下几条」这类都是中间指标** —— 它们说明流水线在动，
但回答不了评委真正会问的那句。

产品的存在理由是「怕漏掉重要的通知」，那么**漏检率**就必须是一个能拿出来的
数字。

## 真值从哪来

**真实数据没有真值** —— 隐私原因下永远无法人工标注。

⇒ 真值只能来自**合成数据**：`domain/synth.py` 里每条语料都带 `Expect`
（「这条本该被怎么判」）。于是合成语料不只是演示数据，**它是唯一能给出真值的
评测集**。

## 四个口径坑（每一个都会造出「假缺陷」）

这一节比上面都重要。**口径错了不会报错，只会给出一个看起来合理、实际指向
错误方向的数。**

**坑 1：分母里混进了「没跑完」。**
流水线跑一批数据时总有一部分**从未被处理**（批大小限制、中途失败、预算耗尽）。
把它们算进「模型漏了」，会得出远低于实际的召回率 —— 而真相是「这次没跑完」。
⇒ **分母只含真正被送进模型的那些**；未处理的、失败的单独报成**覆盖率**。

**坑 2：同一个真值标签下混着两种语义。**
「源文里有一个日期」这句话不够 —— 必须说清它是「截止」还是「活动开始」。
⇒ 所以 `Expect` 里是 `deadline_text` 与 `start_text` **两个字段**。

**坑 3：问一个字段它答不了的问题。**
「时刻」要求精确到分钟，而源文只写了日期、没写时间 —— 这时模型**填不进**，
不是它漏了，是**这一格本来就不该有值**。
⇒ `Expect.start_not_inferable` 标出这种输入，它们**不进分母**。

**坑 4：只统计「有产出的」。**
只在「产出了条目」的那些上算幻觉率 ⇒ **漏抽得越多、分母越小、幻觉率反而
越好看**。
⇒ 幻觉率的分母是**所有被检查过的输入**，不只是有产出的那些。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from yechao.domain.synth import Expect


@dataclass
class Counts:
    """一个指标的三件套。※ **分母必须能解释「它代表了多少数据」**。"""

    #: 分子（命中的）。
    hit: int = 0
    #: 分母（**真正被考察的**）。
    total: int = 0

    @property
    def rate(self) -> float | None:
        """`None` = **分母为 0**，不是 0.0。

        ※ 本模块最要紧的一条设计就在这：分母为 0 时给的是「算不了」，
        而不是「0%」——后者会被读成「一次都没命中」，而真相是「没测」。
        与隐私页的 `names_were_checked` 是同一条判据。
        """
        if self.total == 0:
            return None
        return self.hit / self.total

    def __str__(self) -> str:
        if self.total == 0:
            return "无法计算（分母为 0）"
        return f"{self.hit}/{self.total} = {self.rate:.1%}"


@dataclass
class MetricsReport:
    """一次评测的全部数字。**每一项都要能说出它的分母是谁。**"""

    #: ※ 覆盖率**单列** —— 它不是某一个模型指标的分母，而是「这次跑了多少」。
    #: 报它，是为了让人知道后面那些数字代表了多少数据（坑 1）。
    coverage: Counts = field(default_factory=Counts)
    #: 预筛召回：**该保留的**里，真的被保留了的有多少。
    #: ※ 这是「漏检」的主指标 —— 产品怕的就是漏掉重要通知。
    prefilter_recall: Counts = field(default_factory=Counts)
    #: 预筛误伤：**该丢的**里，被错误保留的有多少。
    prefilter_false_keep: Counts = field(default_factory=Counts)
    #: 类目正确率：模型给出的类目与真值一致的比例。
    kind_accuracy: Counts = field(default_factory=Counts)
    #: 截止日抽取：真值说「有截止日」的那些里，真的抽出来了的有多少。
    deadline_recall: Counts = field(default_factory=Counts)
    #: 活动开始时刻抽取。※ 分母**不含** `start_not_inferable` 的那些（坑 3）。
    start_recall: Counts = field(default_factory=Counts)
    #: **幻觉率**：模型给出的字段里，源文里找不到依据的比例。
    #: ※ 分母是**所有被检查过的输入**，不只「有产出的」（坑 4）。
    hallucination: Counts = field(default_factory=Counts)
    #: 脱敏召回：真值标出的号码/姓名里，真的被抹掉的有多少。
    scrub_recall: Counts = field(default_factory=Counts)
    #: 脱敏误伤：被抹掉的东西里，真值**没标**的有多少（抹多了）。
    scrub_false_positive: Counts = field(default_factory=Counts)

    #: 判据不适用而**没进**任何分母的输入条数。**必须报出来** ——
    #: 否则「分母怎么这么小」永远说不清（坑 3）。
    excluded_not_applicable: int = 0
    #: 喂进来的语料总条数（含没被处理的）。
    corpus_size: int = 0

    def lines(self) -> list[str]:
        """给人看的报告。"""
        out = [
            f"语料 {self.corpus_size} 条",
            f"预筛保留（留下的 / 全部）    {self.coverage}",
            f"预筛召回（该留的留下了）      {self.prefilter_recall}",
            f"预筛误伤（该丢的留下了）      {self.prefilter_false_keep}",
            f"类目正确率                    {self.kind_accuracy}",
            f"截止日抽取召回                {self.deadline_recall}",
            f"开始时刻抽取召回              {self.start_recall}",
            f"幻觉率（字段无依据的比例）    {self.hallucination}",
            f"脱敏召回（该抹的抹了）        {self.scrub_recall}",
            f"脱敏误伤（抹多了）            {self.scrub_false_positive}",
        ]
        if self.excluded_not_applicable:
            out.append(
                f"※ 有 {self.excluded_not_applicable} 条**判据不适用**，"
                f"没有进任何分母（源文里就没有那一格该有的信息）"
            )
        return out


def prefilter_metrics(
    decisions: dict[int, bool], expects: dict[int, Expect]
) -> tuple[Counts, Counts]:
    """预筛的两个指标。

    ※ **分母是「真值说该保留/该丢的条数」，不是「语料总条数」。**
    用总条数当分母会把两个不相干的量混起来。
    """
    recall = Counts()
    false_keep = Counts()

    for msg_id, kept in decisions.items():
        exp = expects.get(msg_id)
        if exp is None:
            continue
        if exp.keep:
            recall.total += 1
            if kept:
                recall.hit += 1
        else:
            false_keep.total += 1
            if kept:
                false_keep.hit += 1

    return recall, false_keep


def scrub_metrics(
    *,
    flagged_numbers: set[str],
    flagged_names: set[str],
    expects: dict[int, Expect],
) -> tuple[Counts, Counts]:
    """脱敏的召回与误伤。

    ※ `flagged_*` 是**真的被抹掉的字符串集合**（由调用方从脱敏结果里收集）。
    """
    recall = Counts()
    fp = Counts()
    want_nums: set[str] = set()
    want_names: set[str] = set()
    for exp in expects.values():
        want_nums.update(exp.numbers)
        want_names.update(exp.names)

    for n in want_nums:
        recall.total += 1
        if n in flagged_numbers:
            recall.hit += 1
    for n in want_names:
        recall.total += 1
        if n in flagged_names:
            recall.hit += 1

    # 误伤：抹掉的东西里，真值没标过的那些
    marked = want_nums | want_names
    for g in flagged_numbers | flagged_names:
        if g not in marked:
            fp.total += 1
            fp.hit += 1
    return recall, fp


def model_metrics(
    *,
    produced: list[dict[str, Any]],
    expects: dict[int, Expect],
    sent_msg_ids: set[int],
    eligible_msg_ids: set[int],
) -> MetricsReport:
    """从抽取产出算模型指标。

    `produced`：每条形如
        `{"msg_ids": [...], "kind": str|None, "has_deadline": bool,
          "has_start": bool, "unsupported_fields": int, "fields": int}`
    —— 由调用方从真实产出里攒出来。
    """
    report = MetricsReport()
    report.corpus_size = len(expects)

    # 坑 1：覆盖率的分母是**该送的**，分子是**真的送了的**。
    # ※ 分子必须是分母的**子集**。`sent_msg_ids` 是「库里条目引用过的
    # 全部消息」—— 别的源进来的消息也在里面，而它们不在 `eligible`。
    # 直接拿两个集合的规模相除，比值会超过 100%。
    report.coverage.total = len(eligible_msg_ids)
    report.coverage.hit = len(sent_msg_ids & eligible_msg_ids)

    # 按真值分组
    by_msg: dict[int, dict[str, Any]] = {}
    for p in produced:
        for mid in p.get("msg_ids", []):
            by_msg[mid] = p

    for mid, exp in expects.items():
        if exp.kind is None:
            continue
        if mid not in sent_msg_ids:
            # ※ 没送进模型的**不进任何模型指标的分母**（坑 1）。
            continue
        p = by_msg.get(mid)
        if p is None:
            # 送了但没产出条目 ⇒ 记「漏抽」，进类目与抽取的分母（**进**！
            # 因为这是模型真的漏了，而不是没跑到）。
            report.kind_accuracy.total += 1
            if exp.deadline_text:
                report.deadline_recall.total += 1
            if exp.start_text and not exp.start_not_inferable:
                report.start_recall.total += 1
            continue

        if exp.kind:
            report.kind_accuracy.total += 1
            if p.get("kind") == exp.kind:
                report.kind_accuracy.hit += 1

        if exp.deadline_text:
            report.deadline_recall.total += 1
            if p.get("has_deadline"):
                report.deadline_recall.hit += 1

        if exp.start_text and not exp.start_not_inferable:
            report.start_recall.total += 1
            if p.get("has_start"):
                report.start_recall.hit += 1
        elif exp.start_not_inferable:
            # ※ **坑 3**：源文只给了相对时间 ⇒ 这一格本来就填不进。
            # 报出来，但不进分母。
            report.excluded_not_applicable += 1

        # 坑 4：幻觉率的分母是**所有被检查过的输入**（这里就是所有送进模型
        # 的），分子是那些无依据的字段。
        n_fields = int(p.get("fields") or 0)
        if n_fields:
            report.hallucination.total += n_fields
            report.hallucination.hit += int(p.get("unsupported_fields") or 0)

    return report
