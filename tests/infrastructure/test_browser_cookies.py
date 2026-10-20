"""浏览器登录态 —— 全部用例都在**造的**库上跑，一条都不读本机真实的浏览器。

## 为什么不读本机真实的浏览器

CI 上没有浏览器；开发机上有的那个**可能正开着**（Windows 上实测：Edge 运行中
对它 `Cookies` 文件 `CreateFileW` 直接 `ERROR_SHARING_VIOLATION(32)`）。
用例一旦依赖它，症状就是「在我机器上是绿的」—— 而那条绿线什么也没证明。

⇒ 做法：在 `tmp_path` 里**造**一份最小 Chromium 库与一份 Firefox 库，
cookie 用**已知密钥**加密，DPAPI 那一步用 monkeypatch 换成「直接返回已知
密钥」。于是解密链路的每一步都被真的跑了一遍（base64 → 去前缀 → DPAPI →
AES-256-GCM），而输入完全可控。

## 样本全部自己编

假域名 `example.test` / `other.test`（RFC 2606 保留，永远不会解析到真机器）、
假 cookie 名 `demo_session`、假值 `made-up-value`。**不照抄任何真实站点的
cookie 名，也不照抄任何现有实现里的样本。**
"""

from __future__ import annotations

import ast
import base64
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import yechao.infrastructure.sources.browser_cookies as browser_cookies
from yechao.domain.errors import ConfigError
from yechao.infrastructure.sources import consent, subscription_source
from yechao.infrastructure.sources._common import CONFIG_DIR_ENV
from yechao.infrastructure.sources.subscription_sites import site_by_key

#: 造库时用的 AES-256 密钥。**已知** —— 所以「解出来是不是原来那串」可断言。
KEY = bytes(range(32))

#: 编出来的域名与值。见模块 docstring。
域名 = "example.test"
别人的域 = "other.test"
会话 = "demo_session"
值 = "made-up-value"


# ── 造库 ────────────────────────────────────────────────────────────


def _加密(value: str, *, prefix: bytes = browser_cookies.V10) -> bytes:
    """按 Chromium 的格式加密一条 cookie：`前缀 + 12 字节 nonce + 密文 + tag`。

    ※ nonce 固定（不是随机的）：产物要能复现，否则断言就得去解一遍才知道
      对不对 —— 那等于用被测代码验证被测代码。
    """
    nonce = bytes(range(browser_cookies.NONCE_BYTES))
    return prefix + nonce + AESGCM(KEY).encrypt(nonce, value.encode("utf-8"), None)


def _造_chromium(root: Path, rows: list[tuple], *, profile: str = "Default") -> Path:
    """在 `root` 下造一份最小 Chromium profile。`rows` = `[(host_key, name, 密文)]`。"""
    root.mkdir(parents=True, exist_ok=True)
    #: 真实浏览器的这一项是 base64(`DPAPI` + 密文)；DPAPI 那一步被 monkeypatch
    #: 换掉了，所以密文本身是什么不影响 —— 但**前缀必须真**，否则走的是另一条
    #: 分支（「没有 DPAPI 前缀」），那条分支有它自己的用例。
    (root / "Local State").write_text(
        json.dumps(
            {
                "os_crypt": {
                    "encrypted_key": base64.b64encode(
                        browser_cookies.DPAPI_PREFIX + b"whatever"
                    ).decode()
                }
            }
        ),
        encoding="utf-8",
    )
    network = root / profile / "Network"
    network.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(network / "Cookies")
    con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB)")
    con.executemany("INSERT INTO cookies VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()
    return root / profile


def _造_firefox(root: Path, rows: list[tuple], *, profile: str = "abc123.default-release") -> Path:
    """在 `root` 下造一份最小 Firefox profile。`rows` = `[(name, value, host)]`。"""
    d = root / profile
    d.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(d / "cookies.sqlite")
    con.execute(
        "CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, name TEXT, value TEXT,"
        " host TEXT, path TEXT, expiry INTEGER, isSecure INTEGER, isHttpOnly INTEGER)"
    )
    con.executemany("INSERT INTO moz_cookies (name, value, host) VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()
    return d


# ── 夹具 ────────────────────────────────────────────────────────────


def _装浏览器(monkeypatch, dirs: dict[str, Path]) -> None:
    """假装这些浏览器装在这些位置（键是 `edge` / `chrome` / `firefox`）。"""
    monkeypatch.setattr(
        browser_cookies,
        "_user_data_dirs",
        lambda key: (dirs[key],) if key in dirs else (),
    )


def _签协议(cfg: Path) -> None:
    cfg.joinpath(consent.CONSENT_FILE).write_text(
        f"[{consent.PROTOCOL_ID}]\n"
        f'version = "{consent.PROTOCOL_VERSION}"\n'
        f"accepted_at = 1760000000\n",
        encoding="utf-8",
    )


@pytest.fixture()
def 环境(tmp_path, monkeypatch):
    """配置目录、DPAPI、浏览器位置**三样**都指到临时位置上，并返回配置目录。

    ※ 三样都要指，少一样就有一条用例在「看开发机上恰好是什么样」：
      · 不指配置目录 ⇒ 读到开发机上真实的 `config/consent.toml`（协议签没签
        变成环境问题）；
      · 不换 DPAPI ⇒ 真去问 Windows 要一个它解不开的密钥，于是每条用例都
        在测「解不开」那条路；
      · 不指浏览器位置 ⇒ 去读开发机上真实的 Edge，而那个库**正被锁着**。
    """
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))
    monkeypatch.setattr(browser_cookies, "_dpapi_unprotect", lambda blob: KEY)
    _装浏览器(monkeypatch, {})
    return cfg


