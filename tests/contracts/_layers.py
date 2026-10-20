"""分层扫描器 —— 供 `tests/contracts/` 下的判据共用。

※ 这个模块**只做扫描**，不做判断。判断在各自的测试里，因为每条判据的
"为什么"不一样，混在一起以后没人说得清哪条在守什么。

## 为什么用 AST 而不是正则

正则扫 `import` 会被三件事骗过去：
  1. 字符串里的 `"import sqlite3"`（注释与文档里到处都是）
  2. `from x import (a,\n b)` 的跨行写法
  3. `if TYPE_CHECKING:` 里的导入 —— 它**运行时确实不执行**，所以它是最像
     "没写"的一条反向依赖：读代码看不见，跑起来也没事，但它已经把两层的
     边界写进文件了。正则扫不出来，AST 扫得出来。

## 为什么层要从**路径**推，不从包名猜

`yechao.domain.scrub` 里的 `domain` 是路径的第二段。用路径推，就不存在
"有人把 `domain` 拼进别的包名里"这种误判。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "yechao"

LAYERS: tuple[str, ...] = ("domain", "application", "infrastructure", "interfaces")

# 根层的文件（不属于任何一层）。它们各自有单独的判据。
ROOT_MODULES: tuple[str, ...] = ("bootstrap.py", "cli_entry.py")


@dataclass(frozen=True)
class Module:
    """一个 .py 文件在"导入图"里的样子。"""

    name: str  # 点分模块名，如 "yechao.domain.scrub"
    path: Path
    layer: str | None  # None = 根层文件（bootstrap.py 等）
    imports: frozenset[str]  # 它导入的所有点分模块名（含 stdlib 与第三方）
    is_package_init: bool

    @property
    def is_nontrivial(self) -> bool:
        """※ `__init__.py` 不算 —— 见 `scan()` 的说明。"""
        return not self.is_package_init


def _module_name(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_relative(name: str, level: int, module: str, is_init: bool) -> str:
    """把 `from .foo import bar` 解析成绝对模块名。

    level=1 表示"当前包"，level=2 表示"上一层包"。当前包 = 去掉最后一段
    （非 __init__ 文件）或就是自身（__init__ 文件）。
    """
    parts = name.split(".")
    if is_init:
        base = parts
    else:
        base = parts[:-1]
    # level=1 指向 base 本身；每多一级向上走一段
    up = level - 1
    if up > len(base):
        return module  # 越过顶层了，不合法但不当成扫描器的问题
    base = base[: len(base) - up] if up else base
    if module:
        base = base + module.split(".")
    return ".".join(base)


def _extract_imports(tree: ast.AST, mod_name: str, is_init: bool) -> frozenset[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入
                resolved = _resolve_relative(
                    node.module or "", node.level, mod_name, is_init
                )
                if resolved:
                    out.add(resolved)
                    for alias in node.names:
                        if alias.name != "*":
                            out.add(f"{resolved}.{alias.name}")
            elif node.module:
                out.add(node.module)
                for alias in node.names:
                    if alias.name != "*":
                        out.add(f"{node.module}.{alias.name}")
    return frozenset(out)


def _layer_of(mod_name: str) -> str | None:
    parts = mod_name.split(".")
    if len(parts) < 2 or parts[0] != "yechao":
        return None
    if parts[1] in LAYERS:
        return parts[1]
    return None


def scan(package_root: Path = PACKAGE_ROOT) -> list[Module]:
    """扫全部 .py，返回每个模块的导入图。

    ※ `__init__.py` 被标出来但**不排除** —— 调用方自己决定要不要算它。
    排除它的理由：一个只有 `__init__.py` 的层，会让"这一层有东西可守"
    看起来成立，而实际上判据扫的是零个有意义的文件。
    """
    mods: list[Module] = []
    for path in sorted(package_root.rglob("*.py")):
        # 构建产物 / 缓存里的 .py 不算项目代码
        if any(part in {"__pycache__", ".venv", "node_modules"} for part in path.parts):
            continue
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))
        name = _module_name(path)
        is_init = path.name == "__init__.py"
        mods.append(
            Module(
                name=name,
                path=path,
                layer=_layer_of(name),
                imports=_extract_imports(tree, name, is_init),
                is_package_init=is_init,
            )
        )
    return mods


def nontrivial_by_layer(mods: list[Module]) -> dict[str, list[Module]]:
    """每层的**非空壳**文件。空转自检读的就是这个。"""
    out: dict[str, list[Module]] = {layer: [] for layer in LAYERS}
    for m in mods:
        if m.layer and m.is_nontrivial:
            out[m.layer].append(m)
    return out


def yechao_imports(mod: Module) -> set[str]:
    """这个模块导入了哪些 `yechao.*` 模块（含自身层）。"""
    return {
        imp
        for imp in mod.imports
        if imp == "yechao" or imp.startswith("yechao.")
    }


def layers_imported_by(mod: Module) -> set[str]:
    """这个模块导入了哪几层（按层名，去重）。"""
    return {_layer_of(imp) for imp in yechao_imports(mod)} - {None}


def top_level_package(mod_name: str) -> str:
    """点分模块名的顶层包。`re` → `re`；`os.path` → `os`；`yechao.domain` → `yechao`。"""
    return mod_name.split(".")[0]
