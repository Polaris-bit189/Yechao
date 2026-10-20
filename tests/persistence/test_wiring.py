"""「能力写好了但没接线」—— 本项目最典型的一类失败。

## 这个文件为什么单独存在

失败：

> **接线存在但从未被调用。** 某个组件写完了、看起来完整、甚至有单元测试，
> 但**全项目没有任何地方实例化它**。它从不生效，而系统照常工作
> —— **没有任何报错**。

这个项目**真的踩到过一次**：`covered_msg_ids()` 在仓储里、有测试、
返回正确的集合 —— 而 `extract.run()` 从来没读过它。

后果两条，且**都不报错**：
  · 重跑 refine 会把已经抽过的消息再抽一遍 ⇒ 重复条目
  · 用户软删的条目会被重新抽出来 ⇒ 删掉的东西自己回来

## 为什么原来的测试没抓到

仓储层那条测试断言的是 **`covered_msg_ids()` 返回得对**。
它**没有**断言调用方用了它。

那正是那种测试抓不到的 bug：
**函数返回值对，调用方没用**。

⇒ 这个文件里的用例断言的是**行为链** —— 跑一遍、再跑一遍，看重跑会不会
产出新东西。它不关心某个函数返回什么，只关心**整条链路真的走了那一步**。
"""

from __future__ import annotations

import pytest

from yechao.application import extract as extract_uc
from yechao.domain.models import PendingMessage
from yechao.infrastructure.persistence import sqlite_store
from yechao.infrastructure.persistence.sqlite_store import SqliteStore
from yechao.infrastructure.runtime.audit_log import NullAuditLog
from yechao.infrastructure.runtime.clock import FixedClock


class _Cfg:
    """一份最小配置桩。

    ⚠️ 用桩而不是真配置：这条用例测的是**管线有没有走那一步**，
    与配置怎么读无关。真有配置参与进来，失败原因就会有两种可能。
    """

    def groups(self):
        from yechao.domain.config_models import GroupConfig

        return [GroupConfig(id=1, name="群1")]

    def persons(self):
        return []

    def categories(self):
        from yechao.domain.categories import default_categories
        from yechao.domain.config_models import CategorySet

        return CategorySet(categories=default_categories(), aliases={})

    def nicknames(self):
        return []


class _Model:
    """一个**永远给出同样答案**的模型桩，并记下被调了几次。"""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        self.calls += 1
        return (
            '[{"kind":"notice","title":"测试条目","quote":"这是一条测试消息",'
            '"detail":null,"deadline":null,"start":null,"place":null,"amount":null}]'
        )


class _Prompt:
    EXTRACT_SYSTEM = "system"
    EXTRACT_MAX_TOKENS = 1024

    def build_extract_user(self, messages, scrubbed):
        return "\n".join(scrubbed)


@pytest.fixture()
def store(tmp_path):
    db = tmp_path / "t.db"
    sqlite_store.init_db(db)
    return SqliteStore(sqlite_store.connect(db))


def _deps(store, model: _Model) -> extract_uc.ExtractDeps:
    return extract_uc.ExtractDeps(
        messages=store, items=store, model=model,
        audit=NullAuditLog(), clock=FixedClock(ts=2000),
        config=_Cfg(), prompt=_Prompt(),
    )


def _seed(store, msg_ids=(1,)) -> None:
    store.upsert_messages([
        PendingMessage(msg_id=i, group_id=1, ts=1000 + i, sender="某人",
                       content="这是一条测试消息，请于某日前提交")
        for i in msg_ids
    ])


def test_重跑抽取不会产出重复条目(store) -> None:
    """⚠️ **这条用例抓的就是那个真 bug。**

    判据是**行为链**：跑一次、再跑一次，第二次不该产出任何新条目，
    而且**根本不该调模型**（没有待处理的消息了）。
    """
    _seed(store)
    model = _Model()
    deps = _deps(store, model)

    first = extract_uc.run(deps)
    assert len(first.items) == 1, "第一次应当产出条目"
    calls_after_first = model.calls
    assert calls_after_first >= 1, "第一次应当调过模型"

    second = extract_uc.run(deps)
    assert second.items == [], (
        "第二次跑又产出了条目 —— 说明 `covered_msg_ids()` 没有被调用。"
        "那会让重跑产出重复条目，而软删的条目会自己回来。"
    )
    assert model.calls == calls_after_first, (
        "第二次跑**根本不该调模型** —— 没有待处理的消息了"
    )


def test_软删的条目不会被下一轮抽取复活(store) -> None:
    """⚠️ **不变量 ② 的第二半**：界面上消失，**且不会被下一轮抽取复活**。

    这一条与上面那条是同一条管线上的两件事：

      · 上面：**正常**条目的来源让消息算「已处理」
      · 这条：**软删**条目的来源**同样**让消息算「已处理」

    第二半更容易被漏 —— 因为一个直觉上「删掉了就不该再算数」的想法，
    恰好会让删掉的东西回来。
    """
    _seed(store)
    model = _Model()
    deps = _deps(store, model)

    first = extract_uc.run(deps)
    assert len(first.items) == 1
    item_id = first.items[0].item_id

    store.soft_delete(item_id, actor="web", ts=2100)
    rows, _ = store.search(limit=10)
    assert rows == [], "界面上应当看不到"

    third = extract_uc.run(deps)
    assert third.items == [], (
        "软删之后又被重新抽出来了 —— 用户删掉的东西自己回来了"
    )


def test_新消息仍然会被处理(store) -> None:
    """⚠️ **阴性对照**。

    上面两条断言「不重复处理」。如果没有这条，一个**永远不处理任何东西**
    的实现也能让它们通过 —— 而那个实现什么都没做成。
    """
    _seed(store, msg_ids=(1,))
    model = _Model()
    deps = _deps(store, model)

    assert len(extract_uc.run(deps).items) == 1

    # 来了一条新消息
    _seed(store, msg_ids=(2,))
    again = extract_uc.run(deps)
    assert len(again.items) == 1, "新消息没有被处理"
