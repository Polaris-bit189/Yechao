"""分层护栏 —— 依赖方向必须只有一个：外 → 内。

⚠️ **这些判据要先于被护栏的代码存在。** 写到后面再补，就会有一堆已经写歪的
代码要先修，而那时它们已经有用例依赖着了。

## 空转自检是什么，为什么必须有

一个扫描空列表的断言恒为真 —— 而它在报告里是绿的。**一个永远为真的断言
比没有断言更危险**：它给人「这一层被守住了」的错觉。

所以本文件里每一条判据都配一句 `_assert_not_vacuous(...)`：它断言**这次
实际扫到了足够多的、有意义的东西**。`__init__.py` 不算数 —— 只放空壳的层
会让「有东西可守」看起来成立，而实际上判据扫的是零个有意义的文件。

## 为什么白名单而不是黑名单

「不许 import `sqlite3` / `fastapi` / …」这种黑名单的漏洞是**它会随技术栈
增长而失效，而且失效是静默的**：新引一个 I/O 库，黑名单不认识它，判据放行。
白名单要求每加一项都得显式写下「为什么这层需要它」。

## ⚠️ 两条判据在写它们的过程中被修正过，理由留在各自的 docstring 里

一条**宽过头**（禁了入口层的 `pathlib`），一条**判错了对象**（把入口层的
「用了适配层」当成了「横跨多层」）。两条都记着，因为**判据出错的方式**
比判据本身更值得留下 —— 它们正是这个文件想防的那类问题的镜像。
"""

from __future__ import annotations

import ast

import pytest

from tests.contracts._layers import (
    LAYERS,
    PACKAGE_ROOT,
    layers_imported_by,
    nontrivial_by_layer,
    scan,
    top_level_package,
)

MODULES = scan()


def _assert_not_vacuous(n: int, what: str, minimum: int = 1) -> None:
    """空转自检。扫到的比 `minimum` 少 ⇒ 这条判据等于没在守。"""
    assert n >= minimum, (
        f"空转自检失败：这条判据本想检查{what}，但只扫到 {n} 个（要求 ≥ {minimum}）。"
        f"扫到空集合的断言恒为真 —— 它在报告里是绿的，却没有守住任何东西。"
    )


# ── 空转自检本身 ────────────────────────────────────────────────────


def test_扫描器扫到了包里的文件():
    """最外层的地基：扫描器自己得是活的。

    ⚠️ 如果 `PACKAGE_ROOT` 拼错（比如仓库挪了位置），`rglob` 会静静地返回空
    —— 于是**本文件里其余每一条判据都变成恒真**。这条先把它钉住。
    """
    assert PACKAGE_ROOT.is_dir(), f"包目录不存在：{PACKAGE_ROOT}"
    _assert_not_vacuous(len(MODULES), "包里的 .py 文件", minimum=8)


def test_每一层都有非空壳的文件():
    """⚠️ 这是本项目最重要的一条空转自检。

    它守的是「其余判据有没有东西可守」。只有 `__init__.py` 的层，会让
    「领域层无 I/O」看起来成立 —— 而实际上它扫的是零个有意义的文件。
    """
    by_layer = nontrivial_by_layer(MODULES)
    empty = [layer for layer, files in by_layer.items() if not files]
    assert not empty, (
        f"这些层只有空壳（或根本没有文件）：{empty}。"
        f"每层都必须有实际被判的文件，否则守它的判据恒为真。"
    )
    for layer, files in by_layer.items():
        _assert_not_vacuous(len(files), f"{layer}/ 下的非空壳文件")


# ── 依赖方向 ────────────────────────────────────────────────────────

FORBIDDEN: dict[str, set[str]] = {
    # 内层不知道外层存在
    "domain": {"application", "infrastructure", "interfaces"},
    # 用例层只依赖 domain 与自己定义的端口
    "application": {"infrastructure", "interfaces"},
    # 适配层是入口层的被调用方，不能反过来。
    # ⚠️ 它**可以** import `domain`（用它的值对象与异常）——
    # 那部分知识本来就在适配层的职责里。
    "infrastructure": {"interfaces"},
    # ⚠️ 入口层**不在这张表里** —— 它有自己的判据，见
    # `test_入口层不直接摸适配层`。放进这张表会**判错对象**：
    # 一个 API 路由文件天然要 import 三层，而那与
    # 「装配分散在各处」是两件事。混起来会让判据**永远为红**，
    # 而一条永远为红的判据会被人加进豁免名单 —— 那时它就真的死了。
    "interfaces": set(),
}


