"""命令行 —— **三步复现是红线**。

```
python -m yechao demo      # 离线建库，不需密钥
python -m yechao refine    # 本地模型抽取
python -m yechao serve     # 网页打开
```

这三步必须能在**任何一台机器**上跑通。
演示不可用的项目，技术再好也进不了赛。

※ 所以本模块有一条它**自己**的判据：`demo` **不许碰网络、不许要密钥**。
它只读仓库里的合成语料。这条由 `tests/interfaces/test_cli.py` 里一个
「把 socket 禁掉再跑 demo」的用例钉住 —— 那比读代码可靠，因为它测的是
**行为**而不是**设置**。

## 失败要响（这是本模块最重要的一条）

一个关键陷阱：**一个失败如果看起来像成功，它比失败本身更危险。**

所以：
  · 每一步结束都打印**实际产出**（几条消息、几个群、几条条目）
  · 失败**一律非零退出码**
  · 摄取时「有源读不到」⇒ 非零退出码，且把那几个源**列出来**
  · **绝不用默认值静默兜底** —— 配置坏了就报错，不猜

## 中文 Windows 控制台是 GBK

**emoji 和部分中文会变成乱码，而父进程拿到的就是
乱码，看起来像「程序坏了」。**

※ 处理方式是**改 stdout 的编码**，不是去掉 emoji。去掉 emoji 等于让所有
平台的用户都少看到信息，而问题只出在一个平台上。

※ 而且改编码要**在同一处**做（`_fix_console`），不能散在各个打印点 ——
散着做的话，新加一个打印点就会漏掉它，症状是「有时候乱码」。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Sequence

COMMANDS = (
    "demo",
    "ingest",
    "refine",
    "digest",
    "serve",
    "export",
    "metrics",
    "doctor",
    "cloud",
    "ledger",
)

#: 默认端口。
#: ※ **Windows 有端口保留段**，8787 / 8899
#: 可能落进去并报 `WinError 10013`。所以这里是**首选值**，
#: 失败时会自动往上找一个能用的（见 `_serve`）。
DEFAULT_PORT = 8787

USAGE = """\
夜抄 · Nocturnal Notes

用法：
  python -m yechao demo       离线建库（合成语料，不需密钥）
  python -m yechao ingest     从真实数据源读消息进库
  python -m yechao refine     本地模型抽取
  python -m yechao digest     生成日报
  python -m yechao serve      起网页（默认 http://127.0.0.1:8787）

  python -m yechao ledger     查失败记账（哪一条消息为什么没进条目）
  python -m yechao cloud      云端二次处理（只发脱敏后的条目）
  python -m yechao export     生成群清单快照（配置页用）
  python -m yechao metrics    端到端指标（对合成语料的真值）
  python -m yechao doctor     自检：环境、配置、库、模型端点

通用选项：
  --data-dir DIR    数据目录（默认 data/）
  --config-dir DIR  配置目录（默认 config/）
  --db PATH         主库路径（默认 <data-dir>/yechao.db）

命令专用选项：
  --source NAME     ingest 专用：用哪个数据源（逗号分隔可给多个）。
                    不给时默认用 synth；名字不认识会报错并列出可用的源。
  --fresh           demo 专用：先删掉旧库再建（默认**不动**已有数据）
  --run RUN_ID      ledger 专用：只看这一次跑的账
  --msg MSG_ID      ledger 专用：只看这一条消息的账（为什么没进条目）
  --limit N         refine 专用：最多跑几批（默认 200）；ledger 专用：列几次（默认 10）
"""


# ── 控制台编码 ──────────────────────────────────────────────────────


def _fix_console() -> None:
    """把 stdout / stderr 换成 UTF-8。

    ※ 只做这一件事，而且只在入口调一次。见模块 docstring。

    ※ `errors="replace"` 而不是 `"strict"`：**打印失败不该让程序崩**。
    一个乱码的问号比一个 traceback 好 —— 后者会让用户以为业务逻辑坏了。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            # 老版本 Python 或已被重定向到不支持 reconfigure 的对象。
            # 不做兜底 —— 打印本来就是尽力而为。
            pass


def _say(msg: str = "") -> None:
    print(msg)


def _ok(msg: str) -> None:
    _say(f"  √ {msg}")


def _warn(msg: str) -> None:
    _say(f"  ! {msg}")


def _fail(msg: str) -> None:
    print(f"  × {msg}", file=sys.stderr)


# ── 参数解析 ────────────────────────────────────────────────────────


class Args:
    """一个极简的参数解析。

    ※ 不用 `argparse`：本项目的参数只有五个，而 `argparse` 的错误消息是
    英文的、且会把「未知参数」与「缺参数」混成一类。这里自己写能给出
    **指得出下一步**的中文提示。
    """

    def __init__(self, argv: Sequence[str]) -> None:
        self.command = ""
        self.rest: list[str] = []
        self.data_dir: str | None = None
        self.config_dir: str | None = None
        self.db: str | None = None
        self.port: int | None = None
        #: 绑哪个地址。默认 127.0.0.1（本机只看得到自己）；
        #: 容器里必须显式给 0.0.0.0 —— 见 `_cmd_serve` 那段注释。
        self.host: str | None = None
        self.limit: int | None = None
        self.dry_run: bool = False
        self.fresh: bool = False
        #: 选中的数据源名（`--source`，可重复、也可用逗号分隔）。
        #:
        #: ※ 收的是**名字**不是实现。CLI 不许 import 适配层
        #: （`test_入口层不直接摸适配层` 钉着这条），所以「名字 → 实现」的
        #: 翻译只能发生在组合根那一侧。
        self.source: list[str] = []
        #: `ledger` 专用的两个筛选：按「哪一次跑」或「哪一条消息」查账。
        self.run_id: str = ""
        self.msg_id: int | None = None
        self.unknown: list[str] = []

        args = list(argv)
        # ※ `--help` / `-h` 是**要认的**（不是「未知参数」）。
        # 第一次写这个解析器时漏了它，`python -m yechao --help` 回了 2 +
        # 「不认识的参数：--help」—— 而那是任何人拿到一个新 CLI 时敲的第一条。
        if args and args[0] in ("-h", "--help", "help"):
            self.command = args.pop(0)
            return
        if args and not args[0].startswith("-"):
            self.command = args.pop(0)

        i = 0
        while i < len(args):
            a = args[i]
            if a == "--dry-run":
                # ※ 一个**不发请求**的开关。它的价值在隐私上，不在省钱：
                # 让人能在真的出网之前**看一眼云端会收到什么**。
                self.dry_run = True
                i += 1
                continue
            if a == "--fresh":
                # ※ 只有 `demo` 认它（在 `main` 里校验，别的命令给了就报错 ——
                # 静默忽略一个「删库」开关是最不该有的那种沉默）。
                self.fresh = True
                i += 1
                continue
            if a == "--source":
                # ※ 只有 `ingest` 认它（同样在 `main` 里校验）。
                # 静默忽略它比忽略 `--fresh` 更隐蔽：用户会以为「消息是从我要的
                # 那个源读进来的」，而实际上那条路根本没走。
                if i + 1 >= len(args):
                    self.unknown.append(a)
                    break
                # 逗号分隔与重复给都认 —— 两种写法在不同人手里都很自然，
                # 而只认一种的症状是「源少了几个」，那看起来像「源没消息」。
                self.source.extend(
                    part.strip() for part in args[i + 1].split(",") if part.strip()
                )
                i += 2
                continue
            if a in (
                "--data-dir", "--config-dir", "--db", "--port", "--limit",
                "--run", "--msg", "--host",
            ):
                if i + 1 >= len(args):
                    self.unknown.append(a)
                    break
                val = args[i + 1]
                if a == "--data-dir":
                    self.data_dir = val
                elif a == "--config-dir":
                    self.config_dir = val
                elif a == "--db":
                    self.db = val
                elif a == "--port":
                    try:
                        self.port = int(val)
                    except ValueError:
                        # 非法值不抛裸 traceback：那是「参数写错了」，
                        # 与「不认识的参数」同一档（中文提示 + 退出码 2）。
                        self.unknown.append(a)
                        break
                elif a == "--host":
                    self.host = val
                elif a == "--limit":
                    try:
                        self.limit = int(val)
                    except ValueError:
                        self.unknown.append(a)
                        break
                elif a == "--run":
                    self.run_id = val
                elif a == "--msg":
                    self.msg_id = int(val)
                i += 2
                continue
            self.unknown.append(a)
            i += 1