# ── 域名匹配 ────────────────────────────────────────────────────────


def test_域级与子域的_cookie_都命中(环境, monkeypatch, tmp_path) -> None:
    """点名一个域时，域级的、以及它下面各子域的 cookie 都算数。"""
    root = tmp_path / "chromium"
    _造_chromium(
        root,
        [
            (域名, "a", _加密("1")),  # 精确等于点名域
            ("." + 域名, "b", _加密("2")),  # 域级（带前导点）
            ("www." + 域名, "c", _加密("3")),  # 子域
        ],
    )
    _装浏览器(monkeypatch, {"edge": root})
    _签协议(环境)

    assert browser_cookies.cookies_for(域名) == "a=1; b=2; c=3"


def test_名字里含点名域但不是它的域_一条都不取(环境, monkeypatch, tmp_path) -> None:
    """※ 这条是**泄密**判据，不是风格判据。

    `endswith("example.test")` 会让 `notexample.test` 命中 —— 于是**别人的域**
    的 cookie 被塞进点名域的请求里。边界必须落在点上。
    """
    root = tmp_path / "chromium"
    _造_chromium(
        root,
        [
            (域名, "a", _加密("1")),
            ("not" + 域名, "b", _加密("2")),
            ("evil-" + 域名, "c", _加密("3")),
            (别人的域, "d", _加密("4")),
        ],
    )
    _装浏览器(monkeypatch, {"edge": root})
    _签协议(环境)

    assert browser_cookies.cookies_for(域名) == "a=1"


def test_点名子域时_父域的域级_cookie_也算数(环境, monkeypatch, tmp_path) -> None:
    """※ 接线会把**目标 URL 的 host**（`www.example.test`）传进来，而浏览器
    存的多半是域级的 `.example.test`。两个方向都要认，否则「域名对得上但读
    不到」—— 而它看起来像「没登录」。"""
    root = tmp_path / "chromium"
    _造_chromium(root, [("." + 域名, "a", _加密("1")), (别人的域, "b", _加密("2"))])
    _装浏览器(monkeypatch, {"edge": root})
    _签协议(环境)

    assert browser_cookies.cookies_for("www." + 域名) == "a=1"


# ── 解密链路 ────────────────────────────────────────────────────────


def test_v10_与_v11_都解得出明文(环境, monkeypatch, tmp_path) -> None:
    root = tmp_path / "chromium"
    _造_chromium(
        root,
        [
            (域名, "a", _加密("1", prefix=browser_cookies.V10)),
            (域名, "b", _加密("2", prefix=browser_cookies.V11)),
        ],
    )
    _装浏览器(monkeypatch, {"edge": root})
    _签协议(环境)

    assert browser_cookies.cookies_for(域名) == "a=1; b=2"


def test_密钥不对时解不开_并如实说是解不开(环境, monkeypatch, tmp_path) -> None:
    """※ 反例：拿错密钥不能**静默给出乱码** —— 乱码会让站点把请求打回登录页，
    而那看起来像「登录态过期了」。"""
    root = tmp_path / "chromium"
    _造_chromium(root, [(域名, "a", _加密("1"))])
    _装浏览器(monkeypatch, {"edge": root})
    _签协议(环境)
    monkeypatch.setattr(browser_cookies, "_dpapi_unprotect", lambda blob: bytes(32))

    assert browser_cookies.cookies_for(域名) is None
    assert "解不开" in browser_cookies.explain(域名)


