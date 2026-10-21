"""CLI —— 三步复现是红线。

※ 这些用例断言的是**行为**，不是读代码：

  · 「`demo` 不需要密钥」→ 把 socket 禁掉再跑一遍
  · 「失败要响」→ 断言**退出码**，不是断言打印了什么
  · 「坏配置不静默兜底」→ 写一份坏配置，断言它**报错**而不是用默认值

一个失败如果看起来像成功，它比失败本身更危险
—— 所以退出码是本文件的主判据。
"""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest

from yechao.interfaces.cli import main


@pytest.fixture()
def workdir(tmp_path: Path) -> tuple[Path, Path]:
    """一对空的数据/配置目录。"""
    return tmp_path / "data", tmp_path / "config"


def _argv(workdir, *extra: str) -> list[str]:
    data, cfg = workdir
    return [
        "--data-dir", str(data),
        "--config-dir", str(cfg),
        *extra,
    ]


# ── demo ────────────────────────────────────────────────────────────


def test_demo_不需要网络(workdir, monkeypatch, capsys) -> None:
    """※ **离线建库，不需密钥** —— 这一条由**行为**钉住。

    做法：把「建连接」这条路径换成一个一被调用就抛的桩。
    如果 `demo` 里任何一处试图联网，它会当场炸。

    ※ **第一版禁错了对象，记在这里。**

    第一版把 `socket.socket` 整个换掉 —— 而 `socket` 是一个**类**，
    `ssl` 模块在 import 时就写了 `class SSLSocket(socket)`。
    于是测试失败在**导入期**，报错是 `TypeError: function() argument
    'code' must be code, not str` —— 一个与「联没联网」毫无关系的信息。

    这个误报很危险：它看起来像「`demo` 联网了」，而真相是测试自己
    破坏了 `ssl` 的导入。**判据禁错了对象，会造出一个指向错误方向的证据。**

    ⇒ 现在只禁**建连接**（`create_connection` 与 `socket.connect`），
    不禁「socket 这个类型存在」。
    """
    def _no_network(*a, **kw):
        raise AssertionError("demo 试图联网 —— 它必须离线可跑")

    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket.socket, "connect", _no_network, raising=False)
    monkeypatch.setattr(socket.socket, "connect_ex", _no_network, raising=False)

    code = main(["demo", *_argv(workdir)])
    assert code == 0

    data, cfg = workdir
    assert (data / "yechao.db").exists()
    assert (cfg / "groups.toml").exists()
    assert (cfg / "categories.toml").exists()


def test_demo_把语料写进库(workdir, capsys) -> None:
    code = main(["demo", *_argv(workdir)])
    assert code == 0

    from yechao.bootstrap import Settings
    from yechao.infrastructure.persistence import sqlite_store

    data, cfg = workdir
    st = Settings.from_paths(data_dir=data, config_dir=cfg)
    conn = sqlite_store.connect(st.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()
    assert n > 0, "demo 之后库里必须有消息（否则后面的 refine 无事可做）"


def test_demo_报告里必须看得见预筛的数字(workdir, capsys) -> None:
    """※ 「筛掉了多少、为什么」必须在 demo 阶段就可见。

    否则评委跑完 demo 看到一堆消息，不知道后面会发生什么 ——
    而那会让「预筛」这个环节在演示里等于不存在。
    """
    main(["demo", *_argv(workdir)])
    out = capsys.readouterr().out
    assert "预筛" in out
    assert "保留" in out
    assert "丢弃" in out


def test_demo_重跑是幂等的(workdir, capsys) -> None:
    """※ 重跑不该产生重复消息 —— 而它会重写配置是**警告过**的。

    配置文件的语义是「只在不存在时写」：已经存在的配置是**用户的**，
    覆盖它等于把用户的编辑丢掉，而 demo 看起来「成功」。
    """
    main(["demo", *_argv(workdir)])
    _, cfg = workdir
    before = (cfg / "categories.toml").read_text(encoding="utf-8")

    code = main(["demo", *_argv(workdir)])
    assert code == 0
    after = (cfg / "categories.toml").read_text(encoding="utf-8")
    assert before == after, "用户已有的配置被覆盖了"

    out = capsys.readouterr().out
    assert "保留不动" in out


def _留一道人工痕迹(db: Path) -> None:
    """在库里写一条**人工干预**（一条软删事件），用来验证 demo 重跑会不会动它。"""
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO edits (kind, item_id, payload, actor, ts) VALUES (?,?,?,?,?)",
        ("soft_delete", 999, '{"was_deleted": false}', "人", 0),
    )
    conn.commit()
    conn.close()