def _settings_from(args: Args) -> Any:
    """按命令行覆盖拼出运行期设置。

    ※ 拼路径的规则**不在这个文件里** —— 它在组合根的 `Settings.from_paths`。
    这里只负责把参数递过去。判据见 `test_入口层不直接摸适配层`。
    """
    from yechao.bootstrap import Settings

    return Settings.from_paths(
        data_dir=args.data_dir, config_dir=args.config_dir, db_path=args.db
    )


def _app(st: Any) -> Any:
    """装配一个 App。

    ※ 入口层只调**组合根**，不自己连库、不自己读 TOML ——
    所以这里出现的是 `make_app` 而不是 `SqliteStore(...)`。
    """
    from yechao.bootstrap import make_app_or_explain

    return make_app_or_explain(settings=st)

def _db_target(st: Any) -> tuple[str, str]:
    """`(后端名, 参数)` —— 数据到底写到哪儿。

    ※ 入口层从**组合根**问这件事，不自己读环境变量、不自己判 scheme：
    「用哪个后端」只有一处答案（`bootstrap.resolve_db_target`）。
    """
    from yechao.bootstrap import db_url_from_env, resolve_db_target

    return resolve_db_target(db_url_from_env(), default_path=st.db_path)


def _db_line(st: Any) -> str:
    """一行说清「数据写到哪儿」。

    ※ 为什么不能直接打印 `st.db_path`：配了 MySQL 时那个路径
    **根本不会被用** —— 而它会照常打印出来，看起来一切正常。
    那正是「一句与行为不符的提示比没有提示更坏」的形状
    （`_cmd_demo` 里关于「将覆盖」的那段注释是同一个教训）。
    """
    backend, target = _db_target(st)
    if backend == "sqlite":
        return str(target)
    return (
        f"{backend}（连接串来自 YECHAO_DB_URL；内容不打印 —— 里面有密码）"
    )



# ── 命令 ────────────────────────────────────────────────────────────


def _remove_db_files(db_path: Path) -> list[str] | None:
    """删主库**连同它的 WAL 边车**。返回删掉的文件名；删不掉返回 `None`。

    ※ **WAL 必须一起删**。SQLite 在 WAL 模式下，未落盘的事务在 `-wal` 里。
    只删 `.db` 而留下 `-wal` / `-shm`，下次打开时可能拿旧 WAL 去重放 ——
    于是你得到一个「删了但没删干净」的库，而它看起来是新建的。
    """
    removed: list[str] = []
    for p in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        try:
            if p.exists():
                p.unlink()
                removed.append(p.name)
        except OSError:
            # 删不掉最常见的原因是 `serve` 还开着、占着这个文件。
            # 不猜、不吞 —— 交给调用方把原因报给用户。
            return None
    return removed


