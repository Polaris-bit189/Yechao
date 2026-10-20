"""写者锁 —— **同一时刻只允许一个写者**。

## 观察到什么，所以要防什么

会改库的命令（`demo` / `ingest` / `refine` / `digest` / `cloud`）同时跑两次，
后果是具体的：

  · 两个 `refine` 会各自去取那批还没处理的消息 ⇒ 同一批被抽两遍、token 花
    两份、库里出现重复条目 —— 而重复条目**不报错**，它只是让日报里多出一行；
  · 两个 `ingest` 各读同一批源消息 ⇒ 消息表里多出一份，账也记两笔。

而 `deploy/docker-compose.yml` 是 `restart: unless-stopped` —— **崩溃会自动
重启**。重启中的新进程与尚未退出的旧进程叠加时，并发是真会发生的。

## 判据

> 同一时刻，改库的命令只允许有一个在跑。

  · **谁是写者**：`WRITER_COMMANDS`。
    `serve` 不算 —— 它的写是用户逐个操作触发的，很轻；
    `doctor` / `ledger` / `metrics` / `export` 不算 —— 它们不改库。
  · 拿不到锁的进程**立刻**得到一个说清「谁在跑、在哪跑」的错误，而不是挂
    在那里（默认不等待，`wait_seconds=0`；等待是显式的可选项）。
  · 持有者被 `kill -9` 之后锁**自动**释放，不需要谁去清文件。

## 怎么判断「持有者还活着」：不判断，让内核判断

「崩溃之后不能留下死锁」这一条排除了两种做法：

**一、只看锁文件在不在。** 文件在磁盘上，进程死了它不会自己消失。于是崩溃
一次之后所有写者都跑不起来，而症状是「报错说有人在跑」，去 `ps` 又找不到
那个人 —— 这正是要防的死锁。

**二、读文件里的 pid，看那个进程还在不在。** 这条比上一条像样，但它在两个
地方会骗人：

  · **pid 会被复用。** 死掉的写者留下的 pid，可能正好是这台机器上另一个
    不相干的进程 —— 于是锁被一个陌生人「持有」着，而没有任何办法分辨。
  · **部署在容器里，数据目录是挂载卷。** 文件里的 pid 属于**另一个 pid
    命名空间**（另一个容器，或另一台宿主机）。在本机查它，查到什么都说明
    不了什么。而 `deploy/docker-compose.yml` 挂的正是宿主的目录。

所以这里用的是**操作系统自己的排它锁**：POSIX 上 `fcntl.flock`，Windows 上
`msvcrt.locking`。两者的共同点是 —— **锁随进程消亡而释放，由内核负责**。
内核知道进程死没死，而且它不需要问任何人。

※ 已知边界：**网络文件系统上的锁语义不由这个模块决定**。NFS 的某些挂载
参数、部分分布式卷驱动会让 `flock` 退化成「谁也不挡」或者「谁也不放」。
本项目部署用的是本地卷（`deploy/docker-compose.yml` 挂的是宿主目录），
不经过网络文件系统。真要在 NFS 上跑，这条判据要**重新验证**，
而不是假定它仍然成立。

⇒ 锁文件里的 `pid` / `host` / `cwd` / `started_at` **只用于报错文案，不参与
判断**。这一句必须写下来：否则将来有人会把它当成判据，于是「pid 在本机查
不到」被读成「没人持锁」—— 而那正是死锁的入口。

`tests/infrastructure/test_writer_lock.py` 里有一条用例专门断言这个**反面**：
文件里记着一个不存在的 pid、而锁没被内核持有时，下一个写者**拿得到**锁。

## 锁文件为什么永远不删

放锁时只关 fd（内核随之放锁），**不 unlink 文件**。理由是一个经典竞态：
A 放锁并删文件、B 在删除之前已经打开了旧 inode 并锁住了它、C 此时新建了
一个新 inode 并锁住 ⇒ B 与 C 同时「持有」。

文件留在磁盘上，锁的状态就**只有一处**说了算（内核）；文件里的内容是
「上一次持有者的记录」，不是「有没有人在持有」。所以 `data/yechao.lock`
长期存在是正常的，它不是残留物。

## 锁文件放哪

`<data_dir>/yechao.lock` —— 跟着数据走（`bootstrap.Settings.data_dir`），
不放仓库根。理由：需要互斥的是**同一个数据目录**，而数据目录是可配的
（容器里指向挂载卷）。

## 怎么验证

`tests/infrastructure/test_writer_lock.py`。每条都用**子进程 + 真文件**：
一个只在单线程里成立的「锁」不是锁。
"""

from __future__ import annotations

import datetime as _dt
import errno
import json
import os
import socket
import time
from pathlib import Path

from yechao.domain.days import DEFAULT_TZ_OFFSET

#: 锁文件名。`<data_dir>/yechao.lock`。
LOCK_FILENAME = "yechao.lock"

