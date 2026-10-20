"""`python -m yechao <命令>` 的入口。

※ 这里是入口层的入口：它只做两件事 ——

  1. 给**写者命令**拿一把进程级互斥锁（`infrastructure/runtime/lock.py`）；
  2. 把 argv 交给 CLI，并把返回值当退出码。

## 为什么锁在这一层，而不是在 `cli.main()` 里

「同一时刻只允许一个写者」是**进程级**的约束，而 `cli.main()` 是被
`tests/interfaces/test_cli.py` 之类**在同一个进程里**反复调用的函数 ——
锁放进 `main()`，那些用例会互相抢锁，而它们跑的是同一个数据目录。
`python -m yechao` 才是「真的起了一个进程」的那条路，锁属于这条路。

`pyproject.toml` 的 `[project.scripts]` 指向**这里**
（`yechao.__main__:main`），所以装包之后的 `yechao refine` 与
`python -m yechao refine` 走的是同一条路、拿的是同一把锁。

※ 它原来指向 `interfaces.cli:main` —— 于是两条入口行为不一致：一条拿锁、
一条不拿，而从命令上完全看不出来。

## 除拿锁之外，这里不做任何判断

任何判断（比如「这个命令该不该跑」）都不该写在这儿，否则它就绕过 CLI
那一层的测试。这里只决定**要不要拿锁**，然后原样转发。
"""

from __future__ import annotations

import sys
from typing import Any

# ※ 控制台编码**在 `cli._fix_console` 那一处改**，这里只调用它、不另写一份
# （`interfaces/cli.py` 的模块 docstring 钉着这条：改编码散到各个打印点，
# 新加一个打印点就会漏掉它，而症状是「有时候乱码」）。
# 下面那句「拿不到锁」的错误就是新加的那个打印点 —— 它必须过这一关，
# 否则中文 Windows 的 GBK 控制台上它可能打不出来（strict 下直接抛）。
from yechao.interfaces.cli import _fix_console
from yechao.interfaces.cli import main as _cli_main


def _acquire_writer_lock(argv: list[str]) -> tuple[Any, str]:
    """写者命令就拿锁。返回 `(锁或 None, 拿不到时的理由或 "")`。

    ※ 为什么用 CLI 的那个参数解析器（`Args`）而不是在这儿再写一个：
    「哪个参数是数据目录」只能有**一处**答案。再写一个的话，`--data-dir`
    的规则改了这边不会跟着改，而症状是「锁落在 A 目录、数据写在 B 目录」
    —— 那时锁挡不住任何东西，却看起来一切正常。
    """
    from yechao.bootstrap import Settings
    from yechao.bootstrap import acquire_writer_lock as _acquire
    from yechao.bootstrap import is_writer_command
    from yechao.interfaces.cli import Args

    try:
        args = Args(argv)
    except Exception:
        # 参数解析不了（比如 `--port abc`）⇒ 那是 CLI 自己要报的错，这里
        # 不抢答、也不拿锁 —— 反正这一次跑不成，拿了锁也要马上放掉。
        return None, ""

    if not is_writer_command(args.command):
        # `serve` / `doctor` / `ledger` / `metrics` / `export` 走到这里。
        return None, ""
    if args.unknown:
        # 有认不出的参数 ⇒ CLI 会退出 2。不必先建锁文件。
        return None, ""

    # ※ 数据目录**从组合根问**，不在这儿拼默认值（`--data-dir` 没给时还要
    # 看 `YECHAO_DATA_DIR`）—— 与 CLI 走的是同一个 `Settings.from_paths`。
    st = Settings.from_paths(data_dir=args.data_dir)

    # ※ 锁落在**库文件所在的那个目录**，不是数据目录。
    # 默认两者是同一个（`db_path = data_dir/yechao.db`），所以行为不变；
    # 而用 `--db` 把库指到别处时，锁会跟着库走 —— 否则两个进程会用**同一个
    # 库**、拿**两把不同的锁**，而锁存在的全部意义就是不让他们同时写它。
    held, why = _acquire(st.db_path.parent, command=args.command)
    return held, why


def main(argv: list[str] | None = None) -> int:
    """进程入口。返回**退出码**。"""
    args = list(sys.argv[1:] if argv is None else argv)

    held, why = _acquire_writer_lock(args)
    if why:
        # ※ **立刻响**，不退让、不等待。第二个写者挂在那里，用户看到的是
        # 「卡死了」—— 而那与「有人在跑」是完全不同的两件事。
        _fix_console()
        sys.stderr.write(why + "\n")
        return 1

    try:
        return _cli_main(args)
    finally:
        # ※ 正常退出这条路也放锁。进程退出时内核会做同一件事（`kill -9`
        # 走的就是那条），所以这里只是让「放锁」在代码里也看得见。
        if held is not None:
            held.release()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
