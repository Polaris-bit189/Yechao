"""命令行 —— **三步复现是红线**。

```
python -m yechao demo      # 离线建库，不需密钥
python -m yechao refine    # 本地模型抽取
python -m yechao serve     # 网页打开
```

这三步必须能在**任何一台机器**上跑通。
演示不可用的项目，技术再好也进不了赛。

⚠️ 所以本模块有一条它**自己**的判据：`demo` **不许碰网络、不许要密钥**。
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

⚠️ 处理方式是**改 stdout 的编码**，不是去掉 emoji。去掉 emoji 等于让所有
平台的用户都少看到信息，而问题只出在一个平台上。

⚠️ 而且改编码要**在同一处**做（`_fix_console`），不能散在各个打印点 ——
散着做的话，新加一个打印点就会漏掉它，症状是「有时候乱码」。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Sequence

COMMANDS = ("demo", "refine", "digest", "serve", "export", "metrics", "doctor", "cloud")

#: 默认端口。
#: ⚠️ **Windows 有端口保留段**，8787 / 8899
#: 可能落进去并报 `WinError 10013`。所以这里是**首选值**，
#: 失败时会自动往上找一个能用的（见 `_serve`）。
DEFAULT_PORT = 8787

USAGE = """\
夜抄 · Nocturnal Notes

用法：
  python -m yechao demo       离线建库（合成语料，不需密钥）
  python -m yechao refine     本地模型抽取
  python -m yechao digest     生成日报
  python -m yechao serve      起网页（默认 http://127.0.0.1:8787）

  python -m yechao cloud      云端二次处理（只发脱敏后的条目）
  python -m yechao export     生成群清单快照（配置页用）
  python -m yechao metrics    端到端指标（对合成语料的真值）
  python -m yechao doctor     自检：环境、配置、库、模型端点

通用选项：
  --data-dir DIR    数据目录（默认 data/）
  --config-dir DIR  配置目录（默认 config/）
  --db PATH         主库路径（默认 <data-dir>/yechao.db）
"""


# ── 控制台编码 ──────────────────────────────────────────────────────


def _fix_console() -> None:
    """把 stdout / stderr 换成 UTF-8。

    ⚠️ 只做这一件事，而且只在入口调一次。见模块 docstring。

    ⚠️ `errors="replace"` 而不是 `"strict"`：**打印失败不该让程序崩**。
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
    _say(f"  ✓ {msg}")


def _warn(msg: str) -> None:
    _say(f"  ! {msg}")


def _fail(msg: str) -> None:
    print(f"  ✗ {msg}", file=sys.stderr)


# ── 参数解析 ────────────────────────────────────────────────────────


