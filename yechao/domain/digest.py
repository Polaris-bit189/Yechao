"""日报渲染 —— 把窗口内的条目组装成一份按天可读的简报纸。

## 要防什么

**没依据的截止日不许进模型。** 这是日报这块最承重的一句：

> 只在显示端拦是不够的 —— 字段还在输入里，模型就会自己把那个日期写进正文，
> 读者看到的还是同一个瞎编的数。**它看不到，才写不出来。**

⇒ 渲染前**必须**把 `deadline_supported` 为假的那些条目的 `deadline_ts`
**从送给模型的输入里摘掉**（不是清字段 —— 是不让它出现在那一份文本里）。

※ 注意顺序：核验闸门（`verify.py`）已经**清过**一次 `deadline_ts` 了。
本模块这一步防的是**另一件事**：闸门判「不适用」的那些（保留了原值），
以及人工录入的条目（没走过闸门）。两者都要在送给模型前被摘掉。

## 摘要归行：由机械行兜底

模型合成失败（整批超时、输出不合法）时，日报**不能是空的** ——
那会让用户以为那天没事。所以每条目都有**机械生成的行**兜底：
`标题 · 地点 · 群名`。

※ 模型行与机械行的**区分**要留在数据里（不是只在渲染时判），
因为「这一行是不是模型写的」是评测要问的问题。

## 为什么分节而不是一坨列表

按**类目**分节，按**截止日紧迫度**排序。理由是产品定位决定的：
**一屏读完，一分钟以内**。
一坨平铺的列表读起来是「翻」，分节是「读」。
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field

from yechao.domain.days import Clock, deadline_level, deadline_text
from yechao.domain.models import Digest, Item

#: 一篇日报最多引用的条目数。
#: ※ 这不是「分页」—— 超出的**要记账**，不能静默丢。
#: 「静默丢数据」的形状是「用户不知道有东西没进来」。
MAX_ITEMS_PER_DIGEST = 60

#: 摘要的字符上限。前端 `DigestSummary.preview` 的注释说 ≤120 字。
PREVIEW_CHARS = 120



@dataclass
class DigestLine:
    """日报里的一行。"""

    item_id: int
    text: str
    #: ※ 这一行是**模型合成的**还是**机械拼的**。
    #: 这个字段必须留着：评测要问「模型写了几行、其中几行对上了来源」。
    by_model: bool = False


@dataclass
class DigestSection:
    kind: str
    label: str
    lines: list[DigestLine] = field(default_factory=list)


@dataclass
class DigestDraft:
    """渲染前的日报草稿 —— 还没落库的形态。"""

    day: str
    window_from: int
    window_to: int
    sections: list[DigestSection]
    #: 窗口内的原始条数（含没进日报的）。给「N 条消息 / M 条条目」那句用。
    total_items: int = 0
    #: 因为超出条数上限而没进日报的。
    omitted: int = 0

    @property
    def line_count(self) -> int:
        return sum(len(s.lines) for s in self.sections)

    @property
    def by_model(self) -> int:
        return sum(1 for s in self.sections for ln in s.lines if ln.by_model)


# ── 送给模型的输入 ──────────────────────────────────────────────────


def build_model_input(items: list[Item], clock: Clock) -> str:
    """组装**送给模型**的日报素材。

    ※ **这里是那条判据的落点**：没依据的截止日**不许出现**在返回的文本里。

    做法：对每条条目，只在 `deadline_supported` 为真时才输出截止日那一行。
    人工录入的条目（没走过闸门）`deadline_supported` 恒为假 ⇒ 它们的截止日
    也不会进去。

    ※ 别改成「输出全部、只在渲染时判」—— 那正是这条判据要防的事。

    ## 每行为什么**必须**带编号

    模型要按 `item_id` 回填，而**它只能照抄输入里给的东西**。原来这里只给了
    类目 slug，没有编号 —— 于是模型没有一个可以照抄的数，回填出来的编号
    永远对不上 `{it.item_id for it in items}`，每一行都被丢掉、退化成机械行。

    ※ 而那个失败**是看不见的**：机械行兜底让日报照样有内容，`ledger` 里的
    `digest_line_unmatched` 也没有专门的出口。整条「本地模型写日报」的路
    等于没跑，而报告上一切正常。

    ⇒ 形如 `- [12] [todo] 标题`：**第一个方括号是编号**（要原样回填的），
    第二个是类目 slug（给模型当上下文用，不写进正文）。两个都由
    `test_素材里的编号与提示词约定一致` 钉住。
    """
    lines: list[str] = []
    for it in items:
        parts = [f"- [{it.item_id}] [{it.kind}] {it.title}"]
        if it.detail:
            parts.append(f"  详情：{it.detail}")
        # ※ 截止日：**只有过了闸门的才写进来**
        if it.deadline_ts is not None and it.deadline_supported:
            d = deadline_text(clock.days_apart(it.deadline_ts, it.event_ts))
            parts.append(f"  {clock.day_of(it.deadline_ts)} 截止")
        if it.start_ts is not None and it.start_supported:
            parts.append(f"  开始：{clock.day_of(it.start_ts)}")
        if it.place:
            parts.append(f"  地点：{it.place}")
        if it.amount:
            parts.append(f"  金额：{it.amount}")
        parts.append(f"  来源群：{it.group_name}")
        lines.append("\n".join(parts))
    return "\n".join(lines)


# ── 机械行 ──────────────────────────────────────────────────────────


def mechanical_line(item: Item, clock: Clock) -> DigestLine:
    """**机械拼**的一行。模型合成失败时的兜底。

    ※ 它必须**永远能产出** —— 不能因为任何字段缺失而抛。
    兜底的东西坏了，就没有兜底了。
    """
    bits = [item.title]
    if item.place:
        bits.append(item.place)
    if item.deadline_ts is not None and item.deadline_supported:
        txt = deadline_text(clock.days_apart(item.deadline_ts, item.event_ts))
        bits.append(txt or clock.day_of(item.deadline_ts))
    bits.append(item.group_name)
    return DigestLine(
        item_id=item.item_id, text=" · ".join(b for b in bits if b), by_model=False
    )


# ── 排序 ────────────────────────────────────────────────────────────


def _sort_key(item: Item, clock: Clock, now: int) -> tuple[int, int, int]:
    """日报内的排序。

    **紧迫的排前面，然后按时间倒序。**
    ※ 紧迫度按**本地日**算（`clock`），而且是相对于**这一天**（`day_start`）
    而不是「此刻」—— 因为日报是**一天一次**生成的，它的排序不该随生成时刻
    漂移（否则同一天的日报在早上生成与晚上生成，顺序不同）。
    """
    urgency = 9
    if item.deadline_ts is not None and item.deadline_supported:
        days = clock.days_apart(item.deadline_ts, now)
        lvl = deadline_level(days)
        urgency = {"expired": 0, "urgent": 1, "soon": 2, "normal": 3}.get(lvl, 4)
    return (urgency, -item.event_ts, item.item_id)


# ── 组装 ────────────────────────────────────────────────────────────


def draft(
    items: list[Item],
    *,
    day: str,
    clock: Clock,
    kinds: dict[str, str] | None = None,
    summaries: dict[int, str] | None = None,
) -> DigestDraft:
    """组装日报草稿。

    `summaries`：`{item_id: 模型写的那一行}`。**没有它也能跑** —— 缺的那些
    用机械行兜底。这是「模型挂了日报也不能空」的落点。

    ※ `omitted` 要如实算。超上限被丢掉的那部分**必须记账** ——
    「静默丢数据」的形状与「今天就只有这么多」一模一样。
    """
    kinds = kinds or {}
    summaries = summaries or {}
    now = clock.day_start_from_str(day)

    eligible = [i for i in items if not i.deleted]
    eligible.sort(key=lambda i: _sort_key(i, clock, now))

    omitted = max(0, len(eligible) - MAX_ITEMS_PER_DIGEST)
    chosen = eligible[:MAX_ITEMS_PER_DIGEST]

    buckets: dict[str, list[Item]] = {}
    for it in chosen:
        buckets.setdefault(it.kind, []).append(it)

    sections: list[DigestSection] = []
    # ※ **分节顺序跟着配置里类目的顺序走，不写死一张表。**
    #
    # 这里原来是一张常量表（`SECTION_ORDER`），后果有两个，而且都是**静的**：
    #   · 用户重排类目、或者新加一个类目 ⇒ 日报的分节顺序不动。日报照样出得来，
    #     只是顺序不是他要的 —— 没有任何一处会红。
    #   · 类目表改名之后，那张表就成了**一张没人对得上的旧表**：所有分节都掉进
    #     下面那个「不在表里」的回落分支，按字母序排。
    #
    # `kinds` 是 `{slug: label}`，由配置**按顺序**拼出来（见
    # `application/digest.py`），所以它的键序就是用户的顺序。
    # 不在配置里的 slug（用户删了类目、条目还挂着）排最后，按字母序保证确定性。
    ordered = [k for k in kinds if k in buckets]
    ordered += sorted(k for k in buckets if k not in kinds)

    for kind in ordered:
        label = kinds.get(kind, kind)
        sec = DigestSection(kind=kind, label=label)
        for it in buckets[kind]:
            text = summaries.get(it.item_id)
            if text:
                sec.lines.append(DigestLine(it.item_id, text, by_model=True))
            else:
                sec.lines.append(mechanical_line(it, clock))
        sections.append(sec)

    return DigestDraft(
        day=day,
        window_from=now,
        window_to=now + 86400,
        sections=sections,
        total_items=len(eligible),
        omitted=omitted,
    )


# ── Markdown 渲染 ───────────────────────────────────────────────────


def render_markdown(d: DigestDraft, *, stats: str = "") -> str:
    """渲染成 Markdown。

    ※ 正文的**第一段**是日报的概览句 —— 前端的 `DigestSummary.preview`
    就是它的截断版，而前端靠「第一段与 preview 开头一致」来把这一段从正文里
    剥掉（避免重复显示）。
    ⇒ 第一段必须**短**（≤ `PREVIEW_CHARS`），而且必须是**纯文本**
    （不能有 markdown 记号，否则剥不干净）。
    """
    out: list[str] = []
    # ※ 标题只写**日期**，不写品牌。
    # 品牌由页面框架负责 —— 正文是 Markdown，同一句话在标题与页眉各写一遍
    # 没有必要；而且「{品牌}日报 · {日期}」这种模板会让换品牌变成一次全文替换。
    out.append(f"# {d.day}")
    out.append("")

    overview = _overview(d, stats)
    out.append(overview)
    out.append("")

    for sec in d.sections:
        out.append(f"## {sec.label}")
        out.append("")
        for ln in sec.lines:
            out.append(f"- {ln.text}")
        out.append("")

    if d.omitted:
        # ※ 超上限**必须说出来**，不能静默。用户看到「今天就只有这些」
        # 与「今天还有 N 条没进来」是两句完全不同的话。
        out.append(f"> 还有 {d.omitted} 条未列入本篇（超出单篇上限）。")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def _overview(d: DigestDraft, stats: str) -> str:
    """概览句。**纯文本**，≤ `PREVIEW_CHARS` 字。"""
    n = d.line_count
    if n == 0:
        return "这一天没有新的事项。"
    sec_names = "、".join(s.label for s in d.sections if s.lines)
    # ※ **先结论、后背景**：读者要知道的是「今天有几件事、分在哪几类」，
    # 而「从多大的盘子里来的」是限定语，挂在后面。
    # 反过来先报「N 个群 M 条消息」，读者要读到句末才知道今天到底有几条。
    text = f"{n} 条要点：{sec_names}"
    if stats:
        text += stats
    text += "。"
    if len(text) > PREVIEW_CHARS:
        text = text[: PREVIEW_CHARS - 1] + "…"
    return text


def extract_preview(body_md: str) -> str | None:
    """从正文里提取摘要 —— **第一段**，已剥 markdown 记号。

    ※ 与 `render_markdown` 的 `_overview` 是**一对**：渲染时怎么写，
    提取时就得怎么读。两边不一致的症状是「卡片上的摘要与正文第一段对不上」，
    而前端的剥除判据正是「前 12 个字一致」—— 对不上会**双重显示**。
    """
    for raw in body_md.split("\n\n"):
        para = raw.strip()
        if not para or para.startswith("#") or para.startswith(">"):
            continue
        # 剥 markdown 记号
        para = para.lstrip("*-+> ").strip()
        para = para.replace("**", "").replace("`", "")
        if not para:
            continue
        return textwrap.shorten(para, width=PREVIEW_CHARS, placeholder="…") if len(para) > PREVIEW_CHARS else para
    return None


def make_digest(
    d: DigestDraft,
    *,
    digest_id: int,
    created_at: int,
    stats: str = "",
) -> Digest:
    """草稿 → 落库形态。"""
    body = render_markdown(d, stats=stats)
    return Digest(
        digest_id=digest_id,
        day=d.day,
        window_from=d.window_from,
        window_to=d.window_to,
        created_at=created_at,
        body_md=body,
        item_count=d.line_count,
        preview=extract_preview(body),
    )


def digest_stats(
    *,
    groups: int,
    items: int,
    messages: int | None = None,
    extracted: int | None = None,
    eligible: int | None = None,
) -> str:
    """日报开头那句概览。

    ## 它要回答什么

    读者看到一份只有几条的日报，第一个问题往往是「今天群里明明很吵，
    怎么就这么点」。所以这句话给的是**漏斗**：从多少群、多少消息，
    最后变成几条。三组数各自回答一件事，混起来就会造出一句假话：

      · `messages` —— 窗口内的**原始消息**条数（「今天群里有多吵」）
      · `extracted / eligible` —— 真的被送进模型的 / 应当被送的。
        两者不能是同一个数：分母里混进「这次没跑完」的，会得出一个远低于
        实际的覆盖率，而真相是「没跑完，不是模型差」。
      · `items` —— 最终提炼出的条目数。它与消息数不是一回事：
        10 条消息可能提炼出 1 条，也可能提炼出 10 条。

    ## ※ 为什么后两组是**可选**的

    它们只有**抽取那一路**才知道；日报这一路手上没有。原来的调用方拿
    `len(items)` 把 `messages` / `extracted` / `eligible` 填成同一个数 ——
    于是这句话永远印着「送进模型 N/N」，读起来是「全都送了」，
    **而那是编的**。

    ⇒ **拿不到就不印。** 少一组数，好过多一组假数：
    「静默返回一个看起来合理的数」正是本项目最防的那种形状。
    """
    bits = [f"{groups} 个群"]
    if messages is not None:
        bits.append(f"{messages} 条消息")
    if extracted is not None and eligible is not None:
        bits.append(f"{extracted}/{eligible} 条送进模型")
    return "（" + "、".join(bits) + "）"
