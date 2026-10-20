"""定时日报 —— 部署侧的契约覆盖（不联网、不调 docker，每次 pytest 都跑）。

## 这个文件补的是哪一类失败

`tests/contracts/test_deploy_contract.py` 守的是**应用那一条路**：
必填变量、初始化脚本被引用、健康检查探真端点。而「每天自己跑一次」是
**另一条路**，它有自己的三种静默失败：

  1. **脚本写好了但没有任何地方引用它。** `deploy/scheduler.sh` 存在、
     有内容、本地还能手跑 —— 而 compose 里没有服务挂它。于是定时**从不发生**，
     而 `docker compose up` 一切正常。
  2. **脚本进了仓库但没进镜像。** 容器起来时 `exec: not found` —— 一句英文
     启动错误，与「定时器没跑」这个真实病因隔着一层。
  3. **跑失败而没人知道。** 定时任务静默失败是最糟的形状：一个容器起来、
     跑一次、挂了、没人知道。所以退出码必须进日志。

## 三条纪律（与 `test_deploy_contract.py` 逐字相同）

1. **从两边各自的来源读**，不在测试里手写期望表。
2. **不联网、不调基础设施工具**：只读文本、比集合。
3. **每条都要能机械地失败** —— 一个永远为绿的部署检查比没有检查更危险。

## 为什么这里自己抄了一份 `_service_block`

`test_deploy_contract.py` 里有一个同名的小工具。**不 import 它**：那是
另一个测试文件的内部工具，跨文件 import 会把两个文件的改动绑在一起
（那边为了自己的用例改一下签名，这边就以「与我无关」的方式红）。
抄一份八行的，换来的是这个文件**自己完整**。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"

#: ※ 与 `test_deploy_contract.py` 里那条**同一个形状**：只认 `YECHAO_` 名字
#: 空间里的大写变量。收紧前缀的理由见那边的注释（判据自己造的假阳性）。
_REQUIRED = re.compile(r"\$\{(YECHAO_[A-Z0-9_]*):\?")

#: 时间点那一项的形状：`${YECHAO_DAILY_AT:-07:30}` —— 有默认值，不是必填。
_WITH_DEFAULT = re.compile(r"\$\{YECHAO_DAILY_AT:-([^}]*)\}")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _service_block(compose: str, name: str) -> str:
    """切出某一个服务的段落。

    ※ 为什么要切：这份 compose 里有**三个**服务，而好几个服务都带
    `healthcheck` / `environment`。在整份文件里搜会命中**第一个**出现的那段
    —— 那取决于书写顺序，是**运气**不是判据。
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


def _scheduler_block() -> str:
    return _service_block(_compose(), "scheduler")


# ── 空转自检 ────────────────────────────────────────────────────────


def test_定时脚本在且非空() -> None:
    """※ **空转自检**：下面每一条都要读这个文件。它不在时，那些判据要么会
    抛（好），要么会扫到一个空字符串然后**恒为真**（坏）。"""
    p = DEPLOY / "scheduler.sh"
    assert p.is_file(), f"缺 {p} —— 「每天自己跑」这件事没有落点"
    assert _read(p).strip(), f"{p} 是空的"


def test_compose_里真的有_scheduler_服务() -> None:
    """※ 空转自检的另一半：服务不在时，后面几条会扫到空串。"""
    block = _scheduler_block()
    assert block.strip(), "scheduler 服务是空的"
    assert "YECHAO_DAILY_AT" in block, (
        "scheduler 服务里没有读 `YECHAO_DAILY_AT` —— 时间点就不可配了"
    )


# ── 判据一：脚本真的被挂上了（「写了但没接」）──────────────────────


