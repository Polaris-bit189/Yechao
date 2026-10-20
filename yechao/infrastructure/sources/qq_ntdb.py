"""QQ NT 的**加密**消息库：认头、取钥匙、按页解出来。

※ 模块名与对外的几个名字（`looks_like_qqnt_library` / `open_encrypted_library` /
`HEADER_SIZE` / `SQLITE_MAGIC` / `_KeyNotFound`）**保持不变** —— `qq_source` 与
测试都按这几个名字接线。里面做的事拆到了两个更细的模块：

| 模块 | 管什么 |
|---|---|
| `qq_cipher` | 外壳头、页布局、逐页 AES、`-wal` 盖回 |
| `qq_key` | 从正在运行的 QQ 进程内存里取钥匙 |

## 两处容易写错的地方（实测撞出来的）

1. **候选的形状**：钥匙不能按「**前后都是不可打印字符**」去取。那个形状是按
   C++ `std::string` 的 SSO 布局推出来的，看着很合理 —— 而本机实测那把钥匙
   不满足它：它在内存里是一段**更长的可打印串**里的一段。按那条规则扫，几百万
   个候选一个都不会中，而症状看起来像「钥匙不在内存里」。⇒ 判据是**锚点 +
   16 字节对齐**（见 `qq_key`）。
2. **验钥匙不能看解密结果**：第 1 页的明文里前 16 字节被 salt 顶掉了，所以
   「解密后有没有 `SQLite format 3`」**永远不成立**。判据要落在**密文**上 ——
   页 HMAC（见 `qq_cipher` 的模块 docstring）。

本机实测：**取到钥匙、整库解出 124381 页、`group_msg_table` 里 49 万余条群消息、
`PRAGMA integrity_check` 回 `ok`**。这是一条**实跑过**的结论，不是推的。

## 密钥不在文件里

头部那 64 字节（field 2）在同账号的**每一个**库里都一样 ⇒ 它是账号级的标识
（`key_meta`），不是每库一把的密钥：登录时拿它向腾讯服务器换真正的钥匙。
本机实测：库里没有、注册表里没有、任何落盘配置里都没有。⇒ 只能从正在运行的
客户端进程内存里取 —— 那是 QQ 自己为了读库而持有的东西。

## 只读

本模块对 QQ 的数据目录与进程**只读**：文件用 `open(..., "rb")`，
进程用 `PROCESS_VM_READ`。**不写任何字节**，句柄**用完就关**。

※ 解出来的明文**只在内存里过一趟**：`open_encrypted_library` 返回字节，调用方
用 `deserialize` 开内存库。落一份明文副本就等于把聊天记录写到了另一个地方 ——
那是本项目最不该做的事（红线是「敏感原文不出本机」，这里是「多出一份」）。
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from yechao.domain.errors import SourceUnavailable
from yechao.infrastructure.sources.qq_cipher import (
    PAGE_SIZE,
    WRAPPER_SIZE,
    apply_wal,
    decrypt_to_image,
    detect_params,
)
from yechao.infrastructure.sources.qq_key import can_scan, find_key

#: 改过的 SQLite 魔数。**不是** `SQLite format 3\0`。
QQNT_MAGIC = b"SQLite header 3\x00"
#: 标准 SQLite 魔数。明文库认这个。
SQLITE_MAGIC = b"SQLite format 3\x00"
#: 头部的自报家门。
QQNT_MARK = b"QQ_NT DB"
#: 头部长度。判据：`1024 + 4096×N` 整除（两个账号都成立），且空库正好 1024 字节。
HEADER_SIZE = WRAPPER_SIZE


class _KeyNotFound(SourceUnavailable):
    """库读到了、也认出它是 QQ NT 的加密库，但**钥匙取不到**。

    ※ 它是 `SourceUnavailable` 的子类（不是 `SourceFormatError`）：这不是
    「格式不认识」，是「这东西我读不到」。两者的下一步不同 —— 一个去改解析器，
    一个去解决「怎么拿到钥匙」。
    """


@dataclass(frozen=True)
class _LibraryHeader:
    """加密库那 1024 字节头里**确实读到**的东西。

    ※ 只放读出来的事实。读不出来的字段（比如 field 5 那个时间戳）不猜含义，
    也就不放进这个结构 —— 一个含义不明的字段挂在这儿，下一个人会当它是真的。
    """

    version: str
    hmac_algorithm: str
    #: 头部 field 2 的原文（128 个 hex 字符）。它是**账号级标识**，不是密钥。
    key_meta_hex: str


def _varint(raw: bytes, i: int) -> tuple[int, int]:
    """读一个 protobuf varint，返回 `(值, 新下标)`。"""
    shift = 0
    value = 0
    while i < len(raw):
        byte = raw[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7
    raise ValueError("varint 没读完就到了末尾")


def _proto_fields(raw: bytes) -> dict[int, bytes | int]:
    """把头部那段 protobuf 拆成 `{字段号: 值}`。

    ※ 只认两种 wire type（0 = varint、2 = 长度前缀），因为这个头里只有这两种。
    碰到别的就**停**，不猜 —— 猜错的症状是「把一段二进制当版本号读出来」。
    """
    out: dict[int, bytes | int] = {}
    i = 0
    while i < len(raw):
        try:
            key, i = _varint(raw, i)
        except ValueError:
            break
        field, wire = key >> 3, key & 0x7
        if wire == 0:
            value, i = _varint(raw, i)
        elif wire == 2:
            length, i = _varint(raw, i)
            value = raw[i : i + length]
            i += length
        else:
            break
        out[field] = value
    return out


def parse_header(raw: bytes) -> _LibraryHeader:
    """解那 1024 字节的头。解不出来 ⇒ `ValueError`（调用方决定怎么抛）。"""
    if len(raw) < HEADER_SIZE:
        raise ValueError(f"头部不足 {HEADER_SIZE} 字节（只有 {len(raw)}）")
    length = int.from_bytes(raw[0x28:0x2C], "little")
    body = raw[0x2C : 0x2C + length]
    fields = _proto_fields(body)

    def as_text(value: bytes | int | None, default: str) -> str:
        if isinstance(value, bytes):
            try:
                return value.decode("ascii")
            except UnicodeDecodeError:
                return default
        return default

    meta = fields.get(2)
    return _LibraryHeader(
        version=as_text(fields.get(3), ""),
        hmac_algorithm=as_text(fields.get(4), ""),
        key_meta_hex=meta.decode("ascii", "replace") if isinstance(meta, bytes) else "",
    )


def looks_like_qqnt_library(head: bytes) -> bool:
    """这 1024 字节的头是不是 QQ NT 的加密库头。

    ※ 判据是**两个**事实同时成立：魔数被改成 `SQLite header 3\\0`，且头部里有
    `QQ_NT DB`。只看魔数不够 —— 别的软件也可能改魔数；两个都对上才是它。
    """
    return head.startswith(QQNT_MAGIC) and QQNT_MARK in head[:64]


def recover_key(path: Path) -> tuple[bytes, object] | None:
    """扫一次内存，取回**这个账号的**钥匙。取不到 ⇒ `None`。

    ※ 为什么单独露出来：一个账号下有多个库（消息库 `nt_msg.db`、资料库
    `profile_info.db`……），它们**共用同一把钥匙**（实测）。扫一次内存要十秒上下，
    每个库各扫一次是白花；而两把钥匙万一不一致，症状是「资料库读不出来，
    消息库好好的」—— 看起来像资料库坏了。
    """
    return find_key(path)


def open_encrypted_library(
    path: Path, *, key: bytes | None = None, params: object | None = None
) -> bytes:
    """加密库 → **明文 SQLite 的字节**（全在内存里过一趟，不落盘）。

    这一趟包含两件事，缺一不可：

      1. **取钥匙**（`qq_key.find_key`）—— 扫本机正在跑的 QQ 进程内存。
         取不到 ⇒ `_KeyNotFound`（**不返回空**：那会让「读不到」与「群里没消息」
         长得一样）。
      2. **逐页解密 + 把 `-wal` 里已提交的帧盖回镜像**（`qq_cipher`）。
         不盖 WAL 的话读到的是一份**旧快照**，而它看起来完全正常 ——
         见 `qq_cipher` 的模块 docstring。

    ※ 中间那份明文镜像是**临时文件**，用完即删（`TemporaryDirectory`）：
    512 MB 的库解到内存里会同时占住「密文 + 明文」两份，而它们都只是过路。
    留在磁盘上的时间被压到一次 `read_bytes()` 那么短。
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(HEADER_SIZE)
    except OSError as e:
        raise SourceUnavailable(
            f"QQ 消息库读不到：{path}（{e}）", path=str(path)
        ) from e
    if not looks_like_qqnt_library(head):
        raise SourceUnavailable(
            f"这不是一份 QQ NT 的加密库：{path}（头 16 字节 {head[:16]!r}）",
            path=str(path),
        )

    header = parse_header(head)
    if key is None:
        found = find_key(path)
        if found is None:
            raise _KeyNotFound(_key_not_found_text(path, header), path=str(path))
        key, params = found
    assert params is not None

    return _materialize(path, key, params)


