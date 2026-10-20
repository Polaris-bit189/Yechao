"""组合根 —— **唯一横跨多层的地方**。

## 为什么需要它

别处横跨多层会让「谁把谁接起来」分散在各处。集中之后，要回答
「这个端口的实现是谁」只有**一处**答案。

## 它做什么

建连接、装配适配器、把用例需要的东西传进去。**它不做任何业务判断** ——
判断都在领域层与用例层。

※ 但它**做一件超出「装配」的事**：给入口层提供几个**搬运数据**的方法
（读快照、读课表、打包配置、只读自检）。理由见 `App` 的 docstring ——
那件事与「入口层不许直接摸适配层」这条判据是同一件事的两半。

※ 它也是**唯一**允许同时 import 所有层的文件。护栏
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
from yechao.infrastructure.cache.null_cache import NullCache
from yechao.infrastructure.cache.redis_cache import RedisCache
from yechao.infrastructure.llm.openai_client import ModelConfig, OpenAIClient
from yechao.infrastructure.persistence import mysql_store, sqlite_store
from yechao.infrastructure.runtime.audit_log import JsonlAuditLog
from yechao.infrastructure.runtime.clock import SystemClock
from yechao.infrastructure.sources import DEFAULT_SOURCES, build

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

    ※ 从环境变量读，**每一项都有默认值** —— 因为「评委 clone 下来直接跑」
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

        ※ 这个方法存在的理由是**入口层不该自己拼路径**：默认值与覆盖
        规则只有一处答案（这里）。CLI 只把 `--data-dir` 的值递进来。
        """
        st = cls.from_env(root)
        if data_dir:
            st.data_dir = Path(data_dir)
            # ※ **`--db` 没给时，主库要跟着 `--data-dir` 走。**
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


#: ※ 与 `pyproject.toml` 的 `version` **必须一致**。
#: 由 `tests/interfaces/test_health.py` 里一条判据钉住（它去读 pyproject 比对）。
#: 两处各写一份必漂移，而漂移的症状是 `/api/health` 报出一个**不存在的版本**
#: —— 排查「线上跑的是哪一版」时，那是一条误导线索。
VERSION = "0.1.0"

#: 缓存的默认 TTL（秒）。可在 `config/settings.toml` 的 `[cache] ttl_seconds` 覆盖。
DEFAULT_CACHE_TTL = 10


def db_url_from_env() -> str:
    """数据库连接串 —— **它唯一的来源**。

    ※ 为什么不放配置文件：配置文件会被导出（`/api/config/export`）、会被
    提交，而连接串里有**密码**。与 `YECHAO_API_KEY` 同一个做法、同一个理由。

    ※ 为什么是**一个**变量而不是「后端 + 主机 + 端口 + 用户 + 密码 + 库名」：
    分开写会造出一个**能配出自相矛盾状态**的组合面（后端=mysql 而主机没给）。
    一个连接串的 scheme 本身就说明了后端是谁，矛盾状态不存在。
    """
    return os.environ.get("YECHAO_DB_URL", "").strip()


def cache_url_from_env() -> str:
    """Redis 连接串。**空 = 本机不启用缓存**（那是默认，不是回落）。

    与数据库那条判据对称：**设了就一定用它**，所以不会出现「我设了它但没生效」。
    连不上时缓存会降级（fail open），而降级**是可观测的**（`/api/health`）。
    """
    return os.environ.get("YECHAO_REDIS_URL", "").strip()