def test_v20_说清是_app_bound_加密(环境, monkeypatch, tmp_path) -> None:
    """Chrome 127+ 的 app-bound 加密**本模块不做**，但要说得让人不必瞎猜。"""
    root = tmp_path / "chromium"
    _造_chromium(root, [(域名, "a", browser_cookies.V20 + bytes(64))])
    _装浏览器(monkeypatch, {"edge": root})
    _签协议(环境)

    assert browser_cookies.cookies_for(域名) is None
    assert "app-bound" in browser_cookies.explain(域名)


def test_一条解不开时_其余的照常给_并在_explain_里看得见(环境, monkeypatch, tmp_path) -> None:
    """部分失败**不整体丢**，但也不能无声：少发几条 cookie 会让站点认为你
    没登录，而那看起来像「登录态过期了」。"""
    root = tmp_path / "chromium"
    _造_chromium(
        root,
        [(域名, "a", _加密("1")), (域名, "b", browser_cookies.V20 + bytes(64))],
    )
    _装浏览器(monkeypatch, {"edge": root})
    _签协议(环境)

    found = browser_cookies._scan(域名)
    assert found.header == "a=1"
    assert any("解不开" in note for note in found.notes), found.notes
    assert "解不开" in browser_cookies.explain(域名)


def test_没装_cryptography_时报错并说清怎么装(环境, monkeypatch, tmp_path) -> None:
    """※ 与 `pymysql` / `redis` 同一条判据：**不回落**。

    回落成「读不到」的症状是「浏览器里明明登录了，工具说没配」——
    而用户会去查一个不存在的问题。
    """
    root = tmp_path / "chromium"
    _造_chromium(root, [(域名, "a", _加密("1"))])
    _装浏览器(monkeypatch, {"edge": root})
    _签协议(环境)
    # ※ 塞的是**子模块的全名**，不是 `"cryptography"`：`from a.b import c` 找的是
    #   `a.b` 那一项，而它早就被本文件顶上的 import 装进 `sys.modules` 了 ——
    #   只把 `a` 置 None 的话，那条 import 会**照样成功**（判据就空转了）。
    monkeypatch.setitem(
        sys.modules, "cryptography.hazmat.primitives.ciphers.aead", None
    )

    with pytest.raises(ConfigError, match="pip install cryptography"):
        browser_cookies.cookies_for(域名)


def test_Firefox_的库不加密_直接读(环境, monkeypatch, tmp_path) -> None:
    root = tmp_path / "firefox"
    _造_firefox(
        root,
        [(会话, 值, "." + 域名), ("别的", "不该出现", 别人的域)],
    )
    _装浏览器(monkeypatch, {"firefox": root})
    _签协议(环境)

    assert browser_cookies.cookies_for(域名) == f"{会话}={值}"


# ── 挑谁 ────────────────────────────────────────────────────────────


def test_按_Edge_Chrome_Firefox_的顺序挑(环境, monkeypatch, tmp_path) -> None:
    """三个都在时按 Edge → Chrome → Firefox；缺一个就顺延。"""
    edge = tmp_path / "edge"
    _造_chromium(edge, [(域名, "from_edge", _加密("E"))])
    chrome = tmp_path / "chrome"
    _造_chromium(chrome, [(域名, "from_chrome", _加密("C"))])
    firefox = tmp_path / "firefox"
    _造_firefox(firefox, [("from_firefox", "F", "." + 域名)])
    _签协议(环境)

    _装浏览器(monkeypatch, {"edge": edge, "chrome": chrome, "firefox": firefox})
    assert browser_cookies.cookies_for(域名) == "from_edge=E"

    _装浏览器(monkeypatch, {"chrome": chrome, "firefox": firefox})
    assert browser_cookies.cookies_for(域名) == "from_chrome=C"

    _装浏览器(monkeypatch, {"firefox": firefox})
    assert browser_cookies.cookies_for(域名) == "from_firefox=F"


