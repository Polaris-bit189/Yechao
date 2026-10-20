"""QQ 源 —— 三条入口、「读不到」与「读到 0 条」的分辨、以及只读那一条。

## 这个文件**不碰本机真实的 QQ**

本机装着 QQ、也真的有一份加密的 `nt_msg.db`，但用例**不去读它**：那样测试就
依赖「这台机器上装了什么」，换一台机器就红。加密那条路用**自己造的**一份合成
加密库来测（`_build_encrypted_library`），密钥由用例直接给 —— 于是「有密钥」
与「没密钥」两条分支都能被穷举。

## 样本全部自己编

※ 红线。夹具里的姓名、号码、群号全部是本项目自编的：假校名「青梧」、
假姓名（孙予棠 / 李青萝 …）、假号码 `1770000100X`、假群号 `820000002`。
编出来的不必像真的，格式对就够。
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import os
import pathlib
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path

import pytest

from yechao.domain.errors import (
    ConfigError,
    SourceError,
    SourceFormatError,
    SourceUnavailable,
)
from yechao.domain.models import PendingMessage
from yechao.infrastructure.sources import qq_key, qq_ntdb, qq_parse, qq_source
from yechao.infrastructure.sources.qq_cipher import detect_params
from yechao.infrastructure.sources.qq_ntdb import _KeyNotFound
from yechao.infrastructure.sources.qq_parse import (
    _ExportHasNoMessages,
    _ExportNotUnderstood,
    _LibraryHasNoMessages,
    _LibraryNotUnderstood,
)

# ── 夹具在哪 ────────────────────────────────────────────────────────

夹具目录 = Path(qq_source.__file__).resolve().parent / "fixtures"
夹具JSON = 夹具目录 / "qq-group-export.json"
夹具TXT = 夹具目录 / "qq-group-export.txt"
夹具库 = 夹具目录 / "qq-group-msg.db"

#: 夹具那一批消息的条数（含一条**匿名**的）。
夹具条数 = 6
#: 夹具那个群。
夹具群号 = 820000002


@pytest.fixture()
def 干净的配置(tmp_path, monkeypatch):
    """把两个环境变量与配置目录都指到临时位置。

    ※ 不这么做的话，用例会读到**开发机上的** `config/sources.toml` ——
    于是「没配目标就抛」这条判据在有配置的机器上永远是红的，而在没配置的
    机器上是绿的。判据依赖机器状态，等于没有判据。
    """
    monkeypatch.delenv(qq_source.TARGET_ENV, raising=False)
    monkeypatch.delenv(qq_source.GROUP_ID_ENV, raising=False)
    monkeypatch.setenv("YECHAO_CONFIG_DIR", str(tmp_path / "config"))
    return tmp_path


def 造源(目标, *, group_id: int = 夹具群号):
    return qq_source._QqSource(
        qq_source._classify(str(目标), group_id=group_id, where="用例")
    )


# ── 三条入口 ────────────────────────────────────────────────────────


def test_夹具都在():
    assert 夹具JSON.is_file(), 夹具JSON
    assert 夹具TXT.is_file(), 夹具TXT
    assert 夹具库.is_file(), 夹具库


def test_json_导出读出全部消息():
    msgs = 造源(夹具JSON).read()
    assert len(msgs) == 夹具条数
    assert {m.sender for m in msgs} == {"班长孙予棠", "学习委员李青萝", "匿名"}
    assert all(m.source == "qq" for m in msgs)
    assert all(m.group_id == 夹具群号 for m in msgs)


def test_文本导出读出全部消息():
    msgs = 造源(夹具TXT).read()
    assert len(msgs) == 夹具条数
    assert {m.sender for m in msgs} == {"班长孙予棠", "学习委员李青萝", "匿名"}


def test_明文库读出全部消息():
    msgs = 造源(夹具库).read()
    assert len(msgs) == 夹具条数
    assert all(isinstance(m, PendingMessage) for m in msgs)


def test_三条入口读出的正文一致():
    """※ 三条入口归一化到同一个形状 —— 这正是端口那句「归一化的责任在数据源」。"""
    a = {m.content for m in 造源(夹具JSON).read()}
    b = {m.content for m in 造源(夹具TXT).read()}
    c = {m.content for m in 造源(夹具库).read()}
    assert a == b == c


def test_匿名的账号是_None_不是_0():
    """※ 「匿名」与「账号是 0」是两件事。

    拿 0 冒充匿名的话，匿名消息会被人物筛选当成某个具体的人发的那条 ——
    而它在界面上看起来完全正常。
    """
    for 目标 in (夹具JSON, 夹具TXT, 夹具库):
        msgs = 造源(目标).read()
        匿名 = [m for m in msgs if m.sender == "匿名"]
        assert len(匿名) == 1, 目标
        assert 匿名[0].actor_account is None, 目标


# ── msg_id 稳定 ─────────────────────────────────────────────────────


def test_同一批消息读两遍算出同一批_id():
    """※ 同一批消息重复摄取**不能产生重复条目**（`msg_id` 是幂等键）。"""
    for 目标 in (夹具JSON, 夹具TXT, 夹具库):
        first = [m.msg_id for m in 造源(目标).read()]
        second = [m.msg_id for m in 造源(目标).read()]
        assert first == second, 目标
        assert len(set(first)) == len(first), f"{目标}：id 有重复"


def test_把消息顺序对调_id_不变(tmp_path):
    """※ 判据的核心：id 只由「这条消息的身份」决定，不由「它排第几」决定。

    顺序变了而 id 不变，重跑摄取才不会把同一批消息又写一遍。
    """
    import json

    data = json.loads(夹具JSON.read_text(encoding="utf-8"))
    before = {m.content: m.msg_id for m in 造源(夹具JSON).read()}

    data["messages"] = list(reversed(data["messages"]))
    shuffled = tmp_path / "shuffled.json"
    shuffled.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    after = {m.content: m.msg_id for m in 造源(shuffled).read()}
    assert before == after


def test_导出文件与消息库算出同一批_id():
    """※ JSON 导出与消息库都带着 QQ 自己的 `msg_id`，所以两条路算出同一批 id。

    这是刻意的：同一批消息换一条入口读进来，不该在库里多出一份。
    （文本导出**没有** msg_id，只能按内容指纹算 —— 那是那个格式的已知代价，
    写在 `qq_parse._content_fingerprint` 的注释里。）
    """
    a = {m.content: m.msg_id for m in 造源(夹具JSON).read()}
    b = {m.content: m.msg_id for m in 造源(夹具库).read()}
    assert a == b


def test_id_在_63_位以内():
    """※ SQLite 的 INTEGER 是有符号 64 位 —— 超出会在插入时报错，而它只在
    **一半**的输入上出现，像随机故障。"""
    for 目标 in (夹具JSON, 夹具TXT, 夹具库):
        for m in 造源(目标).read():
            assert 0 <= m.msg_id < 2**63


# ── 「读不到」与「读到 0 条」是两件事 ───────────────────────────────


def test_文件不在抛_SourceUnavailable(tmp_path):
    源 = 造源(tmp_path / "没有这个文件.json")
    with pytest.raises(SourceUnavailable):
        源.read()


def test_空的_json_导出抛_HasNoMessages(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text('{"messages": []}', encoding="utf-8")
    with pytest.raises(_ExportHasNoMessages):
        造源(p).read()


def test_空的文本导出抛_HasNoMessages(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("# 只有注释\n\n", encoding="utf-8")
    with pytest.raises(_ExportHasNoMessages):
        造源(p).read()


def test_空的库抛_LibraryHasNoMessages(tmp_path):
    p = tmp_path / "empty.db"
    con = sqlite3.connect(p)
    con.execute(
        "CREATE TABLE group_msg_table (msg_id INTEGER, group_id INTEGER,"
        " ts INTEGER, sender TEXT, account INTEGER, content TEXT)"
    )
    con.commit()
    con.close()
    with pytest.raises(_LibraryHasNoMessages):
        造源(p).read()


def test_读不到与读到_0_条是两种不同的错(tmp_path):
    """※ 这条判据是本项目最要紧的一条：**两者都抛，但类型与下一步不同**。

    「这个群读不到」与「这个群今天没消息」长得一样 —— 下游拿到两个空，会给出
    同一份日报，并用「今天很安静」的口气叙述它。
    """
    没有 = tmp_path / "nope.json"
    空的 = tmp_path / "empty.json"
    空的.write_text('{"messages": []}', encoding="utf-8")

    with pytest.raises(SourceUnavailable) as 读不到:
        造源(没有).read()
    with pytest.raises(SourceFormatError) as 零条:
        造源(空的).read()

    assert type(读不到.value) is not type(零条.value)
    # `SourceFormatError.fatal = True`（要停下来看），`SourceUnavailable.fatal = False`
    # （这一条失败不阻断其余源）。两者混起来会让「读不到」被当成「今天很安静」。
    assert 零条.value.fatal is True
    assert 读不到.value.fatal is False
    assert isinstance(读不到.value, SourceError)


def test_不认识的_json_结构抛_NotUnderstood(tmp_path):
    p = tmp_path / "weird.json"
    p.write_text('{"items": [{"a": 1}]}', encoding="utf-8")
    with pytest.raises(_ExportNotUnderstood):
        造源(p).read()


def test_文本字段数不对抛_NotUnderstood(tmp_path):
    p = tmp_path / "bad.txt"
    p.write_text("1750000000\t某人\t正文里少了一个字段\n", encoding="utf-8")
    with pytest.raises(_ExportNotUnderstood):
        造源(p).read()


def test_不是_sqlite_也不是_qqnt_的库抛_NotUnderstood(tmp_path):
    p = tmp_path / "junk.db"
    p.write_bytes(b"this is definitely not a database" * 100)
    with pytest.raises(_LibraryNotUnderstood):
        造源(p).read()


# ── 加密库：造一份自己的，两条分支都测到 ────────────────────────────


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _qqnt_header(key_meta_hex: str = "ab" * 64) -> bytes:
    """造一个 QQ NT 的 1024 字节头（结构照 `qq_ntdb` 里实测的那一份）。"""
    body = (
        b"\x12" + _varint(len(key_meta_hex)) + key_meta_hex.encode("ascii")
        + b"\x1a" + _varint(7) + b"1.1.0.1"
        + b"\x22" + _varint(9) + b"HMAC_SHA1"
    )
    head = bytearray(qq_ntdb.HEADER_SIZE)
    head[0:16] = qq_ntdb.QQNT_MAGIC
    head[0x20:0x28] = qq_ntdb.QQNT_MARK
    head[0x28:0x2C] = len(body).to_bytes(4, "little")
    head[0x2C:0x2C + len(body)] = body
    return bytes(head)


def _to_reserved_layout(plain: bytes, reserve: int) -> bytes:
    """把一份普通 SQLite 库改成**带保留字节**的页布局。

    ## 为什么需要它（这一步是本文件里最容易被误解的地方）

    SQLCipher 的每一页末尾有 `reserve` 个字节**不属于页内容** —— 那里放 IV 与
    HMAC。SQLite 把单元内容从「可用区的末尾」往前堆，所以一份**普通**的 SQLite
    库（保留字节 = 0）内容会一直堆到第 4095 字节；直接拿它当 SQLCipher 的明文页，
    最后那几十字节会被 IV/HMAC 覆盖掉，还原出来是坏的。

    ⇒ 要造一份**像真的**加密库，输入得是一份「保留字节 = reserve」的库。
    这个函数做的就是这次改写：把每页的内容区整体前移 `reserve` 字节，同时改掉
    页头里的「内容区起点」与单元指针 —— 保留字节那一档就空出来了。

    ※ 它**只在测试里用**：产品路径上这个布局是 SQLCipher 自己写出来的。
    """
    out = bytearray()
    for i in range(len(plain) // 4096):
        pg = plain[i * 4096:(i + 1) * 4096]
        new = bytearray(4096)
        base = 100 if i == 0 else 0            # 页 1 的 b-tree 页头在第 100 字节
        ptype = pg[base]
        ncell = int.from_bytes(pg[base + 3:base + 5], "big")
        start = int.from_bytes(pg[base + 5:base + 7], "big") or 65536
        ptr = base + (12 if ptype in (2, 10) else 8)
        ns = start - reserve
        new[:ns] = pg[:ns]
        new[ns:4096 - reserve] = pg[start:4096]
        new[base + 5:base + 7] = (ns & 0xFFFF).to_bytes(2, "big")
        if i == 0:
            new[20] = reserve
        for c in range(ncell):
            off = int.from_bytes(pg[ptr + 2 * c:ptr + 2 * c + 2], "big") - reserve
            new[ptr + 2 * c:ptr + 2 * c + 2] = off.to_bytes(2, "big")
        out += new
    return bytes(out)


def _build_encrypted_library(
    plain: bytes, key: bytes, *, iters: int = 4000, reserve: int = 48
) -> bytes:
    """按 `qq_ntdb` 实测的页布局，把一份明文库加密成一份 QQ NT 加密库。

    ※ 加密这一侧是**用例自己写的**（用 `cryptography`，不用 sqlcipher3）——
    于是它不依赖本机装了什么，而「解密能还原」这件事也就真的被验到了。
    ※ 输入必须已经带保留字节（见 `_to_reserved_layout`）。
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    salt = bytes(range(16))
    enck = hashlib.pbkdf2_hmac("sha512", key, salt, iters, dklen=32)
    page_size = qq_ntdb.PAGE_SIZE
    out = bytearray()
    for i in range(len(plain) // page_size):
        page = plain[i * page_size:(i + 1) * page_size]
        skip = 16 if i == 0 else 0
        body = page[skip:page_size - reserve]
        body = body[: len(body) // 16 * 16]
        iv = bytes((i * 7 + j) % 256 for j in range(16))
        enc = Cipher(algorithms.AES(enck), modes.CBC(iv)).encryptor()
        ct = enc.update(body) + enc.finalize()
        buf = bytearray(page_size)
        if i == 0:
            buf[0:16] = salt
        buf[skip:skip + len(ct)] = ct
        buf[page_size - reserve:page_size - reserve + 16] = iv
        # 页 HMAC：真库的每一页末尾都有它，而**验钥匙就是靠它**（见 qq_cipher）。
        # 不写这一段的话，合成库只有「能解开」而没有「能验」，于是
        # detect_params 一条都认不出来 —— 而那与产品路径无关。
        hmac_key = hashlib.pbkdf2_hmac(
            "sha512", enck, bytes(b ^ 0x3A for b in salt), 2, dklen=32
        )
        mac = hmac.new(
            hmac_key, ct + iv + (i + 1).to_bytes(4, "little"), "sha1"
        ).digest()
        at = page_size - reserve + 16
        buf[at:at + len(mac)] = mac
        out += buf
    return _qqnt_header() + bytes(out)


#: 加密库用的保留字节数。48 = 16（IV）+ 32（HMAC），SQLCipher 4 的默认档。
保留字节 = 48


@pytest.fixture()
def 加密库(tmp_path):
    """一份用已知口令加密的合成 QQ 库，外加它的明文。"""
    明文 = _to_reserved_layout(夹具库.read_bytes(), 保留字节)
    key = b"yechao-qq-test01"
    p = tmp_path / "nt_msg.db"
    p.write_bytes(_build_encrypted_library(明文, key, reserve=保留字节))
    return p, 明文, key


def test_头部能认出加密库(加密库):
    p, _, _ = 加密库
    head = p.open("rb").read(qq_ntdb.HEADER_SIZE)
    assert qq_ntdb.looks_like_qqnt_library(head)
    header = qq_ntdb.parse_header(head)
    assert header.version == "1.1.0.1"
    assert header.hmac_algorithm == "HMAC_SHA1"
    assert len(header.key_meta_hex) == 128


def test_明文库不会被误认成加密库():
    head = 夹具库.open("rb").read(qq_ntdb.HEADER_SIZE)
    assert not qq_ntdb.looks_like_qqnt_library(head)


def _build_profile_db(mapping: dict[str, tuple[int, str]]) -> bytes:
    """造一份**同形**的 `profile_info.db`（只要解析用到的那三列）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        f = pathlib.Path(d) / "p.db"
        con = sqlite3.connect(str(f))
        con.execute(
            'CREATE TABLE "profile_info_v6" ("1000" TEXT, "1002" INTEGER, "20002" TEXT)'
        )
        con.executemany(
            'INSERT INTO "profile_info_v6" VALUES (?,?,?)',
            [(uid, uin, name) for uid, (uin, name) in mapping.items()],
        )
        con.commit()
        con.close()
        return f.read_bytes()


def test_资料库补上发送者的账号(tmp_path, monkeypatch):
    """uid → QQ 号由**同目录的** `profile_info.db` 补上，而两个库共用**一把**钥匙。

    ※ 两件事一起验，因为它们是一处的两面：一个账号下每个库各扫一遍内存的话，
    除了白花十秒，还会让「两把钥匙万一不一致」这种状态无声地发生 —— 症状是
    「资料库读不出来，消息库好好的」，看起来像资料库坏了。
    ※ 账号**不是**由 uid 哈希出来的：哈希会**编一个不存在的号**，而那个数字会被
    下游当成一个真实的人（见 `qq_parse.parse_library` 的说明）。
    """
    key = b"yechao-qq-test01"
    msg = tmp_path / "nt_msg.db"
    msg.write_bytes(
        _build_encrypted_library(
            _to_reserved_layout(夹具库.read_bytes(), 保留字节), key, reserve=保留字节
        )
    )
    prof = tmp_path / "profile_info.db"
    prof.write_bytes(
        _build_encrypted_library(
            _to_reserved_layout(
                _build_profile_db({"u_合成700101": (17700001001, "班长孙予棠")}),
                保留字节,
            ),
            key,
            reserve=保留字节,
        )
    )

    次数 = []

    def 假找钥匙(path, **kw):
        次数.append(path.name)
        return key, detect_params(path, key)

    monkeypatch.setattr(qq_ntdb, "find_key", 假找钥匙)
    msgs = 造源(msg).read()

    assert 次数 == ["nt_msg.db"], f"钥匙应当只取一次，实际取了 {次数}"
    有号的 = [m for m in msgs if m.actor_account is not None]
    assert len(有号的) == 1
    assert 有号的[0].actor_account == 17700001001
    # 没有资料库记录的那些：**如实置 None**，不是 0
    assert all(m.actor_account is None for m in msgs if m.actor_account != 17700001001)


def test_按页解密能还原明文(加密库):
    p, 明文, key = 加密库
    assert qq_ntdb.key_opens_library(p, key)
    assert qq_ntdb.decrypt_library(p, key) == 明文


def test_口令不对就打不开(加密库):
    p, _, _ = 加密库
    assert not qq_ntdb.key_opens_library(p, b"wrong-key-000000")


def test_有密钥时加密库能读出消息(加密库, monkeypatch):
    """端到端：认头 → 取钥匙 → 按页解密 → 内存里 deserialize → 解析 → 过时间窗。

    钥匙这一环被换成了「直接给」（真那一步要扫进程内存），其余每一步都真的跑了。
    """
    p, _, key = 加密库
    params = detect_params(p, key)
    monkeypatch.setattr(qq_ntdb, "find_key", lambda path, **kw: (key, params))
    msgs = 造源(p).read()
    assert len(msgs) == 夹具条数
    assert {m.content for m in msgs} == {m.content for m in 造源(夹具库).read()}


def test_取不到密钥时抛_KeyNotFound_且不返回空(加密库, monkeypatch):
    """「没找到密钥」与「这个库里没有消息」是两件事。

    返回空会让两者长得一样 —— 而下游会给出一份「今天群里很安静」口吻的日报，
    真相却是**我们根本没读到**。
    """
    p, _, _ = 加密库
    monkeypatch.setattr(qq_ntdb, "find_key", lambda path, **kw: None)
    with pytest.raises(_KeyNotFound) as e:
        造源(p).read()
    assert isinstance(e.value, SourceUnavailable)
    # 报错里要说清「试过什么、缺什么、下一步」——这是那条判据的落点。
    text = str(e.value)
    assert "密钥" in text
    assert "登录" in text


def test_候选口令的取法():
    """候选的剪枝：16 字节全可打印，且**至少两类字符**。

    剪枝不是判据（真正的判据是页 HMAC），但不过它候选是千万级；
    而每一个候选都要跑一次 PBKDF2。
    """
    assert qq_key._candidate_ok(b"Ab3dEf6hIj9lMn2p")
    assert not qq_key._candidate_ok(b"aaaaaaaaaaaaaaaa")   # 只有一类字符
    assert not qq_key._candidate_ok(b"abc\ndefghijklmno")  # 含不可打印
    assert not qq_key._candidate_ok(b"short")


def test_锚点附近那把钥匙会被找到(加密库):
    """这一条守的是「找的那一步真的在找」：把钥匙与锚点摆进一段内存，
    按锚点 + 16 字节对齐的规则必须把它挑出来。

    ※ 为什么是「锚点 + 对齐」而不是「前后不可打印」：后者看着合理，而按它在
    真机上扫 220 万个候选**一个都没中** —— 真钥匙在内存里是一段更长的可打印串
    里的一段。
    """
    p, _, key = 加密库
    page1 = p.read_bytes()[qq_ntdb.HEADER_SIZE:qq_ntdb.HEADER_SIZE + 4096]
    blob = bytearray(8192)
    blob[4096:4096 + len(qq_key.ANCHORS[0])] = qq_key.ANCHORS[0]
    at = 4096 + 32
    blob[at:at + 16] = key
    hit = qq_key._scan_region(bytes(blob), page1, None)
    assert hit is not None
    assert hit[0] == key
    # 没有钥匙的内存里不能凭空找出一把
    assert qq_key._scan_region(bytes(8192), page1, None) is None


def test_按页解密与_sqlcipher_的布局一致(tmp_path):
    """用**真正的** SQLCipher 实现交叉验证一次页布局。

    ※ 没有 `sqlcipher3` 时跳过：它不是本项目的依赖（本项目只要 `cryptography`）。
    这条用例的意义是「我推出来的页布局不是自说自话」——有它才能说这句话。
    """
    sqlcipher3 = pytest.importorskip("sqlcipher3")
    key = b"yechao-qq-cross01"
    src = tmp_path / "ref.db"
    con = sqlcipher3.connect(str(src))
    # ※ pragma 的顺序是**实测出来的**：`PRAGMA key` 要在前面，否则这些设置
    #   会被忽略、退回 SQLCipher 的默认值（256000 次迭代 / HMAC_SHA512）。
    con.execute("PRAGMA key='%s'" % key.decode())
    con.execute("PRAGMA cipher_page_size=4096")
    con.execute("PRAGMA kdf_iter=4000")
    con.execute("PRAGMA cipher_hmac_algorithm=HMAC_SHA1")
    con.execute("PRAGMA cipher_kdf_algorithm=PBKDF2_HMAC_SHA512")
    con.execute("CREATE TABLE t(a INTEGER)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()

    # 套上 QQ 那 1024 字节外壳 —— 那才是我们认的形状
    p = tmp_path / "nt_msg.db"
    p.write_bytes(_qqnt_header() + src.read_bytes())
    assert qq_ntdb.key_opens_library(p, key)
    plain = qq_ntdb.decrypt_library(p, key)
    back = sqlite3.connect(":memory:")
    back.deserialize(plain)
    assert back.execute("SELECT * FROM t").fetchall() == [(1,)]


# ── 只读 ────────────────────────────────────────────────────────────


def test_明文库是只读打开的():
    """※ 不变量 ① 的取向：**让数据库本身拒绝**，而不是靠代码自觉。

    靠自觉的约束迟早被静默绕开，而绕开时没有声音。
    """
    con = qq_source._open_plain_sqlite(夹具库)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO group_msg_table VALUES (1,1,1,'x',NULL,'y')")
    finally:
        con.close()


def test_读完就把库关掉(monkeypatch):
    """※ 「用完就关」—— 不关的话 QQ 的库文件会被这个进程占着。

    ※ 判据是「拿到的那个连接事后**真的不能用**了」，而不是「有人调了 close()」：
    后者可以被一次空调用满足。`sqlite3.Connection` 是 C 类型、patch 不了它的
    方法，所以这里包的是**我们自己的**那个打开函数。
    """
    captured = []
    real = qq_source._open_plain_sqlite

    def spy(path):
        con = real(path)
        captured.append(con)
        return con

    monkeypatch.setattr(qq_source, "_open_plain_sqlite", spy)
    造源(夹具库).read()
    assert captured, "没有走到打开库那一步"
    with pytest.raises(sqlite3.ProgrammingError):
        captured[0].execute("SELECT 1")


def test_源码里没有写操作():
    """※ 只读是一条**可机械检查**的判据，所以它不该只靠人看。

    扫的是本源三个模块的**语法树**，不是正文 —— 正文里为了说明「不许写进程内存」
    提到了那几个 API 的名字，扫正文会把**说明本身**扫出来 —— 所以扫语法树。
    查两件事：不许调用写内存 / 删文件的 API，不许用非只读模式打开文件。
    """
    禁止标识 = {"WriteProcessMemory", "VirtualProtectEx", "unlink", "rmtree", "shutil"}
    for mod in (qq_source, qq_parse, qq_ntdb):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Name):
                assert node.id not in 禁止标识, f"{mod.__name__}:{node.lineno} {node.id}"
            elif isinstance(node, ast.Attribute):
                assert node.attr not in 禁止标识, (
                    f"{mod.__name__}:{node.lineno} .{node.attr}"
                )
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "open" and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)):
                assert node.args[1].value == "rb", (
                    f"{mod.__name__}:{node.lineno} 用 {node.args[1].value!r} 打开了文件"
                    f" —— 只读那一条只允许 'rb'"
                )


# ── 目标怎么定 ──────────────────────────────────────────────────────


def test_没配目标就抛_ConfigError(干净的配置):
    with pytest.raises(ConfigError) as e:
        qq_source.resolve_target()
    assert qq_source.TARGET_ENV in str(e.value)


def test_后缀不认就抛_ConfigError(干净的配置, tmp_path, monkeypatch):
    p = tmp_path / "x.csv"
    p.write_text("a,b\n", encoding="utf-8")
    monkeypatch.setenv(qq_source.TARGET_ENV, str(p))
    with pytest.raises(ConfigError):
        qq_source.resolve_target()


def test_目录目标找到那个唯一的库(干净的配置, tmp_path, monkeypatch):
    目录 = tmp_path / "nt_qq" / "nt_db"
    目录.mkdir(parents=True)
    (目录 / "nt_msg.db").write_bytes(夹具库.read_bytes())
    monkeypatch.setenv(qq_source.TARGET_ENV, str(目录))
    target = qq_source.resolve_target()
    assert target.kind == "library"
    assert Path(target.raw).name == "nt_msg.db"
    assert len(造源(Path(target.raw)).read()) == 夹具条数


def test_目录目标也能指向_nt_db_那一层(干净的配置, tmp_path, monkeypatch):
    """※ 两种目录形状都要认：账号目录，以及直接把 `nt_db` 那一层指过来。

    本机实测：后者是很自然的一种给法，而按账号目录的形状去拼会拼成
    `nt_db/nt_qq/nt_db/nt_msg.db` —— 找不到，报「这个目录里没有库」。
    """
    目录 = tmp_path / "nt_db"
    目录.mkdir(parents=True)
    (目录 / "nt_msg.db").write_bytes(夹具库.read_bytes())
    monkeypatch.setenv(qq_source.TARGET_ENV, str(目录))
    target = qq_source.resolve_target()
    assert Path(target.raw) == 目录 / "nt_msg.db"
    assert len(造源(Path(target.raw)).read()) == 夹具条数


def test_目录里有多个_db_且都不是_nt_msg_就抛(干净的配置, tmp_path, monkeypatch):
    """※ **不挑一个**：挑错的症状是「读的是另一个库」，而它看起来完全正常。"""
    目录 = tmp_path / "somewhere"
    目录.mkdir()
    for name in ("a.db", "b.db"):
        (目录 / name).write_bytes(夹具库.read_bytes())
    monkeypatch.setenv(qq_source.TARGET_ENV, str(目录))
    with pytest.raises(SourceUnavailable):
        qq_source.resolve_target()


def test_环境变量优先于配置文件(干净的配置, tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    # ※ 用 TOML 的**字面量字符串**（单引号）：Windows 路径里的反斜杠在双引号
    #   字符串里是转义字符，写进去会变成一个解析错误。
    (cfg / "sources.toml").write_text(
        f"[qq]\ntarget = '{夹具TXT}'\ngroup_id = {夹具群号}\n", encoding="utf-8"
    )
    monkeypatch.setenv(qq_source.TARGET_ENV, str(夹具JSON))
    target = qq_source.resolve_target()
    assert Path(target.raw) == 夹具JSON


def test_配置目录的口径与组合根一致():
    """※ `_common.config_dir()` 与 `bootstrap.Settings.from_env` 必须同口径。

    代价是「同一个默认值有两处」，所以两边各钉一次 —— 它漂了的话，用户改了
    `config/sources.toml` 而程序去别处找，症状是「我明明配了，它说没配」。
    """
    from yechao.bootstrap import DEFAULT_CONFIG_DIR, Settings
    from yechao.infrastructure.sources._common import (
        CONFIG_DIR_ENV,
        DEFAULT_CONFIG_DIR as 源里的默认,
        config_dir,
    )

    assert CONFIG_DIR_ENV == "YECHAO_CONFIG_DIR"
    assert 源里的默认 == DEFAULT_CONFIG_DIR
    assert Settings.from_env().config_dir.name == config_dir().name


# ── 时间窗 ──────────────────────────────────────────────────────────


def test_时间窗是半开区间():
    """※ `[since, until)`：相邻两天拼起来不重不漏。闭区间会让 00:00:00 那一刻的
    消息同时落进两天里。"""
    msgs = 造源(夹具JSON).read()
    ts = sorted(m.ts for m in msgs)

    只有第二条 = 造源(夹具JSON).read(since=ts[1], until=ts[1] + 1)
    assert [m.ts for m in 只有第二条] == [ts[1]]

    # until 是**开**的：正好等于第一条时刻的 until 不含它。
    assert 造源(夹具JSON).read(until=ts[0]) == []
    # since 是**闭**的：正好等于第一条时刻的 since 含它。
    assert [m.ts for m in 造源(夹具JSON).read(since=ts[0], until=ts[1])] == [ts[0]]