def resolve_db_target(url: str, *, default_path: Path) -> tuple[str, str]:
    """连接串 → `(后端名, 参数)`。**纯函数**，所以「配错了」能单独测。

    | 输入 | 结果 |
    |---|---|
    | 空 | `("sqlite", <默认库路径>)` —— 本机默认 |
    | `sqlite://<路径>` | `("sqlite", <路径>)` |
    | `mysql://…` | `("mysql", <连接串>)` |
    | 其它 scheme | **抛 `ConfigError`** |

    ## ※ 「读不到就回落成 SQLite」是这里**最不能**做的事

    回落的症状是：用户配了一个连不上的 RDS，程序**一声不吭**地在本地文件里
    写数据。而它**看起来是成功的** —— 网页能开、条目能出、日报能生成。
    直到有人发现云上的库里什么都没有。

    所以：认不出的 scheme ⇒ 报错并列出支持哪几种；MySQL 连接串不完整
    ⇒ `parse_mysql_url` 报错（它也**不给默认值**，理由见那边的注释）。

    ※ 「空 ⇒ SQLite」不是回落，是**默认**：`YECHAO_DB_URL` 没设时用户
    没有表达任何意图，而本机默认就是 SQLite。这两件事的区别是
    「用户想要 A 但我们给了 B」与「用户没说，我们用默认」。
    """
    if not url:
        return "sqlite", str(default_path)
    scheme = url.split("://", 1)[0].lower()
    if scheme == "sqlite":
        path = url.split("://", 1)[1]
        if not path:
            raise ConfigError(
                "sqlite:// 后面要给库文件路径（例如 sqlite:///app/data/yechao.db）。"
                "※ 这里**不会**替你填一个默认路径 —— 那会让你以为连的是你写的那个库。"
            )
        return "sqlite", path
    if scheme == "mysql":
        return "mysql", url
    raise ConfigError(
        f"不认识的数据库连接串 scheme：{scheme}://。"
        f"只认这两种：`sqlite://<库文件路径>` 与 "
        f"`mysql://user:pass@host:3306/dbname`。"
        f"（支持的后端名：{"、".join(mysql_store.SUPPORTED_SCHEMES)}）"
    )


def read_cache_ttl(st: "Settings") -> int:
    """读 `config/settings.toml` 的 `[cache] ttl_seconds`。

    ※ TTL **不是**机密，所以它在配置文件里（能被导出、能被评委看见），
    而连接串在环境变量里。这条分界与 `[llm]` / `YECHAO_API_KEY` 是同一条。

    ※ 键**在但值不合法** ⇒ 报错，不静默用默认值：一个「配了 30 但实际
    跑 10」的配置，与「没配」在行为上一样、在意图上完全不同 ——
    静默取默认值会让前者永远查不出来。
    """
    from yechao.infrastructure.config.toml_store import load_toml

    path = st.config_dir / "settings.toml"
    if not path.exists():
        return DEFAULT_CACHE_TTL
    raw = load_toml(path).get("cache") or {}
    if not isinstance(raw, dict) or "ttl_seconds" not in raw:
        return DEFAULT_CACHE_TTL
    value = raw["ttl_seconds"]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConfigError(
            f"config/settings.toml 的 [cache] ttl_seconds 只能是非负整数，"
            f"现在写的是 {value!r}。"
        )
    return value


