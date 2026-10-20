"""部署链路的契约覆盖 —— 不联网、不调基础设施工具，每次 pytest 都跑。

## 为什么这个文件存在



> 部署链路的问题有**同一个形状**：**两份东西之间的覆盖关系**
> （基础设施要什么 vs 脚本给什么；单元要什么 vs 初始化脚本建什么）。
> 这类问题**漏一项不会报错** —— 只有专门的断言能拦。

而部署脚本有一个特别的失败模式：**它只在真正部署的那一刻才报错，
而那一刻你在烧云资源的小时费。**

## 三条纪律

1. **从两边各自的来源读**，不在测试里手写一张期望值表 ——
   手写的会漂移。
2. **不联网、不调基础设施工具**：只读文本、比集合。这样它能在每次
   `pytest` 里跑，而不是等到部署那天。
3. **每条都要能机械地失败**：一个永远为绿的部署检查，比没有检查更危险。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── 空转自检 ────────────────────────────────────────────────────────


def test_部署文件都在() -> None:
    """※ 空转自检。

    下面每一条判据都要读这几个文件。它们不在时，那些判据要么会抛
    （好），要么会扫到一个空字符串然后**恒为真**（坏）。
    这一条先把「有东西可读」钉住。
    """
    for name in ("Dockerfile", "docker-compose.yml", "entrypoint.sh"):
        p = DEPLOY / name
        assert p.is_file(), f"缺 {p}"
        assert _read(p).strip(), f"{p} 是空的"


# ── 判据一：参数契约对得上 ──────────────────────────────────────────


#: compose 里 `${VAR:?msg}` 的写法 = **必填**。`${VAR:-}` = 可空。
#: ※ 只认**变量名全大写且以已知前缀开头**的写法。
#: 原来写的是 `\${([A-Z_][A-Z0-9_]*):\?}` —— 它在**这个测试文件**里
#: 也匹配到了我自己写的那句 `"${VAR:?}"`，于是 `VAR` 被当成一个必填项，
#: 断言要求 `.env.example` 里有它。一个**判据自己造的假阳性**。
#: 收紧到 `YECHAO_` 前缀：本项目所有必填项都是这个名字空间里的。
_REQUIRED = re.compile(r"\$\{(YECHAO_[A-Z0-9_]*):\?")


def _compose_required_vars() -> set[str]:
    """从 compose 的**声明侧**读必填变量。

    ※ 这是「从基础设施声明里读」那半边 —— 不是手写一张表。
    """
    return set(_REQUIRED.findall(_read(DEPLOY / "docker-compose.yml")))


def test_compose_真的声明了必填项() -> None:
    """※ **空转自检** —— 上一条的判据如果读到空集，它恒为真。"""
    required = _compose_required_vars()
    assert required, (
        "compose 里一个 `${VAR:?}` 都没有读到 —— "
        "要么 compose 真的没有必填项，要么解析器坏了。两种都要查。"
    )
    # 这些是本项目**一定**需要的
    for must in ("YECHAO_PORT", "YECHAO_DATA_DIR", "YECHAO_CONFIG_DIR"):
        assert must in required, f"compose 没有把 {must} 声明为必填"


def test_每个必填变量都有说明和示例值() -> None:
    """※ 必填项**要么有默认值，要么必须在部署脚本的前置检查里被显式验证**

    这里守后一半：`.env.example` 要给每一个必填变量一个示例值。
    缺一个，用户照文档部署会在 compose 中止 —— 而错误信息在另一个工具的
    语汇里，排查方向会偏。
    """
    env_example = ROOT / ".env.example"
    assert env_example.is_file(), "缺 .env.example —— 用户没有可照抄的模板"
    text = _read(env_example)

    required = _compose_required_vars()
    declared = set(re.findall(r"^\s*([A-Z_][A-Z0-9_]*)=", text, re.MULTILINE))

    missing = required - declared
    assert not missing, (
        f"这些必填变量在 compose 里是 `${{VAR:?}}`，但 .env.example 里没有示例：\n"
        f"  {sorted(missing)}\n"
        f"用户在部署时会撞上「缺少必填值」——而那本该在这里被指出来。"
    )


def test_没有多余的变量_在_example_里却不在_compose_里() -> None:
    """※ 反向也要查。

    一个 `.env.example` 里有、而 compose 完全没用的变量，会让人以为
    「我设了它就会生效」—— 而它什么都没做。**静默无效的配置**与
    「静默用默认值」是同一类失败。
    """
    env_example = ROOT / ".env.example"
    if not env_example.is_file():
        pytest.skip(".env.example 不存在（上一条已经报过）")
    declared = set(re.findall(r"^\s*([A-Z_][A-Z0-9_]*)=", _read(env_example), re.MULTILINE))
    compose = _read(DEPLOY / "docker-compose.yml")

    # ※ `COMPOSE_*` / `DOCKER_*` 是 compose 自己认的，不该要求它们出现在文件里
    unused = {
        v for v in declared
        if v not in compose and not v.startswith(("COMPOSE_", "DOCKER_"))
    }
    assert not unused, (
        f"这些变量写在 .env.example 里，但 compose 根本没读它们：\n  {sorted(unused)}\n"
        f"用户会以为设了它就会生效。"
    )


# ── 判据二：初始化脚本被挂上了 ──────────────────────────────────────


def test_Dockerfile_的_ENTRYPOINT_指向那个初始化脚本() -> None:
    """※ ****：

    > 首启初始化脚本写好了，但**没有任何地方引用它**。于是它从不执行，
    > 而依赖它的服务单元全部起不来。

    ※ 这条判据的价值在它的**方向**：它从 **Dockerfile** 里读 ENTRYPOINT，
    去确认那个路径**存在**。它不是在测试里写一句「entrypoint 应该在」——
    那样写的话，改了 Dockerfile 之后测试还是绿的。
    """
    dockerfile = _read(DEPLOY / "Dockerfile")
    m = re.search(r"^ENTRYPOINT\s*\[([^\]]*)\]", dockerfile, re.MULTILINE)
    assert m, "Dockerfile 里没有 ENTRYPOINT —— 初始化脚本不会被任何东西引用"

    # 解析 `["./deploy/entrypoint.sh"]` 这种 JSON 形式
    parts = [p.strip().strip('"').strip("'") for p in m.group(1).split(",")]
    script = parts[0]
    assert script, "ENTRYPOINT 是空的"
    # ※ 从 ENTRYPOINT 的**指令行**里读的，不是全文搜 —— 见上面那条的注释。
    # 这一条本来就是指令行级的，所以不怕注释。

    # 路径是相对镜像 WORKDIR 的
    resolved = DEPLOY / Path(script).name
    assert resolved.is_file(), (
        f"Dockerfile 的 ENTRYPOINT 指向 {script}，但仓库里没有 {resolved}"
    )


def test_初始化脚本真的做了初始化() -> None:
    """※ 脚本存在但什么都不做，等于不存在。

    这里断言它**真的**做了首启该做的那件事（配置为空时从模板填充）。
    用的是文本判据 —— 因为真跑它需要 docker，而这条要在每次 pytest 里跑。
    """
    script = _read(DEPLOY / "entrypoint.sh")
    assert "config.example" in script, (
        "初始化脚本没有引用 config.example —— 那它不会给容器填配置"
    )
    assert "serve" in script, "初始化脚本最后应当把服务起起来"


def test_初始化脚本不含_CRLF() -> None:
    """※ **脚本带 CRLF 时，容器里的 `sh` 会报出一个和真正病因无关的错。**

    症状是 `/bin/sh: ./deploy/entrypoint.sh: not found` —— 看起来像
    「文件不存在」，而它明明存在。Windows 上编辑过的脚本很容易带 CRLF，
    而那个错**在本地跑测试时永远不出现**。
    """
    raw = (DEPLOY / "entrypoint.sh").read_bytes()
    assert b"\r\n" not in raw, (
        "entrypoint.sh 里有 CRLF 行尾 —— 容器里会报 'not found'，"
        "而那个错误信息指向「文件不存在」。"
    )


# ── 判据三：镜像里真的有前端产物 ────────────────────────────────────


def test_Dockerfile_把_web_dist_拷进镜像() -> None:
    """※ 没有它，容器里的 `serve` 只能回 501 —— 而「网页能打开」是复现红线。

    这个失败**在本地永远不会出现**（本地有 `web/dist/`），所以必须由
    一条读 Dockerfile 的判据来守。
    """
    # ※ **判据必须只看真的指令行，不看注释。**
    #
    # 第一版写的是 `assert "web/dist" in dockerfile` —— 而 Dockerfile 的
    # **注释里**也提到了 `web/dist`（解释为什么不需要 Node）。
    # 于是把 `COPY web/dist ...` 整行删掉之后，测试**照样全绿**。
    #
    # 变异测试抓到了它：那是判断「判据是不是恒真」的唯一办法 ——
    # 光看它绿，分不出「守住了」与「什么都没守」。
    #
    # ⇒ 只看**以 COPY 开头的行**。
    dockerfile = _read(DEPLOY / "Dockerfile")
    copy_lines = [
        ln.strip() for ln in dockerfile.splitlines()
        if ln.strip().upper().startswith("COPY ")
    ]
    assert any("web/dist" in ln for ln in copy_lines), (
        "Dockerfile 里没有一条 COPY 把 web/dist 拷进镜像。"
        "（注释里提到不算 —— 注释不会让文件进镜像。）"
        f"实际的 COPY 行：{copy_lines}"
    )


def test_web_dist_真的在仓库里() -> None:
    """※ 镜像里的 COPY 只在**构建**时才有东西可拷。

    而 `web/dist/` 被 `.gitignore` 的 `dist/` 规则挡住了 —— 除非专门放行。
    这条断言它**真的在仓库里**，而不只是「本地有这个目录」。
    """
    dist = ROOT / "web" / "dist" / "index.html"
    assert dist.is_file(), (
        "web/dist 不在仓库里 —— README 第 ③ 步的「网页打开」会回 501。"
        "检查 .gitignore 里有没有 `!web/dist/`。"
    )


# ── 判据四：健康检查探的是一个真会读库的端点 ────────────────────────


def _service_block(compose: str, name: str) -> str:
    """切出某一个服务的段落。

    ※ 为什么要切：这份 compose 里有**两个**服务（应用与 Redis），
    而两个都带 `healthcheck`。在整份文件里 `re.search("healthcheck:...")`
    会命中**第一个**出现的那段 —— 现在的书写顺序恰好让第一个是应用，
    但那是**运气**，不是判据：谁把 redis 挪到前面，这条判据就会去检查
    Redis 的探活，而它对应用的健康检查什么都没说（却仍然是绿的）。
    """
    m = re.search(
        rf"^  {re.escape(name)}:\n(.*?)(?=^  \S|\Z)",
        compose,
        re.MULTILINE | re.DOTALL,
    )
    assert m, f"compose 里找不到服务 {name}"
    return m.group(1)


def _health_probe(compose: str) -> str:
    block = _service_block(compose, "yechao")
    m = re.search(r"healthcheck:\s*\n\s*test:\s*\[([^\]]*)\]", block)
    assert m, "yechao 服务没有 healthcheck —— 一个不探活的部署等于没验证过"
    return m.group(1)


def test_健康检查探的是_api_health() -> None:
    """※ 首页是静态文件 —— 它 200 **不代表**数据库通、配置读得到。

    一个只探静态页的健康检查会让「服务起了但库是坏的」看起来像健康
    （「健康检查里报『未挂载』而没人发现，
    说明那个健康检查字段本身也没被验证过」）。

    ※ 而探 `/api/items?limit=1` 只比探首页好一点点：它证明了「库能读出
    东西」，但**验不到缓存**，也**分不清**「服务活着但库坏了」与
    「服务根本没起来」。⇒ 改成 `/api/health`：它同时回答服务 / 库 / 缓存
    三件事，而库挂了时它回 **503**。
    """
    probe = _health_probe(_read(DEPLOY / "docker-compose.yml"))
    assert "/api/health" in probe, (
        f"健康检查探的不是 `/api/health`，而是：{probe}\n"
        f"那个端点同时回答「服务活着 / 库通不通 / 缓存是哪个后端」，"
        f"而 `/api/items` 只证明库能读出东西。"
    )


def test_健康检查在非_2xx_上会失败() -> None:
    """※ 一条**恒为绿**的健康检查比没有检查更危险。

    机制是：`urllib.request.urlopen` 在 4xx/5xx 上**抛异常** ⇒ 进程退出码
    非零 ⇒ 容器被判为 unhealthy。所以探活脚本里**不能**把异常吞掉 ——
    一个 `try: ... except: pass` 的探活永远成功，而它在报告里是绿的。

    判据是文本的（真跑它需要 docker），但它抓的正是那个形态。
    """
    probe = _health_probe(_read(DEPLOY / "docker-compose.yml"))
    assert "urlopen" in probe, (
        f"探活没有用 `urlopen` —— 它是「非 2xx 即失败」的机制所在：{probe}"
    )
    assert "except" not in probe, (
        f"探活脚本里有 `except` —— 那会把失败吞掉，检查就恒为绿了：{probe}"
    )