def _cmd_demo(args: Args) -> int:
    """**离线建库，不需密钥。**

    ※ 这一步不许碰网络。它做的事：
      1. 建库（表结构）
      2. 把合成语料写进 `messages`
      3. 写默认配置（群 / 类目）
      4. 写群清单快照（配置页读它）
      5. 跑一遍**预筛**，把「筛掉了多少、为什么」如实报出来

    ※ 第 5 步看着多余，但它承担一件事：让「预筛」这个环节在 demo 阶段
    就**可见**。否则评委跑完 demo 看到一堆消息，不知道后面会发生什么。

    ## ※ 第 2 步**必须走源层**

    `demo` 走的是 `ingest` 用例（`app.ingest_deps()`），不直接问语料要消息。

    直接取的后果不是「demo 坏了」，而是**源层变成死代码**：它跑得通，但全项目没有
    任何一条路径在跑它。于是即使明天把真源写出来，也没有任何东西能证明它
    被正确接线 —— 症状是「跑得通，但没人跑它」，而它**不报错**。

    ⇒ 现在第 2 步走 `App.ingest_deps()` → `application.ingest.run()`，
    与 `ingest` 命令**同一条路**。差别只剩源是哪个：demo 用仓库里的合成源，
    `ingest` 用命令行指定的源。

    ※ 而且报告里的每一个数字（消息数、每群条数、预筛的输入）都取自
    `outcome.messages` —— **这一次真的读到的**那一批，不是再取一份。
    """
    from yechao.application import ingest as ingest_uc
    from yechao.bootstrap import CONFIG_FILENAMES
    from yechao.domain import prefilter
    from yechao.domain.categories import default_categories
    from yechao.domain.config_models import GroupConfig
    from yechao.domain.synth import GROUPS

    st = _settings_from(args)
    _say("夜抄 · 离线建库")
    _say(f"  数据目录 {st.data_dir}")
    _say(f"  配置目录 {st.config_dir}")
    _say(f"  主库     {_db_line(st)}")
    _say()

    if args.fresh and st.db_path.exists():
        removed = _remove_db_files(st.db_path)
        if removed is None:
            _fail("--fresh：旧库删不掉 —— 多半是 `serve` 还开着，占着这个文件")
            return 1
        _warn(f"--fresh：删了旧库（{'、'.join(removed)}），从头建")
    elif st.db_path.exists():
        # ※ **不静默删除**。用户可能刚在网页上改过东西 ——
        # 静默删库是最惊吓的一种数据丢失。所以默认路径**永不删**，
        # 想干净重来只能显式加 `--fresh`。
        #
        # ※ 这一段原来写的是「主库已存在，**将覆盖**」—— 那是一句**假话**，
        # 而且是实测出来的：第二次跑报「写入消息 0 条」，而人工软删的那条、
        # 以及所有人工编辑一条都没动。`init_db` 是幂等的、`upsert_messages`
        # 用的是 `INSERT OR IGNORE` ⇒ 它**什么都不覆盖**。
        #
        # 一句与行为不符的提示比没有提示更坏：用户会以为重跑 demo 已经回到
        # 干净状态，于是不去查「为什么旧条目还在」。
        _warn(f"主库已存在，保留不动：{st.db_path}")
        _warn("只补新增的语料消息，不动已有条目、软删状态与人工编辑")
        _warn("想要干净重来：python -m yechao demo --fresh")

    # ※ **一次装配，一次关闭。** 原来这里有**三处**独立的 `_app(st)` 调用，
    # 每一处都新开一条 SQLite 连接且**从不关闭** —— 于是 `demo` 跑完，库文件
    # 仍被自己这个进程占着。在真实命令行下看不出来（进程一退出就释放），
    # 但同进程重跑必现：Windows 报 `WinError 32`，`--fresh` 删不掉自己的旧库。
    # 这是「实现写对了、调用方没按约束使用它」的又一例。
    #
    # ※ 现在**只有这一处装配**：建库（`make_app` 里的 `init_db`）与写入走的是
    # 同一个 `App`。原来还额外开了一条 `init_store(st)` 的裸连接 —— 那正是
    # 「demo 绕过源层」的物证，已经删掉（见 `bootstrap.py` 末尾那段说明）。
    app_for_write = _app(st)
    try:
        outcome = ingest_uc.run(app_for_write.ingest_deps())
        if not outcome.ok:
            # ※ 合成源**不该**读不到（语料在代码里）。真读不到就是接线或素材
            # 坏了 —— 那是必须响的失败，不是「今天很安静」。
            for src_name, err in outcome.errors:
                _fail(f"源 {src_name} 读不到：{err}")
            _fail("demo 的源读不到 —— 这不是「没消息」，是坏了")
            return 1

        # ※ 报告与预筛都用**这一次真的读到的**那一批，不再另取一份。
        msgs = outcome.messages
        n_new = outcome.inserted

        # ── 写默认配置 ──
        # ※ 只在**文件不存在**时写。已经存在的配置是**用户的** ——
        # 覆盖它等于把用户的编辑丢掉，而 demo 看起来「成功」。
        wrote: list[str] = []
        groups_file = st.config_dir / CONFIG_FILENAMES["groups"]
        cats_file = st.config_dir / CONFIG_FILENAMES["categories"]
        if not groups_file.exists():
            st.ensure_dirs()
            app_for_write.config.set_groups([
                # ※ **成员昵称要写进配置** —— 它是姓名脱敏的名单来源。
                # 不写的话，`names_checked` 永远是 0，而隐私页会把那个 0
                # 显示成「查过、没漏」。实测踩过：dry-run 报「比对过 0 个」。
                GroupConfig(
                    id=g.id, name=g.name, tier=g.tier, members=g.nicknames
                )
                for g in GROUPS
            ])
            wrote.append(CONFIG_FILENAMES["groups"])
        else:
            _warn(f"{CONFIG_FILENAMES['groups']} 已存在，保留不动（那是你的配置）")

        if not cats_file.exists():
            st.ensure_dirs()
            from yechao.domain.config_models import CategorySet

            app_for_write.config.set_categories(
                CategorySet(categories=default_categories(), aliases={})
            )
            wrote.append(CONFIG_FILENAMES["categories"])

        # ── 群清单快照 ──
        import json

        snapshot = {
            "generated_at": 1792333200,  # ※ 固定值：demo 必须**确定性**
            "groups": [
                {
                    "id": g.id,
                    "name": g.name,
                    # 该群在 QQ 库里一共有多少条历史消息 ——
                    # demo 里拿语料条数顶上，并如实说明这是合成数。
                    "messages": sum(1 for m in msgs if m.group_id == g.id),
                    "last_ts": max(
                        (m.ts for m in msgs if m.group_id == g.id), default=0
                    ),
                }
                for g in GROUPS
            ],
        }
        app_for_write.write_groups_snapshot(snapshot)

        # ── 预筛（让它可见）──
        tiers = {g.id: g.tier for g in GROUPS}
        kept, dropped = prefilter.prescreen(msgs, tiers=tiers)

        # ── 报告 ──
        _say("建库完成")
        _ok(f"写入消息 {n_new} 条，共 {app_for_write.store.message_count()} 条")
        _ok(f"群 {len(GROUPS)} 个")
        if wrote:
            _ok("写了默认配置：" + "、".join(wrote))
        _say()
        _say(f"预筛：保留 {len(kept)} 条，丢弃 {len(dropped)} 条")
        by_rule: dict[str, int] = {}
        for _, d in dropped:
            by_rule[d.rule] = by_rule.get(d.rule, 0) + 1
        for rule, n in sorted(by_rule.items(), key=lambda kv: -kv[1]):
            _say(f"  · {_rule_label(rule)}：{n} 条")
        _say()
        # ※ demo 走的是**同一条**摄取用例，所以它也留了一条账
        # （`runs` 里 kind=ingest）。不说的话，「账落在哪」这件事在第一步
        # 就是隐形的 —— 而第一步正是用户第一次看见这个库的时候。
        _ledger_hint(st, outcome.run_id)
        _say()
        _say("下一步：python -m yechao refine")
        return 0
    finally:
        app_for_write.close()


def _cmd_ingest(args: Args) -> int:
    """从一个**真实数据源**读消息进库。

    ## 它与 `demo` 的差别只有一处：源在哪儿

    `demo` 的源**在仓库里**（合成语料），所以它必须离线、不需密钥 ——
    那条判据由「把 socket 禁掉再跑 demo」钉着。
    `ingest` 的源**在外部**（群库、会议转写、邮箱），读它本来就要碰外部世界，
    **「不许碰网络」这条判据对它不成立**。

    ## 但另一条判据两个命令都守：**读不到就抛、解析出 0 条也抛**

    这条由**源实现**守（端口的 docstring 写着）。CLI 的责任是**把抛出来的
    东西报出来，并且非零退出** ——

      · 「今天只有这些」与「今天有三个源没读到」必须长得不一样
      · 所以有源读不到时，**其余源的消息照样落库**（局部失败不该放大成
        全量失败），但退出码非零、失败的那几个源**逐个列出来**

    ※ 这正是「一个失败如果看起来像成功，它比失败本身更危险」的一个落点：
    静默跳过读不到的源，用户看到的是「今天只有这些」。
    """
    from yechao.application import ingest as ingest_uc
    from yechao.bootstrap import make_app_or_explain
    from yechao.domain.errors import ConfigError

    st = _settings_from(args)
    app = make_app_or_explain(settings=st)

    _say("夜抄 · 摄取消息")
    _say(f"  数据目录 {st.data_dir}")
    _say(f"  主库     {_db_line(st)}")

    try:
        # ※ 只递**名字**，由组合根翻成实例 —— 入口层不许 import 适配层。
        deps = app.ingest_deps(args.source or None)
    except ConfigError as e:
        # ※ 源名不认识 ⇒ **退出码 2**（用法错），不是 1（跑起来才失败的）。
        # 两者的下一步不同：前者去改命令行，后者去查源本身。
        _fail(str(e))
        return 2

    try:
        outcome = ingest_uc.run(deps)
        total = app.store.message_count()
    finally:
        app.close()

    _say("摄取完成")
    # ※ **逐个源报数**，而不是只报一个总数。不给 `--source` 时会用默认源 ——
    # 而「我明明想读会议转写，怎么读的是合成语料」必须是**一眼可见**的，
    # 不能靠用户记得默认值是什么。读不到的那些也在里面，值为 0。
    for name in sorted(outcome.per_source):
        _ok(f"源 {name}：读到 {outcome.per_source[name]} 条")
    _ok(f"写入消息 {outcome.inserted} 条，共 {total} 条")

    # ※ 账那一句放在**分支之前**：源读不到时正是最想问「账落在哪」
    # 的时候，而它原来只在成功路径上打印 —— 失败的那条路上，用户拿不到
    # 任何可以继续查的线索。
    _say()
    _ledger_hint(st, outcome.run_id)

    if outcome.errors:
        _say()
        for name, err in outcome.errors:
            _fail(f"源 {name} 读不到：{err}")
        _say()
        _fail(f"{len(outcome.errors)} 个源没读到 —— 上面的数字**不是全部**")
        return 1

    _say()
    _say("下一步：python -m yechao refine")
    return 0


def _rule_label(rule: str) -> str:
    """把判据名翻成人话。

    ※ **CLI 输出是给人看的**，所以这里做一层翻译。而内部契约用的是
    英文判据名（`pure_ack` 之类）—— 那是给测试与日志用的**稳定标识**，
    不该因为界面文案改动而变。
    """
    return {
        "pure_ack": "纯应答",
        "pure_symbol": "纯表情/符号",
        "too_short": "太短",
        "spam": "刷屏（同一人同群连发）",
        "no_signal": "没命中任何保留规则",
        "high_tier": "高优群",
        "role_sender": "有角色的人发的",
        "action_word": "命中动作词",
    }.get(rule, rule)


