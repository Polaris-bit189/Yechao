"""出网审计归档 —— **只存计数与指纹，不存内容**。

## 要防什么

赛题把「不得侵犯他人隐私」写成**资格级**条款。而「我们做了脱敏」是一句
**无法证伪的承诺** —— 评委没法验证，于是它不产生区分度。

⇒ 把边界从纪律变成**可回放的功能**：每次出网时留一条记录，让任何人都能事后
复核「那一刻送出去的到底是什么」。

## 记录里能不能存内容

**不能。** 存了就是新造一个泄漏面 —— 而它恰好会存下最该保护的那些文本。

那存什么才能**既证明又不泄漏**？存三样：
  · **计数**（多少次出网、覆盖多少条消息、多少字符、抹了多少处）
  · **指纹**（`sha256` 前 16 位）—— 能回答「这两次送的是不是同一份」，
    回答不了「里面是什么」
  · **比对了几个姓名**（`names_checked`）—— 见下

## 「查过了、没命中」与「根本没查」怎么区分

这是本模块最要紧的一个字段。**如果这一轮没有可比对的名单，那么「命中 0」
的含义是「没查」，不是「通过」。这两件事在结果里长得一模一样**。

⇒ 每条记录里**必须**有 `names_checked`。有它，人一眼能分清两者；没有它，
两者无法区分。

## 这套记录本身怎么防悄悄失效

**记录必须由脱敏那一刻产出，不能事后补算。** 所以写入点是
`application/extract.py` 里**紧接着** `scrub()` 的那一句 —— 检查与脱敏是
**同一次动作**，用的是**同一份判据**，不可能漂移。

## 为什么是 JSONL 而不是 SQLite

审计记录要能被**独立于主库**读取与分发（评委可能只想看这一份）。
而 JSONL 是纯文本、可 diff、可 `grep`、可手改 —— 一个「能被审计的审计」
应该是透明的。放进 SQLite 会让它变成一坨二进制。

※ 副产品：**主库被删了，审计还在**。这正是要的 —— 它是独立证据。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

#: 文件名前缀。`data/egress-audit-YYYYMMDD.jsonl`。
PREFIX = "egress-audit-"
SUFFIX = ".jsonl"

#: 一条记录里**允许出现**的键。别的一律丢弃。
#:
#: ※ **白名单而不是黑名单**：黑名单会在有人加新字段时静默放行 ——
#: 而如果那个新字段是内容（`"text": ...`），它就一路写进了磁盘。
#: 白名单要求每加一项都得显式写下「为什么这一项不泄漏」。
ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "surface",       # 出网点：refine / digest / reorder
        "batch",         # 第几批
        "group_id",      # 群号（不是内容）
        "day",           # 日报的日期
        "messages",      # 覆盖了多少条消息/条目
        "chars",         # 出网文本的字符数
        "num_placeholders",    # 抹了多少处号码
        "name_placeholders",   # 抹了多少处姓名
        "names_checked",       # **比对了几个姓名**（形态二的关键）
        "name_hits",           # 名单里的姓名命中次数（目标恒为 0）
        "num_hits",            # 号码命中次数（目标恒为 0）
        "sha256",        # 文本指纹
        "ts",            # 记录时刻
    }
)


class JsonlAuditLog:
    """JSONL 归档。

    实现 `application.ports.EgressAudit`。
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.dir = Path(data_dir)

    def _path_for(self, ts: int) -> Path:
        import datetime as _dt

        # utcfromtimestamp 已废弃，改用显式 UTC 的时区感知写法；归档按 UTC 日切分不变。
        day = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y%m%d")
        return self.dir / f"{PREFIX}{day}{SUFFIX}"

    def record(self, entry: dict[str, Any], *, ts: int | None = None) -> None:
        """追加一条。

        ※ **过滤到白名单** —— 见 `ALLOWED_KEYS` 的注释。
        """
        import time

        stamp = ts if ts is not None else int(time.time())
        safe = {k: v for k, v in entry.items() if k in ALLOWED_KEYS}
        safe["ts"] = stamp

        path = self._path_for(stamp)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n"
        # ※ 追加写的原子性由 O_APPEND 保证：同一文件系统上，
        # 只要单行不超过 PIPE_BUF 就不会被写花。审计行远小于 4KB。
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line)

    def read_all(self) -> list[dict[str, Any]]:
        """读全部审计记录。

        ※ **坏行跳过、不抛**：一份被手改坏的审计文件不该让隐私页整个打不开
        —— 那会让用户以为「没有记录」，而真相是「读不出来」。
        坏行数**会被带出去**（`_bad`），让调用方能区分这两种情况。
        """
        out: list[dict[str, Any]] = []
        if not self.dir.is_dir():
            return out
        bad = 0
        for path in sorted(self.dir.glob(f"{PREFIX}*{SUFFIX}")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                bad += 1
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if isinstance(row, dict):
                    row["_file"] = path.name
                    out.append(row)
                else:
                    bad += 1
        if bad:
            out.append({"_bad_lines": bad, "_file": ""})
        return out

    def stats(self) -> dict[str, Any]:
        """文件层面的统计（`/api/privacy` 的 `files` 字段用它）。"""
        files = sorted(self.dir.glob(f"{PREFIX}*{SUFFIX}")) if self.dir.is_dir() else []
        return {"files": len(files)}


class NullAuditLog:
    """一个什么都不记的归档 —— **只给测试用**。

    ※ 它存在是为了让「没接审计」这件事**显式**（测试里显式写
    `NullAuditLog()`），而不是让运行时默默变成一个 no-op。
    生产路径上不该出现它 —— 那会让隐私页永远显示「还没有出网记录」，
    而那个 0 读起来像「一切正常」。
    """

    def record(self, entry: dict[str, Any], *, ts: int | None = None) -> None:
        return None

    def read_all(self) -> list[dict[str, Any]]:
        return []
