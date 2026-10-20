"""从**本机浏览器**里读登录态 —— 让用户「登录一次，之后自动」。

## 要解决什么

订阅源的凭据原来只有两条路：环境变量，或者没有。要用户自己打开开发者工具、
把 cookie 拷出来、贴进环境变量 —— 那件事每过期一次就要重做一遍，而且
「怎么拷」本身就是一道门槛。**要用户手工拷，等于没解决「自动」。**

用户真正已经做过的动作是：**在自己的浏览器里正常登录一次**。那份登录态就
在磁盘上（浏览器自己要存它，不然关掉浏览器就得重登）。本模块去把它读出来。

## 读的是什么（解密链路）

Chromium 系（Edge / Chrome）把 cookie 加密了两层：

  1. `<User Data>/Local State` 里的 `os_crypt.encrypted_key`：**base64** 的
     `DPAPI` 前缀 + 密文。去掉前 5 字节 `DPAPI` 之后交给 Windows 的
     `CryptUnprotectData` —— 它用**当前 Windows 用户**的凭据解密。
  2. `<User Data>/<Profile>/Network/Cookies`（SQLite）的 `cookies` 表里，
     `encrypted_value` = `v10` / `v11` + 12 字节 nonce + 密文 + 16 字节 tag，
     算法 AES-256-GCM，密钥就是第 1 步解出来的那 32 字节。

Firefox 不加密：`cookies.sqlite` 的 `moz_cookies` 表里 `value` 就是明文。

※ **DPAPI 那一步不做降级。** 它绑在「这台机器的这个 Windows 用户」上，
换个人、换台机器就是解不开 —— 那是设计，不是缺陷。解不开时**抛**（由
`_scan` 记成一笔 note），不返回空密钥：空密钥的症状是「解出来的 cookie 是
乱码」，而那看起来像「站点拒绝了我」—— 一句把人引向错方向的话。

## 怎么挑浏览器

按「**哪个 profile 里真的有点名域名的 cookie**」挑，顺序是
Edge → Chrome → Firefox（`BROWSERS` 的顺序就是优先级）。

※ 为什么不按「哪个浏览器装过」挑：装过不等于登录过。挑一个装过但没登录的，
用户看到的是「没配登录态」，而他明明登录了 —— 于是他开始怀疑工具。
先**看有没有那份 cookie**，再决定用谁，这条路径上就不会有这种错。

※ 同一个浏览器里有多个 profile 都有那份 cookie 时，取**排在最前的那个**
（`Default` 先，其余按名字）。它们多半是同一个账号；不是的话也无法从磁盘上
判断哪个「更该用」，硬猜会让人不知道自己在用哪个账号 —— 于是如实取第一个，
并把**用的是哪一个**写进 `_Found`（`explain` 会把它打出来）。

## 三条硬约束

1. **只读。** 库先**复制**到临时目录再读，读完删掉副本；
   **原文件一个字节都不动**（也从不被 sqlite3 打开 —— 测试钉住这一条）。

   ※ 但复制**不是万灵药**：Windows 上浏览器运行时会以**独占**方式打开
     `Cookies`（实测 `CreateFileW` 直接 `ERROR_SHARING_VIOLATION(32)`），
     那时连复制也失败。本模块不硬来 —— 跳过它、继续找别的浏览器，并把
     「库读不到（浏览器正开着）」记成 note，由 `explain` 打给用户看。
2. **只取点名的域名。** 后缀匹配，只把命中的那几条拼成 `Cookie:` 头；
   不把整个 cookie 库倒出来。
3. **值绝不进日志、异常、审计。** 唯一持有值的是 `_Found`，它的 `__repr__`
   打的是 `***`；`notes` 里只放「哪个浏览器、哪个 profile、为什么没读到」。

## ※ 这个模块为什么不联网

它读的全是本机文件（`Local State`、两个 SQLite 库）。一旦它开始联网，
「敏感原文在本机完成」这条主张就出现了第一个例外，而例外会扩散。
`tests/infrastructure/test_browser_cookies.py::test_模块里没有任何联网的导入`
用 AST 钉住它 —— 连写在函数体里的 `import` 也扫（那是最容易藏的地方）。

## ※ 为什么入口要先过协议闸门

读浏览器 profile 是「用**用户的身份**去访问第三方站点」的准备动作，与
`subscription_source` 里那件事同一条判据 ⇒ 两个入口（`cookies_for` /
`explain`）都先调 `consent.check_consent()`，没签就抛。见
`docs/协议/第三方账号授权.md`。
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from yechao.domain.errors import ConfigError
# ※ 这里写**模块名**而不是 `from yechao.infrastructure.sources import consent`：
# 后者在导入图上是一条指向**包**的边，而 `sources/__init__.py` 要 import
# `subscription_source`，`subscription_source` 又要 import 本模块 —— 于是
# `包 → 本模块 → 包` 成一个环（`test_没有导入环` 会报出来）。
# 指到 `consent` 这个模块上，那条边就落在一个不回头的地方。
import yechao.infrastructure.sources.consent as consent

# ── 常量 ────────────────────────────────────────────────────────────

#: Chromium 密文的前缀。`v10` 与 `v11` 是同一套东西（历史原因有两个写法）。
V10 = b"v10"
V11 = b"v11"

#: Chrome 127+ 的 **app-bound 加密**。密钥由浏览器自己在启动时解锁，
#: 而且校验调用方的可执行文件路径 —— 第三方进程读不出来。
#:
#: ※ 本模块**不做**它，也不假装做了：遇到它抛一句说清的话。写在这里是因为
#: 「读不到」有很多种原因，而这一种**换台机器、重装都不解决**，用户不该
#: 在「我是不是没登录」上耗时间。
V20 = b"v20"

#: `encrypted_key` 里 DPAPI 密文的前缀。
DPAPI_PREFIX = b"DPAPI"

#: Chromium 的 AES-GCM nonce 长度。**固定 12 字节**（不是从密文里推的）。
NONCE_BYTES = 12

#: 前缀本身占几个字节。
PREFIX_BYTES = 3

#: GCM tag 长度。只用来做「太短了」的粗检 —— 真校验由 AES-GCM 自己做。
TAG_BYTES = 16


# ── 内部类型 ────────────────────────────────────────────────────────


class _Unreadable(Exception):
    """这个 cookie 库（或它的一条记录）读不出来。

    ※ 它是**模块内部**的信号，不往外抛：一个 profile 读不出来不该让整个查找
      失败 —— 换一个浏览器可能就读得到。`_scan` 把它接住，记成一笔 note，
      继续往下找。见 `explain`。
    """


@dataclass(frozen=True)
class _Found:
    """一次查找的结果：**登录态串** + 它是从哪儿来的 + 一路上看到了什么。

    ※ `header` 带 `field(repr=False)`：值绝不进 `repr`。理由与
      `subscription_source._Credential.password` 逐字相同 —— 那是**类型系统
      层面**的保证，而自己写 `__repr__` 只在「有人调 repr」时生效
      （`dataclasses.asdict`、日志框架的 `%r`、pytest 断言失败时的回显
      各自走不同的路）。
    """

    #: 可以直接放进 `Cookie:` 头的串。空串 = 没找到。
    header: str = field(default="", repr=False)
    #: 从哪个浏览器读到的（`Edge` / `Chrome` / `Firefox`）。没找到时是空串。
    browser: str = ""
    #: 从哪个 profile 读到的（`Default` / `Profile 1` / `xxxx.default-release`）。
    profile: str = ""
    #: 一路上看到了什么（没装、没有这个域的 cookie、库被锁住……）。
    #: **只放位置与原因，不放任何值。**
    notes: tuple[str, ...] = ()

    def __repr__(self) -> str:
        """再钉一层：即使有人把 `repr=False` 删了，`***` 仍然在这里。

        ※ 为什么要两层：`field(repr=False)` 是「**不给**」，`'***'` 是
          「给一个**看得见脱敏过**的」。后者的价值在输出里 —— 一个人看到
          `header='***'` 就知道这里有个值被藏起来了，而看到字段整个消失
          只会以为它本来就是空的。
        """
        return (
            f"_Found(browser={self.browser!r}, profile={self.profile!r}, "
            f"header='***', notes={self.notes!r})"
        )


@dataclass(frozen=True)
class _Browser:
    """一个浏览器：怎么找它、怎么解它。"""

    key: str
    label: str
    #: `chromium`（要 DPAPI + AES-GCM）还是 `firefox`（明文）。
    family: str


#: 查找顺序 = 优先级。**顺序就是判据**（见模块 docstring「怎么挑浏览器」）：
#: 用户更可能在 Edge 里登录（Windows 的默认浏览器），Chrome 次之，Firefox 再次。
BROWSERS: tuple[_Browser, ...] = (
    _Browser(key="edge", label="Edge", family="chromium"),
    _Browser(key="chrome", label="Chrome", family="chromium"),
    _Browser(key="firefox", label="Firefox", family="firefox"),
)


# ── 浏览器装在哪儿 ──────────────────────────────────────────────────


def _user_data_dirs(key: str) -> tuple[Path, ...]:
    """这个浏览器的 profile 根目录的**候选位置**（可能不存在）。

    ※ 返回候选而不是「存在的那些」：`explain` 要能区分「这个浏览器没装」
      与「装了但没有这个域的 cookie」—— 两句让人去查不同方向的话。

    ※ 三个平台都列：Windows 是本项目的主场，但列一下 macOS / Linux 的路径
      只多三行，而少列一个平台的症状是「在这个平台上静默返回 None」。
      （※ **实测只在 Windows 上做过** —— 见模块末尾的说明。）
    """
    home = Path.home()
    if key == "firefox":
        if sys.platform == "win32":
            roaming = os.environ.get("APPDATA")
            if roaming:
                return (Path(roaming) / "Mozilla" / "Firefox" / "Profiles",)
            return ()
        if sys.platform == "darwin":
            return (home / "Library" / "Application Support" / "Firefox" / "Profiles",)
        return (home / ".mozilla" / "firefox",)

    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return ()
        base = Path(local)
        if key == "edge":
            return (base / "Microsoft" / "Edge" / "User Data",)
        return (base / "Google" / "Chrome" / "User Data",)

    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
        if key == "edge":
            return (base / "Microsoft Edge",)
        return (base / "Google Chrome",)

    if key == "edge":
        return (home / ".config" / "microsoft-edge",)
    return (home / ".config" / "google-chrome",)


def _profile_rank(name: str) -> tuple[int, str]:
    """profile 目录的排序键：`Default` 在最前，`Profile N` 按数字，其余按名字。"""
    if name == "Default":
        return (0, "")
    if name.startswith("Profile ") and name[len("Profile ") :].isdigit():
        return (1, f"{int(name[len('Profile ') :]):04d}")
    return (2, name.lower())


def _profiles(browser: _Browser, root: Path) -> list[Path]:
    """`root` 下面**真的有 cookie 库**的那些 profile 目录，按 `_profile_rank` 排。

    ※ 判据是「有这个文件」而不是「目录名像 profile」：`User Data` 下有一堆
      不是 profile 的目录（`Crashpad`、`ShaderCache`……），按名字认会漏掉
      自定义名字的 profile，按文件认不会。
    """
    if browser.family == "firefox":
        found = [p.parent for p in root.glob("*/cookies.sqlite")]
    else:
        found = [
            entry
            for entry in root.iterdir()
            if entry.is_dir()
            and ((entry / "Network" / "Cookies").is_file() or (entry / "Cookies").is_file())
        ]
    return sorted(found, key=lambda p: _profile_rank(p.name))


# ── 只读地拿到一张表 ────────────────────────────────────────────────


@contextmanager
def _readonly_copy(path: Path) -> Iterator[Path]:
    """把库**复制**到临时目录，交出副本的路径；出来时把副本删掉。

    ※ 为什么要复制（两条，都是实测过的）：

      1. **SQLite 会写。** 直接开原库时，SQLite 可能要在旁边建 `-wal`、
         回滚 `-journal`。浏览器正在用的库被第三方这么一碰，轻则读出错，
         重则改到浏览器的状态 —— 而后者是**绝不允许**的。
      2. **浏览器开着时库可能被独占锁住。** Windows 上实测：Edge 运行中，
         对它 `Cookies` 文件 `CreateFileW` 直接 `ERROR_SHARING_VIOLATION
         (32)`，`shutil.copyfile` 与 `open()` 一样失败。复制是一次普通读，
         锁得住就是锁得住 —— 那时如实报（见 `_scan` 的 notes），不硬开。

    ※ 为什么**连副本也要删**：副本里有**未脱敏的登录态**。留在临时目录里
      就是一次泄漏，而且它**看起来像垃圾文件**，没人会去清。

    ※ 复制 `-wal` / `-shm` / `-journal` 这三个兄弟文件（有就一起拷）：
      SQLite 的一致性依赖它们。少了 `-wal` 读到的是上一次 checkpoint 的快照
      （不新，但自洽）；少了**热的** `-journal` 则可能读到写了一半的状态。
    """
    tmp = tempfile.mkdtemp(prefix="yechao-cookies-")
    try:
        dst = Path(tmp) / path.name
        try:
            shutil.copyfile(path, dst)
        except OSError as e:
            raise _Unreadable(
                f"库读不到（{e.strerror or e}）—— 浏览器多半正开着，"
                f"Windows 上它会**独占锁住**这个文件（实测：连只读打开都不行）；"
                f"把这个浏览器关掉再试"
            ) from e
        for side in ("-wal", "-shm", "-journal"):
            src = path.with_name(path.name + side)
            if src.is_file():
                try:
                    shutil.copyfile(src, Path(tmp) / src.name)
                except OSError:
                    # 兄弟文件拷不到不算失败：主体在，读到的是稍旧但自洽的快照。
                    pass
        yield dst
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _query(copy: Path, sql: str, *, readonly: bool) -> list[tuple]:
    """在副本上跑一句 SQL。"""
    if readonly:
        con = sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True)
    else:
        con = sqlite3.connect(str(copy))
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _rows(db: Path, sql: str) -> list[tuple]:
    """读一张表。读不出来 ⇒ `_Unreadable`（由 `_scan` 记成 note）。

    ※ 先**只读**开副本；只读开不动时再开一次可写的。判据是实测的一条：
      浏览器崩在写事务中间时会留下一个**热的** `-journal`，而 SQLite 在只读
      连接上不能回滚它（`attempt to write a readonly database`）。
      在**副本**上允许它回滚是正确且安全的 —— 原文件仍然一个字节都没动。
    """
    with _readonly_copy(db) as copy:
        try:
            return _query(copy, sql, readonly=True)
        except sqlite3.OperationalError:
            pass
        except sqlite3.DatabaseError as e:
            raise _Unreadable(f"{db.name} 读不出来（{type(e).__name__}: {e}）") from e
        try:
            return _query(copy, sql, readonly=False)
        except sqlite3.DatabaseError as e:
            raise _Unreadable(f"{db.name} 读不出来（{type(e).__name__}: {e}）") from e


# ── Windows 的 DPAPI（用 ctypes 调，不引依赖） ──────────────────────


def _dpapi_unprotect(blob: bytes) -> bytes:
    """`CryptUnprotectData` —— 用**当前 Windows 用户**的凭据解密一段密文。

    ※ 为什么用 `ctypes` 而不是 `pywin32`：那是**只为这一步**存在的依赖，
      而这一步只有十几行。本项目默认依赖里每多一项，「clone 下来直接跑」
      就多一个失败点。

    ※ 解不开就抛：见模块 docstring 里那一段（空密钥的症状是乱码，
      而乱码看起来像「站点拒绝了我」）。
    """
    if os.name != "nt":
        raise _Unreadable("DPAPI 只有 Windows 上有 —— 这个平台读不了 Chromium 的登录态")

    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_Blob),  # pDataIn
        ctypes.POINTER(wintypes.LPWSTR),  # ppszDataDescr
        ctypes.POINTER(_Blob),  # pOptionalEntropy
        ctypes.c_void_p,  # pvReserved
        ctypes.c_void_p,  # pPromptStruct
        wintypes.DWORD,  # dwFlags
        ctypes.POINTER(_Blob),  # pDataOut
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    buf = ctypes.create_string_buffer(blob, len(blob))
    data_in = _Blob(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    data_out = _Blob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(data_in), None, None, None, None, 0, ctypes.byref(data_out)
    )
    if not ok:
        # ※ 消息里**不带** blob 的内容。
        raise _Unreadable(
            f"DPAPI 解不开（WinError {ctypes.get_last_error()}）—— "
            f"这份密钥属于**另一个 Windows 用户**"
        )
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(data_out.pbData, ctypes.c_void_p))


# ── AES-GCM（延迟导入 cryptography） ────────────────────────────────


def _aesgcm():
    """拿到 `AESGCM` 类。**在这里才 import** —— 与 `pymysql` / `redis` 同一条判据。

    ※ `cryptography` 进的是 `pyproject.toml` 的**可选依赖**：默认那一份必须
      能跑通三步复现，而它只有「读 Chromium 的登录态」这条路才需要
      （Firefox 的库是明文，用不上它）。

    ※ 没装时**报错并说清怎么装**，不回落成「读不到」—— 回落的话症状是
      「浏览器里明明登录了，工具说没配」，而用户会去查一个不存在的问题。
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as e:  # pragma: no cover - 依赖缺失时才走
        raise ConfigError(
            "要读 Chromium 系浏览器（Edge / Chrome）的登录态，得先装："
            "`pip install cryptography`（或 `pip install \"yechao[browser]\"`）。"
            "※ 装了它之前，程序**不会**退回「读不到」—— 那会让你以为浏览器里"
            "根本没登录。Firefox 的登录态不加密，不需要它。"
        ) from e
    return AESGCM


