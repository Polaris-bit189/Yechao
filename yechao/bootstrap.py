"""组合根 —— **唯一横跨多层的地方**。

## 为什么需要它

别处横跨多层会让「谁把谁接起来」分散在各处。集中之后，要回答
「这个端口的实现是谁」只有**一处**答案。

## 它做什么

建连接、装配适配器、把用例需要的东西传进去。**它不做任何业务判断** ——
判断都在领域层与用例层。

⚠️ 但它**做一件超出「装配」的事**：给入口层提供几个**搬运数据**的方法
（读快照、读课表、打包配置、只读自检）。理由见 `App` 的 docstring ——
那件事与「入口层不许直接摸适配层」这条判据是同一件事的两半。

⚠️ 它也是**唯一**允许同时 import 所有层的文件。护栏
（`tests/contracts/test_layering.py`）钉这条 —— 而它同时有一条**反向**判据：
组合根必须真的在装配（导入 ≥3 层），否则那条判据守的是一个空集。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yechao.application import digest as digest_uc
from yechao.application import extract as extract_uc
from yechao.application import ingest as ingest_uc
from yechao.application import reorder as reorder_uc
from yechao.domain.errors import ConfigError, StorageError
from yechao.infrastructure.config.toml_store import (
    CATEGORIES_FILE,
    GROUPS_FILE,
    TomlConfigStore,
    dumps_toml,
)
from yechao.infrastructure.llm.openai_client import ModelConfig, OpenAIClient
from yechao.infrastructure.persistence import sqlite_store
from yechao.infrastructure.runtime.audit_log import JsonlAuditLog
from yechao.infrastructure.runtime.clock import SystemClock
from yechao.infrastructure.sources.synth_source import SynthSource

#: 仓库根目录（`bootstrap.py` 的上一层）。
ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATA_DIR = "data"
DEFAULT_CONFIG_DIR = "config"
DEFAULT_DB_NAME = "yechao.db"

#: 写默认配置时要用的文件名 —— 入口层从这儿拿，不自己去适配层取。
CONFIG_FILENAMES = {"groups": GROUPS_FILE, "categories": CATEGORIES_FILE}


def env_path(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    """运行期路径。

    ⚠️ 从环境变量读，**每一项都有默认值** —— 因为「评委 clone 下来直接跑」
    不能要求他先配环境。而默认值一旦被用了，要能被**看出来**
    （`demo` / `doctor` 会打印它用的路径），否则「静默用了默认配置」
    就成了那个看起来像成功的失败。
    """

    data_dir: Path
    config_dir: Path
    db_path: Path

    @classmethod
    def from_env(cls, root: Path | None = None) -> "Settings":
        base = root or ROOT
        data = Path(env_path("YECHAO_DATA_DIR", str(base / DEFAULT_DATA_DIR)))
        cfg = Path(env_path("YECHAO_CONFIG_DIR", str(base / DEFAULT_CONFIG_DIR)))
        db = Path(env_path("YECHAO_DB", str(data / DEFAULT_DB_NAME)))
        return cls(data_dir=data, config_dir=cfg, db_path=db)

    @classmethod
    def from_paths(
        cls,
        *,
        data_dir: str | Path | None = None,
        config_dir: str | Path | None = None,
        db_path: str | Path | None = None,
        root: Path | None = None,
    ) -> "Settings":
        """环境变量 + 命令行覆盖。

        ⚠️ 这个方法存在的理由是**入口层不该自己拼路径**：默认值与覆盖
        规则只有一处答案（这里）。CLI 只把 `--data-dir` 的值递进来。
        """
        st = cls.from_env(root)
        if data_dir:
            st.data_dir = Path(data_dir)
            # ⚠️ **`--db` 没给时，主库要跟着 `--data-dir` 走。**
            #
            # 一开始写成「先 `from_env()` 把 db_path 定死，再覆盖 data_dir」
            # —— 于是 `--data-dir /tmp/x` 只改了数据目录，主库仍然写到
            # `<仓库>/data/yechao.db`。
            #
            # 这个 bug 的形状值得记：**参数生效了，但只是一半**。
            # 用户看到的是「我指定了目录，但库不在这里」，而错误信息指向
            # 「库不存在」—— 排查方向完全错。
            #
            # 判据：`data_dir` 与 `db_path` 的**默认值之间有依赖**，
            # 所以覆盖前者时必须重算后者。只有当后者被**显式**给了才不动它。
            if not db_path:
                st.db_path = st.data_dir / DEFAULT_DB_NAME
        if config_dir:
            st.config_dir = Path(config_dir)
        if db_path:
            st.db_path = Path(db_path)
        return st

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)


@dataclass
class App:
    """装配好的应用 —— 入口层拿到的就是它。

    ## ⚠️ 为什么它上面有那么多「搬运」方法

    入口层有一条判据：**不许直接 import `yechao.infrastructure`**
    （`tests/contracts/test_layering.py::test_入口层不直接摸适配层`）。

    那条判据是对的（「这个端口的实现是谁」要只有一处答案），但它把几个
    **纯粹是 I/O 搬运**的活逼到了没地方可去：

      · 读一份 JSON 快照（配置页的群清单）
      · 读一份 TOML（课表）
      · 把三份 TOML 拼成一个可导出的包
      · 试一试只读连接**真的**会被拒绝写

    这些活不**判断**任何业务规则 —— 它们是「把文件读成对象」。
    所以它们属于适配层那一侧，而入口层通过 `App` 的方法调用它们。

    ⚠️ 关键区别：入口层拿到的是**已经装好的结果**，不是**适配器的实例**。
    后者（在入口层写 `SqliteStore(conn)`）会让「谁接的线」散出去；前者不会。

    这就是「禁 import 适配层」这条判据与「把这些方法放这儿」之间的关系 ——
    它们是同一件事的两半。
    """

    settings: Settings
    store: Any            # SqliteStore（读写）
    reader: Any           # SqliteStore（只读）
    config: TomlConfigStore
    audit: JsonlAuditLog
    clock: SystemClock
    model: OpenAIClient
    #: ⚠️ 云端那一个。它是**另一个客户端**，不是 `model` 的别名 ——
    #: 两者看的东西不同（原文 vs 脱敏条目），配错了就是隐私事故。
    cloud: OpenAIClient

    # ── 用例的依赖包 ──────────────────────────────────────────

    def extract_deps(self) -> extract_uc.ExtractDeps:
        from yechao.application import prompts

        return extract_uc.ExtractDeps(
            messages=self.store,
            items=self.store,
            model=self.model,
            audit=self.audit,
            clock=self.clock,
            config=self.config,
            prompt=_PromptAdapter(prompts, self.config),
        )

    def digest_deps(self) -> digest_uc.DigestDeps:
        return digest_uc.DigestDeps(
            items=self.store,
            digests=self.store,
            model=self.model,
            clock=self.clock,
            config=self.config,
            audit=self.audit,
        )

    def ingest_deps(self) -> ingest_uc.IngestDeps:
        return ingest_uc.IngestDeps(messages=self.store, sources=[SynthSource()])

    def cloud_deps(self) -> Any:
        from yechao.application import cloud_refine

        return cloud_refine.CloudDeps(
            items=self.store,
            model=self.cloud,
            audit=self.audit,
            clock=self.clock,
            config=self.config,
            data_dir=self.settings.data_dir,
        )

    def reorder_deps(self) -> reorder_uc.ReorderDeps:
        return reorder_uc.ReorderDeps(
            items=self.store,
            edits=self.store,
            jobs=self.store,
            model=self.model,
            config=self.config,
            audit=self.audit,
        )

    # ── 前端产物 ──────────────────────────────────────────────

    @property
    def web_dist(self) -> Path | None:
        """前端的**已构建**产物目录。

        ⚠️ `serve` 服务的是 `web/dist/`，**不是源码**。README 第 ③ 步
        「浏览器打开就能看到网页」依赖 `dist/` 在仓库里。

        找不到时返回 `None`（调用方据此回 501 并给一句能照着做的提示），
        **不是**静默回一个 404 —— 那看起来像「路由写错了」。
        """
        for candidate in (
            self.settings.data_dir.parent / "web" / "dist",
            ROOT / "web" / "dist",
        ):
            if candidate.is_dir():
                return candidate
        return None

    # ── 配置包 ────────────────────────────────────────────────

    def build_pack(self) -> str:
        """把三份 TOML 拼成一个可导出的包。

        ⚠️ **不含密钥** —— 密钥走 `.env`，不在这三份配置里。
        导出的东西会被提交、会被发给评委，所以它不该有密钥。

        ⚠️ `[settings]` 只带**非机器相关**的项。`qq_db_dir` 那种带出去会让
        别台机器上的用户照着做 —— 而那是**连接超时**，不是报错，
        排查方向完全错。
        """
        cats = self.config.categories()
        data: dict[str, Any] = {
            "group": [
                {"id": g.id, "name": g.name, "tier": g.tier, "enabled": g.enabled}
                for g in self.config.groups()
            ],
            "person": [
                {"uin": p.uin, "label": p.label, "note": p.note}
                for p in self.config.persons()
            ],
            "category": [
                {"slug": c.slug, "label": c.label, "icon": c.icon, "desc": c.desc}
                for c in cats.categories
            ],
        }
        if cats.aliases:
            data["aliases"] = dict(cats.aliases)
        settings = self.config.settings()
        if "llm" in settings:
            data["llm"] = settings["llm"]
        return dumps_toml(data)

    # ── 数据源侧的两份素材文件 ─────────────────────────────────

    def read_groups_snapshot(self) -> dict[str, Any] | None:
        """读 CLI 预生成的群清单快照。

        ⚠️ 它是**快照**，不是实时查 QQ 库 —— 所以界面上有 `available`
        这个概念。读不到时返回 `None`（界面据此说「还没读过」），
        **不是**返回一个空列表 —— 那会被读成「你的库里一个群都没有」，
        一句假话。
        """
        path = self.settings.data_dir / "groups-snapshot.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # ⚠️ 文件在但读不出来 —— 这也是「没读过」，不是「空的」。
            # 两者形状相同是问题的根源。
            return None
        return data if isinstance(data, dict) else None

    def write_groups_snapshot(self, payload: dict[str, Any]) -> Path:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.settings.data_dir / "groups-snapshot.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def load_courses(self) -> list[Any] | None:
        """读课表。

        ⚠️ 返回 `None` = **读不出来**（文件不在 / 格式不认识），
        `[]` = **课表是空的**。这两件事的形状**必须**不同 ——
        混起来会让「没读课表」显示成「没有课冲突」，而后者是一句假话
        （见 `domain/conflicts.py::course_conflicts`）。
        """
        from yechao.domain.conflicts import Course
        from yechao.infrastructure.config.toml_store import load_toml

        path = self.settings.config_dir / "courses.toml"
        if not path.exists():
            return None
        try:
            raw = load_toml(path)
        except ConfigError:
            return None
        rows = raw.get("course")
        if not isinstance(rows, list):
            return None
        out: list[Course] = []
        for r in rows:
            if not isinstance(r, dict) or "start" not in r:
                continue
            out.append(
                Course(
                    label=str(r.get("label") or "课"),
                    start=int(r["start"]),
                    end=int(r.get("end") or (int(r["start"]) + 90 * 60)),
                )
            )
        return out

    # ── 只读自检（`doctor` 用）────────────────────────────────

    def readonly_actually_rejects_writes(self) -> tuple[bool, str]:
        """试一试只读连接**真的**会拒绝写。返回 `(是否被拒绝, 说明)`。

        ⚠️ 这个方法存在的理由：**一个「看起来在保护」的
        机制可能什么都没做，而且不报错。**

        不变量 ① 说「网页侧不得写入主库」—— 但「设了只读」与「只读真的
        生效」是两件事。判据是**行为**：真发一条 INSERT 出去，看它抛不抛。
        """
        readonly = sqlite_store.open_readonly(self.settings.db_path)
        try:
            readonly.execute(
                "INSERT INTO kvs (key, value) VALUES ('__probe__', 'x')"
            )
        except Exception:
            return True, "写操作被数据库拒绝（这是要的行为）"
        else:
            return False, "只读连接**竟然写成功了** —— 不变量 ① 被破坏"
        finally:
            readonly.close()

    def close(self) -> None:
        seen: set[int] = set()
        for conn in (self.store.conn, self.reader.conn):
            if id(conn) in seen:
                continue
            seen.add(id(conn))
            try:
                conn.close()
            except Exception:
                pass


class _PromptAdapter:
    """把 `application/prompts.py` 的函数适配成 `ExtractDeps.prompt` 需要的样子。

    ⚠️ 这层适配存在的理由：`prompts.py` 是**纯函数**（好测），而用例要的是
    一个**对象**（`deps.prompt.build_extract_user(...)`）。与其把纯函数改成
    对象（那会让测试变麻烦），不如在这里贴一层。

    ## ⚠️ 这里踩过一次坑，而且是最难发现的那一类

    第一版的 `EXTRACT_SYSTEM` 直接取了模块里的常量：

        self.EXTRACT_SYSTEM = mod.EXTRACT_SYSTEM

    而那个常量是一个**带 `{categories}` 占位符的模板** —— 真正的提示词要
    用 `build_extract_system(...)` 渲染出来。于是发出去的提示词里，
    类目表那一整块是一个字面量 `{categories}`。

    **模型照样回了形状完全正确的 JSON** —— 字段齐全、摘录能回连、语法合法。
    唯一的区别是：**类目判据完全没有依据**，它在猜。

    这个失败**看起来像成功**：CLI 报「批次 4/4 成功」，条目也真的产出了。
    靠读输出发现不了它，靠测试也发现不了（测试断言的形状是对的）——
    只有**跑一遍真流程、看实际产出**才看得见。

    ⇒ 所以现在是 **property**，每次现渲染：
      · 渲染是必须的（模板 → 提示词）
      · 每次现渲染而不是缓存，是因为**类目表是用户可编辑的数据**
        （`CategoryDetail.desc` 的注释逐字：「类目判据，会逐字进提示词。
        编辑器必须能改它。」）—— 缓存会让「刚改完类目但它没生效」出现。
    """

    def __init__(self, mod: Any, config: Any) -> None:
        self._mod = mod
        self._config = config

    @property
    def EXTRACT_SYSTEM(self) -> str:
        """**渲染过**的系统提示词。见上面那段。"""
        return self._mod.build_extract_system(self._config.categories().categories)

    @property
    def EXTRACT_MAX_TOKENS(self) -> int:
        return self._mod.EXTRACT_MAX_TOKENS

    def build_extract_user(self, messages: list[Any], scrubbed: list[str]) -> str:
        return self._mod.build_extract_user(messages, scrubbed)


def make_app(
    *,
    root: Path | None = None,
    settings: Settings | None = None,
    readonly: bool = False,
) -> App:
    """装配。`readonly=True` 时**只建只读连接**。"""
    st = settings or Settings.from_env(root)

    # ── 首启：配置目录是空的 ⇒ 从模板复制一份 ──────────────────
    #
    # ⚠️ 这一步原来没有，而它的缺失让**干净 clone 的第一次运行**直接失败：
    # `groups.toml` 不存在 ⇒ `groups()` 返回空列表 ⇒ 预筛拿不到 tier。
    #
    # ⚠️ **只在目录为空时复制。** 已经有配置的是**用户的** ——
    # 覆盖它会丢掉他的编辑，而服务「启动成功」会让这件事看起来没问题。
    _seed_config_if_empty(st)

    if readonly:
        # ⚠️ 用 factory 而不是一个共享连接 —— HTTP 端点在线程池里跑，
        # 同一个连接被多个线程同时用会炸（见 `SqliteStore` 的 docstring）。
        reader = sqlite_store.SqliteStore(
            factory=lambda: sqlite_store.open_readonly(st.db_path)
        )
        store = reader
    else:
        if not st.db_path.exists():
            sqlite_store.init_db(st.db_path)
        store = sqlite_store.SqliteStore(sqlite_store.connect(st.db_path))
        # ⚠️ 读连接同样按线程隔离：`serve` 走的就是这条 reader。
        reader = sqlite_store.SqliteStore(
            factory=lambda: sqlite_store.open_readonly(st.db_path)
        )

    return App(
        settings=st,
        store=store,
        reader=reader,
        config=TomlConfigStore(st.config_dir),
        audit=JsonlAuditLog(st.data_dir),
        clock=SystemClock(),
        model=OpenAIClient(_read_llm(st)),
        cloud=OpenAIClient(_read_cloud(st)),
    )


def _seed_config_if_empty(st: Settings) -> None:
    """配置目录为空时，从 `config.example/` 复制一份。

    ⚠️ 判据是「目录里**一个文件都没有**」，不是「某个文件不存在」——
    用户可能只删了一个文件；那时候不该用模板把它盖回去，因为他删它是有意的。
    半份配置由 `ConfigError` 报出来，而不是被静默补齐。
    """
    import shutil

    template = ROOT / "config.example"
    if not template.is_dir():
        return
    if st.config_dir.is_dir() and any(st.config_dir.iterdir()):
        return
    st.config_dir.mkdir(parents=True, exist_ok=True)
    for src in template.iterdir():
        if src.is_file():
            shutil.copy2(src, st.config_dir / src.name)


def _read_llm(st: Settings) -> ModelConfig:
    """读模型配置。

    ⚠️ 端点与模型名从**配置文件**读（用户能在网页上改），
    **密钥从环境变量**读 —— 配置文件会被导出、会被提交。

    ⚠️ 三个环境变量名都认：不同人的习惯不同，而认错了的症状是「401」，
    那会让排查方向跑偏到「模型名写错了」。
    """
    cfg = TomlConfigStore(st.config_dir).llm()
    key = (
        os.environ.get("YECHAO_API_KEY")
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    )
    return ModelConfig(
        base_url=cfg.base_url.strip(),
        model=cfg.model.strip(),
        enable_thinking=cfg.enable_thinking,
        api_key=key.strip(),
    )


def _read_cloud(st: Settings) -> ModelConfig:
    """读**云端**端点。

    ⚠️ 密钥与本地那个**分开读**：`YECHAO_CLOUD_API_KEY` 优先，
    回落顺序与本地一致。两边共用一个 key 是可能的（同一个服务商），
    但**分得开**是必要的 —— 它们对两个不同的地址、看两种不同的东西。
    """
    cfg = TomlConfigStore(st.config_dir).cloud()
    key = (
        os.environ.get("YECHAO_CLOUD_API_KEY")
        or os.environ.get("YECHAO_API_KEY")
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    )
    return ModelConfig(
        base_url=cfg.base_url.strip(),
        model=cfg.model.strip(),
        enable_thinking=cfg.enable_thinking,
        api_key=key.strip(),
    )


def make_app_or_explain(**kw: Any) -> App:
    """给 CLI 用：把配置/存储错误变成一句能照着做的提示。

    ⚠️ 「失败要响」不等于「抛一个 traceback」—— 用户要看到的是
    「哪里有问题」加「该改哪个文件」，那才是响。
    """
    try:
        return make_app(**kw)
    except (ConfigError, StorageError) as e:
        raise SystemExit(f"{e}\n") from e


def init_store(settings: Settings) -> Any:
    """建库并返回一个**读写** store。`demo` / `export` 用。

    ⚠️ 放这里的理由与 `App` 那几个方法相同：入口层不该自己
    `connect(...)`。入口层拿返回值只做「写入我给的数据」，不做判断。
    """
    settings.ensure_dirs()
    sqlite_store.init_db(settings.db_path)
    return sqlite_store.SqliteStore(sqlite_store.connect(settings.db_path))