@pytest.mark.parametrize("layer", LAYERS)
def test_依赖方向只能由外向内(layer: str):
    banned = FORBIDDEN[layer]
    offenders: list[str] = []
    checked = 0
    for mod in MODULES:
        if mod.layer != layer or not mod.is_nontrivial:
            continue
        checked += 1
        bad = layers_imported_by(mod) & banned
        if bad:
            offenders.append(f"{mod.name} → {sorted(bad)}")
    _assert_not_vacuous(checked, f"{layer}/ 下被扫描的模块")
    assert not offenders, (
        f"{layer}/ 不许依赖 {sorted(banned)}（依赖方向只有一个：外 → 内）。"
        f"违规：{offenders}"
    )


# ── 领域层无 I/O ────────────────────────────────────────────────────

# ⚠️ **白名单**：列出来的每一项都要能回答「为什么领域层需要它」。
# 全是标准库里的**纯计算**部分 —— 它们不碰文件、不碰网络、不取当前时间。
DOMAIN_ALLOWED_STDLIB: frozenset[str] = frozenset(
    {
        # ⚠️ `from __future__ import annotations` 是**编译期指令**，不是运行时
        # 导入 —— 它不影响依赖方向，也碰不到 I/O。放行它。
        # （第一次跑这条判据时它就被拦下来了：判据在工作。）
        "__future__",
        "dataclasses",  # 值对象
        "datetime",  # ⚠️ 只用 date/time 的算术；不许调 now() —— 见下面的判据
        "enum",  # 闭集
        "re",  # 正则：判据的载体
        "typing",  # 类型标注
        "unicodedata",  # 文本规范化（查零宽字符的类别）
        "collections",  # Counter / defaultdict：纯算法
        "itertools",  # 纯算法
        "functools",  # 纯算法
        "math",  # 纯算法
        "json",  # ⚠️ 只用于**解析模型输出**这一格式转换，不读写文件
        "textwrap",  # 日报排版（纯字符串处理）
        "hashlib",  # 指纹（纯计算，不碰文件）
    }
)

# 领域层**绝对不许**出现的顶层包 —— 即使它上面被误加进白名单也不会放行。
DOMAIN_FORBIDDEN_HARD: frozenset[str] = frozenset(
    {
        "sqlite3", "fastapi", "starlette", "uvicorn", "requests", "httpx",
        "socket", "pathlib", "os", "io", "subprocess", "shutil", "tempfile",
        "logging", "time", "random", "uuid", "secrets",
    }
)


def test_领域层不碰_IO():
    """领域层全是判据。判据的唯一价值是**可被验证** —— 而「这个函数在什么
    输入下返回什么」只有在它不碰外部世界时才回答得了。

    一旦它开始读文件、取当前时间、连数据库，同一段代码在不同机器上的行为
    就可能不同，测试也就只能测「在我这台机器上」。
    """
    offenders: list[str] = []
    checked = 0
    for mod in MODULES:
        if mod.layer != "domain" or not mod.is_nontrivial:
            continue
        checked += 1
        for imp in mod.imports:
            top = top_level_package(imp)
            if top in DOMAIN_FORBIDDEN_HARD:
                offenders.append(f"{mod.name} 导入了 {imp}")
            elif top not in DOMAIN_ALLOWED_STDLIB and top != "yechao":
                offenders.append(
                    f"{mod.name} 导入了 {imp}（{top!r} 不在领域层的白名单里）"
                )
    _assert_not_vacuous(checked, "领域层模块", minimum=3)
    assert not offenders, (
        "领域层不许碰 I/O，也不许引白名单之外的包。\n"
        "新增一项时必须先回答：为什么这一层需要它？\n  " + "\n  ".join(offenders)
    )