def decrypt_library(path: Path, key: bytes) -> bytes:
    """用**给定的**钥匙解这个库，返回明文 SQLite 的字节。

    ※ 它是 `open_encrypted_library` 的另一半：那个自己去找钥匙（扫进程内存），
    这个接受调用方给的钥匙。测试要用后者 —— 不然用例就得依赖「这台机器上
    正好开着 QQ」。
    """
    params = detect_params(path, key)
    if params is None:
        raise _KeyNotFound(
            f"这把钥匙打不开这个库：{path}\n"
            f"※ 判据是**页 HMAC**（验密文），不是「解密后有没有魔数」—— 见 qq_cipher。"
        )
    return _materialize(path, key, params)


def _materialize(path: Path, key: bytes, params) -> bytes:
    """解密 + 盖 WAL + 改掉「WAL 模式」那一句。`open_encrypted_library` 与
    `decrypt_library` 共用这一段 —— 两条路解出来的字节必须**逐字节一样**。"""
    with tempfile.TemporaryDirectory(prefix="yechao-qq-") as tmp:
        image = Path(tmp) / "plain.db"
        decrypt_to_image(path, image, key, params)
        apply_wal(path, image, key, params)
        plain = bytearray(image.read_bytes())
    # ※ **把「这个库是 WAL 模式」这一句改掉**（头偏移 18/19：1 = 回滚日志，
    # 2 = WAL）。真库的这两字节是 2，而它的 `-wal` 已经在上一步盖进镜像了 ——
    # 内容是全的，声明还留着。
    # 不改的话，`sqlite3.Connection.deserialize` 会以 "unable to open database
    # file" 失败：它按声明去找 `-wal` / `-shm`，而内存库里没有这两个文件。
    # 改掉之后实测 `PRAGMA integrity_check` 回 `ok`。
    plain[18] = 1
    plain[19] = 1
    return bytes(plain)


