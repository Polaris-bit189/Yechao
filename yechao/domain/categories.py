"""类目体系 —— 七个默认类目，以及「类目判定看什么」的判据。

## 类目判据看**这条消息要读者做什么**，不看它出现了哪个词

这是本模块最重要的一句。

例：一条消息写着「**讲座**通知：请于周五前**报名**」。
  · 看词：「讲座」→ 活动；「报名」→ 学业。两个词都在，判哪个？
  · 看**要读者做什么**：它要读者**去报名**（一件有截止的事）⇒ **学业**。

再例：「社团**招新**，欢迎**报名**参加」。
  · 要读者做什么：去参加一个活动（招新摆摊）⇒ **活动**。

⇒ 每条类目的 `desc` 是**判据**，写成「这条消息要读者做什么」，而不是
「含哪些词」。

⚠️ 这个 `desc` 会**逐字进提示词**（`web/src/types.ts` 的 `CategoryDetail.desc`
注释：「类目判据，会逐字进提示词」）。所以它的措辞是**给模型看的**，
要写成可执行的判据，不要写成给人看的简介。

## 为什么默认类目在后端而不是前端

前端**没有**写死类目表 —— 它全部从 `/api/categories` 来（`kind_label` /
`kind_icon` 也是后端拼好塞进每个 `Item` 的）。理由：
展示字段前后端各拼一次，漂移之后的症状是「卡片上有类目名、详情页没有」——
两边代码单看都正常。

⚠️ 但前端的**图标与颜色**是按 slug 映射的**前端决定**（`kindIcon.tsx` /
`kindColor.ts`），不是后端字段 —— 那两张表刻意不来自后端，因为
「把前端库的命名写进用户数据」会在换图标库那天让用户数据报废。

## 为什么 `icon` 是 emoji 而不再是 lucide 名

前端当前**全站零 emoji**（那一轮视觉重构的硬要求）。所以 `icon` 字段在本版
**只用于未知 slug 的回落**（用户在网页上新建类目时填的 emoji）。

⚠️ 它仍然必须在契约里、必须能被编辑 —— 因为它是**用户数据**，去掉字段会
让已有的 `categories.toml` 读不出来。
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Category:
    """一个类目。字段名与前端 `CategoryDetail` 逐个对应。"""

    slug: str
    label: str
    #: 用户可编辑的 emoji。当前版本只在「未知 slug 回落」时才会显示。
    icon: str
    #: **判据**，逐字进提示词。写成「要读者做什么」。
    desc: str


#: 七个默认类目。顺序**就是**界面上「类目」页的排列顺序。
DEFAULT_CATEGORIES: tuple[Category, ...] = (
    Category(
        slug="notice",
        label="通知公告",
        icon="📋",
        desc=(
            "要读者知道某个安排，通常没有要求读者立刻动手。"
            "例如校历变更、放假安排、系统维护、政策说明。"
        ),
    ),
    Category(
        slug="academic",
        label="学业",
        icon="📚",
        desc=(
            "要读者**去办一件有截止时间的手续**。"
            "例如选课、报名考试、提交作业或材料、申请缓考、缴费注册。"
            "判据是「有一件必须办的事 + 一个截止时间」，"
            "而不是消息里出现了「课」「考试」这些词。"
        ),
    ),
    Category(
        slug="activity",
        label="活动",
        icon="🎤",
        desc=(
            "要读者**去参加一个在特定时间地点发生的活动**。"
            "例如讲座、比赛、晚会、招新、展览、运动会。"
            "判据是「有一个开始时刻 + 一个地点 + 去参加」，"
            "而不是消息里出现了「活动」这个词。"
        ),
    ),
    Category(
        slug="life",
        label="生活",
        icon="🏠",
        desc=(
            "与校园日常生活有关的信息，既不是要办的手续也不是要参加的活动。"
            "例如食堂开饭时间、宿舍水电、班车时刻、周边店铺、天气提醒。"
        ),
    ),
    Category(
        slug="secondhand",
        label="二手",
        icon="🛒",
        desc=(
            "要读者**买卖或转让物品**。例如出闲置、求购、拼单、代购。"
            "通常带有价格。"
        ),
    ),
    Category(
        slug="lostfound",
        label="失物招领",
        icon="🔍",
        desc=(
            "要读者**认领或归还失物**。例如捡到校园卡、丢失雨伞、寻找失主。"
            "判据是「有一件东西丢了或捡到了 + 要找它的主人或失主」。"
        ),
    ),
    Category(
        slug="job",
        label="兼职招聘",
        icon="💼",
        desc=(
            "要读者**去应聘或参与一个有报酬/有岗位性质的工作**。"
            "例如勤工助学、实习招聘、兼职、志愿服务招募。"
        ),
    ),
)


def default_categories() -> list[Category]:
    return list(DEFAULT_CATEGORIES)


def by_slug(cats: list[Category] | tuple[Category, ...]) -> dict[str, Category]:
    return {c.slug: c for c in cats}


#: 未知类目的回落展示。⚠️ 用 slug 本身当 label，**不编一个**中文名 ——
#: 编出来的名字会让用户以为系统认识这个类目。
def fallback_label(slug: str) -> str:
    return slug


#: 未知类目的回落图标。前端有它自己的一套（按 slug 映射 lucide 图标，
#: 未知回落到 `Tag`），所以这个值只在**后端自用**（日志、CLI 输出）时出现。
FALLBACK_ICON = "🏷️"


def resolve_kind(
    slug: str, cats: dict[str, Category]
) -> tuple[str, str, str]:
    """把 slug 解析成 `(label, icon, 真实 slug)`。

    ⚠️ 未知 slug **不报错、也不替换成别的类目** —— 它照原样展示。
    理由：用户可能改过 slug 而别名表还没生效；把一条真实存在的条目藏起来，
    比显示一个陌生的 slug 更坏（用户会以为数据丢了）。
    """
    c = cats.get(slug)
    if c is None:
        return fallback_label(slug), FALLBACK_ICON, slug
    return c.label, c.icon, c.slug


# ── 别名（改名留痕）────────────────────────────────────────────────


def apply_aliases(slug: str, aliases: dict[str, str]) -> str:
    """把旧 slug 换成新 slug。**跟着链走**，但要防环。

    ⚠️ 别名表是 `{旧 → 新}`。用户连续改两次名（`a → b → c`）会留下
    `{a: b, b: c}`，所以要**追到底**才能得到 `c`。

    ⚠️ 防环的理由：手改 `categories.toml` 造出 `{a: b, b: a}` 是可能的，
    而一个不设终止条件的 `while` 会在那里**挂死**（表现为「网页打不开」，
    排查方向完全错）。
    """
    seen: set[str] = set()
    cur = slug
    while cur in aliases and cur not in seen:
        seen.add(cur)
        cur = aliases[cur]
    return cur


def rename(
    cats: list[Category], aliases: dict[str, str], old: str, new: str
) -> tuple[list[Category], dict[str, str]]:
    """改名。返回新的 `(类目表, 别名表)`。

    ⚠️ **条目的 `item_id` 不变**，改名走的是**别名**（软删不变量的延伸：
    物理改条目会让「撤销需要原来是什么」无从下手）。
    """
    out: list[Category] = []
    for c in cats:
        out.append(replace(c, slug=new) if c.slug == old else c)
    new_aliases = dict(aliases)
    new_aliases[old] = new
    # 指向 old 的那些旧名字，现在指向 new（跟着链走）
    for k, v in list(new_aliases.items()):
        if v == old:
            new_aliases[k] = new
    return out, new_aliases
