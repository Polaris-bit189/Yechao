"""QQ 的 SQLCipher 库怎么解 —— 逐页 AES，外加把 `-wal` 盖回去。

## 形状（实测，Windows，QQ NT 9.9.31.49738）

```
[ 1024 字节明文外壳 ][ SQLCipher 第 1 页 ][ 第 2 页 ] ...
                     ^ 文件偏移 1024 处的头 16 字节 = salt
```

  · 页大小 4096，**每一页**末尾留一段 `reserve`：`IV(16) + 页 HMAC`，向上取整到
    16 的倍数（HMAC_SHA1 ⇒ 48）
  · **第 1 页**：密文从页内偏移 16 开始（前 16 字节是 salt），IV 在密文**之后**；
    其余页密文从 0 开始
  · 钥匙是一串 16 字节的**可打印 ASCII**，经 PBKDF2 出 32 字节 AES 密钥
  · 页 HMAC 的密钥另派生一次：salt 逐字节异或 `0x3a`，迭代 2 次

## 判据：验钥匙**不能**看解密结果里有没有 `SQLite format 3`

这是最容易写错的一条，而且**写错了也看不出来**：

第 1 页的明文里，前 16 字节被 salt 顶掉了（那是 SQLCipher 的设计：salt 写在
明文头的位置上）。⇒ 解出来的正文**从第 16 字节才开始**，那 16 个字节的魔数
在密文里根本不存在。

所以验钥匙要验**密文**：对密文算页 HMAC，与页尾存的那一段比。它不经过 AES，
也就不会被「AES 解错了但看起来像明文」骗过去。

※ 这个判据的错误版本代价极大：它会**否定掉正确的钥匙**，而症状是
「扫了几千个候选，一个都不对」—— 看起来像「钥匙不在内存里」。

## `-wal` 为什么必须管

QQ 的库跑在 WAL 模式（实测：`nt_msg.db-wal` 有 4.9 MB，而主库里的最新一条
比它旧）。WAL 里的每一帧是一页**新版本**。不盖回去，读到的就是一份**旧快照**
—— 而且它完全正常，只是少了几天的消息。

这与 `docs/技术提示.md` 里那条同形：**一个失败如果看起来像成功，比失败本身
危险。** 所以这里不是「尽量把 WAL 也读一下」，是**不读 WAL 就不该声称读到了
最新的消息**。

### WAL 的形状与「只取完好的那一段」

WAL 的 32 字节头与每帧 24 字节的帧头**是明文**（只有页是加密的）。SQLite 用一条
**滚动校验和**判断日志有没有写坏，而那个校验和是**按存储形态算的**（也就是按
密文算）。

⇒ 这里做两件事，顺序不能反：

  1. **先按密文验校验和**，找出完好的前缀 —— 与 SQLite 自己的恢复规则一致。
     从活着的 QQ 那里拷来的 WAL 很可能尾巴是半页，照单全收会把半写的一页
     盖上去。
  2. **再把帧按页号盖回镜像**（`page_no` 决定落点），只盖到**最后一个提交帧**
     （`commit_size != 0` 的那一帧）为止 —— 提交之后的帧属于一个没做完的事务。

※ 第 2 步不需要重算校验和，因为我们不是在**回放日志**，而是在**物化最终镜像**：
镜像写完之后 SQLite 不会再去看 WAL。
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import struct
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

#: 外壳头大小（明文那一段）。与 `qq_locate.WRAPPER_SIZE` 是同一个数 ——
#: 这里不 import 它，因为那是**定位**那一层的事；两处都只是「1024」这个事实。
WRAPPER_SIZE = 1024
PAGE_SIZE = 4096
SALT_SIZE = 16
#: PBKDF2 的迭代次数（实测）。
KDF_ITER = 4000
#: 派生页 HMAC 密钥用的迭代次数（SQLCipher 的「快派生」）。
FAST_ITER = 2
#: HMAC 密钥的 salt = 页 salt 逐字节异或这个值。
HMAC_MASK = 0x3A
#: 页 HMAC 覆盖的尾部：页号，小端 4 字节。
_PAGE_NO_TAIL = 4

#: 页 HMAC 的候选，按代价从低到高（先试短的）。
PAGE_HMACS = ("sha1", "sha256", "sha512")
#: KDF 的候选。※ 顺序是**线索驱动的**：库的外壳头里写着 `HMAC_SHA1`，
#: 实测那一行说的是**页** HMAC；KDF 那一侧实测是 SHA512。
KDF_HMACS = ("sha512", "sha256", "sha1")

_DIGEST_SIZE = {"sha1": 20, "sha256": 32, "sha512": 64}
#: 外壳头里那个名字 → hashlib 的名字。
_WRAPPER_HMAC = {"HMAC_SHA1": "sha1", "HMAC_SHA256": "sha256", "HMAC_SHA512": "sha512"}


@dataclass(frozen=True)
class _Params:
    """一套已经对上的 SQLCipher 参数。"""

    page_hmac: str
    kdf_hmac: str

    @property
    def digest_size(self) -> int:
        return _DIGEST_SIZE[self.page_hmac]

    @property
    def reserve(self) -> int:
        """每页末尾留多少：IV + HMAC，向上取整到 16 的倍数。"""
        return -(-(SALT_SIZE + self.digest_size) // 16) * 16

    def label(self) -> str:
        return f"{self.page_hmac}/{self.kdf_hmac}"


def _read_at(path: Path, offset: int, size: int) -> bytes:
    """只读地读一段。**打不开就抛 `OSError`**，由调用方翻译成源层的错误。"""
    with open(path, "rb") as fh:
        fh.seek(offset)
        return fh.read(size)


def derive_key(passphrase: bytes, salt: bytes, kdf_hmac: str) -> bytes:
    """钥匙 → 32 字节 AES 密钥。"""
    return hashlib.pbkdf2_hmac(kdf_hmac, passphrase, salt, KDF_ITER, 32)


def page_hmac_key(aes_key: bytes, salt: bytes, kdf_hmac: str) -> bytes:
    """页 HMAC 的密钥：salt 异或 0x3a 之后，用**快派生**再走一遍。"""
    masked = bytes(b ^ HMAC_MASK for b in salt)
    return hashlib.pbkdf2_hmac(kdf_hmac, aes_key, masked, FAST_ITER, 32)


def page1_matches(page1: bytes, passphrase: bytes, params: _Params) -> bool:
    """这一页是不是用这把钥匙 + 这套参数加密的 —— **验密文，不验明文**。

    见模块 docstring：解出来的正文里没有魔数可看，所以判据落在页尾那一段 HMAC 上。
    """
    if len(page1) < PAGE_SIZE:
        return False
    salt = page1[:SALT_SIZE]
    aes_key = derive_key(passphrase, salt, params.kdf_hmac)
    hmac_key = page_hmac_key(aes_key, salt, params.kdf_hmac)
    data_end = PAGE_SIZE - params.reserve
    if data_end <= SALT_SIZE:
        return False
    # 页 HMAC 覆盖：密文 + IV + 页号（小端）。顺序是实测的，不能换。
    msg = (
        page1[SALT_SIZE:data_end]
        + page1[data_end : data_end + SALT_SIZE]
        + struct.pack("<I", 1)
    )
    got = hmac_mod.new(hmac_key, msg, params.page_hmac).digest()
    stored = page1[data_end + SALT_SIZE : data_end + SALT_SIZE + params.digest_size]
    return hmac_mod.compare_digest(got, stored)


def detect_params(db_path: Path, passphrase: bytes, hint: str | None = None) -> _Params:
    """把页 HMAC / KDF 的组合试出来。试不出来 ⇒ `None`（由调用方报错）。

    ※ 组合是**试**出来的，不是写死的：SQLCipher 的这两个算法随库的类型与客户端
    版本变，而写死一个默认值的症状是「换台机器就再也解不开，且报的是「钥匙不对」」。
    `hint` 是外壳头里自报的那个名字 —— 只用来**排前面**，不用来定论。
    """
    page1 = _read_at(db_path, WRAPPER_SIZE, PAGE_SIZE)
    if len(page1) < PAGE_SIZE:
        return None
    order = [h for h in PAGE_HMACS if h != hint] if hint in PAGE_HMACS else list(PAGE_HMACS)
    if hint in PAGE_HMACS:
        order = [hint] + [h for h in PAGE_HMACS if h != hint]
    for page in order:
        for kdf in KDF_HMACS:
            params = _Params(page_hmac=page, kdf_hmac=kdf)
            if page1_matches(page1, passphrase, params):
                return params
    return None


def decrypt_page(page: bytes, aes_key: bytes, params: _Params, *, first: bool) -> bytes:
    """解一页，返回**该页的明文**（4096 字节，第 1 页的魔数已补回）。"""
    skip = SALT_SIZE if first else 0
    enc_len = (PAGE_SIZE - skip) - params.reserve
    if enc_len <= 0:
        raise ValueError("页参数不对：reserve 比一页还大")
    ct = page[skip : skip + enc_len]
    iv = page[skip + enc_len : skip + enc_len + SALT_SIZE]
    body = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor().update(ct)
    full = bytearray(PAGE_SIZE)
    if first:
        # ※ 前 16 字节的魔数是被 salt 顶掉的，补回来这一页才是标准 SQLite 页。
        full[:SALT_SIZE] = b"SQLite format 3\x00"
        full[SALT_SIZE : SALT_SIZE + len(body)] = body
        full[16] = PAGE_SIZE >> 8
        full[17] = PAGE_SIZE & 0xFF
    else:
        full[: len(body)] = body
    return bytes(full)


def _wal_checksum(seed: tuple[int, int], chunks: list[bytes], little: bool) -> tuple[int, int]:
    """SQLite 的滚动校验和。输入长度都是 8 的倍数。"""
    fmt = "<I" if little else ">I"
    s0, s1 = seed
    for chunk in chunks:
        for off in range(0, len(chunk) - 7, 8):
            w0 = struct.unpack_from(fmt, chunk, off)[0]
            w1 = struct.unpack_from(fmt, chunk, off + 4)[0]
            s0 = (s0 + w0 + s1) & 0xFFFFFFFF
            s1 = (s1 + w1 + s0) & 0xFFFFFFFF
    return s0, s1


def _wal_frames(wal: bytes) -> tuple[list[tuple[int, bytes]], int]:
    """WAL → `[(页号, 加密页)]`，只到**最后一个提交帧**为止。

    ※ 中途任何一处不对（magic 不认、页大小不是 4096、校验和对不上、salt 换了）
    就**就地停下**，返回已经确认的那一段 —— 与 SQLite 自己的恢复规则一致。
    照单全收的症状是「把半写的一页盖到镜像上」，而它不报错。
    """
    if len(wal) < 32:
        return [], 0
    magic = struct.unpack_from(">I", wal, 0)[0]
    if magic == 0x377F0682:
        little = True
    elif magic == 0x377F0683:
        little = False
    else:
        return [], 0
    if struct.unpack_from(">I", wal, 8)[0] != PAGE_SIZE:
        return [], 0
    salt = wal[16:24]
    if _wal_checksum((0, 0), [wal[:24]], little) != struct.unpack_from(">II", wal, 24):
        return [], 0

    frame_size = 24 + PAGE_SIZE
    total = (len(wal) - 32) // frame_size
    seed = struct.unpack_from(">II", wal, 24)
    frames: list[tuple[int, bytes]] = []
    last_commit = -1
    for idx in range(total):
        off = 32 + idx * frame_size
        head = wal[off : off + 24]
        page = wal[off + 24 : off + frame_size]
        if head[8:16] != salt:
            break
        seed = _wal_checksum(seed, [head[:8], page], little)
        if seed != struct.unpack_from(">II", head, 16):
            break
        page_no, commit_size = struct.unpack_from(">II", head, 0)
        if page_no == 0:
            break
        frames.append((page_no, page))
        if commit_size:
            last_commit = len(frames) - 1
    if last_commit < 0:
        return [], 0
    return frames[: last_commit + 1], len(frames)


def decrypt_to_image(db_path: Path, out_path: Path, passphrase: bytes, params: _Params) -> int:
    """把整库解成一份**明文 SQLite** 写到 `out_path`。返回页数。

    ※ 写的是副本，**原库一个字节都不动**（全程只读打开）。
    """
    with open(db_path, "rb") as fh:
        fh.seek(WRAPPER_SIZE)
        head = fh.read(PAGE_SIZE)
    if len(head) < PAGE_SIZE:
        raise ValueError(f"{db_path} 在 {WRAPPER_SIZE} 之后不足一页 —— 不是一份 QQ 库")
    salt = head[:SALT_SIZE]
    aes_key = derive_key(passphrase, salt, params.kdf_hmac)

    pages = 0
    with open(db_path, "rb") as src, open(out_path, "wb") as dst:
        src.seek(WRAPPER_SIZE)
        first = True
        while True:
            page = src.read(PAGE_SIZE)
            if len(page) < PAGE_SIZE:
                break
            dst.write(decrypt_page(page, aes_key, params, first=first))
            first = False
            pages += 1
    return pages


def apply_wal(db_path: Path, image_path: Path, passphrase: bytes, params: _Params) -> int:
    """把 `-wal` 里**已经提交**的那几帧盖回镜像。返回盖了几页（0 = 没有或没用）。

    ※ 校验和按**密文**验（见模块 docstring），所以这里必须拿到加密页本身。
    """
    wal_path = db_path.with_name(db_path.name + "-wal")
    try:
        wal = wal_path.read_bytes()
    except OSError:
        return 0
    frames, _ = _wal_frames(wal)
    if not frames:
        return 0

    salt = _read_at(db_path, WRAPPER_SIZE, SALT_SIZE)
    aes_key = derive_key(passphrase, salt, params.kdf_hmac)
    applied = 0
    with open(image_path, "r+b") as img:
        for page_no, page in frames:
            img.seek((page_no - 1) * PAGE_SIZE)
            img.write(decrypt_page(page, aes_key, params, first=page_no == 1))
            applied += 1
    return applied