def _cmd_refine(args: Args) -> int:
    """本地模型抽取。"""
    from yechao.application import extract as extract_uc
    from yechao.bootstrap import make_app_or_explain

    st = _settings_from(args)
    app = make_app_or_explain(settings=st)

    _say("夜抄 · 本地模型抽取")
    llm = app.config.llm()
    if not llm.configured:
        # ※ **失败要响**，而且要说清**去哪配**。
        # 不兜底到一个默认端点 —— 那会把「没配」变成「连不上」，
        # 而两个要查的方向完全不同。
        _fail("还没配模型端点。")
        _say()
        _say("  在 config/settings.toml 里写：")
        _say()
        _say("    [llm]")
        _say('    base_url = "http://127.0.0.1:11434/v1/chat/completions"')
        _say('    model = "qwen3:8b"')
        _say()
        _say("  或者用命令行临时指定（不改文件）：")
        _say("    set YECHAO_DATA_DIR=... ")
        _say("  本地 Ollama 用户：先 `ollama serve`，再 `ollama pull qwen3:8b`。")
        _say()
        _say("  也可以不配 —— 网页仍可打开，但日报会是机械整理的。")
        return 2

    _say(f"  端点 {llm.base_url}")
    _say(f"  模型 {llm.model}")
    _say()

    outcome = extract_uc.run(app.extract_deps(), budget=args.limit or 200)
    _report_extract(outcome, st)

    if outcome.ledger.count("batch_failed"):
        # ※ 有整批失败 ⇒ **非零退出码**。全成了才 0。
        return 1
    return 0


def _report_extract(outcome: Any, st: Any = None) -> None:
    _say("抽取完成")
    _ok(f"条目 {len(outcome.items)} 条")
    # ※ 这一句与下面「整批失败」那一栏**必须一致**：`batches_ok` 记的是
    # 「这一批有没有产出可用的东西」，不是「这一批没把异常抛出来」
    # —— 后者会让「模型端点整个连不上」显示成「批次 22/22 成功」，
    # 而同一份输出里写着「整批失败 34 条」。见 `extract._run_batch`。
    _ok(
        f"批次 {outcome.batches_ok}/{outcome.batches_total} 成功"
    )
    # ※ 覆盖率**单独报**。它不是「模型漏了多少」——
    # 混起来会得出一条远低于实际的召回率，而真相是「这次没跑完」。
    _ok(f"覆盖率 {outcome.coverage:.0%}（真的送进模型的比例）")

    if outcome.ledger:
        _say()
        _say("记账（每一条都能回答「为什么」）：")
        for kind, n in sorted(
            outcome.ledger.summary().items(), key=lambda kv: -kv[1]
        ):
            _say(f"  · {_ledger_label(kind)}：{n}")

    # ※ 上面那些数字**只在这次输出里**，而账是落库的 —— 所以要告诉用户
    # 它落在哪、怎么看上一轮。不打印这一句的话，账落库这件事**对用户
    # 不存在**，而那正是「写了但没接」在用户体验上的样子。
    if st is not None:
        _say()
        _ledger_hint(st, outcome.run_id)


def _ledger_label(kind: str) -> str:
    """判据名 → 人话。

    ※ 未知的 kind **原样打出来**（`.get(kind, kind)`），不吞成一个「其它」
    —— 吞掉的话，将来新加一类账时界面上看不出来，而「少了一类」是无声的。
    """
    return {
        "filtered": "预筛丢弃",
        "spam": "刷屏",
        "quote_mismatch": "摘录对不上源文",
        "no_kind": "模型没给类目",
        "gate_cleared": "闸门清掉了字段（源文里查不到依据）",
        "batch_failed": "整批失败（按受影响的消息逐条记）",
        "oversized": "单条超长（静默截断的高危来源）",
        "unprocessed": "没跑到（预算耗尽 / 已取消）",
        "source_unreadable": "源读不到",
        "model_failed": "模型没跑成（日报用机械行兜底）",
        "line_unmatched": "模型写的行回连不到条目",
        "line_mechanical": "日报这一行是机械行（没有模型版本）",
    }.get(kind, kind)


def _cmd_digest(args: Args) -> int:
    """生成日报。"""
    from yechao.application import digest as digest_uc
    from yechao.bootstrap import make_app_or_explain

    st = _settings_from(args)
    app = make_app_or_explain(settings=st)

    _say("夜抄 · 生成日报")
    # ※ 默认生成**语料最后一天**的日报，不是「今天」。
    # 理由：合成语料的时间是固定的（2026-10-18 起），而「今天」会随运行时刻
    # 漂移 —— 那样跑 demo 的人在第二天就看不到日报了，
    # 而症状是「日报页是空的」，看起来像功能坏了。
    from yechao.domain.synth import CORPUS

    day = app.clock.day_of(max(c.msg.ts for c in CORPUS))
    count = 0
    for offset in range(0, 3):
        d = app.clock.day_of(
            max(c.msg.ts for c in CORPUS) - offset * 86400
        )
        # ※ 云端摘要存在审计归档旁边（`data/cloud-lines.json`）。
        # 不放进主库的理由：它是**中间产物**，重跑一次 cloud 就会换掉；
        # 而主库里的条目是**事实**，两者混在一起会让「这条摘要是哪来的」
        # 说不清。
        cloud_lines = _load_cloud_lines(app, d)
        outcome = digest_uc.run(
            app.digest_deps(), day=d, cloud_lines=cloud_lines
        )
        if outcome.digest:
            count += 1
            _ok(
                f"{d}：{outcome.digest.item_count} 条"
                + (
                    f"（模型写 {outcome.by_model} / 机械 {outcome.mechanical}）"
                    if outcome.by_model or outcome.mechanical
                    else ""
                )
            )
            if outcome.failure:
                _warn(f"模型这次没跑成：{outcome.failure}")
                _warn("上面那条是用机械行兜底的 —— 这不是错误，但要知道。")
        else:
            _ok(f"{d}：那一天没有条目")

    _say()
    if count == 0:
        _warn("没有生成任何日报 —— 先跑 `python -m yechao refine`")
        return 1
    # ※ 报了**最后一次**的 run_id（这个循环最多跑 3 天，一天一轮）。
    _ledger_hint(st, outcome.run_id)
    _say()
    _say("下一步：python -m yechao serve")
    return 0