class Args:
    """一个极简的参数解析。

    ⚠️ 不用 `argparse`：本项目的参数只有五个，而 `argparse` 的错误消息是
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
        self.limit: int | None = None
        self.dry_run: bool = False
        self.unknown: list[str] = []

        args = list(argv)
        # ⚠️ `--help` / `-h` 是**要认的**（不是「未知参数」）。
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
                # ⚠️ 一个**不发请求**的开关。它的价值在隐私上，不在省钱：
                # 让人能在真的出网之前**看一眼云端会收到什么**。
                self.dry_run = True
                i += 1
                continue
            if a in ("--data-dir", "--config-dir", "--db", "--port", "--limit"):
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
                    self.port = int(val)
                elif a == "--limit":
                    self.limit = int(val)
                i += 2
                continue
            self.unknown.append(a)
            i += 1


def _settings_from(args: Args) -> Any:
    """按命令行覆盖拼出运行期设置。

    ⚠️ 拼路径的规则**不在这个文件里** —— 它在组合根的 `Settings.from_paths`。
    这里只负责把参数递过去。判据见 `test_入口层不直接摸适配层`。
    """
    from yechao.bootstrap import Settings

    return Settings.from_paths(
        data_dir=args.data_dir, config_dir=args.config_dir, db_path=args.db
    )


def _app(st: Any) -> Any:
    """装配一个 App。

    ⚠️ 入口层只调**组合根**，不自己连库、不自己读 TOML ——
    所以这里出现的是 `make_app` 而不是 `SqliteStore(...)`。
    """
    from yechao.bootstrap import make_app_or_explain

    return make_app_or_explain(settings=st)


# ── 命令 ────────────────────────────────────────────────────────────


def _cmd_demo(args: Args) -> int:
    """**离线建库，不需密钥。**

    ⚠️ 这一步不许碰网络。它做的事：
      1. 建库（表结构）
      2. 把合成语料写进 `messages`
      3. 写默认配置（群 / 类目）
      4. 写群清单快照（配置页读它）
      5. 跑一遍**预筛**，把「筛掉了多少、为什么」如实报出来

    ⚠️ 第 5 步看着多余，但它承担一件事：让「预筛」这个环节在 demo 阶段
    就**可见**。否则评委跑完 demo 看到一堆消息，不知道后面会发生什么。
    """
    from yechao.bootstrap import CONFIG_FILENAMES, init_store
    from yechao.domain import prefilter
    from yechao.domain.categories import default_categories
    from yechao.domain.config_models import GroupConfig
    from yechao.domain.synth import GROUPS

    st = _settings_from(args)
    _say("夜抄 · 离线建库")
    _say(f"  数据目录 {st.data_dir}")
    _say(f"  配置目录 {st.config_dir}")
    _say(f"  主库     {st.db_path}")
    _say()

    if st.db_path.exists():
        # ⚠️ **不静默删除**。用户可能刚在网页上改过东西 ——
        # 静默删库是最惊吓的一种数据丢失。
        _warn(f"主库已存在，将覆盖：{st.db_path}")

    store = init_store(st)
    try:
        n_new = store.upsert_messages(list(_corpus_messages()))

        # ── 写默认配置 ──
        # ⚠️ 只在**文件不存在**时写。已经存在的配置是**用户的** ——
        # 覆盖它等于把用户的编辑丢掉，而 demo 看起来「成功」。
        wrote: list[str] = []
        groups_file = st.config_dir / CONFIG_FILENAMES["groups"]
        cats_file = st.config_dir / CONFIG_FILENAMES["categories"]
        if not groups_file.exists():
            st.ensure_dirs()
            app_for_write = _app(st)
            app_for_write.config.set_groups([
                # ⚠️ **成员昵称要写进配置** —— 它是姓名脱敏的名单来源。
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

            _app(st).config.set_categories(
                CategorySet(categories=default_categories(), aliases={})
            )
            wrote.append(CONFIG_FILENAMES["categories"])

        # ── 群清单快照 ──
        import json

        snapshot = {
            "generated_at": 1792333200,  # ⚠️ 固定值：demo 必须**确定性**
            "groups": [
                {
                    "id": g.id,
                    "name": g.name,
                    # 「该群在 QQ 库里的全量历史消息条数」——
                    # demo 里就用语料条数，如实说明它是合成数。
                    "messages": sum(1 for m in _corpus_messages() if m.group_id == g.id),
                    "last_ts": max(
                        (m.ts for m in _corpus_messages() if m.group_id == g.id),
                        default=0,
                    ),
                }
                for g in GROUPS
            ],
        }
        _app(st).write_groups_snapshot(snapshot)

        # ── 预筛（让它可见）──
        tiers = {g.id: g.tier for g in GROUPS}
        kept, dropped = prefilter.prescreen(list(_corpus_messages()), tiers=tiers)

        # ── 报告 ──
        _say("建库完成")
        _ok(f"写入消息 {n_new} 条，共 {store.message_count()} 条")
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
        _say("下一步：python -m yechao refine")
        return 0
    finally:
        store.conn.close()


def _rule_label(rule: str) -> str:
    """把判据名翻成人话。

    ⚠️ **CLI 输出是给人看的**，所以这里做一层翻译。而内部契约用的是
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
        # ⚠️ **失败要响**，而且要说清**去哪配**。
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
    _report_extract(outcome)

    if outcome.ledger.count("batch_failed"):
        # ⚠️ 有整批失败 ⇒ **非零退出码**。全成了才 0。
        return 1
    return 0


def _report_extract(outcome: Any) -> None:
    _say("抽取完成")
    _ok(f"条目 {len(outcome.items)} 条")
    _ok(
        f"批次 {outcome.batches_ok}/{outcome.batches_total} 成功"
    )
    # ⚠️ 覆盖率**单独报**。它不是「模型漏了多少」——
    # 混起来会得出一条远低于实际的召回率，而真相是「这次没跑完」。
    _ok(f"覆盖率 {outcome.coverage:.0%}（真的送进模型的比例）")

    if not outcome.ledger:
        return
    _say()
    _say("记账（每一条都能回答「为什么」）：")
    for kind, n in sorted(outcome.ledger.summary().items(), key=lambda kv: -kv[1]):
        _say(f"  · {_ledger_label(kind)}：{n}")


def _ledger_label(kind: str) -> str:
    return {
        "filtered": "预筛丢弃",
        "spam": "刷屏",
        "quote_mismatch": "摘录对不上源文",
        "no_kind": "模型没给类目",
        "gate_cleared": "闸门清掉了字段（源文里查不到依据）",
        "batch_failed": "整批失败",
        "oversized": "单条超长（静默截断的高危来源）",
        "unprocessed": "没跑到（预算耗尽 / 已取消）",
    }.get(kind, kind)


def _cmd_digest(args: Args) -> int:
    """生成日报。"""
    from yechao.application import digest as digest_uc
    from yechao.bootstrap import make_app_or_explain

    st = _settings_from(args)
    app = make_app_or_explain(settings=st)

    _say("夜抄 · 生成日报")
    # ⚠️ 默认生成**语料最后一天**的日报，不是「今天」。
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
        # ⚠️ 云端摘要存在审计归档旁边（`data/cloud-lines.json`）。
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

    # ⚠️ **发出去的只有脱敏后的条目** —— 这句话在这里再说一次，
    # 因为它是这一步的全部意义。
    _say("  出网内容：脱敏后的结构化条目（不含原文、人名、QQ 号、群名）")
    _say()

    from yechao.domain.synth import CORPUS

    day = app.clock.day_of(max(c.msg.ts for c in CORPUS))

    if args.dry_run:
        # ⚠️ dry-run 要**真的显示会发什么**，不是只说「将发送 N 条」。
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
        # ⚠️ 这一句是**这一路的核心证据**：它把「比对了几个名字」写出来。
        # 只写「已脱敏」是承诺；写出数字才是可证伪的。
        _say(f"姓名名单：比对过 {checked} 个。"
             + ("" if checked else "⚠️ 名单是空的 —— 姓名那一维**没查**，不是「查了没漏」。"))
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
    path = _app(st).write_groups_snapshot(payload)
    _ok(f"写了 {path}（{len(payload['groups'])} 个群）")
    return 0


