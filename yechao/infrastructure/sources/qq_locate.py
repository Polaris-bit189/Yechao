"""本机 QQ 的数据根与账号目录 —— **零操作**的那一半。

## 这一层要解决的是「用户不用做任何事」

观察到的现象：QQ 把群消息记在本机，但记在哪由**安装时**定，且每台机器不同。
如果这个源要求用户先把路径找出来填进配置，那它在真实使用里等于不存在 ——
与 `meeting_wemeet` 撞的是同一堵墙。

⇒ 所以先把路径**问出来**，而不是猜：QQ 自己把数据根写在一处固定的地方。

## 实测到的形状（Windows，2026-10，QQ NT 9.9.31.49738）

| 东西 | 位置 | 形状 |
|---|---|---|
| **数据根** | `C:\\Users\\Public\\Documents\\Tencent\\QQ\\UserDataInfo.ini` 的 `[UserDataSet] UserDataSavePath` | ini，明文 |
| 账号目录 | `<根>\\<QQ号>\\` | 目录名就是 QQ 号 |
| 消息库 | `<根>\\<QQ号>\\nt_qq\\nt_db\\nt_msg.db` | 加密（见 `qq_cipher`） |
| 资料库 | 同目录 `profile_info.db` | 同一把钥匙，uid → QQ 号与昵称 |
| 全局登录库 | `<根>\\nt_qq\\global\\nt_db\\login.db` | 同目录，账号清单 |

### 为什么根路径要**读 ini** 而不是拼默认值

`C:\\Users\\<用户>\\Documents\\Tencent Files` 只是**默认**位置，用户在安装时可以把
数据盘换到别处（本机就在默认位置，所以这条判据不是实测出来的，是**从 ini 的存在
本身**推出来的：QQ 专门写了一份 ini 记录它，说明它可以不是默认值）。

⇒ 判据：**先读 ini，读不到才回落默认路径**。回落的那一份会被显式写进报错与
报告里（「用的哪个根」必须一眼可见），因为「读到了默认位置」与「用户配的就是
那个位置」是两件事，而它们的下一步不同。

### 为什么账号要**显式选**，不能默认挑一个

本机有两个账号（实测：`2161893504` 与 `3086849596`）。多个账号时不挑，**抛**：

  · 挑错的症状是「我明明有两个号，它只读了其中一个」——
    而报告里写着「源 qq：读到 N 条」，看起来完全正常。
  · 这与 `mail_source` 里「同时给两个目标 ⇒ 抛」是同一条判据：
    说不清哪个生效的状态，要报出来，不是要静默挑一个的那种。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from yechao.domain.errors import ConfigError
from yechao.infrastructure.sources._common import SOURCES_FILE, config_dir, source_table

#: 数据根的环境变量名（一次性覆盖用）。
ROOT_ENV = "YECHAO_QQ_ROOT"
#: 账号的环境变量名（本机有多个号时用来点名）。
ACCOUNT_ENV = "YECHAO_QQ_ACCOUNT"

#: QQ 记录数据根的那份 ini。**位置是固定的**（它在 QQ 自己的安装目录之外，
#: 因为数据根本身可以换盘）。
USER_DATA_INI = Path(r"C:\Users\Public\Documents\Tencent\QQ\UserDataInfo.ini")
#: ini 里那一段与那一个键。※ 键名必须**连等号一起**比 —— 同一段里还有
#: `UserDataSavePathType`，它以前缀相同。
INI_SECTION = "UserDataSet"
INI_KEY = "UserDataSavePath"

#: ini 读不到时的回落位置。※ 它是**回落**，不是默认值：用了它会被说出来。
FALLBACK_ROOT = Path.home() / "Documents" / "Tencent Files"

#: 账号目录下的相对路径。
NT_QQ = "nt_qq"
NT_DB = "nt_db"
#: 消息库与资料库的文件名。※ 都是**实测到的**，不是猜的。
MSG_DB = "nt_msg.db"
PROFILE_DB = "profile_info.db"
#: 外壳头的大小（明文那一段）。见 `qq_cipher`。
WRAPPER_SIZE = 1024


@dataclass(frozen=True)
class _Account:
    """一个已登录过的账号：它的 QQ 号，以及它那几个库在哪。"""

    uin: str
    db_dir: Path

    @property
    def msg_db(self) -> Path:
        return self.db_dir / MSG_DB

    @property
    def profile_db(self) -> Path:
        return self.db_dir / PROFILE_DB

    @property
    def where(self) -> str:
        """它是怎么被选中的 —— 给报错用（「这个账号是从哪儿来的」）。"""
        return f"{self.db_dir}"


def _read_ini_root() -> Path | None:
    """从 QQ 自己写的那份 ini 里读数据根。读不到 ⇒ `None`（**不抛**）。

    ※ 读不到不是错误：QQ 没装、或者装了一个不写这份 ini 的版本，都会走到这里，
    而调用方有回落。把它做成异常，症状是「没装 QQ 的机器上，这个源报的是
    「ini 读不出来」而不是「本机没有 QQ 数据」」—— 后者才是用户要的下一步。
    """
    try:
        text = USER_DATA_INI.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    in_section = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_section = line.strip("[]").strip().lower() == INI_SECTION.lower()
            continue
        if not in_section or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip().lower() != INI_KEY.lower():
            continue
        value = value.strip().strip('"')
        if value:
            return Path(value)
    return None


def find_root() -> tuple[Path, str]:
    """数据根，以及**它是从哪儿来的**（给报告用）。

    顺序：环境变量 > `sources.toml` 的 `[qq] root` > QQ 的 ini > 回落位置。

    ※ 前三者都是「用户或 QQ 自己说的」，最后一个是「我们猜的」。所以返回值里
    带一句出处 —— 猜的那一份必须能被看出来（见模块 docstring）。
    """
    env = (os.environ.get(ROOT_ENV) or "").strip()
    if env:
        return Path(env).expanduser(), ROOT_ENV

    table = source_table("qq", hint=_CONFIG_HINT)
    cfg = str(table.get("root") or "").strip()
    if cfg:
        return Path(cfg).expanduser(), f"{SOURCES_FILE} 的 [qq] root"

    ini = _read_ini_root()
    if ini is not None:
        return ini, f"{USER_DATA_INI} 的 {INI_KEY}"

    return FALLBACK_ROOT, "回落位置（QQ 没留下 ini）"


def list_accounts(root: Path) -> list[_Account]:
    """根下面**有消息库**的那些账号目录。按 QQ 号排序，结果确定。

    ※ 判据是「目录里有 `nt_qq/nt_db/nt_msg.db`」，不是「目录名是数字」：
    登录过的号才有库，而没登录过的号目录可能只有配置 —— 把它算进来，症状是
    「它说有三个号，其中一个读不出任何消息」。
    """
    if not root.is_dir():
        return []
    out: list[_Account] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        db_dir = child / NT_QQ / NT_DB
        if (db_dir / MSG_DB).is_file():
            out.append(_Account(uin=child.name, db_dir=db_dir))
    return out


def resolve_account() -> tuple[_Account, str]:
    """定下要读哪个账号。**说不清就抛**（见模块 docstring）。

    ※ 它返回 `(账号, 它从哪儿来的)`：报错与报告都要能说清「读的是哪个号」——
    多个账号是本机的常态，而「读的是哪个」决定了用户看到的是不是他的数据。
    """
    root, root_where = find_root()
    accounts = list_accounts(root)
    if not accounts:
        raise ConfigError(
            f"本机没有找到 QQ 的消息库。找过的地方：{root}（{root_where}）\n"
            f"※ 期望的形状是 <数据根>/<QQ号>/nt_qq/nt_db/{MSG_DB}。\n"
            f"※ 如果你把 QQ 的数据目录放在了别处，用 {ROOT_ENV}=<数据根> 指一下，"
            f"或者写进 {config_dir() / SOURCES_FILE} 的 [qq] root。"
        )

    want = (os.environ.get(ACCOUNT_ENV) or "").strip()
    where = ACCOUNT_ENV
    if not want:
        table = source_table("qq", hint=_CONFIG_HINT)
        want = str(table.get("account") or "").strip()
        where = f"{SOURCES_FILE} 的 [qq] account"

    if want:
        for acc in accounts:
            if acc.uin == want:
                return acc, where
        raise ConfigError(
            f"点名的账号 {want!r} 在本机没有消息库。找到的是："
            f"{'、'.join(a.uin for a in accounts)}（在 {root} 下）。"
        )

    if len(accounts) == 1:
        return accounts[0], f"本机只有一个账号（{root_where}）"

    raise ConfigError(
        f"本机有 {len(accounts)} 个 QQ 账号，不知道读哪一个："
        f"{'、'.join(a.uin for a in accounts)}\n"
        f"※ **不默认挑一个**：挑错的症状是「我明明有两个号，它只读了其中一个」，"
        f"而报告里写着「源 qq：读到 N 条」，看起来完全正常。\n"
        f"※ 点名一个：{ACCOUNT_ENV}=<QQ号>，或者写进 "
        f"{config_dir() / SOURCES_FILE} 的 [qq] account。"
    )


_CONFIG_HINT = (
    "  [qq]\n"
    '  root = "C:/Users/你/Documents/Tencent Files"   # 不给就自动找\n'
    '  account = "123456789"                          # 本机有多个号时必须点名\n'
)
