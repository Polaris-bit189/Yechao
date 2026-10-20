"""配置的形状 —— 群、人物、模型设置。

⚠️ **字段名是契约**：`groups.toml` 的 `id` / `name` /
`tier`。改了就失效 —— 用户的配置文件会读不出来。

## 为什么这些放在 domain 而不是 infrastructure

它们是**值对象**：没有 I/O，只有形状与少量判据（「tier 只许是这三个值」
「uin 必须是正整数」）。而形状是各层共用的 —— 适配层读出来、用例层用它、
领域层判它。

⚠️ 而 `load_toml` / `_atomic_write` 那些**在适配层** —— 它们碰文件。
这条分界正是分层护栏在守的东西。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from yechao.domain.categories import Category

#: ⚠️ `tier` 是**闭集**。三个取值：
#:   · `high` —— 通知群。消息**无条件保留**（预筛不过）。
#:   · `normal` —— 一般群。
#:   · `low` —— 二手/水群。**不额外保留**（但也不额外丢）。
#: 见 `domain/prefilter.py::TIER_KEEP`。
VALID_TIERS: frozenset[str] = frozenset({"high", "normal", "low"})


@dataclass(frozen=True)
class GroupConfig:
    """一个被监视的群。

    ⚠️ `name` **可以是空串** —— 那是给「快照里名字取不到」的群留的路。
    服务端**不许**对空名做校验（前端注释逐字：「`collect` 刻意留 `""`，
    无名群必须能原样加进来，所以**服务端不许收紧**」）。
    """

    id: int
    name: str = ""
    tier: str = "normal"
    enabled: bool = True
    #: 可选的成员昵称表 —— 姓名脱敏的名单来源之一。
    members: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.tier not in VALID_TIERS:
            # ⚠️ 抛而不是回落成 `normal`。回落的症状是「我把这个群标成
            # 高优了但它还是被筛掉了」—— 而用户会去查消息，不会来查拼写。
            from yechao.domain.errors import ValidationError

            raise ValidationError(
                f"群 {self.id} 的 tier={self.tier!r} 不认识 —— "
                f"只允许 {sorted(VALID_TIERS)}",
                field="tier",
            )
        if self.id <= 0:
            from yechao.domain.errors import ValidationError

            raise ValidationError(
                f"群号必须是正整数（{self.id} 不是）", field="id"
            )


@dataclass(frozen=True)
class PersonConfig:
    """一个被监视的人物。

    ⚠️ `uin` 是 QQ 号，**必须正整数** —— 「0 不是有效账号」
    （前端逐字）。这一条在前后端各有一份：前端做**即时反馈**，
    后端做**真正的守门**（前端校验可以被绕过）。
    """

    uin: int
    label: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.uin <= 0:
            from yechao.domain.errors import ValidationError

            raise ValidationError(
                f"QQ 号必须是正整数（{self.uin} 不是有效账号）", field="uin"
            )


@dataclass(frozen=True)
class LlmConfig:
    """模型设置。

    ⚠️ **没有密钥字段** —— 密钥走环境变量（`.env`），不在这份配置里。
    所以前端也没有密钥输入框。把它加进来的那一刻，导出配置就会带上它。
    """

    base_url: str = ""
    model: str = ""
    #: ⚠️ **三态**。`None` = 不发这个字段。
    enable_thinking: bool | None = None

    @property
    def configured(self) -> bool:
        """配好了吗。**空 = 没配** —— 不试着一个默认地址（那会把
        「没配」变成「连不上」，排查方向完全错）。"""
        return bool(self.base_url.strip() and self.model.strip())

    def normalized(self) -> "LlmConfig":
        """`trim()` 之后的自己。

        ⚠️ 写侧归一化**必须看得见它的结果**（前端逐字）：
        PUT 的响应要回归一化后的值，否则下一次 GET 回来会「变样」。
        """
        return LlmConfig(
            base_url=self.base_url.strip(),
            model=self.model.strip(),
            enable_thinking=self.enable_thinking,
        )


@dataclass(frozen=True)
class CloudConfig(LlmConfig):
    """**云端二次处理**的端点。

    ⚠️ 它继承 `LlmConfig` 是因为**形状完全相同**（endpoint + model +
    三态的 thinking），而不是因为它们是一回事。分成两个类型/两段配置的
    理由是**边界**：

      · 本地模型看的是**原文**（只在本机内存里）
      · 云端模型只看**脱敏后的结构化条目**

    这两件事的隐私含义完全不同。混成一个 `[llm]` 段的话，
    「这个地址收到的是什么」就再也说不清了 —— 而那正是本项目最核心的
    一个主张。

    ⇒ 所以配置里是**两段**：`[llm]`（本地）与 `[cloud]`（云端），
    而两者都可以不配。云端不配 ⇒ 二次处理这一步跳过，其余照常。
    """

    pass


@dataclass(frozen=True)
class CategorySet:
    """类目表 + 别名表。

    为什么是**一个**对象而不是两个参数：它们必须**同时**被替换
    （改名时两张表都变），分开传会让「改了表没改别名」变成一个说得通的状态。
    """

    categories: list[Category] = field(default_factory=list)
    #: `{旧 slug: 新 slug}` —— 改过名的类目在这里留痕。
    aliases: dict[str, str] = field(default_factory=dict)

    def by_slug(self) -> dict[str, Category]:
        return {c.slug: c for c in self.categories}

    def labels(self) -> dict[str, str]:
        return {c.slug: c.label for c in self.categories}
