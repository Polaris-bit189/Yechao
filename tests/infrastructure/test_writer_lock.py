"""写者锁 —— 「同一时刻只允许一个写者」。

## 为什么每条判据都用子进程 + 真文件

一个只在单线程里成立的「锁」不是锁。要防的是**两个进程**同时改同一个库，
所以这里每条关于互斥的判据都**起真进程**、用**真锁文件** —— 只测内存里的
状态等于什么都没测。

（唯一例外是「同一进程内第二次也拿不到」那一条：它测的是同一个进程里的
两个 fd 之间会不会冲突。这条是前几条的**前提** —— 它不成立的话，
「两个进程」那几条会因为别的原因偶然变绿。）

## 判据一览

  · 第一个拿到锁，第二个**立刻**失败（不是挂住）
  · 放锁之后第二个能拿到
  · 持锁进程被强杀之后不死锁
  · 锁文件里记着一个**不存在的 pid** 时仍然拿得到 —— 这条断言的是反面：
    「有人持锁」的判据不是「文件在不在」，也不是「文件里那个 pid 还活着吗」
  · 非写者命令（serve / doctor 之类）不受影响
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from yechao.infrastructure.runtime import lock

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 持锁子进程睡多久。测试总是先把它杀掉，所以这个值只要「比一次测试长」。
_HOLD_SECONDS = 120

#: 子进程的脚本 —— 直接用适配层那个模块拿锁，不经过 CLI。
#: 这样「锁」与「接线」是两条独立的判据：脚本里没有 CLI，`__main__` 那条
#: 用例里没有这个脚本。
_HOLDER_SRC = f"""\
import sys
import time

from yechao.infrastructure.runtime.lock import acquire

held, why = acquire(sys.argv[1], command=sys.argv[2])
print("HELD" if held else "BUSY", flush=True)
if held is None:
    print(why, flush=True)
    sys.exit(1)
