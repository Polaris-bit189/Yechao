"""今天的日报 —— 「每天早上打开就有」这句话的落点。

## 要防什么

产品的定位是**每天一份**日报，而在此之前只有手动 `python -m yechao digest`。
没人跑就没有 —— 于是「今天没有日报」会变成常态，而它与「今天模型没跑成」
在界面上是**两句不同的话**：

  · 「今天没有日报」  = 这一天没有内容（窗口里没有条目）
  · 「今天模型没跑成」= 有内容，但本该由模型写的那些行没有写成

没有定时，这两句话会被同一件事盖住：**没人跑**。这个模块就是那个
「每天跑一次」的入口。

## 三条判据

### ① 「今天」从时钟端口取，不取机器时间

`app.clock.today()`。领域层与用例层都不许取「今天」（
`tests/contracts/test_layering.py::test_领域层不取当前时间` 钉着），
这个模块也不自己算 —— 它只问 `App.clock`。

为什么这件事重要：时间可注入，测试才能问「跑 2026-03-05 那一天」，
而不是「跑我按下回车的那一刻」。**不注入的后果**是一条只在我这台机器上、
只在我跑的那一秒成立的判据。

⇒ 这个文件里**不出现** `datetime.now()` / `date.today()` / `time.time()`。
「今天是几号」只有一处答案（`App.clock`），由组合根装进来。

### ② 复用 `application.digest.run`，不重写一条日报流水线

日报那一路的判据都在那条路上：没依据的截止日不进模型、机械行兜底、
条目与记账同事务。这里只做三件事 —— 定哪一天、拿依赖、把结果**说出来**。

※ 与 `python -m yechao digest` 的差别只有一个：**跑哪一天**。
那条命令跑的是合成语料最后一天（演示要可复现），这里跑的是今天。

### ③ 失败要响：模型挂了日报照样出得来，但「模型没跑成」看得出来

三种状态各有退出码，理由写在下面「退出码」那一节。要点是：**调度器只看得到
退出码**，所以「跑成了」与「跑成了但模型没跑」不能共用一个码。

## 这个模块与 `cli.py` 的关系

有两件事的答案在 `interfaces/cli.py` 里，这里**复用**而不是各写一份：
控制台编码（`_fix_console`）与云端摘要的读取（`_load_cloud_lines`）。
两份实现必漂移，而漂移的症状是静默的 —— 编码漂移的症状是「有时候乱码」，
摘要读取漂移的症状是「`daily` 与 `digest` 对同一天给出两篇不同的日报」。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Sequence

# ── 退出码 ──────────────────────────────────────────────────────────
#
# 三种状态各自一个码，因为**调度器只看得到退出码**：
#
#   0  跑成了：日报写进库了，而且模型这一路也跑成了
#   3  跑成了但降级：日报写进库了，但**本该由模型写的那些行一行都没有**
#      （模型整批失败，或者它写回来的行一条都没回连上）—— 机械行兜底了
#   1  没跑成：日报**没有**写进库（配置坏了、库连不上、写不进去）
#
# ※ 为什么降级不是 0：与「跑成了」共用一个码，等于把「模型没跑成」这件事
# 从退出码里**抹掉** —— 而调度器只读退出码。那正是本项目最在意的那类失败：
# 一个失败如果看起来像成功，就会一路往下跑，直到有人发现日报里全是机械行。
#
# ※ 为什么降级是 3 而不是 2：2 在本项目的 CLI 里已经有主了（用法错、
# 「还没配端点」）。复用 2 会让「你参数写错了」与「日报出来了但模型没跑」
# 在调度器眼里长得一样，而这两件事的下一步动作完全不同：前者改命令，
# 后者去查模型端点。
#
# ※ 降级是**非零**，但调度器**不把它当崩溃**：见 `deploy/scheduler.sh`
# —— 它把状态行连同退出码原样打进日志，然后继续睡到明天。非零是给
# 「读日志的人」和「外部的告警钩子」用的，不是给容器重启策略用的
# （重启修不好一个连不上的模型端点，只会让日志里多几行一样的话）。

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_DEGRADED = 3

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_FAILED = "failed"

#: 状态 → 人话。※ 未知状态**原样打出来**（`.get(x, x)`）——
#: 吞成「其它」的话，将来新加一种状态时日志里看不出来，而「少了一种」是无声的。
_STATUS_TEXT = {
    STATUS_OK: "跑成了",
    STATUS_DEGRADED: "跑成了但模型没跑（日报是机械行兜底的）",
    STATUS_FAILED: "没跑成",
}

_EXIT_OF_STATUS = {
    STATUS_OK: EXIT_OK,
    STATUS_DEGRADED: EXIT_DEGRADED,
    STATUS_FAILED: EXIT_FAILED,
}

USAGE = """\
夜抄 · 今天的日报

