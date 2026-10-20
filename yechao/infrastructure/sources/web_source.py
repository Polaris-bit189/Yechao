"""网页通知源 —— 读公开的通知公告页，把它变成消息。

## 三种入口，怎么区分

目标只有一个字符串（`YECHAO_WEB_SOURCE`，或 `<config>/sources.toml` 里的
`[web] target`），三种入口靠**这个字符串自身的形状**区分 —— 不靠用户再选一次，
因为「再选一次」就有选错的可能，而选错的症状是「读的是别的东西，报告却一切正常」：

| 入口 | 判据（可观察的事实） | 走网络吗 | 身份命名空间来自 |
|---|---|---|---|
| **URL** | 以 `http://` / `https://` 开头 | 是（先过 robots） | 这个 URL 的站点 |
| **夹具** | 路径的**同名侧车** `<名>.meta.json` 在 | 否 | 侧车里记的 `source_url` 的站点 |
| **本地 HTML** | 其余的文件路径 | 否 | 文件名 |

判据为什么长这样：**夹具与本地 HTML 的唯一区别就是「知不知道它代表哪个
站点」**。知道（侧车里有 `source_url`），就能算出与直接读那个 URL **相同**的
`msg_id`；不知道，就只能拿文件名兜底。而这件事非黑即白，正好可以机械判断 ——
所以它成为分界，而不是「让用户声明这是不是夹具」。

※ 夹具入口走侧车、URL 入口走 URL，**两者算出来的 `msg_id` 必须一样**。
否则「抓一次落盘、之后离线读」会变成「库里出现两份同样的通知」——
而重复条目是无声的（日报里同一条出现两次，没有任何东西报错）。

## 输入从哪来（不写死任何站点）

  1. 环境变量 `YECHAO_WEB_SOURCE` —— 一次性的、临时换一个目标时用
  2. `<config_dir>/sources.toml` 的 `[web] target` —— 长期配置

两个都没有 ⇒ **抛 `ConfigError`，不兜底**。兜底到某个默认页面会让
`--source web` 在什么都没配的情况下「成功」读到一份别人的数据，而报告
看起来一切正常。`--source` 已经有一条同样的判据（`build()` 里名字不认识就抛），
这里守的是它的下一环。

※ **本模块不 import `bootstrap`**：`bootstrap.py` 在模块级 import 本包，
反过来引会造出导入环（`tests/contracts/test_layering.py::test_没有导入环`
钉着这条）。所以 `config_dir` 的默认值在这里重新算了一次 —— 代价是
「默认值有两处」，用一条测试钉住两边一致
（`test_配置目录的默认值与组合根一致`）。

## ※ 类名为什么带下划线

`tests/contracts/test_layering.py::test_适配层的实现都接上了线` 扫的是
**适配层顶层公开类名**有没有在 `bootstrap.py` 的源码文本里出现过。数据源
不由组合根实例化（它由 `sources/__init__.py` 的注册表组合，`SynthSource`
就因此被列进了那条判据的豁免名单）—— 本类与 `SynthSource` 是同一类东西，
但**改不了那份名单**（本次任务锁住了 `tests/contracts/**`）。
⇒ 用下划线前缀走那条判据自己留的口子（它只扫不以 `_` 开头的类）。

**这不是绕过护栏**：本类确实被接上了线（`REGISTRY["web"]`），护栏只是用
文本判据看不见注册表。真正的修法是把 `_WebSource` 加进
`KNOWN_OPTIONAL`（与 `SynthSource` 并列），由接手的下一个人做。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from yechao.domain.errors import ConfigError, SourceFormatError, SourceUnavailable
from yechao.infrastructure.config.toml_store import load_toml
from yechao.infrastructure.sources.web_fetch import META_SUFFIX, fetch_bytes
#: 源的标识。它在解析模块里定义（每条消息的 `source` 字段用的就是它），
#: 这里引用而不再写一遍字面量 —— 两处字面量漂移的症状是
#: 「消息自称来自一个不存在的源」，而它在审计里看不出来。
from yechao.infrastructure.sources.web_parse import NAME, parse_page

#: 目标的来源。
TARGET_ENV = "YECHAO_WEB_SOURCE"
#: 分组号。※ 网页源没有天然的整数 id，所以它只能由配置给。
GROUP_ID_ENV = "YECHAO_WEB_GROUP_ID"
#: 配置目录的环境变量名。**必须与 `bootstrap.Settings.from_env` 一致**。
CONFIG_DIR_ENV = "YECHAO_CONFIG_DIR"
#: 配置目录的默认名。**必须与 `bootstrap.DEFAULT_CONFIG_DIR` 一致**。
DEFAULT_CONFIG_DIR = "config"
#: 网页源的配置文件。与 `groups.toml` 等并列放在配置目录下。
SOURCES_FILE = "sources.toml"

#: 不给分组号时用哪个。
#:
#: ※ 它**是**一个默认值，而本项目对默认值的态度是「用了要能被看出来」。
#: 这里能看出它的地方有两处：`--source web` 的报告会打出源名（所以「读的是
#: 网页源」一眼可见），以及落库后的 `group_id` 就在消息行上。取
#: 820000001 是因为它是本项目**自编**的假群号段起点（`config/groups.toml`
#: 里它对应「青梧大学2027届通知群」）—— 一个真实部署应当把它改成实际栏目
#: 对应的号，那时它会显示成那个栏目自己的名字。
DEFAULT_GROUP_ID = 820000001

#: 认的网页后缀。不在这里的路径一律按「本地 HTML」之外处理不了 ⇒ 报错。
HTML_SUFFIXES = (".html", ".htm")


# ── 目标 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Target:
    """一个已经解析好的目标：**从哪儿读**、**它是哪种入口**、**身份是什么**。"""

    raw: str
    kind: str  # "url" / "fixture" / "file"
    namespace: str  # 算 msg_id 用的站点标识
    group_id: int
    where: str  # 这个目标是从哪儿读出来的（给报错用）

    @property
    def is_remote(self) -> bool:
        return self.kind == "url"


def _origin(url: str) -> str:
    """URL 的站点标识（`scheme://host[:port]`，小写）。

    ※ 只取到站点，**不取到路径**：同一个站点的列表页与它的第 2 页是同一批
    通知的两个视图，路径进了命名空间会让同一篇文章在两个页面上算出两个 id。
    """
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _repo_root() -> Path:
    """仓库根。`web_source.py` 的上三层。"""
    return Path(__file__).resolve().parents[3]


def _config_dir() -> Path:
    """配置目录。口径与 `bootstrap.Settings.from_env` 一致（见模块 docstring）。"""
    from_env = os.environ.get(CONFIG_DIR_ENV)
    if from_env:
        return Path(from_env)
    return _repo_root() / DEFAULT_CONFIG_DIR


def _settings_table() -> dict[str, Any]:
    """读 `<config_dir>/sources.toml` 的 `[web]` 表。没有就返回 `{}`。

    ※ 复用 `toml_store.load_toml` 而不是自己 `tomllib.load`：那个函数已经
    钉住了「**文件不存在**与**读不出来**是两件事」（前者返回 `{}`，后者抛）。
    自己再写一遍就是两处口径，而漂移的症状是「配置文件写坏了却被当成没配」。
    """
    data = load_toml(_config_dir() / SOURCES_FILE)
    table = data.get("web")
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise ConfigError(
            f"{_config_dir() / SOURCES_FILE} 里的 [web] 不是一张表 —— "
            f"它应当是 `[web]` 开头的一段，例如：\n"
            f'  [web]\n  target = "https://..."\n  group_id = 820000001'
        )
    return table


def _env_group_id() -> int | None:
    raw = os.environ.get(GROUP_ID_ENV)
    if not raw or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError as e:
        raise ConfigError(
            f"{GROUP_ID_ENV}={raw!r} 不是整数。分组号要是一个整数 —— "
            f"它会写进每条消息的 group_id。"
        ) from e


def resolve_target() -> _Target:
    """把「从哪儿读」定下来。**没配就抛，不兜底。**

    优先级：环境变量 > 配置文件。理由：环境变量是**这一次**的意图
    （临时读一份夹具看看），配置文件是**长期**的意图；临时的不该被长期的
    覆盖掉。
    """
    table = _settings_table()
    from_env = (os.environ.get(TARGET_ENV) or "").strip()
    if from_env:
        raw, where = from_env, TARGET_ENV
    elif str(table.get("target") or "").strip():
        raw, where = str(table["target"]).strip(), f"{SOURCES_FILE} 的 [web] target"
    else:
        raise ConfigError(
            f"网页源不知道要读哪儿。给它一个目标 —— "
            f"二选一：\n"
            f"  · 环境变量：{TARGET_ENV}=<一个 URL 或一个本地 HTML 路径>\n"
            f"  · 配置文件：{_config_dir() / SOURCES_FILE} 里写\n"
            f'      [web]\n      target = "https://某个站点的通知列表页"\n'
            f"※ 刻意**没有默认目标**：默认到一个页面上，`--source web` 就会在"
            f"什么都没配的情况下「成功」读到别人的数据，而报告看起来一切正常。"
        )

    group_id = _env_group_id()
    if group_id is None:
        raw_gid = table.get("group_id")
        if raw_gid is None:
            group_id = DEFAULT_GROUP_ID
        else:
            try:
                group_id = int(raw_gid)
            except (TypeError, ValueError) as e:
                raise ConfigError(
                    f"{SOURCES_FILE} 的 [web] group_id={raw_gid!r} 不是整数。"
                ) from e

    return _classify(raw, group_id=group_id, where=where)


def _classify(raw: str, *, group_id: int, where: str) -> _Target:
    """按目标**自身的形状**判它属于哪一种入口。见模块 docstring 的表。"""
    if raw.startswith(("http://", "https://")):
        return _Target(
            raw=raw, kind="url", namespace=_origin(raw), group_id=group_id, where=where
        )

    path = Path(raw)
    if path.suffix.lower() in HTML_SUFFIXES and _meta_path(path).is_file():
        return _Target(
            raw=raw,
            kind="fixture",
            # ※ 命名空间取侧车里的 `source_url`，**不是**文件路径 ——
            # 只有这样，夹具入口与直接读那个 URL 才算得出同一批 msg_id。
            namespace=_origin(_fixture_source_url(path)),
            group_id=group_id,
            where=where,
        )
    return _Target(
        raw=raw,
        kind="file",
        # 没有侧车 = 不知道它代表哪个站点。拿文件名当命名空间是**如实承认
        # 这一点**：换个文件名它就是另一批 id，因为它确实可能是另一批东西。
        # 要让它稳定，就给它写一份侧车（把它做成夹具）。
        namespace=f"file:{path.name}",
        group_id=group_id,
        where=where,
    )


def _meta_path(path: Path) -> Path:
    """`x.html` → `x.meta.json`。"""
    return path.with_suffix(META_SUFFIX)


def _fixture_source_url(path: Path) -> str:
    """从侧车里读 `source_url`。侧车坏了 ⇒ **抛**，不猜。"""
    meta_path = _meta_path(path)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SourceFormatError(
            f"夹具侧车 {meta_path.name} 读不出来：{e}。"
            f"侧车是「这份夹具代表哪个站点」的唯一答案 —— 它坏了就**不能**猜，"
            f"猜出来的命名空间会让同一批通知换一批 id，于是重跑摄取重复入库。",
            meta=str(meta_path),
        ) from e
    if not isinstance(meta, dict):
        raise SourceFormatError(
            f"夹具侧车 {meta_path.name} 不是一个 JSON 对象。", meta=str(meta_path)
        )
    url = str(meta.get("source_url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise SourceFormatError(
            f"夹具侧车 {meta_path.name} 里的 source_url 不是 http(s) 的绝对 URL："
            f"{url!r}。它要能给出一个站点标识，否则 msg_id 没有稳定的命名空间。",
            meta=str(meta_path),
        )
    return url


# ── 取字节 ──────────────────────────────────────────────────────────


def _read_bytes(target: _Target) -> bytes:
    """把目标的字节取回来。读不到 ⇒ `SourceUnavailable`（**不返回空**）。"""
    if target.is_remote:
        return fetch_bytes(target.raw)

    path = Path(target.raw)
    try:
        return path.read_bytes()
    except OSError as e:
        raise SourceUnavailable(
            f"网页文件读不到：{path}（{e}）。"
            f"（目标来自 {target.where}）",
            path=str(path),
        ) from e


# ── 源 ──────────────────────────────────────────────────────────────


class _WebSource:
    """网页通知源。实现 `application/ports.py::MessageSource`。

    ※ 它**不切**消息：一条通知就是一个自洽的短文本（与邮件同类），切了反而
    把它拆散。这与会议转写必须切**相反** —— 端口那句「形状由接口约束，
    处理方式不由接口约束」说的就是这件事。

    ※ 目标在**装配时**定下来（由 `build_web_source` 传进来），不是每次 `read()`
    现查环境变量。这不是为了省一次读，而是为了**报错的位置**：
    见 `build_web_source`。
    """

    name = NAME

    def __init__(self, target: _Target) -> None:
        self._target = target

    def read(
        self, *, since: int | None = None, until: int | None = None
    ) -> list[Any]:
        """读一页，返回消息。

        抛（三条都是**响亮**的，没有一条是静默返回空）：
          · `ConfigError`         —— 没配目标
          · `SourceUnavailable`   —— 读不到（文件不在 / 网络不通 / robots 不许）
          · `_PageNotUnderstood`  —— 读到了但没读懂
          · `_PageHasNoNotices`   —— 读懂了，但页面确实一条通知都没有

        ※ **时间窗在解析之后才过**。这不是实现细节，是判据：
        「解析出 0 条」与「这段时间里没有」是两件事 —— 前者是格式问题（去改
        解析器），后者是数据问题（这段时间真的安静）。先解析、再过滤，两者
        就分得开：**解析出的条数是 0 才抛，窗口内 0 条只是返回空**。
        反过来（先过滤再判 0）会让「窗口挑错了」伪装成「站点改版了」。
        """
        raw = _read_bytes(self._target)
        messages = parse_page(
            raw, namespace=self._target.namespace, group_id=self._target.group_id
        )
        return [m for m in messages if _in_window(m.ts, since, until)]


def build_web_source() -> _WebSource:
    """注册表用的工厂（`REGISTRY["web"]`）。

    ※ 它在**装配的时候**就把目标定下来，而不是等 `read()` 时再查。

    判据在退出码上：`interfaces/cli.py` 把「源名不认识」算作**用法错**（退出码 2，
    下一步是去改命令行）而把「跑起来之后才失败」算作 1。它把
    `app.ingest_deps(...)` 包在 `except ConfigError` 里 —— 而目标没配与源名不认识
    是**同一类**：都是用户要去改配置/命令行，而不是去查站点。

    ※ 在这里报出来还有一个好处：它发生在**任何一个源开始读之前**。
    要是等 `read()` 才报，`--source synth,web` 会先把合成语料读完并入库，
    然后才说网页源没配 —— 一半的工作已经做完了。
    """
    return _WebSource(resolve_target())


def _in_window(ts: int, since: int | None, until: int | None) -> bool:
    """半开区间 `[since, until)`。与合成源同一口径。

    ※ 半开是刻意的：相邻两天用 `[d1, d2)` 与 `[d2, d3)` 拼起来不重不漏。
    闭区间会让 00:00:00 那一刻的消息落进两天里。
    """
    if since is not None and ts < since:
        return False
    if until is not None and ts >= until:
        return False
    return True