def _cmd_cloud(args: Args) -> int:
    """云端二次处理 —— **只发脱敏后的条目，不发原文**。"""
    from yechao.application import cloud_refine
    from yechao.bootstrap import make_app_or_explain

    st = _settings_from(args)
    app = make_app_or_explain(settings=st)

    _say("夜抄 · 云端二次处理")
    cfg = app.config.cloud()
    if not cfg.configured:
        _fail("还没配云端端点。")
        _say()
        _say("  在 config/settings.toml 里写：")
        _say()
        _say("    [cloud]")
        _say('    base_url = "https://你的端点/v1/chat/completions"')
        _say('    model = "你的模型名"')
        _say()
        _say("  密钥走环境变量 YECHAO_CLOUD_API_KEY（不进配置文件）。")
        _say("  不配也能用 —— 云端这一步会跳过，其余照常。")
        return 2

    _say(f"  端点 {cfg.base_url}")
    _say(f"  模型 {cfg.model}")

    # ※ **发出去的只有脱敏后的条目** —— 这句话在这里再说一次，
    # 因为它是这一步的全部意义。
    _say("  出网内容：脱敏后的结构化条目（不含原文、人名、QQ 号、群名）")
    _say()

    from yechao.domain.synth import CORPUS

    day = app.clock.day_of(max(c.msg.ts for c in CORPUS))

    if args.dry_run:
        # ※ dry-run 要**真的显示会发什么**，不是只说「将发送 N 条」。
        # 一条能让人看见边界的记录，比一句承诺有用。
        since, until = app.clock.range_of_day(day)
        items = app.reader.window_items(since=since, until=until)
        cats = app.config.categories()
        payload, checked = cloud_refine.build_payload(
            items, kinds=cats.by_slug(), aliases=cats.aliases,
            names=app.config.nicknames(),
        )
        import json as _json

        _say(f"【试运行】以下是将要发往云端的**全部内容**（{len(payload)} 条）：")
        _say(_json.dumps(payload, ensure_ascii=False, indent=2)[:3000])
        _say()
        # ※ 这一句是**这一路的核心证据**：它把「比对了几个名字」写出来。
        # 只写「已脱敏」是承诺；写出数字才是可证伪的。
        _say(f"姓名名单：比对过 {checked} 个。"
             + ("" if checked else "※ 名单是空的 —— 姓名那一维**没查**，不是「查了没漏」。"))
        _say("没有发出任何请求。")
        return 0

    outcome = cloud_refine.run(app.cloud_deps(), day=day)

    _ok(f"发出 {outcome.sent} 条，{outcome.batches} 批")
    if outcome.kind_changed:
        _ok(f"类目改了 {outcome.kind_changed} 条")
    if outcome.lines:
        _ok(f"拿到 {len(outcome.lines)} 行摘要")
    if outcome.failures:
        for f in outcome.failures[:3]:
            _fail(f"有一批失败：{f}")
        return 1
    _say()
    _say("下一步：python -m yechao digest（会用上这些摘要）")
    return 0


def _load_cloud_lines(app: Any, day: str) -> dict[int, str]:
    """读云端复核留下的摘要。没有就返回空 —— 那是**正常**的（云端没配）。"""
    import json as _json

    path = app.settings.data_dir / "cloud-lines.json"
    if not path.exists():
        return {}
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    raw = data.get(day) or {}
    out: dict[int, str] = {}
    for k, v in raw.items():
        try:
            out[int(k)] = str(v)
        except (TypeError, ValueError):
            continue
    return out


def _ledger_hint(st: Any, run_id: str) -> None:
    """「这次的账落在哪、怎么看上次的」—— 两句话，**一处生成**。

    ※ 为什么三个命令共用同一句：账落库之后要能被**指认**。各写一句的话
    它们迟早漂移，而漂移的症状是「按 CLI 说的命令查不到」—— 一句与行为
    不符的提示比没有提示更坏。

    ※ `run_id` 为空时（不该发生）也照实说，不编一个。
    """
    if not run_id:
        _warn("这一轮没有留下 run_id —— 账没落库（这是个 bug，不是配置问题）")
        return
    _ok(f"账已落库：run_id={run_id}")
    _say(f"    主库 {_db_line(st)}")
    _say(f"    查这一次：python -m yechao ledger --run {run_id}")
    _say("    查最近几次：python -m yechao ledger")


def _cmd_ledger(args: Args) -> int:
    """查失败记账 —— 「每一条消息为什么」的那本账。

    ## ※ 它是只读的

    走 `app.reader`（只读连接，不变量 ①）—— 查账的人**不该有能力改库**。
    这不是形式主义：账是证据，一个能改证据的查询工具不叫证据。

    ## 三种查法

      · 不带参数：最近跑过哪几次（抬头）
      · `--run <id>`：这一次跑的账
      · `--msg <id>`：**这一条消息**为什么没进条目

    第三种是产品承诺（「每一条消息都能回答为什么」）最直接的那个落点。
    """
    from yechao.bootstrap import make_app_or_explain

    st = _settings_from(args)
    app = make_app_or_explain(settings=st)
    _say("夜抄 · 失败记账")
    _say(f"  主库 {_db_line(st)}")
    try:
        if args.run_id:
            _show_run(app, args.run_id)
        elif args.msg_id is not None:
            _show_message(app, args.msg_id)
        else:
            _show_runs(app, args.limit or 10)
    finally:
        # ※ 同 `_cmd_demo`：装配出来的连接要关，否则库文件一直被这个进程
        # 占着（Windows 上 `demo --fresh` 会因此删不掉自己的旧库）。
        app.close()
    return 0


def _show_runs(app: Any, limit: int) -> None:
    """最近跑过哪几次。"""
    runs = app.reader.recent_runs(limit=limit)
    if not runs:
        _warn("还没有跑过 refine / ingest / digest —— 账是空的")
        _say("  这不是「没有问题」，是「还没有证据」。")
        _say("  跑一次 `python -m yechao refine`（或 demo / ingest）之后再看。")
        return
    _say()
    _say(f"  最近 {len(runs)} 次跑（最新的在前）：")
    for r in runs:
        tail = f"送进模型 {r.model_sent}/{r.model_eligible}" if r.model_eligible else ""
        _say(
            f"    {r.run_id}  {_run_kind_label(r.kind)}  "
            f"{_fmt_ts(r.started_at, _tz_offset(app))}  "
            f"{_run_status_label(r.status)}  "
            f"看到 {r.seen} 条 · 产出 {r.produced} 条"
            + (f" · {tail}" if tail else "")
        )
    _say()
    _say("  查某一轮的账：python -m yechao ledger --run <run_id>")
    _say("  查某一条消息的账：python -m yechao ledger --msg <消息号>")


def _show_run(app: Any, run_id: str) -> None:
    """某一次跑：抬头 + 它的全部账。"""
    run = app.reader.get_run(run_id)
    if run is None:
        # ※ 与「这一轮没有账」**必须分开说**：前者是 run_id 抄错了，
        # 后者是「这一轮全成了」。混成一句「没有账」会让抄错的人以为
        # 一切正常。
        _fail(f"库里没有这一次跑：{run_id}")
        _say("  跑过的那些（最近的在前）：")
        for r in app.reader.recent_runs(limit=5):
            _say(f"    {r.run_id}  {_run_kind_label(r.kind)}  "
                 f"{_fmt_ts(r.started_at, _tz_offset(app))}")
        return
    _say()
    _say(
        f"  {_run_kind_label(run.kind)} · {_fmt_ts(run.started_at, _tz_offset(app))}"
        f" · {_run_status_label(run.status)}"
    )
    _say(
        f"    看到 {run.seen} 条 · 产出 {run.produced} 条"
        + (
            f" · 送进模型 {run.model_sent}/{run.model_eligible}"
            if run.model_eligible else ""
        )
    )
    _show_entries(app.reader.entries_of_run(run_id))


def _show_message(app: Any, msg_id: int) -> None:
    """**这一条消息**的账。"""
    _say()
    _say(f"  消息 {msg_id} 的账（最新的在前）：")
    entries = app.reader.entries_of_message(msg_id)
    if not entries:
        # ※ 空账**有两种含义**，必须分开说 —— 混成一句「没问题」会让
        # 第二种情况（压根没跑到）永远查不出来。
        _warn(f"消息 {msg_id} 一笔账都没有。两种可能，要分开看：")
        _say("    · 它进了条目（那就没有账，账记的是**没成**的事）")
        _say("    · 它压根没被任何一轮跑到（窗口之外）—— 看 `ledger` 的「看到 N 条」")
        return
    _show_entries(entries)