def test_挑真的有点名域名的那个_profile(环境, monkeypatch, tmp_path) -> None:
    """※ 判据是「哪个 profile 里**真的有**那份 cookie」，不是「哪个目录叫
    Default」—— 按目录名挑会挑到一个没登录的 profile，用户看到的是
    「没配登录态」，而他明明登录了。"""
    root = tmp_path / "chromium"
    _造_chromium(root, [(别人的域, "a", _加密("1"))], profile="Default")
    _造_chromium(root, [(域名, "b", _加密("2"))], profile="Profile 1")
    _装浏览器(monkeypatch, {"edge": root})
    _签协议(环境)

    found = browser_cookies._scan(域名)
    assert found.header == "b=2"
    assert found.profile == "Profile 1"
    assert found.browser == "Edge"


# ── 只读与副本 ──────────────────────────────────────────────────────


def test_读之前先复制_原库一个字节都不动(环境, monkeypatch, tmp_path) -> None:
    """※ 两条一起钉：sqlite3 只碰**临时副本**，原文件前后逐字节相同。"""
    root = tmp_path / "chromium"
    profile = _造_chromium(root, [(域名, "a", _加密("1"))])
    db = profile / "Network" / "Cookies"
    原样 = db.read_bytes()
    原时间 = db.stat().st_mtime_ns
    _装浏览器(monkeypatch, {"edge": root})
    _签协议(环境)

    开过的 = []
    真的开 = sqlite3.connect

    def 记账(*args, **kwargs):
        开过的.append(args[0] if args else kwargs.get("database"))
        return 真的开(*args, **kwargs)

    monkeypatch.setattr(browser_cookies.sqlite3, "connect", 记账)
    assert browser_cookies.cookies_for(域名) == "a=1"

    assert 开过的, "一次 sqlite3.connect 都没记到 —— 这条判据是空转的"
    for path in 开过的:
        assert "yechao-cookies-" in str(path), f"没有走临时副本：{path}"
        assert str(db) not in str(path), f"直接开了原库：{path}"
    assert db.read_bytes() == 原样, "原库被改动了"
    assert db.stat().st_mtime_ns == 原时间, "原库被碰过了（mtime 变了）"


def test_库被占用时_跳过它继续找别的浏览器(环境, monkeypatch, tmp_path) -> None:
    """※ 浏览器开着时库会被独占锁住（Windows 上实测 `ERROR_SHARING_VIOLATION`）。

    一条路被锁住**不该**让整个查找失败 —— 别的浏览器里可能有。但也不能无声：
    原因要落在 note 里，`explain` 打得出来。
    """
    edge = tmp_path / "edge"
    _造_chromium(edge, [(域名, "from_edge", _加密("E"))])
    chrome = tmp_path / "chrome"
    _造_chromium(chrome, [(域名, "from_chrome", _加密("C"))])
    _装浏览器(monkeypatch, {"edge": edge, "chrome": chrome})
    _签协议(环境)

    真的拷 = shutil.copyfile

    def 只让_edge_失败(src, dst, **kwargs):
        if "edge" in str(src):
            raise PermissionError(13, "Permission denied")
        return 真的拷(src, dst, **kwargs)

    monkeypatch.setattr(browser_cookies.shutil, "copyfile", 只让_edge_失败)

    assert browser_cookies.cookies_for(域名) == "from_chrome=C"
    notes = browser_cookies._scan(域名).notes
    assert any("库读不到" in note for note in notes), notes


def test_副本读完就删掉(环境, monkeypatch, tmp_path) -> None:
    """副本里有**未脱敏的登录态** —— 留在临时目录里就是一次泄漏。"""
    root = tmp_path / "chromium"
    _造_chromium(root, [(域名, "a", _加密("1"))])
    _装浏览器(monkeypatch, {"edge": root})
    _签协议(环境)

    建过 = []
    真的建 = browser_cookies.tempfile.mkdtemp

    def 记账(*args, **kwargs):
        path = 真的建(*args, **kwargs)
        建过.append(Path(path))
        return path

    monkeypatch.setattr(browser_cookies.tempfile, "mkdtemp", 记账)
    assert browser_cookies.cookies_for(域名) == "a=1"

    assert 建过, "一个临时目录都没建 —— 这条判据是空转的"
    for path in 建过:
        assert not path.exists(), f"副本没删掉：{path}"


# ── 读不到 ──────────────────────────────────────────────────────────


def test_一个浏览器都没有时返回_None(环境) -> None:
    _签协议(环境)
    assert browser_cookies.cookies_for(域名) is None
    assert "没找到" in browser_cookies.explain(域名)