def test_领域层不取当前时间():
    """⚠️ 时间的**类型与算术**允许，**获取当前时刻**不允许。

    `datetime.now()` / `date.today()` 让同一段代码在不同时刻行为不同 ——
    那正是「测试只能测我这一刻跑的那次」的形状。时间必须由调用方
    （用例层，从时钟端口）传进来。

    ⚠️ 这条判据用 AST 找 `now(...)` / `today(...)` 的**调用**，不是找字符串
    —— `# 不许调 now()` 这样的注释不该触发它。
    """
    offenders: list[str] = []
    checked = 0
    for mod in MODULES:
        if mod.layer != "domain" or not mod.is_nontrivial:
            continue
        checked += 1
        tree = ast.parse(mod.path.read_text(encoding="utf-8"), filename=str(mod.path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Attribute) and fn.attr in {"now", "today", "utcnow"}:
                    offenders.append(f"{mod.name}:{node.lineno} {ast.unparse(node)}")
    _assert_not_vacuous(checked, "领域层模块", minimum=3)
    assert not offenders, (
        "领域层不许自己取当前时刻（时间必须由用例层从时钟端口传进来）：\n  "
        + "\n  ".join(offenders)
    )


# ── 导入环 ──────────────────────────────────────────────────────────

_DIRECT_EDGES_CACHE: dict[str, frozenset[str]] = {}


def _internal_edges() -> dict[str, frozenset[str]]:
    """模块 → 它导入的**本项目**模块（只保留包内点分名）。

    ⚠️ 收窄到「模块名本身」，不是「模块名.属性」：`from yechao.domain import a`
    与 `from yechao.domain.a import b` 在环的意义上不一样 —— 前者指包，
    后者指模块。
    """
    if _DIRECT_EDGES_CACHE:
        return _DIRECT_EDGES_CACHE
    names = {m.name for m in MODULES}
    for mod in MODULES:
        edges = {imp for imp in mod.imports if imp in names and imp != mod.name}
        _DIRECT_EDGES_CACHE[mod.name] = frozenset(edges)
    return _DIRECT_EDGES_CACHE


def _find_cycle(edges: dict[str, frozenset[str]]) -> list[str] | None:
    """DFS 找一条环。返回环上节点的路径，找不到返回 None。"""
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {n: WHITE for n in edges}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GREY
        stack.append(node)
        for nxt in sorted(edges.get(node, ())):
            if color.get(nxt, WHITE) == GREY:
                i = stack.index(nxt)
                return stack[i:] + [nxt]
            if color.get(nxt, WHITE) == WHITE:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for n in sorted(edges):
        if color[n] == WHITE:
            found = visit(n)
            if found:
                return found
    return None


def test_没有导入环():
    """环会造出「单独跑绿、一起跑红」这类难查的失败。

    ⚠️ 它同时是**分层被写歪的前兆**：一个环通常意味着两层互相需要，
    而那是「这个接口该换个地方」的信号，不是「加一个 import 就好」。
    """
    edges = _internal_edges()
    _assert_not_vacuous(len(edges), "包内模块")
    _assert_not_vacuous(
        sum(len(v) for v in edges.values()), "包内的导入边", minimum=3
    )
    cycle = _find_cycle(edges)
    assert cycle is None, "存在导入环：" + " → ".join(cycle or [])


# ── 组合根 ──────────────────────────────────────────────────────────

COMBINATION_ROOT = "yechao.bootstrap"


def test_组合根真的在装配():
    """⚠️ 这条判据是本项目**唯一**一条「反向的空转自检」。

    原来这里有一条「只有组合根横跨多层」，它把 `bootstrap.py` 排除在外。
    那条判据有**两个**问题：

    **一、它扫到的是空集。** 排除掉组合根之后，其余模块本来就不该横跨三层
    —— 所以它在正确代码上**恒为真**。它守着一个空集，而报告里是绿的。

    **二、它判错了对象。** 「一个文件 import 了几层」与「装配分散在各处」
    是两件事。一个 API 路由文件天然要 import 三层（domain 的异常、
    application 的用例、一个端口类型），但它**没有装配任何东西** ——
    它从 `bootstrap.App` 拿到已经装好的对象。

    ⇒ 判据改成**正面**的：组合根必须**真的**在装配（导入 ≥3 层）。
    「入口层不自己接线」由 `test_入口层不直接摸适配层` 独立守。
    两条合起来，才是原来想守的那一件事。
    """
    roots = [m for m in MODULES if m.name == COMBINATION_ROOT]
    assert roots, f"找不到组合根 {COMBINATION_ROOT} —— 「唯一装配的地方」不存在"
    imported = layers_imported_by(roots[0])
    assert len(imported) >= 3, (
        f"组合根只导入了 {sorted(imported)} 层 —— 它应当是**唯一**装配的地方，"
        f"少于三层说明它没在真正装配（那别的文件就会去自己接线）。"
    )


def test_适配层的实现都接上了线():
    """⚠️ 守的是第四种部署失败：

    > **接线存在但从未被调用。** 某个组件写完了、看起来完整、甚至有单元
    > 测试，但**全项目没有任何地方实例化它**。它从不生效，而系统照常工作
    > —— **没有任何报错**。

    ⇒ 判据：每个适配层的实现类都要在组合根里被**提到**。
    「写了但没接」是一个可机械检查的形态，所以它不该等到部署那天才发现。

    ⚠️ 这条用的是**文本判据**（名字在组合根源码里出现过），不是 import 图。
    因为「接线」（`Y()`）与「引用」（`Y` 只用于类型标注）在 import 图里
    长得一样 —— 而后者**不算接线**。文本判据会宽一点（注释里提到也算），
    但它抓的是「压根没人提过」这一档，而那一档正是真问题。
    """
    impls: list[tuple[str, str]] = []
    for mod in MODULES:
        if mod.layer != "infrastructure" or not mod.is_nontrivial:
            continue
        tree = ast.parse(mod.path.read_text(encoding="utf-8"), filename=str(mod.path))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                impls.append((mod.name, node.name))

    # ⚠️ 空转自检：适配层一个实现类都没有时，这条判据毫无意义。
    _assert_not_vacuous(len(impls), "适配层的实现类", minimum=3)

    root = next(m for m in MODULES if m.name == COMBINATION_ROOT)
    root_src = root.path.read_text(encoding="utf-8")

    # 这几种是**按需**构造的，不由组合根起：
    #   · `NullAuditLog` —— 只给测试用（生产路径上出现它会让隐私页永远空）
    #   · `FixedClock`    —— 只给测试用
    #   · `SynthSource`   —— 数据源由用例那侧组合，组合根只给一条默认链
    KNOWN_OPTIONAL = {"NullAuditLog", "FixedClock", "SynthSource"}

    missing = [
        f"{mod}.{cls}"
        for mod, cls in impls
        if cls not in root_src and cls not in KNOWN_OPTIONAL
    ]
    assert not missing, (
        "这些适配层实现**没有任何地方实例化它们** —— 写了但没接线，"
        "它会静默地不生效：\n  " + "\n  ".join(missing)
    )


# ── 入口层 ──────────────────────────────────────────────────────────


def test_入口层不直接摸适配层():
    """接口层只做输入输出。任何业务判断都不该出现在这里 ——
    这一层的代码没法被穷举测试，所以判据要都在能被测的地方。

    ## ⚠️ 这条判据的第一版写歪了，记在这里

    第一版连 `pathlib` / `os` 也一起禁了。那是**把两件事混成一件**：

      · 「不许绕过组合根去拿数据」 ← 真正的规则
      · 「不许用文件系统」         ← **不是**规则

    一个 CLI 要读自己的参数、要判断端口能不能绑 —— 那些**本来就是**输入
    输出，正是这一层的职责。禁掉 `pathlib` 只会逼着人把 CLI 的代码搬到
    别处，而搬到哪儿都不对。

    **判据的范围宽过头，和宽不够一样坏**：它会逼着正确的代码去绕路，
    而绕的路往往比原路更糟。

    所以现在只禁一件事：**直接 import `yechao.infrastructure`** ——
    适配器的实例一律从组合根（`bootstrap.App`）拿。
    """
    offenders: list[str] = []
    checked = 0
    for mod in MODULES:
        if mod.layer != "interfaces" or not mod.is_nontrivial:
            continue
        checked += 1
        for imp in mod.imports:
            # ⚠️ 禁的是**适配层**，不是标准库。见 docstring。
            if imp.startswith("yechao.infrastructure"):
                offenders.append(f"{mod.name} 直接导入了 {imp}")
    _assert_not_vacuous(checked, "入口层模块")
    assert not offenders, (
        "入口层不许直接碰适配层 —— 适配器的实例从组合根（bootstrap.App）拿。"
        "（stdlib 不算，见这条判据的 docstring。）\n  "
        + "\n  ".join(offenders)
    )
