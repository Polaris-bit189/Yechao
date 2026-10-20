"""两个源**必须一致**的那几件事。

## 这里放什么、不放什么

不是「公用的都往这儿扔」—— 只放**口径**：

  · 配置目录在哪、`sources.toml` 怎么读（`[web]` 与 `[mail]` 是**同一份文件**
    里的两张表，读它的人必须按同一套规矩读）
  · 时间窗是**半开区间** `[since, until)`

别的东西放进来，这个模块就会长成一个杂物抽屉；而杂物抽屉的代价是：下一个人
往里塞东西时不用再回答「它属于哪儿」。

## 为什么配置目录的默认值在这里**重新算了一次**

`bootstrap.py` 在模块级 import 本包（`from yechao.infrastructure.sources import ...`），
所以本包**不能**反过来 import `bootstrap` —— 那是导入环
（`tests/contracts/test_layering.py::test_没有导入环` 钉着这条）。

⇒ 默认值在这里算一次，它与 `bootstrap.Settings.from_env` 的那一份**必须一致**。
代价是「同一个默认值有两处」，用测试钉住
（`tests/infrastructure/test_web_source.py::test_配置目录的口径与组合根一致`
与 `tests/infrastructure/test_mail_source.py::test_配置目录的口径与组合根一致`
各钉一次 —— 两个源都依赖这个函数，所以两边都要能发现它漂了）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from yechao.domain.errors import ConfigError
from yechao.infrastructure.config.toml_store import load_toml

#: 配置目录的环境变量名。**必须与 `bootstrap.Settings.from_env` 一致**。
CONFIG_DIR_ENV = "YECHAO_CONFIG_DIR"
#: 配置目录的默认名。**必须与 `bootstrap.DEFAULT_CONFIG_DIR` 一致**。
DEFAULT_CONFIG_DIR = "config"
#: 数据源目标的文件名。与 `groups.toml` 等并列放在配置目录下。
SOURCES_FILE = "sources.toml"


def repo_root() -> Path:
    """仓库根。`_common.py` 的上三层（`sources/` → `infrastructure/` → `yechao/` → 仓库根）。"""
    return Path(__file__).resolve().parents[3]


def config_dir() -> Path:
    """配置目录。口径与 `bootstrap.Settings.from_env` 一致（见模块 docstring）。"""
    from_env = os.environ.get(CONFIG_DIR_ENV)
    if from_env:
        return Path(from_env)
    return repo_root() / DEFAULT_CONFIG_DIR


def sources_path() -> Path:
    """`<config_dir>/sources.toml` —— 数据源的目标写在这一份文件里。"""
    return config_dir() / SOURCES_FILE


def source_table(name: str, *, hint: str) -> dict[str, Any]:
    """读 `sources.toml` 的 `[name]` 表。**没有这张表**就返回 `{}`。

    ※ 复用 `toml_store.load_toml` 而不是自己 `tomllib.load`：那个函数已经钉住了
    「**文件不存在**与**读不出来**是两件事」（前者返回 `{}`，后者抛）。
    自己再写一遍就是两处口径，而漂移的症状是「配置文件写坏了却被当成没配」。

    ※ 表在、但不是一张表（比如写成了 `web = "..."`）⇒ **抛**。它几乎总是
    「少写了一对方括号」，而把它当成「没配」的症状是「我明明配了，它说没配」——
    一句让用户去查错方向的话。`hint` 是给用户照抄的一段配置示例，由调用方给
    （只有它知道这张表该长什么样）。
    """
    path = sources_path()
    data = load_toml(path)
    table = data.get(name)
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise ConfigError(
            f"{path} 里的 [{name}] 不是一张表 —— 它应当是 `[{name}]` 开头的一段，"
            f"例如：\n{hint}"
        )
    return table


def in_window(ts: int, since: int | None, until: int | None) -> bool:
    """半开区间 `[since, until)`。**两个源共用这一条口径。**

    ※ 半开是刻意的：相邻两天用 `[d1, d2)` 与 `[d2, d3)` 拼起来不重不漏。
    闭区间会让 00:00:00 那一刻的消息同时落进两天里。
    """
    if since is not None and ts < since:
        return False
    if until is not None and ts >= until:
        return False
    return True