def test_有浏览器但没有这个域的_cookie_时返回_None(环境, monkeypatch, tmp_path) -> None:
    root = tmp_path / "chromium"
    _造_chromium(root, [(别人的域, "a", _加密("1"))])
    _装浏览器(monkeypatch, {"edge": root})
    _签协议(环境)

    assert browser_cookies.cookies_for(域名) is None
    assert f"没有 {域名} 的 cookie" in browser_cookies.explain(域名)


def test_库文件坏了时返回_None_而不是抛(环境, monkeypatch, tmp_path) -> None:
    """※ 契约是「没有返回 None」。坏掉的库要**记成 note**，不往外抛 ——
    一个 profile 坏了不该让整条路炸掉，但也必须看得见。"""
    root = tmp_path / "chromium"
    profile = _造_chromium(root, [(域名, "a", _加密("1"))])
    (profile / "Network" / "Cookies").write_bytes("这不是一个 sqlite 库".encode("utf-8"))
    _装浏览器(monkeypatch, {"edge": root})
    _签协议(环境)

    assert browser_cookies.cookies_for(域名) is None
    assert "读不出来" in browser_cookies.explain(域名)


# ── 协议闸门 ────────────────────────────────────────────────────────


def test_协议没签就抛(环境, monkeypatch, tmp_path) -> None:
    """※ 读浏览器 profile 是「用用户的身份去访问第三方站点」的准备动作，
    与订阅源那件事同一条判据 —— 没签就抛，两个入口都要。"""
    root = tmp_path / "chromium"
    _造_chromium(root, [(域名, "a", _加密("1"))])
    _装浏览器(monkeypatch, {"edge": root})
    # **不**签协议

    with pytest.raises(ConfigError) as 找:
        browser_cookies.cookies_for(域名)
    assert "协议" in str(找.value)

    with pytest.raises(ConfigError):
        browser_cookies.explain(域名)


# ── 值不外泄 ────────────────────────────────────────────────────────


def test_repr_里没有值(环境, monkeypatch, tmp_path) -> None:
    """※ 先断言值**真的**在 header 里（不然这条判据是空转的），再断言
    `repr` 里没有它。"""
    root = tmp_path / "chromium"
    _造_chromium(root, [(域名, 会话, _加密(值))])
    _装浏览器(monkeypatch, {"edge": root})
    _签协议(环境)

    found = browser_cookies._scan(域名)
    assert 值 in found.header, "值没进 header —— 那这条判据守的是个空集"
    assert 值 not in repr(found)
    assert "***" in repr(found), "要能看出这里有个值被藏了"


# ── 不联网 ──────────────────────────────────────────────────────────


#: 一旦出现在这个模块里就说明「本地预处理」破了口子的包。
联网的包 = frozenset(
    {
        "socket", "ssl", "urllib", "http", "ftplib", "smtplib", "telnetlib",
        "xmlrpc", "requests", "httpx", "aiohttp", "asyncio", "webbrowser",
    }
)