def test_scheduler_的_entrypoint_指向那个脚本() -> None:
    """※ 这条判据的价值在它的**方向**：它从 **compose** 里读 entrypoint，
    去确认那个路径**在仓库里存在**。

    它不是在测试里写一句「entrypoint 应该在」—— 那样写的话，改了 compose
    之后测试还是绿的。而漏掉它的症状是：定时**从不发生**，而
    `docker compose up` 一切正常。
    """
    block = _scheduler_block()
    m = re.search(r"^\s*entrypoint:\s*\[([^\]]*)\]", block, re.MULTILINE)
    assert m, (
        "scheduler 服务没有 entrypoint —— 镜像的 ENTRYPOINT 会把 `serve` "
        "起起来，而定时脚本一次都不会被执行。"
        "※ 只写 `command:` 不够：ENTRYPOINT 还在，它会把 command 当成"
        "自己的参数。"
    )
    script = m.group(1).split(",")[0].strip().strip('"').strip("'")
    assert script, "entrypoint 是空的"
    resolved = DEPLOY / Path(script).name
    assert resolved.is_file(), (
        f"scheduler 的 entrypoint 指向 {script}，但仓库里没有 {resolved}"
    )


def test_Dockerfile_把定时脚本拷进镜像() -> None:
    """※ 脚本在仓库里、compose 也挂上了，但**镜像里没有** —— 症状是容器
    起来时 `exec: ./deploy/scheduler.sh: not found`。

    那是启动期的一句英文报错，与「定时器没跑」这个真实病因隔着一层。
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
    assert any("scheduler.sh" in ln for ln in copy_lines), (
        "Dockerfile 里没有一条 COPY 把 deploy/scheduler.sh 拷进镜像。"
        f"（注释里提到不算。）实际的 COPY 行：{copy_lines}"
    )


# ── 判据二：脚本真的在跑日报（脚本存在但什么都不做，等于不存在）────


def test_定时脚本真的跑日报入口() -> None:
    """※ 文本判据（真跑它需要 docker），但它抓的正是「挂了一个空脚本」这个
    形态 —— 那个脚本会准时醒来、准时什么都不做，而日志里一片安静。
    """
    script = _read(DEPLOY / "scheduler.sh")
    assert "yechao.interfaces.daily" in script, (
        "定时脚本没有跑 `python -m yechao.interfaces.daily` —— "
        "那它每天醒来一次，什么都不会发生。"
    )


def test_定时脚本把退出码打进日志() -> None:
    """※ **失败要看得见。**

    一个把退出码丢掉的定时任务，在 `docker compose logs` 里成功与失败
    长得一模一样 —— 而「今天没跑成」正是最需要被看见的那件事。

    ※ 判据只看**含 `echo` 且引用退出码的那些行**，不是全文搜「退出码」：
    注释里提到不算（同 `web/dist` 那一条的教训）。
    """
    lines = _read(DEPLOY / "scheduler.sh").splitlines()
    assert any("$?" in ln for ln in lines), (
        "脚本里没有一处取 `$?` —— 退出码被丢掉了"
    )
    reporting = [ln for ln in lines if "echo" in ln and "$code" in ln]
    assert reporting, (
        "取到退出码之后**没有任何一行把它打出来** —— 那等于没取。"
        f"含 echo 的行：{[ln.strip() for ln in lines if 'echo' in ln]}"
    )


def test_定时脚本不含_CRLF() -> None:
    """※ **脚本带 CRLF 时，容器里的 `sh` 会报出一个和真正病因无关的错。**

    症状是 `/bin/sh: ./deploy/scheduler.sh: not found` —— 看起来像
    「文件不存在」，而它明明存在（`test_scheduler_的_entrypoint_指向那个脚本`
    刚刚证明过它存在）。这个错**在本地跑测试时永远不出现**。
    """
    raw = (DEPLOY / "scheduler.sh").read_bytes()
    assert b"\r\n" not in raw, (
        "scheduler.sh 里有 CRLF 行尾 —— 容器里会报 'not found'，"
        "而那个错误信息指向「文件不存在」。"
    )


# ── 判据三：时间点可配（有默认值）──────────────────────────────────


def test_时间点可配且默认早上() -> None:
    """※ 默认值这件事要在**声明侧**成立（compose），不是在 `.env.example`
    里写一句「默认 07:30」就算 —— 用户不设它时用的是哪个值，只有 compose
    说了算。
    """
    m = _WITH_DEFAULT.search(_compose())
    assert m, (
        "compose 里没有 `${YECHAO_DAILY_AT:-默认值}` 这种写法 —— "
        "要么它是必填（用户不设就起不来），要么它根本没被读。"
    )
    default = m.group(1).strip()
    assert re.fullmatch(r"\d{1,2}:\d{2}", default), (
        f"默认时刻 {default!r} 不是 `HH:MM` 的形状 —— 脚本会当场报错退出"
    )


def test_时间点在_env_example_里有示例值() -> None:
    """※ 反向也要查（与 `test_deploy_contract.py` 那两条同一个取向）：
    `.env.example` 是用户照抄的模板。缺了它，用户会以为这个开关不存在 ——
    而「以为不存在」的结果是他永远用默认值，且不知道为什么。
    """
    text = _read(ROOT / ".env.example")
    m = re.search(r"^\s*YECHAO_DAILY_AT=(\S+)\s*$", text, re.MULTILINE)
    assert m, "`.env.example` 里没有 `YECHAO_DAILY_AT=` 的示例值"
    assert re.fullmatch(r"\d{1,2}:\d{2}", m.group(1)), (
        f"示例值 {m.group(1)!r} 不是 `HH:MM` 的形状 —— 照抄它会被脚本拒绝"
    )


# ── 判据四：两个服务的声明不会漂移 ─────────────────────────────────


def test_scheduler_声明了应用的全部必填变量() -> None:
    """※ **两份声明之间的覆盖关系** —— 部署链路那一类失败的共同形状。

    调度器与 `yechao` 读写**同一个库、同一份配置**。某个必填变量只声明在
    应用那一侧时，症状是「应用起得来、日报永远没生成」，而两个服务各看一半
    日志 —— 两边都不报错。

    ※ 例外只有一个：`YECHAO_PORT`。它是**应用听哪个端口**，而调度器不听
    端口 —— 要求它也声明端口是把「必填」与「用得上」混成一件事。
    """
    compose = _compose()
    app_required = set(_REQUIRED.findall(_service_block(compose, "yechao")))
    sched_required = set(_REQUIRED.findall(_scheduler_block()))

    assert app_required, "应用那一侧一个必填项都没读到 —— 解析器坏了"
    missing = (app_required - {"YECHAO_PORT"}) - sched_required
    assert not missing, (
        f"这些必填变量只声明在 `yechao` 服务里，scheduler 没有：\n"
        f"  {sorted(missing)}\n"
        f"调度器会缺着它们跑，而症状是「应用起得来、日报永远没生成」。"
    )


# ── 判据五：调度器自己也活着 ───────────────────────────────────────


def test_scheduler_有重启策略() -> None:
    """※ 没有 `restart:` 时，调度器的主进程一退出，容器就**停在 Exited**
    —— 而那是「今天没有日报」且**没人知道**的形状。"""
    assert re.search(r"^\s*restart:\s*\S+", _scheduler_block(), re.MULTILINE), (
        "scheduler 服务没有 `restart:` —— 它的主进程（那个循环）一退出，"
        "容器就停在 Exited，而日报从此不再生成。"
    )


def test_scheduler_的探活探的是心跳文件() -> None:
    """※ 一个卡死在 `sleep` 里的调度器与一个正常工作的调度器，在
    `docker compose ps` 里长得一模一样（都是 Up）—— 而前者意味着
    「今天没有日报」。

    ⇒ 探活必须探「循环还在动」的证据，而不是「容器还在不在」。
    """
    m = re.search(
        r"healthcheck:\s*\n\s*test:\s*\[([^\]]*)\]", _scheduler_block()
    )
    assert m, "scheduler 服务没有 healthcheck —— 它卡住了没人知道"
    probe = m.group(1)
    assert "scheduler-heartbeat" in probe, (
        f"探活没有看心跳文件：{probe}"
    )
    assert "except" not in probe, (
        f"探活里有 `except` —— 那会把失败吞掉，检查就恒为绿了：{probe}"
    )