def _chromium_key(user_data: Path) -> bytes:
    """`Local State` → base64 → 去 `DPAPI` 前缀 → DPAPI → 32 字节 AES 密钥。"""
    state = user_data / "Local State"
    try:
        raw = json.loads(state.read_text(encoding="utf-8"))
    except OSError as e:
        raise _Unreadable(f"读不到 Local State（{e.strerror or e}）") from e
    except ValueError as e:
        raise _Unreadable(f"Local State 不是合法 JSON（{e}）") from e

    enc = (raw.get("os_crypt") or {}).get("encrypted_key")
    if not enc:
        # ※ 浏览器**从来没启动过**时就是这个样子（`os_crypt` 还没建起来）。
        raise _Unreadable("Local State 里没有 os_crypt.encrypted_key（浏览器没启动过？）")
    try:
        blob = base64.b64decode(enc)
    except (ValueError, TypeError) as e:
        raise _Unreadable("encrypted_key 不是合法 base64") from e
    if not blob.startswith(DPAPI_PREFIX):
        raise _Unreadable("encrypted_key 没有 DPAPI 前缀（这不是 Windows 上的 Chromium？）")
    return _dpapi_unprotect(blob[len(DPAPI_PREFIX) :])


def _decrypt_chromium(key: bytes, blob: bytes) -> str:
    """解一条 `encrypted_value`：`v10`/`v11` + 12 字节 nonce + 密文 + 16 字节 tag。"""
    prefix = blob[:PREFIX_BYTES]
    if prefix == V20:
        raise _Unreadable(
            "这条是 v20（app-bound 加密）—— 密钥由浏览器自己解锁且校验调用方，"
            "本模块解不开；换一份环境变量里的会话凭据"
        )
    if prefix not in (V10, V11):
        raise _Unreadable(f"不认识的密文前缀 {prefix!r}")
    body = blob[PREFIX_BYTES:]
    if len(body) < NONCE_BYTES + TAG_BYTES:
        raise _Unreadable("密文比 nonce+tag 还短")
    nonce, payload = body[:NONCE_BYTES], body[NONCE_BYTES:]
    # ※ `_aesgcm()` 必须在 try **外面**：缺依赖时它抛的是 `ConfigError`，
    #   而下面那个捕得很宽（见它的注释）—— 放进去会被吞成「解不开」，于是
    #   「没装 cryptography」显示成「这条 cookie 坏了」，一句把人引向错方向的话。
    aesgcm = _aesgcm()
    try:
        plain = aesgcm(key).decrypt(nonce, payload, None)
    except Exception as e:
        # ※ 这里捕得宽是刻意的：`cryptography` 抛的是它自己的 `InvalidTag`，
        #   而本模块**刻意不 import 它** —— 那会把 `cryptography` 变成模块级
        #   依赖，与「默认依赖里不出现它」那条判据冲突。宽捕在这里是安全的：
        #   解不开就是解不开，两种原因对调用方的意义相同。
        raise _Unreadable(f"解不开（{type(e).__name__}）") from e
    return plain.decode("utf-8", errors="replace")