用法：
  python -m yechao.interfaces.daily [选项]

选项：
  --data-dir DIR     数据目录（默认 data/）
  --config-dir DIR   配置目录（默认 config/）
  --db PATH          主库路径（默认 <data-dir>/yechao.db）
  --day YYYY-MM-DD   跑**指定的一天**（默认：今天 —— 取自 App.clock）。
                     ※ 它是给**补跑**与**测试**用的，不是「换一个默认值」：
                     不给它时，「今天」只有一处答案（时钟端口）。

退出码：
  0  跑成了
  3  跑成了但模型没跑（日报是机械行兜底的）
  1  没跑成
  2  参数不认识
"""


@dataclass
class DailyOutcome:
    """今天这一跑的结果 —— **给调用方判断用**，不是给打印用。"""

    #: 跑的是哪一天（`YYYY-MM-DD`，本地日）。
    day: str
    #: 落库后的日报 id。
    digest_id: int = 0
    #: 这一次跑在库里的 id（`runs.run_id`）—— 拿它查账。
    run_id: str = ""
    #: 窗口里的条目数（= 日报的行数）。
    lines: int = 0
    #: 模型写了几行 / 机械行几行。
    by_model: int = 0
    mechanical: int = 0
    #: 整批失败的原因（None = 模型这一路没失败）。
    model_failure: str | None = None
    #: 三态之一。见上面的「退出码」那一节。
    status: str = STATUS_FAILED
    #: 一句补充说明（比如「今天窗口里没有条目」）。空串 = 没什么要补的。
    note: str = ""

    @property
    def exit_code(self) -> int:
        return _EXIT_OF_STATUS.get(self.status, EXIT_FAILED)

    @property
    def status_text(self) -> str:
        return _STATUS_TEXT.get(self.status, self.status)


def run_today(
    app: Any, *, day: str | None = None, use_model: bool = True
) -> DailyOutcome:
    """跑某一天的日报（默认今天）。**落库**，并返回结果。

    ## 哪一天

    `day` 不给时是 `app.clock.today()` —— **不是** `datetime.now()`，
    也不是「语料最后一天」。给 `day` 只有一个用途：补跑与测试。

    ## 同一天跑两次是**覆盖**，不是跳过

    推导：

    1. **存储层已经定义了这件事是覆盖。** `digests.day` 是 UNIQUE，
       而 `insert_digest` 在插入前先 `DELETE FROM digests WHERE day = ?`
       （见 `sqlite_store.insert_digest`）。在入口层再写一个「已经存在就
       跳过」的判断，等于给同一个问题**第二个答案** —— 而两个答案会打架：
       谁生效取决于谁先跑，而两条路各自看起来都对。
    2. **覆盖是对的，不只是「存储层这么写的」。** 一天里会**持续**有新条目
       进来：早上 07:30 跑完之后，上午还有人发通知。跳过的话，早上那一版会
       冻住一整天，想刷新只能去库里手删那一行。覆盖让「再跑一次」就是刷新，
       而且**不需要多一个开关**。
    3. **代价**：两次跑之间「变了什么」没有留痕（旧版被删掉了）。这个代价
       接受 —— 日报是**当日的快照**，不是版本化的文档；条目本身没被删
       （那是事实），日报只是它的一个视图。

    ※ 一个已知的残留：存储层那次 `DELETE` 不会连带清掉 `item_digests` 里
    指向旧 digest_id 的行（那张表没有外键级联）。那些行**读不出来**
    （所有读路径都按 digest_id 查，而它是自增的、不复用），所以它们不影响
    任何结果；这是仓储层的事，不在这一层绕开。

    ## 怎么判断「模型跑成了没有」

    `digest_uc.run` 把机械行兜底做得很干净 —— 干净到**模型整批挂掉时日报
    照样有内容**。所以「有日报」不能当成「跑成了」。判据取两条，都是从
    用例层**返回的东西**上读的（不是在这里重推一遍它的规则）：

      · `outcome.failure` 非空 ⇒ 模型整批失败（用例层已经记了账）；
      · 日报**有行**（`item_count > 0`）却**一行都不是模型写的**
        （`by_model == 0`）⇒ 模型跑了但一行都没用上。这一条是「模型写的行
        全都没回连上」那一种失败 —— `domain/digest.py` 的 docstring 里
        写着它的形状：*整条「本地模型写日报」的路等于没跑，而报告上一切
        正常*。机械行兜底让它在日报上看不出来，所以要在**这里**看出来。

    ※ 反过来也要对：窗口里没有条目时 `by_model == 0` 是**正常**的
    （没有东西要写），不能判成降级 —— 那会让每一个空白的日子都报红。
    """
    from yechao.application import digest as digest_uc

    target = day or app.clock.today()

    model_unconfigured = use_model and not app.config.llm().configured
    if model_unconfigured:
        # ※ **端点没配时不去调模型。**
        #
        # 观察到什么现象：`[llm]` 是空的时候，`OpenAIClient` 会拿一个空 URL
        # 去建 `Request`，抛出来的是裸的 `ValueError: unknown url type: ''`
        # —— 它**不是** `ModelError`，所以用例层的机械行兜底接不住它：
        # 日报**出不来**，日志里是一段 traceback（「失败响成了看起来像程序
        # 坏了」）。
        #
        # 为什么这一档要兜：日报的定位是「每天一份」，而这条正是
        # `deploy/scheduler.sh` 在早上 07:30 无人值守跑的那条路 ——
        # 宁可出来一篇机械行日报加一句「还没配端点」，也不要一段 traceback。
        #
        # ※ 它只挡住**「没配」**这一档，挡不住**「配错了」**（地址写歪了
        # 还是那个 `ValueError`）。后者是客户端该报的，不在这层兜 ——
        # 在这一层兜等于把「客户端对坏输入的处理」抄了第二份。
        #
        # 判据的形状与 CLI 的 `_cmd_cloud` 一致：先问配置「配好了吗」，
        # 而不是拿一个空值去试。
        use_model = False

    outcome = digest_uc.run(
        app.digest_deps(),
        day=target,
        use_model=use_model,
        cloud_lines=_cloud_lines(app, target),
    )

    digest = outcome.digest
    if digest is None:
        # ※ 正常路径上到不了这里：`digest_uc.run` 一定会落库（没有条目也会
        # 写一篇空的 —— 「这一天没有新的事项」是一句话，不是一片空白）。
        # 留着它是因为「没落库」与「落库了但是空的」必须是两件事。
        return DailyOutcome(
            day=target,
            run_id=outcome.run_id,
            status=STATUS_FAILED,
            note="日报没有落库 —— 这一跑没有产出可读的东西。",
        )

    lines = digest.item_count
    degraded = bool(outcome.failure) or (lines > 0 and outcome.by_model == 0)
    notes: list[str] = []
    if model_unconfigured and lines > 0:
        notes.append(
            "还没配本地模型端点（`config/settings.toml` 的 `[llm]`）—— "
            "这一篇是机械行，不是模型写的。"
        )
    if lines == 0:
        # ※ 这一句是产品那句话的后半截：「今天没有日报」与「今天模型没跑成」
        # 是两句不同的话。这一行说的是前一句 —— 而且要**说出来**，
        # 否则「今天没有内容」与「今天没跑」在日志里长得一样。
        notes.append(
            "今天窗口里没有条目 —— 这是「这一天没有内容」，不是「日报没生成」。"
            "若不符合预期，往上查 ingest / refine。"
        )

    return DailyOutcome(
        day=target,
        digest_id=digest.digest_id,
        run_id=outcome.run_id,
        lines=lines,
        by_model=outcome.by_model,
        mechanical=outcome.mechanical,
        model_failure=outcome.failure,
        status=STATUS_DEGRADED if degraded else STATUS_OK,
        note="；".join(notes),
    )


# ── 打印 ────────────────────────────────────────────────────────────


def _say(msg: str = "") -> None:
    print(msg)


def _fail(msg: str) -> None:
    print(f"  ! {msg}", file=sys.stderr)


def _field(label: str, value: str) -> str:
    """`  标签 值`，标签按**显示宽度**对齐。

    ※ 不按 `len()` 对齐：一个汉字占两列，`日期` 与 `数据目录` 的 `len()`
    差 2 而显示宽度差 4 —— 按 `len()` 补出来的表在终端里是歪的，
    而这份输出是**给人扫的**（`docker compose logs scheduler`）。
    """
    width = sum(2 if ord(ch) > 0x2E7F else 1 for ch in label)
    return f"  {label}{' ' * max(1, 10 - width)}{value}"


def report(app: Any, result: DailyOutcome) -> None:
    """把这一跑**说出来**。

    ※ 打印不是装饰：`docker compose logs scheduler` 里能看到的只有这些行。
    一个不打印的定时任务，失败与成功在日志里长得一样。
    """
    _say("夜抄 · 今天的日报")
    _say(_field("日期", f"{result.day}（取自 App.clock，不是机器时间）"))
    _say(_field("数据目录", str(app.settings.data_dir)))
    _say(_field("配置目录", str(app.settings.config_dir)))
    _say(_field("存储", _storage_line(app)))
    if result.digest_id:
        _say(
            _field(
                "日报",
                f"#{result.digest_id}（{result.day}，{result.lines} 条："
                f"模型写 {result.by_model} / 机械 {result.mechanical}）",
            )
        )
    if result.run_id:
        # ※ 账**落库了**这件事要告诉用户怎么看 —— 不打印这一句，
        # 「账落库」对用户就不存在（与 CLI 的 `_ledger_hint` 同一个理由）。
        _say(_field("账", f"python -m yechao ledger --run {result.run_id}"))
    if result.note:
        _say(_field("注意", result.note))
    if result.model_failure:
        _fail(f"模型这次没跑成：{result.model_failure}")
        _fail(
            "上面那一篇是用机械行兜底的 —— 日报出来了，但「本该由模型写的"
            "那些行」一行都没有。这不是「今天没内容」。"
        )
    _say(_field("状态", f"{result.status_text}（退出码 {result.exit_code}）"))


def _storage_line(app: Any) -> str:
    """数据到底写到哪儿。

    ※ 不能直接打印 `settings.db_path`：配了 MySQL 时那个路径**根本不会被
    用** —— 而它会照常打印出来，看起来一切正常。连接串里的密码也不打印
    （它可能被贴进日志与工单）。
    """
    from yechao.bootstrap import db_url_from_env, resolve_db_target

    backend, target = resolve_db_target(
        db_url_from_env(), default_path=app.settings.db_path
    )
    if backend == "sqlite":
        return f"sqlite {target}"
    return f"{backend}（连接串来自 YECHAO_DB_URL，不打印 —— 里面有密码）"


# ── 入口 ────────────────────────────────────────────────────────────


def _parse(argv: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    """极简参数解析。返回 `(认出来的, 不认识的)`。

    ※ 这里**不复用 CLI 的 `Args`**：那个解析器认十二个开关（`--fresh` /
    `--source` / `--limit`…），而这一条路只认四个。复用会让「`--source`
    在这条路上给了会怎样」变成一个要回答的问题 —— 而答案只能是「静默忽略」，
    那正是本项目的 CLI 明确拒绝做的事（见 `cli.main` 里那几条 `return 2`）。

    ※ 不认识的参数**不静默忽略**：调用方（`deploy/scheduler.sh`）把它当
    拼错的信号，退出码 2。
    """
    known = {"--data-dir", "--config-dir", "--db", "--day"}
    out: dict[str, str] = {}
    unknown: list[str] = []
    i = 0
    args = list(argv)
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help", "help"):
            out["--help"] = ""
            i += 1
            continue
        if a in known:
            if i + 1 >= len(args):
                unknown.append(a)  # 给了开关没给值
                break
            out[a] = args[i + 1]
            i += 2
            continue
        unknown.append(a)
        i += 1
    return out, unknown


def main(argv: Sequence[str] | None = None) -> int:
    """入口。返回**退出码**（不 `sys.exit` —— 那样测试只能去截 `SystemExit`）。"""
    from yechao.interfaces import cli

    # 中文 Windows 控制台是 GBK，而这里的输出全是中文。复用 CLI 那一份
    # `_fix_console`，不要在本文件里再写一遍 —— 两个入口对「控制台怎么处理」
    # 只能有一个答案；各写一份的话，哪天只改了一边，症状是「从这个入口跑
    # 乱码、从那个入口跑正常」，看起来像偶发。
    cli._fix_console()

    opts, unknown = _parse(sys.argv[1:] if argv is None else argv)

    if "--help" in opts:
        _say(USAGE)
        return EXIT_OK
    if unknown:
        _fail(f"不认识的参数：{' '.join(unknown)}")
        _say()
        _say(USAGE)
        return 2

    from yechao.bootstrap import Settings, make_app_or_explain
    from yechao.domain.errors import YechaoError

    st = Settings.from_paths(
        data_dir=opts.get("--data-dir"),
        config_dir=opts.get("--config-dir"),
        db_path=opts.get("--db"),
    )

    # ※ **写者锁要在这儿自己拿。**
    # 这个入口绕过了 `yechao/__main__.py`（那是给 `python -m yechao <命令>` 的），
    # 而日报会写库 —— 定时任务与手工 `digest` 撞在一起时，症状是「同一天两篇
    # 日报」或「同一篇写两遍」，两者都不报错。
    # 锁跟着**库文件所在目录**走，与 `__main__.py` 同一个判据。
    from yechao.bootstrap import acquire_writer_lock

    held, why = acquire_writer_lock(st.db_path.parent, command="daily")
    if why:
        _fail(why)
        _fail("没跑成：日报**没有**写进库。")
        return EXIT_FAILED

    try:
        # ※ 配置/存储坏了 ⇒ **退出码 1**（没跑成），而且**不吞异常**：
        # `make_app_or_explain` 抛的那句话是「该改哪个文件」，比 traceback 有用。
        app = make_app_or_explain(settings=st)
        result = run_today(app, day=opts.get("--day") or None)
    except SystemExit as e:
        _fail(str(e).strip())
        _fail("没跑成：日报**没有**写进库。")
        return EXIT_FAILED
    except YechaoError as e:
        _fail(str(e))
        _fail("没跑成：日报**没有**写进库。")
        return EXIT_FAILED
    finally:
        # 正常退出也放锁；进程被强杀时内核会做同一件事。
        if held is not None:
            held.release()

    report(app, result)
    return result.exit_code


def _cloud_lines(app: Any, day: str) -> dict[int, str]:
    """读云端复核留下的摘要（`data/cloud-lines.json`）。

    ※ **复用 CLI 那一份读取实现**，不在这里再写一个：两份读取实现必漂移，
    而漂移的症状是静默的 —— 这里漏读了，`daily` 就会**退回本地模型**，
    产出与 `python -m yechao digest` **不同的一篇**日报（同一天、两个入口、
    两篇内容），而两边都不报错。
    """
    from yechao.interfaces import cli

    return cli._load_cloud_lines(app, day)


if __name__ == "__main__":
    sys.exit(main())