#: 会改库的命令 —— 它们必须互斥。
#:
#: ※ `demo` 也在里面：它写消息、写默认配置，`demo --fresh` 还会**删库**。
#: 把它排除掉的症状是「一边 refine 一边 demo --fresh」，而那个组合会让
#: refine 正在读的库文件凭空消失。
#:
#: ※ `export` 不在里面：它只写一份快照文件（`data/groups-snapshot.json`），
#: 不碰主库，与 refine 并发不会造出重复条目。
WRITER_COMMANDS: frozenset[str] = frozenset(
    {"demo", "ingest", "refine", "digest", "cloud"}
)

#: 锁落在文件的**哪一个字节**上。
#:
#: ※ **不能是 0。** Windows 的字节区间锁是**强制**的：锁住第 0 字节之后，
#: 别的进程连读那一段都读不到（实测 `errno 13`），于是「谁在跑」这句报错
#: 文案永远读不出来 —— 报错就变成了一句空话。
#: 把锁挪到 4096 之后，元数据（写在文件开头）对别人仍然可读。
#:
#: ※ POSIX 的 `flock` 是**建议**锁、且锁整个文件，读不受影响 ——
#: 两边对调用方呈现的行为一致：**锁住 ≠ 读不到**。
LOCK_BYTE = 4096

#: 等待模式下两次重试之间的间隔。
_POLL_SECONDS = 0.1

#: 元数据的字节上限。
#:
#: ※ **必须不大于 `LOCK_BYTE`，这不是省内存，是正确性。**
#: Windows 判的是**整个读区间**：`os.read(fd, 65536)` 就算文件只有 100
#: 字节，请求的区间也已经盖住了第 4096 字节 —— 于是读被拒（`errno 13`），
#: 而症状是「报错说读不出持锁者是谁」，看起来像「文件坏了」。
#: 第一版就是这么写的，实测踩到。卡在 `LOCK_BYTE` 以内，读区间与锁区间
#: **永不重叠**。
_MAX_META_BYTES = LOCK_BYTE


def lock_path(data_dir: str | os.PathLike[str]) -> Path:
    """锁文件在哪儿。**纯函数**，所以「锁落在哪个目录」能单独测。"""
    return Path(data_dir) / LOCK_FILENAME


def is_writer(command: str) -> bool:
    """这个命令算不算写者。"""
    return command in WRITER_COMMANDS


def acquire(
    data_dir: str | os.PathLike[str],
    *,
    command: str,
    wait_seconds: float = 0.0,
) -> tuple[_Held | None, str]:
    """拿锁。

    返回 `(锁, "")`；拿不到返回 `(None, 给人看的理由)`。

    ※ 默认 `wait_seconds=0`：拿不到就**立刻**返回。等待是**可选**的，而且
    必须由调用方显式给出 —— 「挂在那里不响」与「卡死了」在用户眼里一模一样，
    而这两件事的下一步完全不同。
    """
    path = lock_path(data_dir)
    # 数据目录可能还不存在（第一次跑 `demo`）。锁跟着数据走，所以这里连带
    # 把目录建出来 —— 而不是退回仓库根去放一个锁文件（那会让两个指向不同
    # 数据目录的进程互相挡住，而它们本来井水不犯河水）。
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, float(wait_seconds))

    while True:
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as e:
            # ※ **不退回「不加锁直接跑」**。那样两个写者会同时改库，而症状
            # 是「偶尔出现重复条目」—— 一个看起来像成功的失败。
            return None, (
                f"锁文件打不开：{path}\n"
                f"  原因：{type(e).__name__}: {e}\n"
                f"  这不是「有人在跑」，是锁这一层自己坏了 —— 先修它。"
            )

        try:
            got = _try_lock(fd)
        except BaseException:
            os.close(fd)
            raise

        if got:
            _write_holder(fd, command=command)
            return _Held(fd, path), ""

        os.close(fd)
        if time.monotonic() >= deadline:
            return None, _busy_message(path, command=command)
        time.sleep(_POLL_SECONDS)


