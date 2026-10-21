"""钥匙从哪儿来：**从正在运行、已登录的 QQ 进程内存里取**。

## 为什么不能从文件里取

实测（本机 QQ NT 9.9.31.49738）：`nt_msg.db` 的外壳头里有一段 128 位 hex，
它**不是**钥匙，是 `key_meta` —— 客户端登录时拿它向腾讯服务器换真正的钥匙。
所以钥匙**离线推不出来**：它既不在库里，也不在注册表里，也不在任何落盘的
配置文件里（这三处都实测找过）。

而 QQ 自己得用这把钥匙读库，所以它**必然在进程内存里**。这就是这条路的全部
依据：**不绕过任何认证，只是把本机已经登录的客户端手里的那把钥匙读出来。**

## 只读，且不改动 QQ 的任何东西

全程只做两件事：枚举进程、读内存。**不注入、不调试、不挂起、不写**。
（与 `browser_cookies` 那条路同一条判据：读用户自己的登录态，用只读的方式。）

## 判据：钥匙是**紧挨着锚点**的一串 16 字节可打印 ASCII

锚点是外壳头里的那一段 protobuf —— 它在内存里也有，因为客户端打开库的时候
把它读进去过。而钥匙按 16 字节对齐、落在锚点附近。

⇒ 顺序是：**先找锚点，再从锚点向两侧按 16 字节步长找候选**，最近的先试。
一页 HMAC 就能判定一把钥匙对不对（见 `qq_cipher.page1_matches`）。

※ 「最近的先试」不是优化，是判据：一台机器上有 8 个 QQ 进程（实测），
里面成百上千个 16 字节可打印串。挨个试要试到天亮；而从锚点向外扩散，
第一个对的就在几十步之内。

## 锚点找不到时的兜底：全扫

锚点可能被搬走（客户端换版本、内存里那份头被释放）。⇒ 兜底是**扫遍每一个
16 字节对齐的窗口**，但先过一遍「16 字节全可打印、且至少两类字符」的筛子 ——
不过筛子的话候选是千万级，每把钥匙要跑一次 PBKDF2，代价是小时级；过了筛子
是万级，分钟级。

## 失败要说清是哪一种

  · **本机没有 QQ 进程** ⇒ 钥匙不在内存里，而这不是「扫了没扫到」——
    它是「客户端没开」。两者的下一步完全不同：一个是去开 QQ，一个是去查版本。
  · **扫了没扫到** ⇒ 客户端在跑但钥匙不在已知形状里（多半是换了加密方式）。
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from yechao.infrastructure.sources.qq_cipher import (
    PAGE_SIZE,
    WRAPPER_SIZE,
    _Params,
    page1_matches,
)

#: 外壳头里那一段的**长度前缀 + 名字**。※ 前缀是 protobuf 的长度字节，
#: 所以三种算法各有一个不同的锚点。
ANCHORS = (b"\x09HMAC_SHA1", b"\x0bHMAC_SHA256", b"\x0bHMAC_SHA512")
#: 钥匙长度（实测 16 字节可打印 ASCII）。
KEY_LEN = 16
#: 候选与锚点的对齐粒度。
ALIGN = 16
#: 从锚点向外找多远。
RADIUS = 1 << 16

#: 可打印 ASCII。
_PRINTABLE = frozenset(range(0x20, 0x7F))

# ※ **不用 `ctypes.wintypes`**：那个模块在 Linux / macOS 上**import 就抛**
# （它的 `VARIANT_BOOL` 用了 `_type_ = "v"`，而那个类型码只在 Windows 上有）。
# 而本模块是**模块级**被导入的（`sources/__init__` → `qq_source` → `qq_ntdb` →
# 这里），所以一句 `import ctypes.wintypes` 会让**非 Windows 机器上整个程序起不来**
# —— 报错是一句与 QQ 毫无关系的 ValueError。判据与 `qq_cipher` 里那个延迟导入
# `cryptography` 是同一条：**平台/可选依赖的缺失不该砸掉默认那条路**。
# 这里直接用 `ctypes` 的等价类型（`DWORD` 就是 `c_ulong`），于是本文件在任何
# 平台上都能被导入；真正的 Windows 调用由 `supported()` 挡住。

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
TH32CS_SNAPPROCESS = 0x00000002
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
#: 可读的那些保护位。
_READABLE = frozenset({0x02, 0x04, 0x08, 0x20, 0x40, 0x80})
#: 一次读多大（大区域分块读，峰值内存不跟着进程大小走）。
_CHUNK = 1 << 20

#: 进程名。大小写不敏感地比。
PROCESS_NAME = "qq.exe"


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_char * 260),
    ]


class _MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", ctypes.c_ulong),
        ("__pad", ctypes.c_ulong),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.c_ulong),
        ("Protect", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
        ("__pad2", ctypes.c_ulong),
    ]


def supported() -> bool:
    """这条路**只在 Windows 上成立**。

    ※ 明写出来而不是让它自然失败：在别的系统上它会以「找不到 QQ 进程」的样子
    失败，而那句话把用户引向「去开 QQ」—— 错的方向。
    """
    return sys.platform == "win32"


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def qq_pids() -> list[int]:
    """本机正在跑的 QQ 进程。没有 ⇒ 空列表（**不抛**）。"""
    if not supported():
        return []
    k32 = _kernel32()
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return []
    entry = _PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
    pids: list[int] = []
    try:
        ok = k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.decode("ascii", "replace").lower() == PROCESS_NAME:
                pids.append(int(entry.th32ProcessID))
            ok = k32.Process32Next(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return pids


def _regions(handle) -> list[tuple[int, int]]:
    """可读的已提交区域 `[(起始, 长度)]`。"""
    k32 = _kernel32()
    out: list[tuple[int, int]] = []
    addr = 0
    mbi = _MEMORY_BASIC_INFORMATION()
    size = ctypes.sizeof(mbi)
    while k32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), size) == size:
        base = mbi.BaseAddress or 0
        length = int(mbi.RegionSize)
        if (
            mbi.State == MEM_COMMIT
            and mbi.Protect in _READABLE
            and not (mbi.Protect & PAGE_GUARD)
            and length > 0
        ):
            out.append((base, length))
        nxt = base + length
        if nxt <= base:
            break
        addr = nxt
    return out


def _read_region(k32, handle, base: int, length: int) -> bytes:
    """分块读一个区域。读不动的地方留空 —— **不抛**（内存是会变的）。"""
    out = bytearray()
    buf = ctypes.create_string_buffer(_CHUNK)
    got = ctypes.c_size_t(0)
    off = 0
    while off < length:
        want = min(_CHUNK, length - off)
        if k32.ReadProcessMemory(
            handle, ctypes.c_void_p(base + off), buf, want, ctypes.byref(got)
        ):
            out += buf.raw[: got.value]
        else:
            out += b"\x00" * want
        off += want
    return bytes(out)


def _candidate_ok(window: bytes) -> bool:
    """像不像一把钥匙：16 字节全可打印，且至少两类字符。

    ※ 「至少两类」是**剪枝**，不是判据：真正的判据是页 HMAC。
    但不过这一道，候选是千万级；过了是万级 —— 而两类字符几乎不会误杀
    （16 字节的随机串全是数字或全是字母，概率极低）。
    """
    if len(window) != KEY_LEN or any(b not in _PRINTABLE for b in window):
        return False
    text = window.decode("ascii")
    classes = (
        any(c.islower() for c in text)
        + any(c.isupper() for c in text)
        + any(c.isdigit() for c in text)
        + any(not c.isalnum() for c in text)
    )
    return classes >= 2


def _load_page1(db_path: Path) -> bytes:
    with open(db_path, "rb") as fh:
        fh.seek(WRAPPER_SIZE)
        return fh.read(PAGE_SIZE)


def _try(window: bytes, page1: bytes, params: _Params | None) -> _Params | None:
    """这一串是不是钥匙。对上就返回那套参数。"""
    if params is not None:
        return params if page1_matches(page1, window, params) else None
    for page in ("sha1", "sha256", "sha512"):
        for kdf in ("sha512", "sha256", "sha1"):
            cand = _Params(page_hmac=page, kdf_hmac=kdf)
            if page1_matches(page1, window, cand):
                return cand
    return None


def _scan_region(blob: bytes, page1: bytes, params: _Params | None):
    """在一个区域里按锚点找。返回 `(钥匙, 参数)` 或 `None`。"""
    seen: set[bytes] = set()
    for anchor in ANCHORS:
        start = blob.find(anchor)
        while start != -1:
            base = (start // ALIGN) * ALIGN
            for step in range(ALIGN, RADIUS, ALIGN):
                for cand in (base - step, base + step):
                    if cand < 0 or cand + KEY_LEN > len(blob):
                        continue
                    window = blob[cand : cand + KEY_LEN]
                    if window in seen or not _candidate_ok(window):
                        continue
                    seen.add(window)
                    found = _try(window, page1, params)
                    if found is not None:
                        return window, found
            start = blob.find(anchor, start + 1)
    return None


def _sweep_region(blob: bytes, page1: bytes, params: _Params | None):
    """兜底：扫遍这个区域里每一个 16 字节对齐的窗口（先过剪枝）。"""
    seen: set[bytes] = set()
    for off in range(0, len(blob) - KEY_LEN + 1, ALIGN):
        window = blob[off : off + KEY_LEN]
        if window in seen or not _candidate_ok(window):
            continue
        seen.add(window)
        found = _try(window, page1, params)
        if found is not None:
            return window, found
    return None


def find_key(db_path: Path, *, deep: bool = True):
    """扫本机 QQ 进程，找回能解开 `db_path` 的钥匙。

    返回 `(钥匙, 参数)`；**找不到返回 `None`**（由调用方决定怎么报 —— 它才知道
    是「QQ 没开」还是「扫了没扫到」）。

    ※ `deep` 是锚点找不到时要不要全扫。默认开：锚点那条路几秒就完，全扫是
    分钟级，但它是「找得到」与「找不到」的差别。
    """
    if not supported():
        return None
    k32 = _kernel32()
    pids = qq_pids()
    if not pids:
        return None
    page1 = _load_page1(db_path)
    if len(page1) < PAGE_SIZE:
        return None

    params: _Params | None = None
    for pid in pids:
        handle = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not handle:
            # 打不开就跳过这个进程。※ **不抛**：多开是常态，另一个进程里
            # 往往有同一把钥匙。全都打不开时，最终报的是「扫了没扫到」。
            continue
        try:
            for base, length in _regions(handle):
                blob = _read_region(k32, handle, base, length)
                if not blob:
                    continue
                hit = _scan_region(blob, page1, params)
                if hit is not None:
                    return hit
        finally:
            k32.CloseHandle(handle)

    if not deep:
        return None
    for pid in pids:
        handle = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not handle:
            continue
        try:
            for base, length in _regions(handle):
                blob = _read_region(k32, handle, base, length)
                if not blob:
                    continue
                hit = _sweep_region(blob, page1, params)
                if hit is not None:
                    return hit
        finally:
            k32.CloseHandle(handle)
    return None


def can_scan() -> tuple[bool, str]:
    """`(能不能扫, 说明)` —— 给报错文案用，**不抛**。"""
    if not supported():
        return False, f"当前系统是 {sys.platform}，读进程内存这条路只在 Windows 上成立"
    if not qq_pids():
        return False, "本机没有正在运行的 QQ 进程"
    if os.name != "nt":  # pragma: no cover - 与上面那条重复，留着是因为它们判据不同
        return False, "不是 Windows"
    return True, "本机有正在运行的 QQ 进程"