def test_模块里没有任何联网的导入() -> None:
    """※ 用 AST 扫**全部** import 节点，不只看顶层的：写在函数体里的
    `import socket` 才是最容易藏的那种。"""
    path = Path(browser_cookies.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    导入的 = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            导入的 += [别名.name for 别名 in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            导入的.append(node.module or "")

    # 空转自检：扫不到 import 的话这条判据恒为真。
    assert len(导入的) >= 5, f"只扫到 {len(导入的)} 个 import —— 判据是空转的"
    顶层 = {名字.split(".")[0] for 名字 in 导入的}
    assert not (顶层 & 联网的包), (
        f"这个模块不许联网（它读的全是本机文件），但它导入了："
        f"{sorted(顶层 & 联网的包)}"
    )


# ── 接线（`subscription_source` 那一侧） ────────────────────────────


def _开开关(cfg: Path, **其他) -> None:
    """在 `sources.toml` 的 `[subscription]` 里写配置。"""
    行 = "\n".join(f"{k} = {v}" for k, v in 其他.items())
    cfg.joinpath("sources.toml").write_text(
        f"[subscription]\n{行}\n" if 行 else "[subscription]\n", encoding="utf-8"
    )


def test_开关没开时不去读浏览器(环境, monkeypatch, tmp_path) -> None:
    """※ 默认关：读用户的浏览器 profile 是一件要**显式同意**的事。"""
    root = tmp_path / "chromium"
    _造_chromium(root, [("xiaohongshu.com", "web_session", _加密("来自浏览器"))])
    _装浏览器(monkeypatch, {"edge": root})
    _签协议(环境)

    def 不该被调用(host):
        raise AssertionError("开关没开却去读了浏览器")

    monkeypatch.setattr(browser_cookies, "cookies_for", 不该被调用)
    assert subscription_source._credential(site_by_key("xiaohongshu")) is None


def test_开关开了时浏览器登录态就是凭据(环境, monkeypatch, tmp_path) -> None:
    root = tmp_path / "chromium"
    _造_chromium(root, [("xiaohongshu.com", "web_session", _加密("来自浏览器"))])
    _装浏览器(monkeypatch, {"edge": root})
    _签协议(环境)
    _开开关(环境, browser_cookies="true")

    凭据 = subscription_source._credential(site_by_key("xiaohongshu"))
    assert 凭据 is not None and 凭据.kind == "cookie"
    assert 凭据.cookie == "web_session=来自浏览器"


def test_环境变量优先于浏览器登录态(环境, monkeypatch, tmp_path) -> None:
    """※ 环境变量是用户**显式**给的那一份，要能盖掉自动读到的 —— 自动读到的
    可能是同一个浏览器里的**另一个**账号。"""
    root = tmp_path / "chromium"
    _造_chromium(root, [("xiaohongshu.com", "web_session", _加密("来自浏览器"))])
    _装浏览器(monkeypatch, {"edge": root})
    _签协议(环境)
    _开开关(环境, browser_cookies="true")

    site = site_by_key("xiaohongshu")
    monkeypatch.setenv(site.cookie_env, "web_session=来自环境变量")

    def 不该被调用(host):
        raise AssertionError("有环境变量却还去读了浏览器")

    monkeypatch.setattr(browser_cookies, "cookies_for", 不该被调用)
    assert subscription_source._credential(site).cookie == "web_session=来自环境变量"


def test_装配时浏览器登录态能一路走到源(环境, monkeypatch, tmp_path) -> None:
    """整条路：`sources.toml` 开开关 → 装配 → 源拿到的凭据是浏览器里那一份。"""
    root = tmp_path / "chromium"
    _造_chromium(root, [("xiaohongshu.com", "web_session", _加密("来自浏览器"))])
    _装浏览器(monkeypatch, {"edge": root})
    _签协议(环境)
    _开开关(环境, site='"xiaohongshu"', target='"@某个账号"', browser_cookies="true")
    for 名字 in (
        subscription_source.TARGET_ENV,
        subscription_source.SITE_ENV,
        subscription_source.MODE_ENV,
        subscription_source.GROUP_ID_ENV,
    ):
        monkeypatch.delenv(名字, raising=False)

    源 = subscription_source.build_subscription_source()
    assert 源._credential.cookie == "web_session=来自浏览器"


def test_开关写错时抛_不猜(环境, monkeypatch, tmp_path) -> None:
    """※ 猜的症状是「我明明写了 false，它还是去读了我的浏览器」。"""
    _签协议(环境)
    _开开关(环境, browser_cookies='"maybe"')
    with pytest.raises(ConfigError, match="browser_cookies"):
        subscription_source._credential(site_by_key("xiaohongshu"))


def test_没读到时的报错说清是为什么(环境, monkeypatch, tmp_path) -> None:
    """※ 「浏览器正开着、库被锁住」与「你根本没登录过」是两件事，下一步完全
    不同 —— 而它们**都**表现为「没有登录态」。"""
    root = tmp_path / "chromium"
    _造_chromium(root, [("xiaohongshu.com", "web_session", _加密("来自浏览器"))])
    _装浏览器(monkeypatch, {"edge": root})
    _签协议(环境)
    _开开关(环境, browser_cookies="true")
    monkeypatch.setattr(browser_cookies.shutil, "copyfile", _总是失败)

    提示 = subscription_source._credential_hint(site_by_key("xiaohongshu"))
    assert "浏览器登录态" in 提示 and "开着" in 提示
    assert "库读不到" in 提示


def test_没开开关时的提示说清怎么开(环境) -> None:
    _签协议(环境)
    提示 = subscription_source._credential_hint(site_by_key("xiaohongshu"))
    assert subscription_source.BROWSER_COOKIES_KEY in 提示
    assert "默认**关**" in 提示


def _总是失败(src, dst, **kwargs):
    raise PermissionError(13, "Permission denied")