def _人工痕迹还在吗(db: Path) -> int:
    import sqlite3

    conn = sqlite3.connect(db)
    n = conn.execute("SELECT count(*) FROM edits WHERE item_id = 999").fetchone()[0]
    conn.close()
    return n


def test_demo_重跑不动已有数据_也不谎称覆盖(workdir, capsys) -> None:
    """※ 这一条钉的是一个**文案与行为不符**的缺陷。

    原来第二次跑打印的是「主库已存在，**将覆盖**」—— 而实测它什么都没覆盖：
    `init_db` 是幂等的、`upsert_messages` 用的是 `INSERT OR IGNORE`。
    实测过的形状：先人工软删一条，再跑 demo，软删状态与 edits 一条没动，
    而屏幕上写着「将覆盖」。

    一句与行为不符的提示**比没有提示更坏**：用户会以为重跑 demo 已经回到
    干净状态，于是不去查「为什么旧条目还在」。

    ⇒ 现在默认路径**永不删**，提示如实说「保留不动」，并把干净重来的路
    （`--fresh`）指出来。
    """
    main(["demo", *_argv(workdir)])
    data, _ = workdir
    db = data / "yechao.db"
    _留一道人工痕迹(db)

    code = main(["demo", *_argv(workdir)])
    out = capsys.readouterr().out

    assert code == 0
    assert "将覆盖" not in out, "还在谎称覆盖"
    assert "保留不动" in out
    assert "--fresh" in out, "说了保留不动，就得指出干净重来该怎么做"
    assert _人工痕迹还在吗(db) == 1, "重跑 demo 动了已有的人工编辑"


def test_demo_fresh_真的重建(workdir, capsys) -> None:
    """`--fresh` 是**唯一**会删库的路径 —— 它必须真的删干净。

    ※ 连 WAL 边车（`-wal` / `-shm`）一起删。只删 `.db` 而留下 `-wal`，
    SQLite 下次打开可能拿旧 WAL 去重放 —— 于是得到一个「删了但没删干净」的
    库，**而它看起来是新建的**。人工痕迹还在不在，就是这条的判据。
    """
    main(["demo", *_argv(workdir)])
    data, _ = workdir
    db = data / "yechao.db"
    _留一道人工痕迹(db)
    assert _人工痕迹还在吗(db) == 1

    code = main(["demo", "--fresh", *_argv(workdir)])
    out = capsys.readouterr().out

    assert code == 0
    assert "fresh" in out
    assert _人工痕迹还在吗(db) == 0, "说是重建了，人工痕迹却还在 —— 没删干净"


def test_fresh_只对_demo_有意义(workdir, capsys) -> None:
    """※ **不静默忽略一个「删库」开关**。

    给了它却没生效，用户会以为库已经重建过了 —— 那正是「失败看起来像成功」。
    所以别的命令拿到 `--fresh` 一律非零退出。
    """
    code = main(["refine", "--fresh", *_argv(workdir)])
    assert code == 2
    assert "--fresh" in capsys.readouterr().err


# ── refine 的失败要响 ────────────────────────────────────────────────


def test_没配模型时_refine_非零退出且指出去哪配(workdir, capsys) -> None:
    """※ 不兜底到一个默认端点 —— 那会把「没配」变成「连不上」，
    而两个要查的方向完全不同。"""
    main(["demo", *_argv(workdir)])
    code = main(["refine", *_argv(workdir)])
    assert code != 0, "没配模型却返回 0 —— 这正是「失败看起来像成功」"

    out = capsys.readouterr()
    text = out.out + out.err
    assert "settings.toml" in text or "base_url" in text, (
        "错误提示必须指出去哪配 —— 只说「失败了」等于没说"
    )