def _cmd_metrics(args: Args) -> int:
    """端到端指标 —— 对合成语料的真值跑一遍。"""
    from yechao.application import metrics as metrics_uc
    from yechao.domain import prefilter, scrub as scrub_mod
    from yechao.domain.synth import CORPUS, GROUPS, expectations

    _say("夜抄 · 端到端指标（对合成语料的真值）")
    _say()
    _say("⚠️ 这些数字**只对合成语料**成立 —— 真实数据没有真值，")
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
            # ⚠️ **用 `h.text`，不要用 `c.msg.content[h.start:h.end]`。**
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
        # ⚠️ 这里没有真跑模型，所以覆盖率如实报「预筛之后全部会被送进模型」。
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
    # ⚠️ 这里读的是 `data/yechao.db` 里 refine 真的写下的东西 ——
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

    if app is not None and st.db_path.exists():
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
                    (it.place, exp.get(mids[0]) and exp[mids[0]].place_text),
                    (it.amount, exp.get(mids[0]) and exp[mids[0]].amount_text),
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
                f"  ⚠️ 有 {report.excluded_not_applicable} 条**判据不适用**"
                f"（源文里就没有那一格该有的信息）—— 它们没进任何分母"
            )
    else:
        _say()
        _say("⚠️ 类目/抽取/幻觉三项指标**要先跑 refine 才有数**：")
        _say("   那是模型的行为，没跑过就不能编一个数字出来。")

    _say()
    _say("口径说明（这几项都会造出「假缺陷」，所以写在这里）：")
    _say("  · 覆盖率的分母是**该送进模型的**，未处理的不算进模型指标的分母")
    _say("  · 开始时刻的分母**不含**「源文只给了相对时间」的那些（结构上填不进）")
    _say("  · 幻觉率的分母是**所有被检查过的字段**，不只是有产出的条目")
    return 0


def _cmd_doctor(args: Args) -> int:
    """自检：环境、配置、库、模型端点。

    ⚠️ 它存在的理由：部署链路的问题有**同一个形状** ——
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

    _say("Python")
    _ok(f"{sys.version.split()[0]}（需要 ≥3.11）")
    if sys.version_info < (3, 11):
        _fail("版本太低 —— 本项目用了 `tomllib`（3.11 起内置）")
        problems += 1

    _say()
    _say("路径")
    for label, p in (
        ("数据目录", st.data_dir), ("配置目录", st.config_dir), ("主库", st.db_path)
    ):
        _say(f"  {label}：{p}")
    if not st.db_path.exists():
        _fail("主库不存在 —— 先跑 `python -m yechao demo`")
        problems += 1
    else:
        _ok("主库在")

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
    _say("模型端点")
    llm = app.config.llm()
    if not llm.configured:
        _warn("没配 —— refine 会拒绝跑（网页仍可用）")
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

    _say()
    if problems:
        _fail(f"{problems} 项需要处理")
        return 1
    _say("全部通过。")
    return 0


def _cmd_serve(args: Args) -> int:
    """起网页。

    ⚠️ Windows 有**端口保留段**，
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
    _say(f"  打开 http://127.0.0.1:{port}")
    _say("  Ctrl+C 退出")
    _say()
    uvicorn.run(api, host="127.0.0.1", port=port, log_level="warning")
    return 0


def _pick_port(preferred: int, *, tries: int = 12) -> tuple[int, str]:
    """找一个能绑的端口。

    返回 `(端口, 为什么要换 —— 没换就是空串)`。

    ⚠️ 换端口时**必须说出来**。静默换端口会让用户按 README 打开
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
    "refine": _cmd_refine,
    "digest": _cmd_digest,
    "export": _cmd_export,
    "metrics": _cmd_metrics,
    "doctor": _cmd_doctor,
    "cloud": _cmd_cloud,
    "serve": _cmd_serve,
}


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口。返回**退出码**。

    ⚠️ 它返回而不是 `sys.exit` —— 那样测试能直接断言退出码，
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
        # ⚠️ 领域错误**不当 traceback 打** —— 它是「用户能修的问题」，
        # 要给一句能照着做的提示。而 traceback 看起来像「程序坏了」。
        _fail(str(e))
        return 1
    except KeyboardInterrupt:
        _say()
        _warn("被中断")
        # ⚠️ 130 是「被 SIGINT 终止」的约定退出码。
        return 130