@dataclass
class App:
    """装配好的应用 —— 入口层拿到的就是它。

    ## ※ 为什么它上面有那么多「搬运」方法

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

    ※ 关键区别：入口层拿到的是**已经装好的结果**，不是**适配器的实例**。
    后者（在入口层写 `SqliteStore(conn)`）会让「谁接的线」散出去；前者不会。

    这就是「禁 import 适配层」这条判据与「把这些方法放这儿」之间的关系 ——
    它们是同一件事的两半。
    """

    settings: Settings
    store: Any            # SqliteStore / MySQLStore（读写）
    reader: Any           # SqliteStore / MySQLStore（只读）
    #: ※ 缓存端口。本机是 `NullCache`（**可观测**的空实现），云上是
    #: `RedisCache`。它的类型在这里只能是 `Any` —— 与仓储同理，
    #: 组合根之上不该出现适配层的类名。
    cache: Any
    config: TomlConfigStore
    audit: JsonlAuditLog
    clock: SystemClock
    model: OpenAIClient
    #: ※ 云端那一个。它是**另一个客户端**，不是 `model` 的别名 ——
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

    def ingest_deps(self, names: list[str] | None = None) -> ingest_uc.IngestDeps:
        """装配摄取用的依赖。

        `names` 是**源名**（`--source` 递进来的那几个），不给就用默认源。

        ※ 这里收的是**名字**而不是源实例，理由是那条护栏：入口层不许
        import 适配层（`test_入口层不直接摸适配层`）。于是「`--source meeting`
        指的是哪个类」只有一处答案 —— `infrastructure/sources/__init__.py`
        里那张表。CLI 只递名字，由组合根把名字翻成实例。

        ※ 名字不认识时 `build` 抛 `ConfigError`，**不回落**到默认源。
        回落的症状是「用户以为读的是真源，其实读的是合成语料」，
        而报告看起来一切正常。
        """
        return ingest_uc.IngestDeps(
            messages=self.store,
            sources=build(names or DEFAULT_SOURCES),
            # ※ 时钟与 `extract_deps` / `digest_deps` 同源（`SystemClock`）——
            # 账里的时间戳全项目只有一处口径。
            clock=self.clock,
        )

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
            clock=self.clock,
        )

    # ── 健康（`GET /api/health` 的三问）────────────────────────

    def health(self) -> dict[str, Any]:
        """服务 / 库 / 缓存，每问都给「通不通 + 用的是什么」。

        ## 为什么原来的探活（`/api/items?limit=1`）不够

        它只证明了「库能读出东西」—— 验不到缓存挂没挂，也分不清
        「服务活着但库坏了」（那种情况下它会回 500，而 500 与「服务没起来」
        在编排层看起来是一样的：都只是「不健康」）。

        ## ※ 库挂了**该不该**让整个服务被判为不健康：**该**

        判据是**产品**：这个产品的每一条路由都要读库（条目、日历、日报、
        类目计数）。库挂了，除了静态首页之外每一个页面都是坏的 ——
        一个回 200 的健康检查会把这件事说成「健康」，而那正是本项目
        最怕的形状（失败看起来像成功）。

        ## ※ 缓存挂了**不该**：缓存是降级，不是故障

        它挂掉时产品仍然**完整可用**（只是慢）。把它也算成「不健康」的
        后果是编排层反复重启一个本来就正常的容器 —— 而重启修不好 Redis，
        只会让它一直重启。所以缓存那一问走 `cache.ok` 与 `degraded`：
        **看得见**，但不改 HTTP 状态码。

        ## 为什么 `service.ok` 恒为 `True`

        能答出这个响应，就说明服务本身活着。写死 `True` 不是偷懒 ——
        它是「这一问的答案由**能不能回这个响应**给出」，再在里面加一层
        「服务是否健康」的判断只会造出第二个真相来源。
        """
        db_ok, db_detail = _safe_probe(self.reader)
        cache_stats = self.cache.stats()
        cache_ok, cache_detail = _safe_probe(self.cache)
        return {
            # ※ 只由**库**决定。见 docstring 的两条判据。
            "ok": db_ok,
            #: 有降级但没坏 —— 前端 / 编排层据此显示提示，而不是判死。
            "degraded": (not cache_ok) or bool(cache_stats.get("errors")),
            "service": {"ok": True, "name": "yechao", "version": VERSION},
            "database": {
                "ok": db_ok,
                "backend": _backend_name(self.reader),
                "detail": db_detail,
            },
            "cache": {
                "ok": cache_ok,
                "backend": cache_stats.get("backend"),
                # ※ `enabled` 与 `ok` **必须分开**：空实现是
                # `ok: true, enabled: false`（没故障、也没在工作）。
                # 合成一个字段就会让「没启用」读起来像「工作正常」。
                "enabled": cache_stats.get("enabled"),
                "detail": cache_detail,
                "stats": cache_stats,
            },
        }

    # ── 前端产物 ──────────────────────────────────────────────

    @property
    def web_dist(self) -> Path | None:
        """前端的**已构建**产物目录。

        ※ `serve` 服务的是 `web/dist/`，**不是源码**。README 第 ③ 步
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

        ※ **不含密钥** —— 密钥走 `.env`，不在这三份配置里。
        导出的东西会被提交、会被发给评委，所以它不该有密钥。

        ※ `[settings]` 只带**非机器相关**的项。`qq_db_dir` 那种带出去会让
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
                {"account": p.account, "label": p.label, "note": p.note}
                for p in self.config.persons()
            ],
            "category": [
                {"slug": c.slug, "label": c.label, "desc": c.desc}
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

        ※ 它是**快照**，不是实时查 QQ 库 —— 所以界面上有 `available`
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
            # ※ 文件在但读不出来 —— 这也是「没读过」，不是「空的」。
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

        ※ 返回 `None` = **读不出来**（文件不在 / 格式不认识），
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

        ※ 这个方法存在的理由：**一个「看起来在保护」的
        机制可能什么都没做，而且不报错。**

        不变量 ① 说「网页侧不得写入主库」—— 但「设了只读」与「只读真的
        生效」是两件事。判据是**行为**：真发一条 INSERT 出去，看它抛不抛。

        ※ 两个后端各探各的：SQLite 靠 `mode=ro` + `PRAGMA query_only`，
        MySQL 靠 `SET SESSION TRANSACTION READ ONLY`。**探错后端等于没探**
        —— 在 MySQL 上跑 SQLite 那句 `PRAGMA` 会抛，于是这个方法会报
        「写被拒绝了」，而它其实什么都没验到。那正是「一个看起来在保护的
        机制什么都没做」。
        """
        backend, target = resolve_db_target(
            db_url_from_env(), default_path=self.settings.db_path
        )
        if backend == "mysql":
            conn = mysql_store.connect_mysql(target, readonly=True)
            try:
                cur = conn.cursor()
                try:
                    cur.execute(
                        "INSERT INTO `kvs` (`key`, `value`) "
                        "VALUES ('__probe__', 'x')"
                    )
                finally:
                    cur.close()
            except Exception:
                return True, "写操作被 MySQL 拒绝（这是要的行为）"
            else:
                return False, "只读连接**竟然写成功了** —— 不变量 ① 被破坏"
            finally:
                conn.close()

        readonly = sqlite_store.open_readonly(Path(target))
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
        """关掉两条连接。

        ※ `getattr(holder, "conn", None)` 而不是直接取属性：关连接是
        **收尾**动作，它自己抛异常会把真正要看的失败盖掉（比如一条测试的
        断言失败，会被 teardown 里一个无关的 `AttributeError` 顶掉）。
        取不到就当没有 —— 关不上不该制造新的问题。
        """
        seen: set[int] = set()
        for holder in (self.store, self.reader):
            conn = getattr(holder, "conn", None)
            if conn is None or id(conn) in seen:
                continue
            seen.add(id(conn))
            try:
                conn.close()
            except Exception:
                pass


