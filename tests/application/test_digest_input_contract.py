"""日报素材 ↔ 提示词 ↔ 回连 —— 三处之间的**一条契约**。

※ 这个文件只守一件事：**模型能照抄回填的那个编号，素材里真的给了。**

为什么值得单独一个文件：这条契约有三处落点（`domain/digest.build_model_input`
产出素材、`application/prompts.DIGEST_SYSTEM` 要求模型回填、
`application/digest.run` 按 `k in valid_ids` 过滤），而三处**各自看都对**。

断掉之后的症状是**沉默的**：模型回填的编号对不上，每一行都被丢掉、
退化成机械行 —— 而机械行兜底让日报照样有内容，报告上一切正常。
「本地模型写日报」这一整条路等于没跑，却没有一处会红。
"""

from __future__ import annotations

import re

from yechao.domain.days import DEFAULT_CLOCK
from yechao.domain.digest import build_model_input
from yechao.domain.models import Item


def _item(item_id: int, kind: str, title: str) -> Item:
    return Item(
        item_id=item_id,
        kind=kind,
        kind_label="",
        title=title,
        detail=None,
        event_ts=1793000000,
        deadline_ts=None,
        group_id=1,
        group_name="青梧摄影社",
        actor=None,
        actor_account=None,
        place=None,
        amount=None,
        links=(),
        source_count=1,
        start_ts=None,
        deadline_supported=False,
        start_supported=False,
    )


def test_素材里的编号就是条目的编号_模型照着抄就能回连() -> None:
    items = [_item(12, "todo", "实验室安全考核报名"), _item(13, "attend", "摄影社招新宣讲")]

    material = build_model_input(items, DEFAULT_CLOCK)

    # ※ 判据写成「素材里给出的编号集合 == 条目自己的编号集合」。
    # 不断言某一行的完整形状 —— 那会让改排版变成改测试。
    given = {int(m) for m in re.findall(r"^-\s*\[(\d+)\]", material, re.M)}
    assert given == {it.item_id for it in items}, (
        "素材里没有给出模型可以照抄回填的编号 —— "
        "回连会整批对不上，而它被机械行兜底掩盖，不会报错"
    )


def test_提示词说的编号约定与素材实际给的对得上() -> None:
    """※ 素材改排版时提示词必须跟着改，反过来也一样。

    这一条**不读素材**，只读提示词里那句约定 —— 两张抄本各钉一半，
    任何一半改了而另一半没改，这两条里总有一条会红。
    """
    from yechao.application.prompts import DIGEST_SYSTEM

    assert "两个方括号" in DIGEST_SYSTEM, (
        "提示词没有说清每行开头有两个方括号（编号 + 类目 slug）—— "
        "模型会把类目 slug 当成要回填的值"
    )
    assert "item_id" in DIGEST_SYSTEM and "照抄" in DIGEST_SYSTEM


def test_素材里类目_slug_仍然给了_模型据此措辞() -> None:
    """编号之外，类目 slug **仍然要在** ——「只要知道」与「要办的事」的
    措辞完全不同（一个是陈述，一个是催办），模型需要这个信号。

    ※ 这条与上一条是同一处的两个方向：不能为了加编号把 slug 挤掉。
    """
    material = build_model_input([_item(7, "know", "校历变更")], DEFAULT_CLOCK)
    assert "[7]" in material
    assert "[know]" in material