# ── 参数 ────────────────────────────────────────────────────────────


def test_不认识的参数返回_2(capsys) -> None:
    assert main(["demo", "--nonsense"]) == 2
    out = capsys.readouterr()
    assert "不认识的参数" in (out.out + out.err)


def test_不认识的命令返回_2(capsys) -> None:
    assert main(["frobnicate"]) == 2


def test_没有命令时打印用法并返回_0(capsys) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "demo" in out and "refine" in out and "serve" in out


# ── 配置坏了不许静默兜底 ─────────────────────────────────────────────


def test_坏配置必须报错而不是用默认值(workdir, capsys) -> None:
    """※ 判据：**文件存在但读不出来 ⇒ 抛**。

    与「文件不存在」（第一次跑，建一份默认的）是两件事。
    混起来的话，用户改坏了的配置会被静默忽略 ——
    而他以为设置生效了，直到某天发现群根本没被监视。
    """
    data, cfg = workdir
    main(["demo", *_argv(workdir)])
    (cfg / "groups.toml").write_text("[[group]]\nname = '缺了 id'\n", encoding="utf-8")

    code = main(["doctor", *_argv(workdir)])
    assert code != 0, "坏配置没被报出来"

    out = capsys.readouterr()
    assert "id" in (out.out + out.err)


# ── 模块入口真的能跑 ────────────────────────────────────────────────