time.sleep({_HOLD_SECONDS})
"""


def _env() -> dict[str, str]:
    env = dict(os.environ)
    # 子进程的 cwd 已经是仓库根，这一句是双保险（比如有人从别处跑 pytest）。
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _spawn(argv: list[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # ※ 中文 Windows 的控制台是 GBK —— 不显式指定就会解出乱码，
        # 而断言会失败在一个与病因无关的地方。CLI 那一侧把 stdout 改成
        # 了 UTF-8（`cli._fix_console`），所以这里按 UTF-8 解。
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
        env=_env(),
    )


@pytest.fixture()
def holders():
    """起持锁子进程，并在用例结束时**保证**把它们收掉。"""
    procs: list[subprocess.Popen[str]] = []

    def start(tmp_path: Path, data_dir: Path, command: str = "refine") -> subprocess.Popen[str]:
        script = tmp_path / "holder.py"
        script.write_text(_HOLDER_SRC, encoding="utf-8")
        proc = _spawn([sys.executable, str(script), str(data_dir), command])
        procs.append(proc)
        line = (proc.stdout.readline() if proc.stdout else "").strip()
        # readline 在子进程退出时返回 "" —— 所以这里不会挂住。
        assert line == "HELD", (
            f"持锁子进程没起来：stdout={line!r} stderr={proc.stderr.read() if proc.stderr else ''}"
        )
        return proc

    yield start

    for p in procs:
        if p.poll() is None:
            p.kill()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - 不该发生
            pass


# ── 判据一：第二个写者立刻失败 ──────────────────────────────────────


def test_第一个拿到锁_第二个立刻失败(holders, tmp_path: Path) -> None:
    """※ 判据的核心一条。

    断言三件事，缺一不可：
      · 退出码非零（失败要响，不许看起来像成功）；
      · **立刻**返回（挂在那里与卡死了长得一样）；
      · 报错说清了「谁在跑、在哪跑」（否则用户只知道有人在跑，不知道等谁）。
    """
    data = tmp_path / "data"
    holder = holders(tmp_path, data)

    started = time.monotonic()
    r = subprocess.run(
        [sys.executable, "-m", "yechao", "refine", "--data-dir", str(data)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
        env=_env(),
        timeout=60,
    )
    elapsed = time.monotonic() - started

    assert r.returncode != 0, f"第二个写者居然跑成了：{r.stdout}"
    assert elapsed < 10, f"第二个写者没有立刻失败，等了 {elapsed:.1f} 秒"
    assert "写者" in r.stderr, f"报错没说是「写者」在跑：{r.stderr!r}"
    assert f"pid={holder.pid}" in r.stderr, (
        f"报错没点名持有者的 pid（{holder.pid}）：{r.stderr!r}"
    )
    assert str(data / lock.LOCK_FILENAME) in r.stderr, "报错没说锁文件在哪"
    assert "refine" in r.stderr, "报错没说持有者在跑什么命令"


def test_非写者命令不受影响(holders, tmp_path: Path) -> None:
    """※ `serve` 的写是用户逐个操作触发的，很轻；`doctor` 只读。

    所以它们**不该**被这把锁挡住 —— 挡住的症状是「网页打不开，而报错在说
    另一个命令在跑」，那是把两件事混成一件。

    `doctor` 走真的子进程（它会自己退出，断言得到退出码）。
    `serve` 只断言**接线决定**：它要起一个服务器，而一个单元测试不该去绑
    端口（端口在 Windows 上有保留段，测试会因为与本判据无关的原因变红）。
    """
    data = tmp_path / "data"
    cfg = tmp_path / "config"

    # 先跑一次 demo 把库建出来 —— 它是写者，得在持锁者起来**之前**跑。
    built = subprocess.run(
        [sys.executable, "-m", "yechao", "demo",
         "--data-dir", str(data), "--config-dir", str(cfg)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), env=_env(), timeout=120,
    )
    assert built.returncode == 0, built.stderr[-800:]

    holders(tmp_path, data)

    r = subprocess.run(
        [sys.executable, "-m", "yechao", "doctor",
         "--data-dir", str(data), "--config-dir", str(cfg)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), env=_env(), timeout=120,
    )
    # 先确认 doctor **真的跑了**（否则下面那句「没被挡住」是空转的：
    # 一个压根没跑起来的进程，stderr 里当然也没有那句话）。
    assert "夜抄 · 自检" in r.stdout, f"doctor 没跑起来：{r.stdout!r} / {r.stderr!r}"
    assert "写者" not in r.stderr, (
        f"doctor 被写者锁挡住了 —— 它只读，不该被挡：{r.stderr!r}"
    )
    # ※ 退出码 0 或 1 都行（自检可能有别的提醒），但**不能**是「锁挡住了」
    # 那条路。上面那句断言的就是这件事。
    assert r.returncode in (0, 1), r.stderr[-400:]


def test_写者与非写者的划分() -> None:
    """※ 判据的**声明侧**：哪些命令算写者。

    行为那一侧由上面两条守；这条守的是「集合本身」——
    有人把 `serve` 加进写者集合时，上面那条 doctor 用例不会红，
    而这条会。
    """
    assert lock.WRITER_COMMANDS == {"demo", "ingest", "refine", "digest", "cloud"}
    for name in ("serve", "doctor", "ledger", "metrics", "export"):
        assert not lock.is_writer(name), f"{name} 不该被当成写者"


def test_接线只给写者命令拿锁(tmp_path: Path) -> None:
    """※ 守的是**接线**（`yechao/__main__.py`），不是锁本身。

    上面几条用子进程真跑，覆盖的是「锁生效了没有」；这条覆盖的是
    「`__main__` 把命令分对类了没有」—— 分错了的话，`doctor` 会被挡、
    或者某个写者压根不拿锁，而**两种症状都不会报错**。
    """
    from yechao import __main__ as entry

    data = tmp_path / "data"
    argv = ["--data-dir", str(data)]

    for name in ("serve", "doctor", "ledger", "metrics", "export"):
        held, why = entry._acquire_writer_lock([name, *argv])
        assert (held, why) == (None, ""), f"{name} 不该拿锁：{why!r}"

    held, why = entry._acquire_writer_lock(["refine", *argv])
    try:
        assert held is not None, why
        assert (data / lock.LOCK_FILENAME).is_file(), "拿了锁却没落锁文件"
    finally:
        if held is not None:
            held.release()


# ── 判据二：放锁之后能拿到 ──────────────────────────────────────────


def test_放锁之后第二个能拿到(tmp_path: Path) -> None:
    """※ 一把「放不掉」的锁与死锁是一回事。"""
    data = tmp_path / "data"

    first, why = lock.acquire(data, command="refine")
    assert first is not None, why
    assert lock.acquire(data, command="refine")[0] is None, "第一个还持着，第二个不该拿到"

    first.release()

    second, why = lock.acquire(data, command="refine")
    assert second is not None, why
    second.release()


def test_同一进程内第二次也拿不到(tmp_path: Path) -> None:
    """※ 这条是前面几条的**前提**：同一个进程里的两个 fd 之间也冲突。

    如果锁实现成了「进程内的一把 threading.Lock」，同一进程内第二次会
    拿到（或者死锁），而跨进程那几条会因为别的原因偶然变绿 ——
    那时「两个进程同时改库」这件事照样发生。
    """
    data = tmp_path / "data"

    first, why = lock.acquire(data, command="refine")
    assert first is not None, why
    try:
        second, why2 = lock.acquire(data, command="refine")
        assert second is None, "同一进程内第二次居然也拿到了 —— 这不是进程间互斥"
        assert why2
    finally:
        first.release()


def test_进程退出后锁自动释放(tmp_path: Path) -> None:
    """※ 持锁子进程**正常退出**之后，下一个能拿到。

    与下一条（强杀）分开写：两条走的是同一个内核机制，但「正常退出」
    这条路如果坏了，症状是「每跑一次就永久卡住一个目录」。
    """
    data = tmp_path / "data"
    script = tmp_path / "short.py"
    script.write_text(
        "import sys\n"
        "from yechao.infrastructure.runtime.lock import acquire\n"
        "held, why = acquire(sys.argv[1], command='refine')\n"
        "print('HELD' if held else 'BUSY', flush=True)\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(script), str(data)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), env=_env(), timeout=60,
    )
    assert r.stdout.strip() == "HELD", r.stdout + r.stderr

    held, why = lock.acquire(data, command="refine")
    assert held is not None, why
    held.release()


# ── 判据三：崩溃之后不死锁 ──────────────────────────────────────────


def test_持锁进程被强杀之后不死锁(holders, tmp_path: Path) -> None:
    """※ 「崩溃之后不能留下死锁」是这条判据的全部意义。

    模拟的是 `kill -9`：持锁进程被**强杀**（没有机会跑任何清理代码），
    下一个写者必须仍然拿得到锁。

    顺带断言锁文件**还在** —— 它证明这条判据不是靠「把文件删掉」实现的，
    而是靠内核在进程消亡时放锁。
    """
    data = tmp_path / "data"
    holder = holders(tmp_path, data)
    path = data / lock.LOCK_FILENAME
    assert path.is_file()

    holder.kill()
    holder.wait(timeout=30)

    held, why = lock.acquire(data, command="refine")
    assert held is not None, f"强杀之后死锁了：{why}"
    held.release()

    assert path.is_file(), "锁文件不该被删掉（删它会造出两个持有者同时存在的竞态）"


def test_锁文件里记着不存在的_pid_时仍然拿得到(tmp_path: Path) -> None:
    """※ **这条断言的是反面**，所以它最要紧。

    写一个锁文件，里面记着一个**不存在的 pid**（模拟：上一个写者崩了，
    文件留在磁盘上）。此时**没有**任何进程持有锁 —— 下一个写者必须立刻
    拿到。

    如果有人把判据改成「读文件里的 pid，看那个进程还在不在」，这条就会
    红；而在真实部署里那个 pid 可能属于**另一个容器**（数据目录是挂载卷），
    于是它会**恒为假** —— 锁永远被一个查不到的人「持有」着。
    """
    data = tmp_path / "data"
    data.mkdir(parents=True)
    path = data / lock.LOCK_FILENAME
    # 一个不可能存在的 pid（Windows 的 pid 上限远小于这个数）。
    path.write_text(
        json.dumps(
            {"pid": 999_999_999, "host": "别处", "command": "refine", "started_at": 1},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 前提：文件真的在，而且里面真的记着那个 pid。
    stale = lock.read_holder(data)
    assert stale is not None and stale["pid"] == 999_999_999

    held, why = lock.acquire(data, command="refine")
    assert held is not None, f"残留的锁文件把下一个写者挡住了：{why}"
    held.release()


# ── 锁文件放哪 ──────────────────────────────────────────────────────


def test_锁文件跟着数据目录走(tmp_path: Path) -> None:
    """※ 需要互斥的是**同一个数据目录**，不是「同一台机器」。

    放进仓库根的话，两个指向不同数据目录的进程会互相挡住 —— 而它们本来
    井水不犯河水。
    """
    a = tmp_path / "a"
    b = tmp_path / "b"

    held_a, why = lock.acquire(a, command="refine")
    assert held_a is not None, why
    try:
        assert (a / lock.LOCK_FILENAME).is_file()
        assert not (REPO_ROOT / lock.LOCK_FILENAME).exists(), "锁文件漏到仓库根去了"

        held_b, why_b = lock.acquire(b, command="refine")
        assert held_b is not None, f"另一个数据目录被挡住了：{why_b}"
        held_b.release()
    finally:
        held_a.release()


# ── 等待是可选的，而且必须显式 ──────────────────────────────────────


def test_默认不等待(tmp_path: Path) -> None:
    """※ 默认必须是「立刻报错」。

    断言的是**时间**：默认调用不能花掉等待模式那一档的时间。
    """
    data = tmp_path / "data"
    held, why = lock.acquire(data, command="refine")
    assert held is not None, why
    try:
        started = time.monotonic()
        second, why2 = lock.acquire(data, command="refine")
        elapsed = time.monotonic() - started
        assert second is None and why2
        assert elapsed < 0.5, f"默认等待了 {elapsed:.2f} 秒 —— 默认必须立刻返回"
    finally:
        held.release()


def test_显式等待会在超时后失败(tmp_path: Path) -> None:
    """※ 等待是**可选**的：给了秒数就等，等到头还是拿不到就照样失败。

    它不许变成「无限等」—— 无限等的症状与卡死一样。
    """
    data = tmp_path / "data"
    held, why = lock.acquire(data, command="refine")
    assert held is not None, why
    try:
        started = time.monotonic()
        second, why2 = lock.acquire(data, command="refine", wait_seconds=0.6)
        elapsed = time.monotonic() - started
        assert second is None and why2
        assert 0.5 <= elapsed < 10, f"等待模式没有按给的秒数收敛：{elapsed:.2f} 秒"
    finally:
        held.release()