def _show_entries(entries: list[Any]) -> None:
    """把账逐笔打出来。

    ※ 一笔一行、字段名照原样（`msg=` / `群=` / `第 N 批`）——
    这一层**不做判断**，只搬运。要翻译成别的话是别处的事。
    """
    if not entries:
        _say("    这一轮一笔账都没有（没有没成的事）")
        return
    _say(f"    共 {len(entries)} 笔：")
    for e in entries:
        parts = [_ledger_label(e.kind)]
        if e.msg_id is not None:
            parts.append(f"消息 {e.msg_id}")
        if e.group_id is not None:
            parts.append(f"群 {e.group_id}")
        if e.batch_no is not None:
            parts.append(f"第 {e.batch_no} 批")
        if e.item_id is not None:
            parts.append(f"条目 {e.item_id}")
        if e.reason:
            parts.append(f"原因 {e.reason}")
        _say("      · " + " · ".join(parts))


def _tz_offset(app: Any) -> int:
    """显示用的时区偏移，**从时钟拿**。

    ※ 不写死 8：全项目「本地时刻」只有一处口径（`domain/days.py`），
    账的显示跟着它走 —— 写死的话，改了时区之后账上的时间与日报的日期
    会对不上，而两处代码单看都正常。
    """
    return int(getattr(app.clock, "tz_offset", 8 * 3600))