def test_python_dash_m_yechao_能跑(tmp_path: Path) -> None:
    """※ **三步复现写的是 `python -m yechao`**，不是 `import`。

    这条用子进程真跑一遍 —— 它能抓到「包能 import 但入口坏了」那一类问题
    （比如 `__main__.py` 的签名与 CLI 的 `main` 对不上）。
    """
    r = subprocess.run(
        [sys.executable, "-m", "yechao", "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert r.returncode == 0, r.stderr[-800:]
    assert "demo" in r.stdout


# ── ingest：源层被接上了没有 ─────────────────────────────────────────


def _消息条数(db: Path) -> int:
    import sqlite3

    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()


def test_ingest_把源的消息写进库(workdir, capsys) -> None:
    """※ 这条是「数据源层不是死代码」的最小证据。

    改之前的样子：`application/ingest.py` 有实现、端口的 docstring 写得
    清清楚楚，而**没有任何命令行能调用它** —— 全仓 `grep -rn ingest tests/`
    是空的，`bootstrap.ingest_deps()` 只有定义处，`demo` 自己取语料绕过整层。
    于是源层跑得通，但**没人跑它**。

    ⇒ 判据是**行为**：跑一遍 `ingest`，库里就得有消息。
    """
    code = main(["ingest", *_argv(workdir)])
    assert code == 0

    data, _ = workdir
    assert _消息条数(data / "yechao.db") > 0, "ingest 之后库里没有消息"

    out = capsys.readouterr().out
    # ※ 报告里必须**点名是哪个源** —— 不给 `--source` 时会用默认源，
    # 而「我明明想读会议转写，怎么读的是合成语料」要一眼可见。
    assert "源 synth" in out
    assert "写入消息" in out


def test_ingest_重跑不产生重复消息(workdir, capsys) -> None:
    """幂等。重复消息会导致重复条目，而那是**无声的** ——
    日报里同一条通知出现两次，没有任何东西会报错。"""
    main(["ingest", *_argv(workdir)])
    data, _ = workdir
    before = _消息条数(data / "yechao.db")
    assert before > 0
    capsys.readouterr()

    code = main(["ingest", *_argv(workdir)])
    assert code == 0
    assert _消息条数(data / "yechao.db") == before, "重跑写出了重复消息"
    assert "写入消息 0 条" in capsys.readouterr().out


def test_ingest_不认识的源名返回_2_并列出可用的源(workdir, capsys) -> None:
    """※ **不回落**到默认源。

    回落的症状是「用户以为读的是他指定的源，其实读的是合成语料」——
    而报告看起来一切正常。所以名字不认识就非零退出，并说清有哪些能用的。
    """
    code = main(["ingest", "--source", "nosuchsource", *_argv(workdir)])
    assert code == 2, "源名写错却没被当成用法错"

    text = "".join(capsys.readouterr())
    assert "synth" in text, "报错要说清有哪些能用的源，否则用户只能去读源码"


def test_ingest_源读不到时非零退出且点名(workdir, monkeypatch, capsys) -> None:
    """※ 「今天只有这些」与「今天有个源没读到」必须长得不一样。

    源**读不到就抛**（端口的判据），CLI 的责任是把它报出来并且非零退出 ——
    静默跳过的话，用户看到的是「今天只有这些」。
    """
    from yechao.domain.errors import SourceUnavailable
    from yechao.infrastructure.sources.synth_source import SynthSource

    def _读不到(self, *, since=None, until=None):
        raise SourceUnavailable("合成语料读不到")

    monkeypatch.setattr(SynthSource, "read", _读不到)

    code = main(["ingest", *_argv(workdir)])
    assert code != 0, "有源读不到却返回 0 —— 那正是「失败看起来像成功」"

    text = "".join(capsys.readouterr())
    assert "synth" in text, "报告要说清是**哪个**源读不到"


# ── ledger：账要能被看到（审计说它「进程退出即消失」）─────────────


def test_demo_之后说清账落在哪_并且真的查得到(workdir, capsys) -> None:
    """※ **账落库了还不够，要能被指认。**

    观察到什么现象：`demo` / `refine` 打印一堆数字（几条消息、预筛丢了几条），
    而那些数字**只在那一次输出里** —— 进程一退出，谁也说不清「上一轮跑了
    什么」。所以每一轮结束都要说清三件事：账落在哪个库、这次的 run_id、
    怎么看上一次。

    判据是**行为**：先看输出里有没有那句，再真的按它给的命令跑一遍 ——
    提示指向的命令必须真的能用（一句与行为不符的提示比没有提示更坏）。
    """
    assert main(["demo", *_argv(workdir)]) == 0
    out = capsys.readouterr().out
    assert "run_id=" in out, "跑完没说账落在哪 —— 账落库这件事对用户不存在"
    assert "ledger" in out, "没说怎么看上一次"

    # ※ 按提示真的跑一遍
    assert main(["ledger", *_argv(workdir)]) == 0
    listed = capsys.readouterr().out
    assert "摄取" in listed, "`ledger` 没列出刚才那一轮"


def test_ledger_按消息查账(workdir, capsys) -> None:
    """※ 「**这一条**消息为什么没进条目」是产品承诺里最直接的那一问。

    `demo` 的合成语料里有被预筛丢掉的消息（纯应答、纯表情），
    它们的账应当能按消息号查出来 —— 而查出来的每一笔都带原因码。
    """
    assert main(["demo", *_argv(workdir)]) == 0
    capsys.readouterr()

    from yechao.bootstrap import Settings
    from yechao.infrastructure.persistence import sqlite_store

    st = Settings.from_paths(data_dir=workdir[0], config_dir=workdir[1])
    conn = sqlite_store.connect(st.db_path)
    try:
        row = conn.execute(
            "SELECT msg_id FROM messages ORDER BY msg_id LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    msg_id = int(row["msg_id"])

    assert main(["ledger", "--msg", str(msg_id), *_argv(workdir)]) == 0
    text = "".join(capsys.readouterr())
    assert str(msg_id) in text


def test_ledger_查不到这一次跑与这一轮没有账分得开(workdir, capsys) -> None:
    """※ **「查不到」与「没有」必须长得不一样。**

    打错一个 run_id 时若显示「这一轮没有账」，那读起来像「一切正常」——
    而真相是这个 id 库里压根没有。两者的下一步完全不同。
    """
    assert main(["demo", *_argv(workdir)]) == 0
    capsys.readouterr()

    assert main(["ledger", "--run", "这个id不存在", *_argv(workdir)]) == 0
    text = "".join(capsys.readouterr())
    assert "库里没有这一次跑" in text
    # ※ 比对的是**那句具体的文案**，不是「没有账」三个字 ——
    # 后者会被临时目录名里的同名子串命中，而那种红指向的是测试自己。
    assert "一笔账都没有" not in text, "打错 id 被说成了「这一轮没有账」"


def test_ledger_空库时说清是还没有证据(workdir, capsys) -> None:
    """※ 空账**不是**「没问题」，是「还没有跑过」。

    混起来的话，一个从没跑过任何东西的库会显示成「一切正常」。
    """
    assert main(["ledger", *_argv(workdir)]) == 0
    text = "".join(capsys.readouterr())
    assert "还没有跑过" in text


def test_run_与_msg_只对_ledger_有意义(workdir, capsys) -> None:
    """※ 同 `--source`：**不静默忽略一个选择开关**。

    给了 `--run` 却没生效，用户会以为「只看这一次」，而实际看到的是全部 ——
    那个数字看起来完全正常。
    """
    assert main(["demo", "--run", "abc", *_argv(workdir)]) == 2
    assert "--run" in capsys.readouterr().err
    assert main(["demo", "--msg", "1", *_argv(workdir)]) == 2


def test_ingest_读不到源时也说清账落在哪(workdir, monkeypatch, capsys) -> None:
    """※ **失败那条路上更要说**。

    「账落在哪」原来只在成功路径上打印 —— 而源读不到的时候，正是用户
    最想问「那我怎么查刚才发生了什么」的时候。
    """
    from yechao.domain.errors import SourceUnavailable
    from yechao.infrastructure.sources.synth_source import SynthSource

    def _读不到(self, *, since=None, until=None):
        raise SourceUnavailable("合成语料读不到")

    monkeypatch.setattr(SynthSource, "read", _读不到)
    code = main(["ingest", *_argv(workdir)])
    assert code != 0
    text = "".join(capsys.readouterr())
    assert "run_id=" in text, "失败那条路上没给账的线索"


def test_doctor_的失败行不会跑到日志最前面(tmp_path) -> None:
    """※ stdout 在**重定向 / 管道 / 被 docker 捕获**时是块缓冲，而 stderr 不是。

    不 flush 的症状：所有 `×` 行会**跳到日志最前面**，与它们所属的段落脱节
    （实测：`doctor > log.txt 2>&1` 之后，失败行全在文件开头）。而部署之后
    `docker compose logs` 就是这种合并流 —— 这个项目里日志是唯一能看的东西。

    这里用**子进程 + 合并两个流**来复现那种形状（同进程的 capsys 分不出这个）。
    """
    cfg = tmp_path / "config"
    cfg.mkdir()
    # 端口 1 上不会有东西 —— 连接会立刻被拒。
    (cfg / "settings.toml").write_text(
        "[llm]" + chr(10)
        + 'base_url = "http://127.0.0.1:1/v1/chat/completions"' + chr(10)
        + 'model = "x"' + chr(10),
        encoding="utf-8",
    )
    r = subprocess.run(
        [
            sys.executable, "-m", "yechao", "doctor",
            "--data-dir", str(tmp_path / "data"),
            "--config-dir", str(cfg),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    out = r.stdout
    段 = out.find("模型端点")
    失败 = out.find("连不上")
    assert 段 >= 0 and 失败 >= 0, out[-600:]
    assert 失败 > 段, (
        "失败行跑到了它所属的段落之前 —— stdout 没有 flush，"
        "重定向 / docker logs 里会全部堆在最前面" + chr(10) + out[-600:]
    )


def test_doctor_报最近一次跑的账(workdir, capsys) -> None:
    """※ 自检的用途是「出问题时先看哪儿」，而「上一轮跑了什么」是第一个。

    没有跑过任何东西时，那一栏要说「账是空的（不是没问题）」——
    而它是一条**提醒**，不是失败（干净 clone 本来就没跑过）。
    """
    # ※ 这一轮**不看退出码**：库里还没有主库时 doctor 本来就回 1
    # （「主库不存在」是另一条判据），而这里要验的是**那一栏怎么说**。
    main(["doctor", *_argv(workdir)])
    text = "".join(capsys.readouterr())
    assert "还没有跑过" in text, "空账被说成了「没问题」"

    assert main(["demo", *_argv(workdir)]) == 0
    capsys.readouterr()
    assert main(["doctor", *_argv(workdir)]) == 0
    text = "".join(capsys.readouterr())
    assert "最近一次" in text
    assert "摄取" in text


def test_source_只对_ingest_有意义(workdir, capsys) -> None:
    """※ **不静默忽略一个选择开关**。

    给了 `--source` 却没生效，用户会以为消息是从他指定的那个源读进来的 ——
    而那条路根本没走。这与 `--fresh` 是同一条判据（见上面那条用例）。
    """
    code = main(["demo", "--source", "synth", *_argv(workdir)])
    assert code == 2
    assert "--source" in capsys.readouterr().err


# ── demo 走源层（改过一次，理由留在这里）─────────────────────────────


def test_demo_真的走了源层(workdir, monkeypatch, capsys) -> None:
    """※ 这条用例存在的**全部**理由：证明 `demo` 不再绕过源层。

    ## 改之前是什么样

    `demo` 里写的是 `store.upsert_messages(list(_corpus_messages()))` ——
    直接从 `domain/synth` 取语料塞进库，中间的 `MessageSource` 端口、
    `application.ingest` 用例、`infrastructure/sources/` 的实现**一层都没经过**。

    后果不是「demo 坏了」，而是**源层是死代码**：跑得通，但没人跑它。
    即使明天把真源写出来，也没有任何东西能证明它被正确接线。

    ## 怎么证明现在走了

    把源的 `read()` 换成一个**只回一条**的桩，再断言库里**只有那一条**。
    如果 `demo` 还在自己取语料，库里会是整份语料（三十多条）—— 这条会红，
    而且红得指向正确的位置。

    ※ 这条判据的形状值得记：它**不读代码**。它制造一个「绕过源层就必然
    看得出来」的差异，然后看那个差异在不在。读代码只能证明「我写了调用」，
    造差异才能证明「调用真的发生了」。
    """
    from yechao.domain.models import PendingMessage
    from yechao.infrastructure.sources.synth_source import SynthSource

    只有一条 = PendingMessage(
        msg_id=880001,
        group_id=820000001,
        ts=1792333200,
        sender="教务员周允",
        content="关于期中教学检查的通知：请各班于10月28日前提交教学检查材料。",
    )

    def _只回一条(self, *, since=None, until=None):
        return [只有一条]

    monkeypatch.setattr(SynthSource, "read", _只回一条)

    code = main(["demo", *_argv(workdir)])
    assert code == 0

    data, _ = workdir
    assert _消息条数(data / "yechao.db") == 1, (
        "库里不止一条 —— demo 没走源层，它在自己取语料"
    )
    # ※ 报告里的数字也必须来自**这一次读到的**那一批，否则会出现
    # 「库里 1 条、报告说 33 条」这种没人看得懂的状态。
    assert "写入消息 1 条" in capsys.readouterr().out


def test_demo_源读不到时失败要响(workdir, monkeypatch, capsys) -> None:
    """※ 合成源**不该**读不到（语料在代码里）。真读不到就是接线或素材坏了。

    这时**不能**当成「今天很安静」继续往下走 —— demo 要非零退出，
    并且说出是哪个源。
    """
    from yechao.domain.errors import SourceUnavailable
    from yechao.infrastructure.sources.synth_source import SynthSource

    def _读不到(self, *, since=None, until=None):
        raise SourceUnavailable("合成语料是空的")

    monkeypatch.setattr(SynthSource, "read", _读不到)

    code = main(["demo", *_argv(workdir)])
    assert code != 0
    assert "synth" in "".join(capsys.readouterr())