class _PromptAdapter:
    """把 `application/prompts.py` 的函数适配成 `ExtractDeps.prompt` 需要的样子。

    ※ 这层适配存在的理由：`prompts.py` 是**纯函数**（好测），而用例要的是
    一个**对象**（`deps.prompt.build_extract_user(...)`）。与其把纯函数改成
    对象（那会让测试变麻烦），不如在这里贴一层。

    ## ※ 这里踩过一次坑，而且是最难发现的那一类

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
        （`desc` 会被原样拼进提示词）。缓存会让「刚改完类目、模型却还用
        旧判据」这种问题变得极难查。
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
    # ※ 这一步原来没有，而它的缺失让**干净 clone 的第一次运行**直接失败：
    # `groups.toml` 不存在 ⇒ `groups()` 返回空列表 ⇒ 预筛拿不到 tier。
    #
    # ※ **只在目录为空时复制。** 已经有配置的是**用户的** ——
    # 覆盖它会丢掉他的编辑，而服务「启动成功」会让这件事看起来没问题。
    _seed_config_if_empty(st)

    store, reader = _build_stores(st, readonly=readonly)

    return App(
        settings=st,
        store=store,
        reader=reader,
        cache=_build_cache(st),
        config=TomlConfigStore(st.config_dir),
        audit=JsonlAuditLog(st.data_dir),
        clock=SystemClock(),
        model=OpenAIClient(_read_llm(st)),
        cloud=OpenAIClient(_read_cloud(st)),
    )


def _build_stores(st: Settings, *, readonly: bool) -> tuple[Any, Any]:
    """按 `YECHAO_DB_URL` 装配**读写**与**只读**两条存储。

    ※ 这是「用哪个后端」**唯一**的答案所在。用例层、入口层都拿不到这个
    判断 —— 它们只看到端口。
    """
    backend, target = resolve_db_target(
        db_url_from_env(), default_path=st.db_path
    )

    if backend == "mysql":
        # ※ 只读那条也走 `connect_mysql(readonly=True)`，而不是「同一个
        # 连接少写点」—— 不变量 ① 靠**数据库**拒绝写，不靠调用方自觉。
        label = _mysql_label(target)
        if readonly:
            reader = mysql_store.MySQLStore(
                factory=lambda: mysql_store.connect_mysql(target, readonly=True),
                readonly=True,
                label=label,
            )
            return reader, reader
        boot = mysql_store.connect_mysql(target, readonly=False)
        # ※ 每次启动都建表（`CREATE TABLE IF NOT EXISTS` 幂等）：RDS 上
        # 第一次连上去的是一个**空库**，没有「文件在不在」可判断。
        mysql_store.init_db(boot)
        boot.close()
        # ※ **写连接也按线程隔离。** HTTP 的写端点（删条目、改类目、撤销、
        # 取消作业）与读端点跑在**同一个**线程池里 —— 共享一条写连接时，
        # 两个并发请求会撞出 `InterfaceError`，实测约 10% 回 500，
        # 还夹着几个**假 404**（`SELECT ... WHERE item_id=?` 被搅成 None，
        # 于是对一条存在的条目说「没有这条条目」）。
        store = mysql_store.MySQLStore(
            factory=lambda: mysql_store.connect_mysql(target, readonly=False),
            label=label,
        )
        # ※ 读连接按线程隔离：HTTP 端点在 FastAPI 的线程池里跑。
        reader = mysql_store.MySQLStore(
            factory=lambda: mysql_store.connect_mysql(target, readonly=True),
            readonly=True,
            label=label,
        )
        return store, reader

    path = Path(target)
    if readonly:
        # ※ 用 factory 而不是一个共享连接 —— HTTP 端点在线程池里跑，
        # 同一个连接被多个线程同时用会炸（见 `SqliteStore` 的 docstring）。
        reader = sqlite_store.SqliteStore(
            factory=lambda: sqlite_store.open_readonly(path), label=str(path)
        )
        return reader, reader

    if not path.exists():
        sqlite_store.init_db(path)
    # ※ **写连接也按线程隔离。** 理由与上一段相同：`serve` 的写端点跑在
    # FastAPI 的线程池里，共享一条连接时并发写会炸 —— 而这条 bug 在
    # 单线程测试里一个都抓不到，只有浏览器真的并发请求时才出现。
    store = sqlite_store.SqliteStore(
        factory=lambda: sqlite_store.connect(path), label=str(path)
    )
    # ※ 读连接同样按线程隔离：`serve` 走的就是这条 reader。
    reader = sqlite_store.SqliteStore(
        factory=lambda: sqlite_store.open_readonly(path), label=str(path)
    )
    return store, reader


def _mysql_label(url: str) -> str:
    """`mysql://user:pass@host:3306/db` → `host:3306/db`。

    ※ **去掉凭据**：这个标签会出现在 `/api/health` 的响应里（那是给人看
    排查线索的地方），而响应可能被贴进工单、日志、聊天窗口。
    连接串里的密码不该跟着走。
    """
    params = mysql_store.parse_mysql_url(url)
    return f"{params['host']}:{params['port']}/{params['database']}"


def _build_cache(st: Settings) -> Any:
    """按 `YECHAO_REDIS_URL` 装配缓存。

    ※ 没设 ⇒ `NullCache`（本机默认），而它**不是**「什么都不传」：
    它有名有姓、能回答「我是 `none` 后端、我没在工作」，所以
    `/api/health` 报得出这件事。见 `infrastructure/cache/__init__.py`。
    """
    ttl = read_cache_ttl(st)
    url = cache_url_from_env()
    if not url:
        return NullCache(default_ttl=ttl)
    return RedisCache.from_url(url, default_ttl=ttl)


def _safe_probe(obj: Any) -> tuple[bool, str]:
    """探活。**任何异常都变成 `(False, 说明)`**。

    ※ 探活本身抛异常时，健康端点不能跟着 500 —— 那就成了「探活把服务
    探死了」，而用户看到的是一句与真实病因无关的 500。
    """
    try:
        return obj.ping()
    except Exception as e:
        return False, f"探活时自己抛了异常：{type(e).__name__}: {e}"


def _backend_name(obj: Any) -> str:
    """问一个存储「你是什么」。问不出来时报 `"?"` —— 而不是猜一个。"""
    try:
        return str(obj.backend_name())
    except Exception:
        return "?"


def _seed_config_if_empty(st: Settings) -> None:
    """配置目录为空时，从 `config.example/` 复制一份。

    ※ 判据是「目录里**一个文件都没有**」，不是「某个文件不存在」——
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

    ※ 端点与模型名从**配置文件**读（用户能在网页上改），
    **密钥从环境变量**读 —— 配置文件会被导出、会被提交。

    ※ 三个环境变量名都认：不同人的习惯不同，而认错了的症状是「401」，
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

    ※ 密钥与本地那个**分开读**：`YECHAO_CLOUD_API_KEY` 优先，
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

    ※ 「失败要响」不等于「抛一个 traceback」—— 用户要看到的是
    「哪里有问题」加「该改哪个文件」，那才是响。
    """
    try:
        return make_app(**kw)
    except (ConfigError, StorageError) as e:
        raise SystemExit(f"{e}\n") from e


# ※ 这里原来有一个 `init_store()`：建库 + 返回一个裸的读写 store，给
# `demo` 用。**它已经删掉**，理由值得留：
#
# 它的存在本身就是「`demo` 绕过了源层」的物证 —— `demo` 拿它直接
# `upsert_messages(语料)`，中间不经过 `MessageSource` / `ingest` 用例。
# 于是数据源层是**死代码**：跑得通，但没人跑它。
#
# 现在 `demo` 与 `ingest` 走同一条路（`App.ingest_deps()` →
# `application.ingest.run()`），`init_store` 就没有调用者了。
# 留着一个没人调用的函数正是本项目最在意的那类失败（「写了但没接」），
# 所以删掉而不是留着。


def acquire_writer_lock(data_dir: Any, *, command: str) -> tuple[Any, str]:
    """给写者命令拿一把**进程级互斥锁**。返回 `(锁或 None, 拿不到的理由或 "")`。

    ※ 为什么入口层通过**这里**拿，而不是自己 import 适配层：
    `tests/contracts/test_layering.py::test_入口层不直接摸适配层` 钉着这条。
    「锁在哪一层」是接线问题，接线的答案只有组合根一处。

    判据与实现见 `infrastructure/runtime/lock.py`（用内核的排它锁，进程被
    强杀时由内核释放）。
    """
    from yechao.infrastructure.runtime import lock

    return lock.acquire(data_dir, command=command)


def is_writer_command(command: str) -> bool:
    """这个命令是不是**写者**（会改主库）。

    ※ 与 `acquire_writer_lock` 同一个理由：入口层不自己 import 适配层。
    """
    from yechao.infrastructure.runtime import lock

    return lock.is_writer(command)