# ── 域名匹配 ────────────────────────────────────────────────────────


def _matches(host_key: str, host: str) -> bool:
    """`host_key`（浏览器存的那一列）算不算点名域名 `host` 的 cookie。

    ※ 浏览器存两种形态：**精确 host**（`www.xiaohongshu.com`）与
      **域级**（`.xiaohongshu.com`，带前导点）。点名的是一个**域**，所以
      两个方向都算「同一个域族」：
        · 存的是域、问的是子域（`.xiaohongshu.com` ↔ `www.xiaohongshu.com`）
          —— 域 cookie 本来就发给所有子域，这是浏览器的语义；
        · 存的是子域、问的是域（`www.xiaohongshu.com` → `xiaohongshu.com`）
          —— 点名的那个域下面的 host，凭据属于同一个站点。

    ※ 为什么不能只写 `endswith(host)`：`notxiaohongshu.com` 会命中它 ——
      于是**别人的域**的 cookie 被塞进点名域的请求里。那是**泄密**，不是小错。
      所以前缀必须是点。（`evil-xiaohongshu.com` 同理被挡掉。）
    """
    key = (host_key or "").strip().lstrip(".").lower()
    want = (host or "").strip().lstrip(".").lower()
    if not key or not want:
        return False
    if key == want:
        return True
    return key.endswith("." + want) or want.endswith("." + key)