def _fmt_ts(ts: int, tz_offset: int) -> str:
    """Unix 秒 → 给人看的本地时刻。

    ※ `0`（没有时间戳）打成一个破折号，**不**打成 1970 —— 后者看起来
    像一个真的时刻，而它只是「没记」。
    """
    import datetime as _dt

    if not ts:
        return "—"
    return _dt.datetime.fromtimestamp(
        ts + tz_offset, tz=_dt.timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S")


def _run_kind_label(kind: str) -> str:
    return {
        "refine": "本地抽取",
        "ingest": "摄取",
        "digest": "日报",
    }.get(kind, kind)


def _run_status_label(status: str) -> str:
    """※ 「ok」不许打成「成功」—— 它答的是「这一轮**有没有没成的事**」，
    不是「做完了」。两者混起来会让「跑完了但有 3 批失败」读成「成功」。
    """
    return {"ok": "无失败", "failed": "有失败"}.get(status, status)


def _cmd_export(args: Args) -> int:
    """生成群清单快照（配置页读它）。"""
    from yechao.domain.synth import GROUPS

    st = _settings_from(args)
    _say("夜抄 · 导出群清单快照")

    msgs = list(_corpus_messages())
    payload = {
        "generated_at": 1792333200,
        "groups": [
            {
                "id": g.id,
                "name": g.name,
                "messages": sum(1 for m in msgs if m.group_id == g.id),
                "last_ts": max((m.ts for m in msgs if m.group_id == g.id), default=0),
            }
            for g in GROUPS
        ],
    }
    app = _app(st)
    try:
        path = app.write_groups_snapshot(payload)
    finally:
        # ※ 同 `_cmd_demo`：装配出来的连接要关，否则库文件一直被这个进程占着。
        app.close()
    _ok(f"写了 {path}（{len(payload['groups'])} 个群）")
    return 0


def _cmd_metrics(args: Args) -> int:
    """端到端指标 —— 对合成语料的真值跑一遍。"""
    from yechao.application import metrics as metrics_uc
    from yechao.domain import prefilter, scrub as scrub_mod
    from yechao.domain.synth import CORPUS, GROUPS, expectations

    _say("夜抄 · 端到端指标（对合成语料的真值）")
    _say()
    _say("※ 这些数字**只对合成语料**成立 —— 真实数据没有真值，")
    _say("   隐私原因下永远无法人工标注。指标的意义是证明流水线**可被度量**，")
    _say("   而不是声称一个普适的准确率。")
    _say()

    expects = expectations()
    msgs = [c.msg for c in CORPUS]
    tiers = {g.id: g.tier for g in GROUPS}

    # ── 预筛 ──
    kept, dropped = prefilter.prescreen(msgs, tiers=tiers)
    kept_ids = {m.msg_id for m in kept}
    decisions = {m.msg_id: (m.msg_id in kept_ids) for m in msgs}
    recall, false_keep = metrics_uc.prefilter_metrics(decisions, expects)

    # ── 脱敏 ──
    names = [n for g in GROUPS for n in g.nicknames]
    flagged_nums: set[str] = set()
    flagged_names: set[str] = set()
    for c in CORPUS:
        _, report = scrub_mod.scrub(c.msg.content, names)
        for h in report.hits:
            # ※ **用 `h.text`，不要用 `c.msg.content[h.start:h.end]`。**
            #
            # `start` / `end` 是替换过程中的坐标，不是原文坐标。
            # 第一版拿它们去切原文，切出来的是 `<号码>认领。` 这种垃圾 ——
            # 于是指标报了 5/7 的召回与 100% 的误伤，**两个错得看起来
            # 合理的数**。指标错了不会报错，只会给出一个指向错误方向的结论
            # 。
            if h.kind == "name":
                flagged_names.add(h.text)
            else:
                flagged_nums.add(h.text)
    s_recall, s_fp = metrics_uc.scrub_metrics(
        flagged_numbers=flagged_nums, flagged_names=flagged_names, expects=expects
    )

    report = metrics_uc.MetricsReport(
        corpus_size=len(CORPUS),
        # ※ 这里没有真跑模型，所以覆盖率如实报「预筛之后全部会被送进模型」。
        # **不假装**跑过 —— 那会是本文件最糟的一种谎。
        prefilter_recall=recall,
        prefilter_false_keep=false_keep,
        scrub_recall=s_recall,
        scrub_false_positive=s_fp,
    )
    report.coverage.hit = len(kept)
    report.coverage.total = len(msgs)

    # ── 模型那一侧：读**库里真实落下的条目**，与本文件的真值比 ──
    #
    # ※ 这里读的是 `data/yechao.db` 里 refine 真的写下的东西 ——
    # 不是重新跑一遍。理由：指标测的是**跑过的那一次**。
    # 重跑一遍会让「上次跑得好」与「这次跑得好」混起来，
    # 而报告不告诉你它测的是哪一次。
    from yechao.bootstrap import make_app_or_explain

    report.coverage = metrics_uc.Counts()
    st = _settings_from(args)
    try:
        app = make_app_or_explain(settings=st)
    except SystemExit:
        app = None

    # ※ **不要用 `st.db_path.exists()` 判「跑过 refine 没有」。**
    # 配了 `YECHAO_DB_URL` 时那个路径根本不会被用（云上指 RDS），于是这一段
    # 永远被跳过 ⇒ 报告永远说「要先跑 refine 才有数」，而模型那一侧的数字
    # 永远不出现。同一个判据在 `_db_line` 里已经论证过一遍了。
    if app is not None:
        items = app.reader.all_items()
        produced: list[dict] = []
        sent_ids: set[int] = set()
        for it in items:
            srcs = app.reader.sources_of(it.item_id)
            mids = [s["msg_id"] for s in srcs]
            sent_ids.update(mids)
            exp = {m: expects.get(m) for m in mids}
            fields = sum(
                1
                for cond in (it.detail, it.place, it.amount, it.deadline_ts, it.start_ts)
                if cond
            )
            unsupported = sum(
                1
                for cond, ok in (
                    # `mids` 为空时（条目没有来源）不要取 `mids[0]` ——
                    # 那会是一段 traceback，而这里该说的是「这条条目没有来源」。
                    (it.place, bool(mids) and exp.get(mids[0]) and exp[mids[0]].place_text),
                    (it.amount, bool(mids) and exp.get(mids[0]) and exp[mids[0]].amount_text),
                )
                if cond and not ok
            )
            produced.append({
                "msg_ids": mids,
                "kind": it.kind,
                "has_deadline": it.deadline_ts is not None,
                "has_start": it.start_ts is not None,
                "fields": fields,
                "unsupported_fields": unsupported,
            })

        eligible = {m.msg_id for m in kept}
        model_rep = metrics_uc.model_metrics(
            produced=produced,
            expects=expects,
            sent_msg_ids=sent_ids,
            eligible_msg_ids=eligible,
        )
        report.coverage = model_rep.coverage
        report.kind_accuracy = model_rep.kind_accuracy
        report.deadline_recall = model_rep.deadline_recall
        report.start_recall = model_rep.start_recall
        report.hallucination = model_rep.hallucination
        report.excluded_not_applicable = model_rep.excluded_not_applicable

        _say()
        _say("以下是**模型那一侧**的数字（读库里 refine 真的落下的条目）：")
        for line in report.lines()[4:]:
            _say(f"  {line}")
        if report.excluded_not_applicable:
            _say("  " + "=" * 50)
            _say(
                f"  ※ 有 {report.excluded_not_applicable} 条**判据不适用**"
                f"（源文里就没有那一格该有的信息）—— 它们没进任何分母"
            )
    else:
        _say()
        _say("※ 类目/抽取/幻觉三项指标**要先跑 refine 才有数**：")
        _say("   那是模型的行为，没跑过就不能编一个数字出来。")

    _say()
    _say("口径说明（这几项都会造出「假缺陷」，所以写在这里）：")
    _say("  · 覆盖率的分母是**该送进模型的**，未处理的不算进模型指标的分母")
    _say("  · 开始时刻的分母**不含**「源文只给了相对时间」的那些（结构上填不进）")
    _say("  · 幻觉率的分母是**所有被检查过的字段**，不只是有产出的条目")
    return 0


def _cmd_doctor(args: Args) -> int:
    """自检：环境、配置、库、模型端点。

    ※ 它存在的理由：部署链路的问题有**同一个形状** ——
    「两份东西之间的覆盖关系」。这类问题**漏一项不会报错**。
    所以这里逐项检查，并且**每一条都说清去哪修**。
    """
    import json
    import urllib.error
    import urllib.request

    from yechao.bootstrap import make_app_or_explain

    st = _settings_from(args)
    _say("夜抄 · 自检")
    _say()
    problems = 0
    #: ※ **提醒与问题要分开数。** 提醒不是失败（本机不启用缓存是正常配置），
    #: 但它们也**不是「通过」** —— 见结尾那段。
    warns = 0

    _say("Python")
    _ok(f"{sys.version.split()[0]}（需要 ≥3.11）")
    if sys.version_info < (3, 11):
        _fail("版本太低 —— 本项目用了 `tomllib`（3.11 起内置）")
        problems += 1

    _say()
    _say("路径")
    _say(f"  数据目录：{st.data_dir}")
    _say(f"  配置目录：{st.config_dir}")
    # ※ 这一行必须报**真正会被用的那个**（见 `_db_line`）。
    _say(f"  主库：{_db_line(st)}")
    backend, target = _db_target(st)
    if backend == "sqlite":
        if not Path(target).exists():
            _fail("主库不存在 —— 先跑 `python -m yechao demo`")
            problems += 1
        else:
            _ok("主库在")
    else:
        # ※ MySQL 那边没有「文件在不在」可查 —— 能不能用由下面
        # 「存储与缓存」那一段的探活回答。这里**不**假装检查过了。
        _ok(f"后端是 {backend}；连通性由下面的探活回答")

    _say()
    _say("配置")
    try:
        app = make_app_or_explain(settings=st)
    except SystemExit as e:
        _fail(str(e))
        return 1

    try:
        groups = app.config.groups()
        _ok(f"群 {len(groups)} 个")
    except Exception as e:
        _fail(f"groups.toml：{e}")
        problems += 1
    try:
        cats = app.config.categories()
        _ok(f"类目 {len(cats.categories)} 个")
    except Exception as e:
        _fail(f"categories.toml：{e}")
        problems += 1
    try:
        people = app.config.persons()
        _ok(f"人物 {len(people)} 个")
    except Exception as e:
        _fail(f"persons.toml：{e}")
        problems += 1

    _say()
    _say("只读连接（不变量 ①）")
    try:
        ok, why = app.readonly_actually_rejects_writes()
        (_ok if ok else _fail)(why)
        if not ok:
            problems += 1
    except Exception as e:
        _fail(f"只读连接打不开：{e}")
        problems += 1

    _say()
    _say("存储与缓存")
    # ※ 与 `GET /api/health` **同一份判断**（`app.health()`），不是另写一套。
    # 两处各写一份的话，「命令行说没事、网页说坏了」会变成一种可能，
    # 而那种不一致**没有一处会报错**。
    health = app.health()
    db = health["database"]
    (_ok if db["ok"] else _fail)(f"库 {db['backend']}：{db['detail']}")
    if not db["ok"]:
        problems += 1

    cache = health["cache"]
    if cache["enabled"]:
        (_ok if cache["ok"] else _fail)(
            f"缓存 {cache['backend']}：{cache['detail']}"
        )
        if not cache["ok"]:
            problems += 1
        stats = cache["stats"]
        # ※ `hit_rate` 是 `None` 时**不打印百分数** —— 那是「还没查过」，
        # 与 `0%`（查过但一次没命中）是两件事。打印成 0% 会让人去查
        # 一个不存在的问题。
        rate = "—" if stats["hit_rate"] is None else f"{stats['hit_rate']:.0%}"
        _say(
            f"    本进程内：命中 {stats['hits']} / 未命中 {stats['misses']}"
            f" / 命中率 {rate}"
        )
    else:
        # ※ 空实现是**警告**不是失败：本机不启用缓存是正常配置。
        # 但要说出来 —— 「没启用」与「启用了但没命中」长得一样，
        # 而后者是故障。
        _warn(f"缓存 {cache['backend']}：{cache['detail']}")
        warns += 1

    _say()
    _say("失败记账")
    # ※ 这一节回答的是「**上一次跑到底发生了什么**」—— 而那件事原来
    # 进程一退出就没证据了。放在 doctor 里，是因为自检的用途正是
    # 「出问题时先看哪儿」，而「上一轮跑了什么」是第一个要看的。
    try:
        last = app.reader.recent_runs(limit=1)
    except Exception as e:
        _fail(f"账读不出来：{type(e).__name__}: {e}")
        problems += 1
        last = None
    if last:
        r = last[0]
        entries = app.reader.entries_of_run(r.run_id)
        _ok(
            f"最近一次：{_run_kind_label(r.kind)} "
            f"{_fmt_ts(r.started_at, _tz_offset(app))} "
            f"{_run_status_label(r.status)}"
        )
        _say(
            f"    看到 {r.seen} 条 · 产出 {r.produced} 条"
            + (
                f" · 送进模型 {r.model_sent}/{r.model_eligible}"
                if r.model_eligible else ""
            )
        )
        if entries:
            by: dict[str, int] = {}
            for e in entries:
                by[e.kind] = by.get(e.kind, 0) + 1
            _say(
                f"    账 {len(entries)} 笔："
                + "、".join(
                    f"{_ledger_label(k)} {n}"
                    for k, n in sorted(by.items(), key=lambda kv: -kv[1])
                )
            )
        else:
            _say("    账 0 笔（这一轮没有没成的事）")
        _say(f"    查它：python -m yechao ledger --run {r.run_id}")
    elif last is not None:
        # ※ 「一笔账都没有」与「没读过」是两句话：空账**不是**「没问题」，
        # 它只说明**还没有跑过**（或者跑的都是空轮）。
        _warn("还没有跑过 refine / ingest / digest —— 账是空的（不是「没问题」）")
        warns += 1

    _say()
    _say("模型端点")
    llm = app.config.llm()
    if not llm.configured:
        _warn("没配 —— refine 会拒绝跑（网页仍可用）")
        warns += 1
    else:
        _say(f"  {llm.base_url} · {llm.model}")
        try:
            req = urllib.request.Request(
                llm.base_url.replace("/chat/completions", "/models"),
                headers={"Authorization": f"Bearer {os.environ.get('YECHAO_API_KEY', '')}"},
            )
            urllib.request.urlopen(req, timeout=5).close()
            _ok("连得上")
        except urllib.error.HTTPError as e:
            _warn(f"连上了但回 {e.code}（多半是端点不提供 /models，不影响）")
            warns += 1
        except Exception as e:
            _fail(f"连不上：{e}")
            problems += 1

    _say()
    _say("网页产物")
    dist = app.web_dist
    if dist is not None:
        _ok(f"web/dist 在（{len(list(dist.rglob('*')))} 个文件）")
    else:
        _warn("web/dist 不在 —— serve 会回 501。跑 `cd web && npm ci && npm run build`")
        warns += 1

    _say()
    if problems:
        _fail(f"{problems} 项需要处理")
        return 1
    if warns:
        # ※ **「全部通过」是一句会误导人的话。**
        #
        # 上面标 `!` 的项里有「没配模型端点」—— 那会让 `refine` 直接拒绝跑，
        # 也就是整个「模型抽取」这一步不存在。干净 clone 实测过：那一项标 `!`，
        # 而结尾照样印「全部通过」，看起来像可交付状态。
        #
        # 提醒**不是失败**（本机不启用缓存、不配模型都是正常配置，所以退出码仍是 0），
        # 但**不能把它们说成通过**。两句话分开说。
        _say(f"通过，但有 {warns} 条提醒（上面标 `!` 的那些）—— 不是全绿。")
        return 0
    _say("全部通过。")
    return 0


def _cmd_serve(args: Args) -> int:
    """起网页。

    ※ Windows 有**端口保留段**，
    8787 / 8899 可能落进去并报 `WinError 10013`。
    所以这里会**自动往上找**一个能用的端口，并把实际用的那个**打印出来**
    —— 静默换端口会让用户按 README 打开一个连不上的地址。
    """
    import uvicorn

    from yechao.bootstrap import make_app_or_explain
    from yechao.interfaces.api import create_app

    st = _settings_from(args)
    app_obj = make_app_or_explain(settings=st, readonly=False)

    port, why = _pick_port(args.port or DEFAULT_PORT)
    api = create_app(app_obj)

    _say("夜抄 · 网页")
    if why:
        _warn(why)
    # ※ 把**用的是哪个后端**打印出来。不打印的话，「用了默认值」与
    # 「用户配了他以为的那个」看起来一样 —— 而这两件事的下一步完全不同
    # （一个是「你其实连的是本地文件」，一个是「对，就是你配的那个库」）。
    _say(f"  存储 {app_obj.store.backend_name()}"
         f" · 缓存 {app_obj.cache.backend_name()}")
    _say(f"  打开 http://127.0.0.1:{port}")
    _say("  Ctrl+C 退出")
    _say()
    # 默认只绑回环，容器里要显式绑 0.0.0.0。
    # Docker 的端口发布是 DNAT 到容器 eth0，不是容器回环 —— 绑 127.0.0.1 时
    # 宿主的 YECHAO_PORT 永远连不上，而 compose 的 healthcheck 恰恰是从
    # 容器内探 127.0.0.1:8000 ⇒ 探活绿、页面打不开。
    # 那是本项目最怕的形状，而且本地 `python -m yechao serve` 永远碰不到。
    uvicorn.run(
        api, host=args.host or "127.0.0.1", port=port, log_level="warning"
    )
    return 0


def _pick_port(preferred: int, *, tries: int = 12) -> tuple[int, str]:
    """找一个能绑的端口。

    返回 `(端口, 为什么要换 —— 没换就是空串)`。

    ※ 换端口时**必须说出来**。静默换端口会让用户按 README 打开
    `127.0.0.1:8787` 看到一个连不上的页面，而他会以为是服务没起来。
    """
    import socket

    for offset in range(tries):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError as e:
                if offset == 0:
                    reason = (
                        f"端口 {preferred} 用不了（{e.strerror or e}）。"
                        f"{'Windows 有端口保留段，' if os.name == 'nt' else ''}"
                        f"已自动换端口 —— 请用下面这个地址。"
                    )
                continue
            if offset == 0:
                return port, ""
            return port, reason
    return preferred, f"端口 {preferred}..{preferred + tries - 1} 都被占用，仍试 {preferred}"


def _corpus_messages() -> tuple[Any, ...]:
    from yechao.domain.synth import messages

    return messages()


# ── 入口 ────────────────────────────────────────────────────────────


_HANDLERS = {
    "demo": _cmd_demo,
    "ingest": _cmd_ingest,
    "refine": _cmd_refine,
    "digest": _cmd_digest,
    "export": _cmd_export,
    "metrics": _cmd_metrics,
    "doctor": _cmd_doctor,
    "cloud": _cmd_cloud,
    "serve": _cmd_serve,
    "ledger": _cmd_ledger,
}


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口。返回**退出码**。

    ※ 它返回而不是 `sys.exit` —— 那样测试能直接断言退出码，
    而不用去截 `SystemExit`。
    """
    _fix_console()

    args = Args(argv if argv is not None else sys.argv[1:])

    if args.unknown:
        _fail(f"不认识的参数：{' '.join(args.unknown)}")
        _say()
        _say(USAGE)
        return 2

    if not args.command or args.command in ("-h", "--help", "help"):
        _say(USAGE)
        return 0

    if args.fresh and args.command != "demo":
        # ※ **不静默忽略一个「删库」开关**。给了它却没生效，用户会以为库已经
        # 重建过了 —— 那正是「失败看起来像成功」。
        _fail(f"--fresh 只对 demo 有意义（当前命令：{args.command}）")
        return 2

    if args.source and args.command != "ingest":
        # ※ 同 `--fresh`：**不静默忽略一个选择开关**。给了 `--source` 却没生效，
        # 用户会以为消息是从他指定的那个源读进来的 —— 而那条路根本没走，
        # 报告里的数字来自别处。这类「失败看起来像成功」必须响。
        _fail(f"--source 只对 ingest 有意义（当前命令：{args.command}）")
        return 2

    if (args.run_id or args.msg_id is not None) and args.command != "ledger":
        # ※ 同 `--source`：**不静默忽略一个选择开关**。给了 `--run` 却没生效，
        # 用户会以为「只看这一次」，而实际上看到的是全部 —— 而那个数字看起来
        # 完全正常。
        _fail(f"--run / --msg 只对 ledger 有意义（当前命令：{args.command}）")
        return 2

    handler = _HANDLERS.get(args.command)
    if handler is None:
        _fail(f"不认识的命令：{args.command}")
        _say()
        _say(USAGE)
        return 2

    from yechao.domain.errors import YechaoError

    try:
        return handler(args)
    except YechaoError as e:
        # ※ 领域错误**不当 traceback 打** —— 它是「用户能修的问题」，
        # 要给一句能照着做的提示。而 traceback 看起来像「程序坏了」。
        _fail(str(e))
        return 1
    except KeyboardInterrupt:
        _say()
        _warn("被中断")
        # ※ 130 是「被 SIGINT 终止」的约定退出码。
        return 130
