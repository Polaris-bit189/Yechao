"""自动更新（`watcher`）—— 部署侧的契约覆盖（不联网、不调 docker，每次 pytest 都跑）。

## 这个文件补的是哪一类失败

`test_deploy_contract.py` 守**应用那一条路**，`test_daily_schedule_contract.py`
守**每天一次那条路**。「每 10 分钟跑一轮取数 → 抽取」是**第三条路**，
而它与前两条共享同一个失败形状（部署链路的问题都长这样）：

> **两份东西之间的覆盖关系** —— 基础设施要什么 vs 脚本给什么。

它自己的三种静默失败，与定时日报那三种**逐字相同**，只是换了对象：

  1. **脚本写好了但没有任何地方引用它。** `deploy/watcher.sh` 存在、
     本地还能手跑 —— 而 compose 里没有服务挂它。于是「自动更新」**从不
     发生**，信息流永远停在那里，而 `docker compose up` 一切正常。
  2. **脚本进了仓库但没进镜像。** 容器起来时 `exec: not found` ——
     一句英文启动错误，与「循环没跑」这个真实病因隔着一层。
  3. **跑失败而没人知道。** 一个静默失败的后台循环，与一个正常工作的
     循环在 `docker compose ps` 里长得一模一样。

## 三条纪律（与另外两个部署契约文件逐字相同）

1. **从两边各自的来源读**，不在测试里手写期望表。
2. **不联网、不调基础设施工具**：只读文本、比集合。
3. **每条都要能机械地失败** —— 一个永远为绿的部署检查比没有检查更危险。

## 为什么这里自己抄了一份 `_service_block`

与 `test_daily_schedule_contract.py` 同一个理由：那是另一个测试文件的内部
工具，跨文件 import 会把两个文件的改动绑在一起。抄一份八行的，换来的是
这个文件**自己完整**。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"

#: ※ 与另外两个部署契约文件**同一个形状**：只认 `YECHAO_` 名字空间里的
#: 大写变量（收窄前缀的理由见 `test_deploy_contract.py` 的注释：判据自己
#: 造的假阳性）。
_REQUIRED = re.compile(r"\$\{(YECHAO_[A-Z0-9_]*):\?")

#: 间隔那一项的形状：`${YECHAO_WATCH_INTERVAL:-600}` —— 有默认值，不是必填。
_WITH_DEFAULT = re.compile(r"\$\{YECHAO_WATCH_INTERVAL:-([^}]*)\}")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _service_block(compose: str, name: str) -> str:
    """切出某一个服务的段落。

    ※ 为什么要切：这份 compose 里有**四个**服务，而其中三个都带
    `healthcheck` / `environment` / `volumes`。在整份文件里搜会命中
    **第一个**出现的那段 —— 那取决于书写顺序，是**运气**不是判据。
    """
    m = re.search(
        rf"^  {re.escape(name)}:\n(.*?)(?=^  \S|\Z)",
        compose,
        re.MULTILINE | re.DOTALL,
    )
    assert m, f"compose 里找不到服务 {name}"
    return m.group(1)


def _compose() -> str:
    return _read(DEPLOY / "docker-compose.yml")


def _watcher_block() -> str:
    return _service_block(_compose(), "watcher")


# ── 空转自检 ────────────────────────────────────────────────────────


def test_自动更新脚本在且非空() -> None:
    """※ **空转自检**：下面每一条都要读这个文件。它不在时，那些判据要么会
    抛（好），要么会扫到一个空字符串然后**恒为真**（坏）。"""
    p = DEPLOY / "watcher.sh"
    assert p.is_file(), f"缺 {p} —— 「每 10 分钟自己更新」这件事没有落点"
    assert _read(p).strip(), f"{p} 是空的"


def test_compose_里真的有_watcher_服务() -> None:
    """※ 空转自检的另一半：服务不在时，后面几条会扫到空串。"""
    block = _watcher_block()
    assert block.strip(), "watcher 服务是空的"
    assert "YECHAO_WATCH_INTERVAL" in block, (
        "watcher 服务里没有读 `YECHAO_WATCH_INTERVAL` —— 间隔就不可配了"
    )


# ── 判据一：脚本真的被挂上了（「写了但没接」）──────────────────────


def test_watcher_的_entrypoint_指向那个脚本() -> None:
    """※ 这条判据的价值在它的**方向**：它从 **compose** 里读 entrypoint，
    去确认那个路径**在仓库里存在**。

    它不是在测试里写一句「entrypoint 应该在」—— 那样写的话，改了 compose
    之后测试还是绿的。而漏掉它的症状是：自动更新**从不发生**，而
    `docker compose up` 一切正常。
    """
    block = _watcher_block()
    m = re.search(r"^\s*entrypoint:\s*\[([^\]]*)\]", block, re.MULTILINE)
    assert m, (
        "watcher 服务没有 entrypoint —— 镜像的 ENTRYPOINT 会把 `serve` "
        "起起来，而循环一次都不会被执行。"
        "※ 只写 `command:` 不够：ENTRYPOINT 还在，它会把 command 当成"
        "自己的参数。"
    )
    script = m.group(1).split(",")[0].strip().strip('"').strip("'")
    assert script, "entrypoint 是空的"
    resolved = DEPLOY / Path(script).name
    assert resolved.is_file(), (
        f"watcher 的 entrypoint 指向 {script}，但仓库里没有 {resolved}"
    )


def test_Dockerfile_把自动更新脚本拷进镜像() -> None:
    """※ 脚本在仓库里、compose 也挂上了，但**镜像里没有** —— 症状是容器
    起来时 `exec: ./deploy/watcher.sh: not found`。

    那是启动期的一句英文报错，与「循环没跑」这个真实病因隔着一层。
    而它在**本地跑测试时永远不会出现**（本地有那个文件）。

    ※ **只看真的指令行，不看注释。** 这是从 `test_deploy_contract.py` 的
    `web/dist` 那一条学来的教训：那条判据第一版全文搜字符串，而注释里也
    提到了它 —— 于是把 COPY 整行删掉之后测试**照样全绿**。
    """
    copy_lines = [
        ln.strip()
        for ln in _read(DEPLOY / "Dockerfile").splitlines()
        if ln.strip().upper().startswith("COPY ")
    ]
    assert any("watcher.sh" in ln for ln in copy_lines), (
        "Dockerfile 里没有一条 COPY 把 deploy/watcher.sh 拷进镜像。"
        f"（注释里提到不算。）实际的 COPY 行：{copy_lines}"
    )


# ── 判据二：脚本真的在跑那条路（挂了一个空脚本，等于不存在）────────


def test_自动更新脚本真的跑_watch_入口() -> None:
    """※ 文本判据（真跑它需要 docker），但它抓的正是「挂了一个空脚本」这个
    形态 —— 那个脚本会起来、什么都不做，而日志里一片安静。

    ※ 认**两种写法**：`yechao.interfaces.watch`（模块路径）与
    `python -m yechao.interfaces.watch`。只认一种的话，将来把循环搬个
    位置，这条判据会红 —— 而它想守的其实是「脚本真的在跑那条路」，
    不是「它用了哪个字符串」。
    """
    script = _read(DEPLOY / "watcher.sh")
    assert "yechao.interfaces.watch" in script, (
        "自动更新脚本没有跑 `python -m yechao.interfaces.watch` —— "
        "那它起来之后什么都不会发生。"
    )


def test_自动更新脚本不含_CRLF() -> None:
    """※ **脚本带 CRLF 时，容器里的 `sh` 会报出一个和真正病因无关的错。**

    症状是 `/bin/sh: ./deploy/watcher.sh: not found` —— 看起来像
    「文件不存在」，而它明明存在。这个错**在本地跑测试时永远不出现**。
    """
    raw = (DEPLOY / "watcher.sh").read_bytes()
    assert b"\r\n" not in raw, (
        "watcher.sh 里有 CRLF 行尾 —— 容器里会报 'not found'，"
        "而那个错误信息指向「文件不存在」。"
    )


# ── 判据三：间隔可配（有默认值），而且默认是十分钟 ──────────────────


def test_间隔可配且默认十分钟() -> None:
    """※ 默认值这件事要在**声明侧**成立（compose），不是在 `.env.example`
    里写一句「默认 600」就算 —— 用户不设它时用的是哪个值，只有 compose
    说了算。
    """
    m = _WITH_DEFAULT.search(_compose())
    assert m, (
        "compose 里没有 `${YECHAO_WATCH_INTERVAL:-默认值}` 这种写法 —— "
        "要么它是必填（用户不设就起不来），要么它根本没被读。"
    )
    default = m.group(1).strip()
    assert default.isdigit() and int(default) > 0, (
        f"默认间隔 {default!r} 不是一个正数秒数 —— 入口会当场报错退出"
    )
    assert int(default) == 600, (
        f"默认间隔是 {default} 秒，而产品要的是 10 分钟（600 秒）—— "
        f"改这个数要同时想清楚「页面刷新就能看到」那句话还成不成立"
    )


def test_间隔在_env_example_里有示例值() -> None:
    """※ 反向也要查：`.env.example` 是用户照抄的模板。缺了它，用户会以为
    这个开关不存在 —— 而「以为不存在」的结果是他永远用默认值，
    且不知道为什么。
    """
    m = re.search(
        r"^\s*YECHAO_WATCH_INTERVAL=(\S+)\s*$",
        _read(ROOT / ".env.example"),
        re.MULTILINE,
    )
    assert m, "`.env.example` 里没有 `YECHAO_WATCH_INTERVAL=` 的示例值"
    assert m.group(1).isdigit() and int(m.group(1)) > 0, (
        f"示例值 {m.group(1)!r} 不是正数秒数 —— 照抄它会被入口拒绝"
    )


# ── 判据四：几个服务的声明不会漂移 ─────────────────────────────────


def test_watcher_声明了应用的全部必填变量() -> None:
    """※ **两份声明之间的覆盖关系** —— 部署链路那一类失败的共同形状。

    watcher 与 `yechao` / `scheduler` 读写**同一个库、同一份配置**。
    某个必填变量只声明在应用那一侧时，症状是「应用起得来、信息流不更新」，
    而几个服务各看一半日志 —— 两边都不报错。

    ※ 例外只有一个：`YECHAO_PORT`。它是**应用听哪个端口**，而 watcher
    不听端口 —— 要求它也声明端口是把「必填」与「用得上」混成一件事。
    """
    compose = _compose()
    app_required = set(_REQUIRED.findall(_service_block(compose, "yechao")))
    watcher_required = set(_REQUIRED.findall(_watcher_block()))

    assert app_required, "应用那一侧一个必填项都没读到 —— 解析器坏了"
    missing = (app_required - {"YECHAO_PORT"}) - watcher_required
    assert not missing, (
        f"这些必填变量只声明在 `yechao` 服务里，watcher 没有：\n"
        f"  {sorted(missing)}\n"
        f"watcher 会缺着它们跑，而症状是「应用起得来、信息流不更新」。"
    )


# ── 判据五：循环自己也活着，而且与日报那个**各自探各自的** ─────────


def test_watcher_有重启策略() -> None:
    """※ 没有 `restart:` 时，循环的主进程一退出，容器就**停在 Exited**
    —— 而那是「信息流不更新了」且**没人知道**的形状。"""
    assert re.search(r"^\s*restart:\s*\S+", _watcher_block(), re.MULTILINE), (
        "watcher 服务没有 `restart:` —— 它的主进程（那个循环）一退出，"
        "容器就停在 Exited，而信息流从此不再更新。"
    )


def test_watcher_的探活探的是心跳文件() -> None:
    """※ 一个卡死在 `sleep` 里的循环与一个正常工作的循环，在
    `docker compose ps` 里长得一模一样（都是 Up）—— 而前者意味着
    「信息流不更新了」。

    ⇒ 探活必须探「循环还在动」的证据，而不是「容器还在不在」。
    """
    m = re.search(
        r"healthcheck:\s*\n\s*test:\s*\[([^\]]*)\]", _watcher_block()
    )
    assert m, "watcher 服务没有 healthcheck —— 它卡住了没人知道"
    probe = m.group(1)
    assert "watcher-heartbeat" in probe, f"探活没有看心跳文件：{probe}"
    assert "except" not in probe, (
        f"探活里有 `except` —— 那会把失败吞掉，检查就恒为绿了：{probe}"
    )


def test_两个循环的心跳文件不是同一个() -> None:
    """※ 共用一份心跳会让「日报那个卡住了」把「取数那个正常」也报成
    不健康，反过来也一样 —— 而两个服务的**下一步动作完全不同**
    （一个去查模型端点，一个去查数据源）。

    判据取**两个探活的字符串**，不写死文件名：改了名字也仍然成立，
    只要它们**不一样**。
    """
    compose = _compose()
    sched = re.search(
        r"healthcheck:\s*\n\s*test:\s*\[([^\]]*)\]",
        _service_block(compose, "scheduler"),
    )
    watcher = re.search(
        r"healthcheck:\s*\n\s*test:\s*\[([^\]]*)\]",
        _service_block(compose, "watcher"),
    )
    assert sched and watcher, "两个服务的探活都要读得到 —— 上一条已经各自钉过"
    assert sched.group(1) != watcher.group(1), (
        "两个循环探的是同一个文件 —— 一个卡住会把另一个也报成不健康"
    )


# ── 判据六：追踪规则的模板要跟着首启初始化走 ───────────────────────


def test_追踪模板在_config_example_里() -> None:
    """※ `deploy/entrypoint.sh` 的「首启初始化」是**整目录复制**
    `config.example/`。模板不在那里，容器里的 `tracks.toml` 就永远不存在
    —— 而「没有规则」是一个**合法状态**，所以它**不报错**：
    用户只会觉得「我配了追踪但它不响」。
    """
    assert (ROOT / "config.example" / "tracks.toml").is_file(), (
        "config.example/ 里没有 tracks.toml —— 容器首启不会给它一份模板"
    )