# ── 读一个 profile ──────────────────────────────────────────────────


def _read_firefox(profile: Path, host: str) -> list[tuple[str, str]]:
    """`moz_cookies` 里的明文。**不加密，直接读。**"""
    rows = _rows(profile / "cookies.sqlite", "SELECT name, value, host FROM moz_cookies")
    return [
        (str(name), "" if value is None else str(value))
        for name, value, host_key in rows
        if _matches(str(host_key), host)
    ]


def _read_chromium(
    user_data: Path, profile: Path, host: str
) -> tuple[list[tuple[str, str]], tuple[str, ...]]:
    """解出这个 profile 里属于 `host` 的 cookie。返回 `(键值对, 提醒)`。

    ※ 一条解不开时**不整体失败**：只要还有解开的，就照常给出去，并记一笔。
      判据是「少发几条 cookie」的后果 —— 站点可能因此认为你没登录，而那
      看起来像「登录态过期了」。让它在 `explain` 里可见，比整个丢掉强。
      全都解不开时才抛（那说明这条路根本走不通）。
    """
    db = profile / "Network" / "Cookies"
    if not db.is_file():
        db = profile / "Cookies"
    key = _chromium_key(user_data)

    pairs: list[tuple[str, str]] = []
    failed: list[str] = []
    for host_key, name, encrypted in _rows(
        db, "SELECT host_key, name, encrypted_value FROM cookies"
    ):
        if not _matches(str(host_key), host):
            continue
        try:
            pairs.append((str(name), _decrypt_chromium(key, bytes(encrypted))))
        except _Unreadable as e:
            failed.append(f"{name}（{e}）")
    if failed and not pairs:
        raise _Unreadable(f"{len(failed)} 条解不开，第一条：{failed[0]}")
    notes = ()
    if failed:
        notes = (f"{len(failed)} 条解不开，已跳过：{'、'.join(failed[:3])}",)
    return pairs, notes