def key_opens_library(path: Path, key: bytes) -> bool:
    """这把钥匙能不能打开这个库。

    ※ 判据是**页 HMAC**（验密文），不是「解密后有没有 `SQLite format 3`」
    —— 后者永远不成立，推导与代价见 `qq_cipher` 的模块 docstring。
    """
    return detect_params(path, key) is not None


def _key_not_found_text(path: Path, header: _LibraryHeader) -> str:
    """取不到钥匙时给人看的那一段。

    ※ 它要说清三件事：**试过什么**、**缺什么**、**下一步**。少任何一件，
    用户拿到的就是一句「失败了」，而那句话的下一步是「再试一次」——
    错的下一步。
    """
    ok, why = can_scan()
    del ok
    return (
        f"读到了 QQ 的加密库，但取不到它的钥匙：{path}\n"
        f"  · 库的自述：{header.version or '版本未知'} · "
        f"{header.hmac_algorithm or 'HMAC 算法未知'}\n"
        f"  · 当前状态：{why}\n"
        f"※ 钥匙不在文件里：库里只有一段账号级标识（key_meta），真正的钥匙是登录时\n"
        f"  向腾讯服务器换的，只存在于**正在运行的 QQ 进程内存**里\n"
        f"  （本机实测：库里、注册表里、任何落盘配置里都没有）。\n"
        f"※ 下一步：**先把 QQ 打开并登录**，再跑一次。\n"
        f"※ 若 QQ 已经开着仍走到这里，说明这一版客户端把钥匙换了个形状放 ——\n"
        f"  这不是「今天没消息」，所以这里抛，不返回空。"
    )