def read_holder(data_dir: str | os.PathLike[str]) -> dict[str, object] | None:
    """读锁文件里记的**上一次持有者**。读不出来返回 `None`。

    ※ 它**只是诊断信息**：`None` 的含义是「文件里没有可读的记录」，
    不是「没人持锁」。锁有没有被持有，只有内核知道（见模块 docstring）。
    """
    path = lock_path(data_dir)
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        blob = os.read(fd, _MAX_META_BYTES)
    except OSError:
        # ※ 这条路在正常实现里走不到（读区间与锁区间永不重叠，见
        # `_MAX_META_BYTES`）。留着是因为它一旦发生，说明那两段重叠了 ——
        # 那时**读不出来**才是正确的结果，而不是让异常穿到调用方去。
        return None
    finally:
        os.close(fd)

    if not blob.strip():
        return None
    try:
        rec = json.loads(blob.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return rec if isinstance(rec, dict) else None


# ── 锁本身 ──────────────────────────────────────────────────────────


def _try_lock(fd: int) -> bool:
    """在 fd 上试一次排它锁。拿到返回 True，被别人占着返回 False。

    ※ **只认「被别人占着」这一种失败**，别的 `OSError` 一律抛出去。
    吞成「拿到了」会让两个写者同时跑；吞成「没拿到」会让所有写者都跑不
    起来（比如这个文件系统压根不支持锁）。两种都不该由这里猜。
    """
    if os.name == "nt":
        import msvcrt

        try:
            # msvcrt.locking 锁的是**当前文件位置**起的一段，所以先定位。
            os.lseek(fd, LOCK_BYTE, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise
        return True

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
            return False
        raise
    return True


def _encode(rec: dict[str, object]) -> bytes:
    return json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _write_holder(fd: int, *, command: str) -> None:
    """把「谁在跑、在哪跑」写进锁文件 —— **拿到锁之后**才写。

    ※ 先拿锁再写：反过来会让两个进程都往同一个文件里写自己的名字，
    而最后留下的那个不一定真的持有锁。
    """
    rec: dict[str, object] = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "command": command,
        "started_at": int(time.time()),
        "cwd": os.getcwd(),
    }
    blob = _encode(rec)

    if len(blob) > _MAX_META_BYTES:
        # 极长的工作目录（Windows 上的长路径）或极长主机名。丢掉 cwd 之后
        # 基本一定装得下。※ 不编一个假的，如实标出来。
        rec["cwd"] = None
        rec["truncated"] = True
        blob = _encode(rec)
    if len(blob) > _MAX_META_BYTES:
        # 还装不下（主机名本身异常长）。退回最小记录 —— 它一定装得下，
        # 而「读不出来」比「信息不全」坏得多：前者让报错变成一句空话。
        blob = _encode({"pid": os.getpid(), "command": command, "truncated": True})

    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, blob)
    # ※ fsync：另一个进程要在**持有者还活着的时候**读这段文字来报错。
    # 不落盘的话它可能读到一段空的，而那句报错就变成「读不出来」。
    os.fsync(fd)


class _Held:
    """一把已经拿到的锁。

    ※ 类名以 `_` 开头是**有意**的：适配层的公开类必须能在组合根里被指认
    （`tests/contracts/test_layering.py::test_适配层的实现都接上了线`），
    而这把锁**不该**由组合根装配 —— 它属于「进程启动」，不属于「一次装配」。
    标成模块内部的东西，比去给那条判据加一个豁免诚实。
    """

    def __init__(self, fd: int, path: Path) -> None:
        self._fd = fd
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def release(self) -> None:
        """放锁。可重复调用。

        ※ **只关 fd，不删文件**（见模块 docstring「锁文件为什么永远不删」）。
        `kill -9` 时内核做的是同一件事 —— 所以「正常退出」与「被强杀」
        走的是同一个机制，不存在一条只有正常退出才走得通的路。
        """
        if self._fd < 0:
            return
        fd, self._fd = self._fd, -1
        os.close(fd)

    def __enter__(self) -> _Held:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# ── 报错文案 ────────────────────────────────────────────────────────


def _busy_message(path: Path, *, command: str) -> str:
    """「谁在跑、在哪跑」—— 一句能照着下一步做的话。

    ※ 文案在**这里**生成，不搬到调用方去拼：它描述的正是这个文件里的那几
    个字段，分开写迟早漂移，而漂移的症状是「报错里说的字段在文件里找不到」。
    """
    lines = [
        "已经有一个写者在跑 —— 本进程不跑（同一时刻只允许一个写者）。",
        f"  想跑的命令：{command}",
    ]
    rec = read_holder(path.parent)
    if rec is None:
        # ※ 如实说「读不出来」，**不**编一个「未知进程」。而且要把下面那句
        # 说清楚：读不出来 ≠ 没人持锁 —— 否则用户会去删文件，而删文件
        # 解决不了任何事（锁在内核那儿，不在文件里）。
        lines.append("  持锁者：读不出来（锁文件是空的，或者被写坏了）")
        lines.append("  ※ 这不等于没人持锁 —— 锁由内核持有，与文件里写了什么无关。")
    else:
        lines.append(
            f"  持锁者：pid={rec.get('pid')} 主机={rec.get('host')} "
            f"命令={rec.get('command')}"
        )
        where = rec.get("cwd")
        lines.append(
            f"          在 {where if where else '（没记下来）'}"
            f" 起于 {_fmt_ts(int(rec.get('started_at') or 0))}"
        )
        if rec.get("truncated"):
            lines.append("          （记录被截短过，超长的那几项没写进来）")
    lines.append(f"  锁文件：{path}")
    lines.append("  等它跑完再试。它被强杀时锁会自动释放 —— 不用手动删锁文件。")
    return "\n".join(lines)


def _fmt_ts(ts: int) -> str:
    """Unix 秒 → 给人看的本地时刻。

    ※ 时区口径**从 `domain.days` 拿**，不写死 8：全项目的「本地时刻」只有
    一处口径，写死的话锁文件里的时刻与日报上的日期会对不上，而两处代码
    单看都正常。`0` 打成破折号，不打成 1970 —— 后者看起来像一个真的时刻。
    """
    if not ts:
        return "—"
    return _dt.datetime.fromtimestamp(
        ts + DEFAULT_TZ_OFFSET, tz=_dt.timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S")