# ── 查找 ────────────────────────────────────────────────────────────


def _scan(host: str) -> _Found:
    """按 `BROWSERS` 的顺序找，返回第一个**真的有** `host` 的 cookie 的地方。"""
    notes: list[str] = []
    for browser in BROWSERS:
        roots = [d for d in _user_data_dirs(browser.key) if d.is_dir()]
        if not roots:
            notes.append(f"{browser.label}：没找到（没装，或不在默认位置）")
            continue
        for root in roots:
            try:
                profiles = _profiles(browser, root)
            except OSError as e:
                # ※ `cookies_for` 承诺「不抛 IO 错误」。列目录这一步也会失败
                #   （权限、路径被占）—— 那也只是一条 note，不是一次崩溃。
                notes.append(f"{browser.label}：{root} 列不出来（{e.strerror or e}）")
                continue
            if not profiles:
                notes.append(f"{browser.label}：{root} 下没有 profile")
                continue
            for profile in profiles:
                try:
                    if browser.family == "firefox":
                        pairs, extra = _read_firefox(profile, host), ()
                    else:
                        pairs, extra = _read_chromium(root, profile, host)
                except _Unreadable as e:
                    notes.append(f"{browser.label} 的 {profile.name}：{e}")
                    continue
                notes.extend(extra)
                if pairs:
                    # ※ 顺序固定（按名字）：同一次查找的产物要能逐字节复现，
                    #   否则「昨天好好的今天不行」里会混进一个假的原因。
                    header = "; ".join(
                        f"{name}={value}" for name, value in sorted(pairs)
                    )
                    return _Found(
                        header=header,
                        browser=browser.label,
                        profile=profile.name,
                        notes=tuple(notes),
                    )
                notes.append(f"{browser.label} 的 {profile.name}：没有 {host} 的 cookie")
    return _Found(notes=tuple(notes))


# ── 入口 ────────────────────────────────────────────────────────────


def cookies_for(host: str) -> str | None:
    """给定域名（如 `xiaohongshu.com`），返回可以直接放进 `Cookie:` 头的串；没有返回 None。

    ※ 先过协议闸门（`consent.check_consent()`）：读浏览器 profile 是「用用户的
      身份去访问第三方站点」的准备动作，没签就抛。

    ※ 它**不抛 IO 错误**：某个 profile 读不出来（浏览器正开着锁住库、密钥是
      另一个 Windows 用户的）就跳过它、继续找别的；都没有才返回 `None`。
      这不是静默 —— 原因在 `explain()` 里，由调用方决定要不要报给用户。
    """
    consent.check_consent()
    return _scan(host).header or None


def explain(host: str) -> str:
    """`cookies_for(host)` 为什么没给东西 —— 一句话，给报错消息用。

    ※ 与 `cookies_for` 分开而不是让它返回一个带解释的对象：那个函数的返回值
      是**直接进 `Cookie:` 头**的，里面不该混进任何别的东西。

    ※ 「没读到」有两种，**下一步完全不同**，所以这里必须分开说：
      · 「浏览器正开着，库被独占锁住」⇒ 关掉浏览器再试；
      · 「这几个 profile 里没有 xiaohongshu.com 的 cookie」⇒ 去那个浏览器里登录。
      混成一句「没读到」，用户会去查错方向。
    """
    consent.check_consent()
    found = _scan(host)
    if found.header:
        head = f"已经从 {found.browser} 的 {found.profile} 里读到 {host} 的登录态"
    else:
        head = f"没有从本机浏览器里读到 {host} 的登录态"
    # ※ 读到了也把 note 带出来：note 里有「某几条解不开、已跳过」这类**部分
    #   失败** —— 少了它，用户看到的是「读到了」，而实际发出去的凭据不全，
    #   症状是站点那边认为他没登录。
    return f"{head}（{'；'.join(found.notes)}）" if found.notes else head
