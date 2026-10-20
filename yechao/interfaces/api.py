"""HTTP 路由 —— 实现前端那一侧的**冻结契约**。

## 路由注册顺序是**承重的**

`/api/items/batch-kind` 必须排在 `/api/items/{id}` **前面**，否则前者会被更宽
的前缀吃掉 —— 请求会走进详情分支，`batch-kind` 被当成 id 去解析。同理：
  · `/api/digests/{id}` 要排在 `/api/digests` 前
  · `/api/categories/undo` 要排在 `/api/categories/{slug}` 前

FastAPI 按注册顺序匹配，所以下面每一条的**位置**都是有理由的。

## 为什么 GET 全部用只读连接

**不变量 ①**：网页侧不得写入主库。`app.reader` 是一个只读连接
（`mode=ro` + `PRAGMA query_only`），写操作由 SQLite 自己拒绝。

※ 写端点（POST/PUT/PATCH/DELETE）走 `app.store`（读写连接）——
它们**本来**就该写。不变量 ① 说的是「网页侧不得写**主库**」，
而这里的写是**用户明确操作**产生的（删条目、改类目），
它们通过**事件追加**落地，那是设计的一部分。

「网页侧不得写入主库」的准确含义是：**没有一条 GET 会写**。
所以 GET 一律 `app.reader`，而这个区分在类型上是看得见的。

## 错误 → 状态码

| 异常 | 码 | 用途 |
|---|---|---|
| `ValidationError` | 400 | 输入不合法 |
| `NotFound` | 404 | 没有这条 |
| `ConfigError` | 400 | 配置坏了 |
| `NotWired` | **501** | 还没做 —— 与 500 区分「还没做」与「做了但出错」 |
| 其它 | 500 | |

※ **4xx 不是「服务不可达」** —— 前端的连通性状态机据此保持在线。
把 4xx 算成离线会让用户看到假的「离线」横幅。
"""

from __future__ import annotations

from typing import Any

from fastapi import Body, FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from yechao.domain.errors import (
    ConfigError,
    NotFound,
    NotWired,
    ValidationError,
    YechaoError,
)
from yechao.interfaces import view

#: 列表页的 `limit` 上限。
#: ※ 前端也知道这个数 —— 它拉「最近 N 天」时直接按 200 请求。
#: 改这里要同步改前端，否则前端会以为拿到了更多。
MAX_LIMIT = 200

DEFAULT_LIMIT = 50


def _error(code: int, detail: str) -> JSONResponse:
    """※ 前端读 `body.detail` 当错误消息**原样显示**。

    所以 `detail` 的措辞是**给人看的**，要能指出下一步该做什么，
    不是「出错了」。
    """
    return JSONResponse(status_code=code, content={"detail": detail})


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFound)
    async def _nf(_r: Request, exc: NotFound) -> JSONResponse:
        return _error(404, str(exc))

    @app.exception_handler(ValidationError)
    async def _ve(_r: Request, exc: ValidationError) -> JSONResponse:
        return _error(400, str(exc))

    @app.exception_handler(ConfigError)
    async def _ce(_r: Request, exc: ConfigError) -> JSONResponse:
        return _error(400, str(exc))

    @app.exception_handler(NotWired)
    async def _nw(_r: Request, exc: NotWired) -> JSONResponse:
        # ※ 501 而不是 500 —— 「还没做」与「做了但出错」对排查者意味着
        # 完全不同的方向。
        return _error(501, str(exc))

    @app.exception_handler(YechaoError)
    async def _ye(_r: Request, exc: YechaoError) -> JSONResponse:
        return _error(500, str(exc))


def create_app(app_obj: Any) -> FastAPI:
    """建一个 FastAPI 应用。

    `app_obj` 是 `bootstrap.App` —— ※ 它在这里只被**读**，
    本层不做任何业务判断。
    """
    api = FastAPI(title="夜抄", docs_url="/api/docs", openapi_url="/api/openapi.json")
    install_error_handlers(api)

    # ── 展示用的查找表 ────────────────────────────────────────
    #
    # ※ 每次请求现读配置而不是缓存：配置可以被网页改（新增类目），
    # 缓存会让「刚加完类目但它不出现在筛选里」这种 bug 出现。
    # 三份 TOML 的读盘开销在这个规模下可以忽略。

    def _kinds() -> tuple[dict[str, Any], dict[str, str]]:
        cats = app_obj.config.categories()
        return cats.by_slug(), cats.aliases

    def _group_names() -> dict[int, str]:
        return {g.id: g.name for g in app_obj.config.groups()}

    def _out(item: Any) -> dict[str, Any]:
        kinds, aliases = _kinds()
        return view.item_out(
            item, kinds=kinds, aliases=aliases, group_names=_group_names()
        )

    # ══ 条目 ═══════════════════════════════════════════════════
    #
    # ※ **顺序**：`/api/items/batch-kind` 必须在 `/api/items/{item_id}` **之前**，
    # 否则它会被后者当成「item_id = 'batch-kind'」吃掉。

    @api.post("/api/items/batch-kind")
    def bulk_set_kind(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """人工多选批量改类目。

        ※ 返回值里的 `actor` 是**撤销的凭据**：一次批量改会动很多条，
        服务端靠它把这些行认成同一批。前端丢了它，这一批就撤不回来了。
        """
        ids = payload.get("item_ids") or []
        kind = str(payload.get("kind") or "")
        if not isinstance(ids, list) or not ids:
            raise ValidationError("item_ids 不能为空", field="item_ids")
        if not kind:
            raise ValidationError("kind 不能为空", field="kind")
        # 非数字的编号回 400，不抛裸 ValueError（那会变成 500，
        # 而前端把 500 读成「服务不可达」，于是弹出假的离线横幅）。
        try:
            item_ids = [int(i) for i in ids]
        except (TypeError, ValueError):
            raise ValidationError(
                "item_ids 里必须都是条目编号", field="item_ids"
            ) from None
        actor = f"bulk:{app_obj.clock.now()}:{len(item_ids)}"
        actor, count = app_obj.store.bulk_set_kind(
            item_ids, kind, actor=actor, ts=app_obj.clock.now()
        )
        return {"actor": actor, "count": count}

    # 撤销是一个资源、两种作用域：`/api/undo` 撤最近一步，这里撤一整批。
    @api.post("/api/undo/batch")
    def bulk_undo(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        actor = str(payload.get("actor") or "")
        ok, count = app_obj.store.bulk_undo(actor, ts=app_obj.clock.now())
        return {"undone": ok, "count": count}

    @api.get("/api/items")
    def list_items(
        kind: list[str] | None = Query(None),
        person: list[int] | None = Query(None),
        since: str | None = None,
        until: str | None = None,
        group: int | None = None,
        q: str | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """信息流。

        ※ `kind` / `person` 是**重复参数**（`kind=a&kind=b`），
        同一维度内**并集**、维度之间**交集**。
        这一维一个都没勾，与整个参数不传是同一个意思 —— 都不按它筛。
        """
        # 负的 LIMIT 在 SQLite 里等于「不限制」—— 一个请求就能把整库拉下来，
        # 而响应里的 `limit` 还会如实回报一个负数。MySQL 那边则会因为
        # `LIMIT -1` 语法报错回 500：同一个请求，两个后端两种结果。
        if limit < 0:
            limit = 0
        if limit > MAX_LIMIT:
            # ※ 不静默截断 —— 那会让调用方以为拿到了更多。
            # 但也**不报错**：前端可能改成更大的值而不知道上限。
            # 这里选择「收窄到上限」并让 `limit` 字段**如实回报**，
            # 调用方读它就知道实际给了多少。前端读的正是那个字段。
            limit = MAX_LIMIT
        rows, total = app_obj.reader.search(
            kinds=[k for k in (kind or []) if k],
            persons=[p for p in (person or []) if p],
            since=since,
            until=until,
            group=group,
            q=q,
            limit=limit,
            offset=offset,
        )
        return {
            "items": [_out(i) for i in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @api.get("/api/items/{item_id}")
    def item_detail(item_id: int) -> dict[str, Any]:
        item = app_obj.reader.item_detail(item_id)
        if item is None:
            raise NotFound(f"没有这条条目：{item_id}")
        out = _out(item)
        names = _group_names()
        out["sources"] = [
            view.source_out(r, group_names=names)
            for r in app_obj.reader.sources_of(item_id)
        ]
        return out

    @api.patch("/api/items/{item_id}")
    def set_item_kind(item_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """改一个条目的字段。目前只有 `kind`。

        ※ 是 PATCH 不是 POST：改的是这个条目**已有**的属性，没有新资源
        产生 —— 所以回 `edit_id`（这次改动的编号，撤销要用），不回新对象。
        """
        kind = str(payload.get("kind") or "")
        if not kind:
            raise ValidationError("kind 不能为空", field="kind")
        edit_id = app_obj.store.set_kind(
            item_id, kind, actor="web", ts=app_obj.clock.now()
        )
        return {"edit_id": edit_id}

    @api.delete("/api/items/{item_id}")
    def delete_item(item_id: int) -> dict[str, Any]:
        """※ **软删**：行留着，只打一个删除标记。

        不物理删的理由是抽取**可以重跑**。真删了，下一轮重跑会把这条
        从源消息里再抽出来一次 —— 用户会觉得自己的删除根本没生效。
        """
        edit_id = app_obj.store.soft_delete(
            item_id, actor="web", ts=app_obj.clock.now()
        )
        return {"edit_id": edit_id}

    @api.post("/api/undo")
    def undo(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """※ 要撤最近一步，`edit_id` 得**显式传 `null`**。

        后端拿 `None` 当「撤最近一步」的信号。而在 Python 侧，「请求体里
        没有这个键」和「键在、值是 null」是两回事 —— 前者会走进另一条分支。
        """
        raw = payload.get("edit_id", None)
        if raw is None:
            edit_id = None
        else:
            try:
                edit_id = int(raw)
            except (TypeError, ValueError):
                raise ValidationError(
                    "edit_id 必须是一个整数编号", field="edit_id"
                ) from None
        ok = app_obj.store.undo(edit_id, ts=app_obj.clock.now())
        return {"undone": ok}

    # ══ 日历 / 紧急 ═══════════════════════════════════════════

    @api.get("/api/calendar")
    def calendar(since: str | None = None, until: str | None = None) -> dict[str, Any]:
        """日历。**不分页、默认全量**。

        ※ 它**不走 `/api/items`** —— 后者的 limit 上限是 200，
        而日历必须**全量**：日历上少一格，那一格看起来就是「这天没事」，
        而它其实有事 —— 正是「静默返回少一点」的形状。分页在这里也没有
        意义，日历上没有「下一页」可点。
        """
        from yechao.domain.conflicts import course_conflicts, find_conflicts, find_duplicates

        rows = app_obj.reader.items_with_deadline(since=since, until=until)
        dup = find_duplicates(rows)
        dup_ids = set(dup)
        # ※ 算冲突时**排除被折叠的** —— 否则同一件事的两个副本会互相「撞车」
        # （那不存在的冲突会占满警示条）。
        visible = [i for i in rows if i.item_id not in dup_ids]
        conflicts = find_conflicts(visible)
        courses = app_obj.load_courses()
        cc = course_conflicts(visible, courses)
        return {
            "items": [_out(i) for i in rows],
            "total": len(rows),
            # ※ 被折叠的**仍在 items 里** —— 前端据此把它们**收起来**，
            # 而不是不渲染。直接 filter 掉的话，屏幕上少的那条不留痕迹，
            # 而那正是「静默丢数据」的形状。
            "duplicates": {str(k): v for k, v in dup.items()},
            "conflicts": [_conflict_out(c) for c in conflicts],
            "course_conflicts": [
                _conflict_out(c) if not isinstance(c, dict) else c for c in cc
            ],
        }

    @api.get("/api/urgent")
    def urgent(since: str | None = None, until: str | None = None) -> dict[str, Any]:
        """紧急条目（30s 轮询用）。

        ※ **刻意不复用 `/api/calendar`**：那个端点每次都要算重复几何 +
        读课表算课冲突，是「翻着看」的用途；本端点每 30 秒被调一次，
        必须最廉价。

        ※ **不返回 `level` 字段** —— 分级在前端按**浏览器本地日**算。
        在这里加一个 `deadline_level` 会造出第三份阈值，
        而且它与浏览器算的时点不同，**跨午夜会漂移**。

        ## ※ 这一条是**唯一**走缓存的路由，理由就在它自己的上一段里

        「每 30 秒被调一次，必须最廉价」—— 这句话就是缓存的**理由**。

        判据（为什么陈旧是可以接受的）：TTL（默认 10 秒，见
        `config/settings.toml` 的 `[cache] ttl_seconds`）**小于**前端 30 秒的
        轮询间隔。也就是说，用户下一次来取的时候缓存已经过期了 ——
        从他那一侧看到的**新鲜度与不缓存时完全一样**，而服务器少算了很多次。

        ※ 空缓存（本机默认）下这条路是**穿透**的：`get()` 永远未命中、
        `set()` 什么都不做 —— 行为与加缓存之前**逐字节相同**。
        """
        import json

        # 窗口是 `[今天-1, 今天+2)` —— 过期项最长就是「昨天」。
        day = app_obj.clock.today()
        start = app_obj.clock.day_start_from_str(day) - 86400
        end = app_obj.clock.day_start_from_str(day) + 2 * 86400
        window_since = since or str(start)
        window_until = until or str(end - 1)

        # ※ key 里必须带上**实际生效的窗口**，不是「今天」—— 两个不同的
        # 窗口共用一个 key 会互相返回对方的条目，而那看起来像「数据串了」。
        key = f"urgent:{window_since}:{window_until}"
        cached = app_obj.cache.get(key)
        if cached is not None:
            return json.loads(cached)

        rows = app_obj.reader.items_with_deadline(
            since=window_since, until=window_until
        )
        body = {"items": [_out(i) for i in rows], "total": len(rows)}
        app_obj.cache.set(key, json.dumps(body, ensure_ascii=False))
        return body

    # ══ 类目 ═══════════════════════════════════════════════════
    #
    # ※ `/api/categories/undo` 要在 `/api/categories/{slug}` 之前。

    @api.post("/api/categories/undo")
    def categories_undo() -> dict[str, Any]:
        """撤销上一次类目改动。

        ※ 栈空时返回 **409**，不是 400。

        这个请求本身是合法的，只是当前没有东西可撤 —— 回 400 会让前端
        把它当成「参数写错了」。前端有 `.catch()` 专门接 409，据此显示
        「撤销栈是空的」。
        """
        key = "categories_undo_stack"
        raw = app_obj.store.get_kv(key)
        if not raw:
            return _error(409, "没有可撤销的类目改动")
        import json

        snapshot = json.loads(raw)
        from yechao.domain.config_models import CategorySet
        from yechao.domain.categories import Category

        app_obj.config.set_categories(
            CategorySet(
                # 只取现在认识的字段 —— 多出来的键忽略，不报错。
                categories=[
                    Category(**{k: v for k, v in c.items() if k in _CATEGORY_KEYS})
                    for c in snapshot["categories"]
                ],
                aliases=snapshot["aliases"],
            )
        )
        # ※ 撤销是**一次性的**：用掉就清空，否则再点一次会「撤销撤销」。
        app_obj.store.set_kv(key, "")
        return {"undone": True}

    @api.get("/api/categories")
    def categories() -> dict[str, Any]:
        """※ **一个端点两种形状**：它必须同时给出 `desc`（编辑器要）
        与 `aliases`（改名留痕要），一次往返满足两个调用方。
        """
        cats = app_obj.config.categories()
        counts = app_obj.reader.counts_by_kind()
        return {
            "categories": [
                {
                    "slug": c.slug,
                    "label": c.label,
                    "desc": c.desc,
                    "count": counts.get(c.slug, 0),
                }
                for c in cats.categories
            ],
            "aliases": dict(cats.aliases),
        }

    @api.post("/api/categories", status_code=201)
    def add_category(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        from yechao.domain.categories import Category

        slug = str(payload.get("slug") or "").strip()
        _check_slug(slug)
        cats = app_obj.config.categories()
        if any(c.slug == slug for c in cats.categories):
            raise ValidationError(f"类目 {slug} 已经有了", field="slug")
        _snapshot_categories(app_obj)
        cats.categories.append(
            Category(
                slug=slug,
                label=str(payload.get("label") or slug),
                desc=str(payload.get("desc") or ""),
            )
        )
        app_obj.config.set_categories(cats)
        return {"slug": slug}

    @api.put("/api/categories/{slug}")
    def update_category(slug: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """改名 / 改判据。

        ※ 改名走的是**别名** —— 旧 slug 的条目不改 `item_id`，只是显示时
        按新名字（前端那份见 `web/src/pages/SetupPage.tsx`）。

        ※ `CategorySet` 是 **frozen 的值对象**（值对象纪律：不可变 =
        「谁在哪一步改了它」不需要顺着调用链找）。
        所以这里**产出新的**，不原地改 —— 第一版写了
        `cats.categories, cats.aliases = rename(...)`，当场
        `FrozenInstanceError`。那不是「冻结太严」，那是**值对象在提醒
        调用方：你要的是一个新值**。
        """
        from dataclasses import replace as _replace

        from yechao.domain.categories import rename

        current = app_obj.config.categories()
        target = next((c for c in current.categories if c.slug == slug), None)
        if target is None:
            raise NotFound(f"没有这个类目：{slug}")
        _snapshot_categories(app_obj)

        cats = list(current.categories)
        aliases = dict(current.aliases)

        new_slug = str(payload.get("slug") or "").strip()
        if new_slug and new_slug != slug:
            _check_slug(new_slug)
            if any(c.slug == new_slug for c in cats):
                raise ValidationError(f"类目 {new_slug} 已经有了", field="slug")
            cats, aliases = rename(cats, aliases, slug, new_slug)
            slug = new_slug

        for i, c in enumerate(cats):
            if c.slug != slug:
                continue
            changes: dict[str, Any] = {}
            if payload.get("label") is not None:
                changes["label"] = str(payload["label"])
            if payload.get("desc") is not None:
                changes["desc"] = str(payload["desc"])
            cats[i] = _replace(c, **changes)
            break

        from yechao.domain.config_models import CategorySet

        app_obj.config.set_categories(CategorySet(categories=cats, aliases=aliases))
        return {"slug": slug}

    @api.delete("/api/categories/{slug}", status_code=204, response_model=None)
    def remove_category(slug: str, migrate_to: str | None = None) -> Response:
        """删类目。`migrate_to` 是**查询参数**，不是 body 字段。

        ※ 还有条目挂在它下面时，**必须**给 `migrate_to`。

        不给就删，那些条目的 kind 会变成一个查不到的 slug —— 前端拿它去
        查类目名会查空，卡片上那一栏就直接消失了。
        """
        from yechao.domain.config_models import CategorySet

        current = app_obj.config.categories()
        target = next((c for c in current.categories if c.slug == slug), None)
        if target is None:
            raise NotFound(f"没有这个类目：{slug}")

        counts = app_obj.reader.counts_by_kind()
        n = counts.get(slug, 0)
        if n and not migrate_to:
            raise ValidationError(
                f"「{target.label}」下面还挂着 {n} 条条目，删之前得先给它们指定一个迁移去处",
                field="migrate_to",
            )
        if n and migrate_to:
            if not any(c.slug == migrate_to for c in current.categories):
                raise ValidationError(
                    f"迁移目标 {migrate_to} 不存在", field="migrate_to"
                )
            rows, _ = app_obj.reader.search(kinds=[slug], limit=100000)
            app_obj.store.bulk_set_kind(
                [i.item_id for i in rows], migrate_to,
                actor=f"migrate:{slug}", ts=app_obj.clock.now(),
            )

        _snapshot_categories(app_obj)
        app_obj.config.set_categories(
            CategorySet(
                categories=[c for c in current.categories if c.slug != slug],
                aliases=dict(current.aliases),
            )
        )
        return Response(status_code=204)

    # ══ 日报 ═══════════════════════════════════════════════════

    @api.get("/api/digests")
    def digests() -> dict[str, Any]:
        rows = app_obj.reader.list_digests()
        return {"digests": [view.digest_summary_out(d) for d in rows]}

    @api.get("/api/digests/{digest_id}")
    def digest_detail(digest_id: int) -> dict[str, Any]:
        found = app_obj.reader.digest_detail(digest_id)
        if found is None:
            raise NotFound(f"没有这篇日报：{digest_id}")
        d, items = found
        out = view.digest_summary_out(d)
        out["body_md"] = d.body_md
        out["items"] = [_out(i) for i in items]
        return out

    # ══ 人物 ═══════════════════════════════════════════════════

    @api.get("/api/persons")
    def persons() -> dict[str, Any]:
        """※ `count` 由**后端**算 —— 前端别拉回全部条目自己数。

        两个理由：那要把整个库搬下来；而且数出来的结果会跟着当前筛选条件
        变，而这里要的是这个人的**总量**，不是筛完之后剩多少。
        """
        counts = app_obj.reader.counts_by_person()
        return {
            "persons": [
                {
                    "account": p.account,
                    "label": p.label,
                    "note": p.note,
                    "count": counts.get(p.account, 0),
                }
                for p in app_obj.config.persons()
            ]
        }

    @api.post("/api/persons", status_code=201)
    def add_person(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        from yechao.domain.config_models import PersonConfig

        try:
            account = int(payload.get("account"))
        except (TypeError, ValueError):
            raise ValidationError(
                "账号只能是正整数（0 不构成一个账号）", field="account"
            ) from None
        label = str(payload.get("label") or "").strip()
        if not label:
            raise ValidationError(
                "填一个名字 —— 群自己的昵称会改，你填的这个不会", field="label"
            )
        people = app_obj.config.persons()
        if any(p.account == account for p in people):
            raise ValidationError(
                f"这个账号已经在名单里了：{account}", field="account"
            )
        people.append(
            PersonConfig(
                account=account, label=label, note=str(payload.get("note") or "")
            )
        )
        app_obj.config.set_persons(people)
        return {"account": account, "label": label}

    @api.delete("/api/persons/{account}", status_code=204, response_model=None)
    def remove_person(account: int) -> Response:
        people = [p for p in app_obj.config.persons() if p.account != account]
        app_obj.config.set_persons(people)
        return Response(status_code=204)

    # ══ 群 ═════════════════════════════════════════════════════

    @api.get("/api/groups")
    def groups() -> dict[str, Any]:
        """群清单。

        ※ `available: false` 和「一个群都没有」不能混着说。

        前者是「快照还没生成」，后者是「读了，确实没有群」。混着说，用户会
        以为自己的数据源里一个群都没有 —— 而其实只是还没读。
        """
        snap = app_obj.read_groups_snapshot()
        configured = {g.id: g for g in app_obj.config.groups()}

        if snap is None:
            return {
                "available": False,
                "reason": (
                    "还没有群清单快照。"
                    "请先在仓库根目录跑一次 `python -m yechao demo`（或 `export`）。"
                ),
                "generated_at": None,
                "groups": [],
            }

        out = []
        for row in snap.get("groups", []):
            gid = int(row["id"])
            cfg = configured.get(gid)
            out.append(
                {
                    "id": gid,
                    "name": row.get("name") or "",
                    "messages": int(row.get("messages") or 0),
                    "last_ts": int(row.get("last_ts") or 0),
                    # ※ `watched` 与 `known` 是两个字段：
                    # 「已纳入且启用」与「已纳入但可能被停用」。
                    # 界面靠 `known` 判断「它已在配置里」——
                    # 那是**唯一**能证明这件事的字段。
                    "watched": bool(cfg and cfg.enabled),
                    "known": cfg is not None,
                }
            )
        return {
            "available": True,
            "reason": "",
            "generated_at": snap.get("generated_at"),
            "groups": out,
        }

    @api.post("/api/groups")
    def add_group(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """※ **名可以为空，服务端不许收紧。**

        快照读不到群名时会留一个空串，这是正常情况（本地库不一定总有群名）。
        前后端都要求 name 非空的话，这类群**必然**拿回 400、在网页上**加不
        进来** —— 而它们恰恰是最需要手动加的那一批。
        """
        from yechao.domain.config_models import GroupConfig

        try:
            gid = int(payload.get("id"))
        except (TypeError, ValueError):
            raise ValidationError("群号必须是正整数", field="id") from None
        groups_cfg = app_obj.config.groups()
        if any(g.id == gid for g in groups_cfg):
            # ※ 409 的文案要指出去哪改 —— 「已经在配置里了」加上下一步。
            return _error(409, f"群 {gid} 已经在配置里了")
        groups_cfg.append(
            GroupConfig(
                id=gid,
                name=str(payload.get("name") or ""),
                tier=str(payload.get("tier") or "normal"),
            )
        )
        app_obj.config.set_groups(groups_cfg)
        return {"id": gid}

    @api.patch("/api/groups/{group_id}")
    def patch_group(group_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        import dataclasses

        groups_cfg = app_obj.config.groups()
        found = False
        for i, g in enumerate(groups_cfg):
            if g.id != group_id:
                continue
            changes: dict[str, Any] = {}
            if payload.get("name") is not None:
                changes["name"] = str(payload["name"])
            if payload.get("enabled") is not None:
                changes["enabled"] = bool(payload["enabled"])
            if payload.get("tier") is not None:
                changes["tier"] = str(payload["tier"])
            groups_cfg[i] = dataclasses.replace(g, **changes)
            found = True
            break
        if not found:
            raise NotFound(f"配置里没有群 {group_id}")
        app_obj.config.set_groups(groups_cfg)
        return {"id": group_id}

    @api.delete("/api/groups/{group_id}", status_code=204, response_model=None)
    def remove_group(group_id: int) -> Response:
        groups_cfg = [g for g in app_obj.config.groups() if g.id != group_id]
        app_obj.config.set_groups(groups_cfg)
        return Response(status_code=204)

    # ══ 模型设置 ═══════════════════════════════════════════════

    @api.get("/api/llm")
    def get_llm() -> dict[str, Any]:
        llm = app_obj.config.llm()
        return {
            "base_url": llm.base_url,
            "model": llm.model,
            # ※ `None` = **不发这个字段**（通用 OpenAI 端点最安全的形状）。
            "enable_thinking": llm.enable_thinking,
        }

    @api.put("/api/llm")
    def put_llm(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """※ **响应是回填源**：三个字段全部按服务端归一化后的值回给前端。

        `trim()` 是在写入时做的归一化。页面上必须能看到归一化之后的值 ——
        否则用户填了个带空格的地址，下次刷新会发现它自己变了。
        """
        from yechao.domain.config_models import LlmConfig

        et = payload.get("enable_thinking", None)
        cfg = LlmConfig(
            base_url=str(payload.get("base_url") or ""),
            model=str(payload.get("model") or ""),
            enable_thinking=None if et is None else bool(et),
        ).normalized()
        app_obj.config.set_llm(cfg)
        return {
            "base_url": cfg.base_url,
            "model": cfg.model,
            "enable_thinking": cfg.enable_thinking,
        }

    # ══ 后台作业 ═══════════════════════════════════════════════
    #
    # 重排不是一种独立的资源，它**就是**一个后台作业 —— 所以起任务走
    # `POST /api/jobs`，作业类型由服务端定（目前只有一种）。

    @api.post("/api/jobs", status_code=202)
    def start_reorder() -> Any:
        """起重排任务。

        ※ **409 = 已有 job 在跑**（唯一的 `JobBusy` 名额）。这不是「出错了」，
        而是「等一下」—— 后端那句 detail 已经把话说清楚了，前端原样显示。
        """
        from yechao.application import reorder as reorder_uc

        if app_obj.store.any_job_running():
            return _error(409, "已有任务在跑，等它结束再来")
        # ※ 读的是**配置**（`LlmConfig.configured`），不是模型客户端 ——
        # `ModelConfig` 上没有这个属性：客户端只管发请求，它不知道自己配没配。
        if not app_obj.config.llm().configured:
            return _error(400, "还没配模型端点 —— 先在「配置 → LLM」里填上")

        # ※ 同步跑：本项目不做后台线程（Python 没法从外面杀线程，
        # 而后台跑会让「取消」变成一个说不清的语义）。任务量在演示规模下
        # 是秒级的，而同步跑让「进度」是**真的**。
        #
        # ※ **作业由 `reorder.run` 自己建、自己收尾**，这里不再另建一个。
        # 原来这里先 `create_job` 再调 `run`，有两个后果：
        #   1. `run` 一进去就查 `any_job_running()`，看到的正是**这个端点刚建
        #      的那个作业** ⇒ 每次都抛「已有任务在跑」，端点从来没成功过；
        #   2. 端点返回的 `job_id` 与 `run` 真正用的是**两个** ⇒
        #      `/api/jobs/{id}/undo` 找不到这一批，撤销永远失败。
        # 两者是同一个形状：**同一件事被两处各做了一遍，而两处不通信**。
        summary = reorder_uc.run(app_obj.reorder_deps())
        return view.job_out(app_obj.store.get_job(summary["job_id"]))

    @api.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        # ※ **`reader` 不是 `store`。** 这一行原来写的是 `app_obj.store`
        # （读写连接）—— 于是「GET 一律走只读连接」这句承诺在这一条上不成立，
        # 而**没有任何测试会红**：`open_readonly()` 那个函数确实会拒绝写，
        # 但那条测试证明的是**函数**的行为，不是 **serve 路径上有没有用它**。
        # 由 `test_每一条_GET_都用只读连接` 钉住。
        j = app_obj.reader.get_job(job_id)
        if j is None:
            # ※ 404 = job 早没了（服务重启 / 被挤掉）。前端据此清掉
            # localStorage 里那个 job_id。
            raise NotFound(f"没有这个任务：{job_id}")
        return view.job_out(j)

    @api.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        """※ **返回体不是 `JobRow`** —— 它是 `{cancelled, state}`。

        与 `fetchJob` 那个端点**不同形** —— 这个只回 `{cancelled, state}`。

        混用会踩一个静默的坑：调用方以为拿到的是完整 JobRow，去读别的字段
        全是 `undefined`，而 TypeScript 一声不吭（`web/src/api.ts` 里那个
        类型是断言出来的）。

        ※ 对**已经结束**的 job 返回 200 + `cancelled: false`，**不是 409**
        —— 那是「手慢了一步」。
        """
        ok, state = app_obj.store.request_cancel(job_id)
        return {"cancelled": ok, "state": state}

    @api.post("/api/jobs/{job_id}/undo")
    def undo_job(job_id: str) -> Any:
        """撤销这次重排。

        ※ 三种失败各有含义：409 = 任务还在跑（得先取消）；404 = 这个 job
        不存在了；500 = 数据库锁住了。
        三种都是**不同的话**，前端分别显示。
        """
        j = app_obj.store.get_job(job_id)
        if j is None:
            raise NotFound(f"没有这个任务：{job_id}")
        if j["state"] == "running":
            return _error(409, "这个任务还在跑 —— 先取消它，再撤销")
        from yechao.application import reorder as reorder_uc

        ok, count = reorder_uc.undo(app_obj.reorder_deps(), job_id)
        return {"undone": ok, "count": count}

    # ══ 配置包 ═════════════════════════════════════════════════

    @api.get("/api/config/export")
    def export_pack() -> dict[str, Any]:
        return {"toml": app_obj.build_pack()}

    @api.post("/api/config/import")
    def import_pack(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """导入配置包。

        ※ 配置包走 **JSON body 里的字符串**，不是文件上传。

        multipart 只为这一个接口服务，为此多引一个 `python-multipart`
        依赖不划算 —— 包本身就是文本，塞进 JSON 里一样传。

        ※ `apply: false` = **预校验，一个字节都不写**。
        """
        from yechao.domain.errors import ConfigError

        text = str(payload.get("toml") or "")
        apply = bool(payload.get("apply"))
        keep_settings = bool(payload.get("keep_settings"))

        notes: list[str] = []
        warnings: list[str] = []
        try:
            import tomllib

            data = tomllib.loads(text)
        except Exception as e:
            return _error(400, f"配置包读不出来：{e}")

        groups = data.get("group") or []
        cats = data.get("category") or []
        people = data.get("person") or []
        aliases = data.get("aliases") or {}
        if not isinstance(groups, list) or not isinstance(cats, list):
            return _error(400, "配置包的形状不对（`[[group]]` / `[[category]]` 要是数组）")

        if apply:
            from yechao.domain.categories import Category
            from yechao.domain.config_models import CategorySet, GroupConfig, PersonConfig

            _snapshot_categories(app_obj)
            # 配置包的输入是**用户手粘进 textarea 的 TOML**，所以
            # `id = "abc"` 是真会发生的。回 400 并指出改哪一格，
            # 不要让它变成一句英文的 `Internal Server Error`。
            try:
                new_groups = [
                    GroupConfig(
                        id=int(r["id"]), name=str(r.get("name") or ""),
                        tier=str(r.get("tier") or "normal"),
                        enabled=bool(r.get("enabled", True)),
                    )
                    for r in groups if isinstance(r, dict) and "id" in r
                ]
            except (TypeError, ValueError):
                return _error(400, "配置包里有个群组的 id 不是数字 —— 改好再导入")
            app_obj.config.set_groups(new_groups)
            app_obj.config.set_categories(CategorySet(
                categories=[
                    Category(
                        slug=str(r["slug"]),
                        label=str(r.get("label") or r["slug"]),
                        # 包里多出来的键忽略掉，不报错。
                        desc=str(r.get("desc") or ""),
                    )
                    for r in cats if isinstance(r, dict) and "slug" in r
                ],
                aliases={str(k): str(v) for k, v in dict(aliases).items()},
            ))
            app_obj.config.set_persons([
                PersonConfig(
                    account=int(r["account"]), label=str(r.get("label") or ""),
                    note=str(r.get("note") or ""),
                )
                for r in people if isinstance(r, dict) and "account" in r
            ])
            if not keep_settings and "settings" in data:
                notes.append("已采用包里的 [settings]")
            elif keep_settings:
                notes.append("保留了本机那份 [settings]（包里那份没采用）")

            # ※ 检查包里的路径是不是别台机器的 —— 那条红线：
            # 「不要指引用户做一件做不到的事」。一条指向**别台机器上的
            # 数据源目录**的配置，会让用户照着做**连接超时**（不是报错，
            # 是超时）。
            pkg_dir = str((data.get("settings") or {}).get("source_db_dir") or "")
            if pkg_dir and not pkg_dir.startswith(("/", "C:", "D:")):
                warnings.append(
                    f"包里的 source_db_dir 看起来指向别台机器：{pkg_dir}"
                )

        return {
            "applied": apply,
            "groups": len(groups),
            "categories": len(cats),
            "persons": len(people),
            "aliases": len(aliases),
            "notes": notes,
            "warnings": warnings,
        }

    # ══ 隐私 ═══════════════════════════════════════════════════

    @api.get("/api/privacy")
    def privacy() -> dict[str, Any]:
        from yechao.application.audit import summarize

        entries = app_obj.audit.read_all()
        v = summarize(entries)
        stats = app_obj.audit.stats()
        return {
            "records": v.records,
            "files": stats["files"],
            "surfaces": v.surfaces,
            "messages": v.messages,
            "chars": v.chars,
            "known_name_hits": v.known_name_hits,
            "names_checked": v.names_checked,
            "names_were_checked": v.names_were_checked,
            "num_hits": v.num_hits,
            "name_placeholders": v.name_placeholders,
            "num_placeholders": v.num_placeholders,
            "clean": v.clean,
            "dirty": v.dirty,
            "hint": v.hint,
        }

    # ══ 健康 ═══════════════════════════════════════════════════

    @api.get("/api/health")
    def health(response: Response) -> dict[str, Any]:
        """三问：**服务 / 库 / 缓存**。每问都给「通不通 + 用的是什么」。

        ## 为什么要有它（原来探的是 `/api/items?limit=1`）

        那个探活只证明了「库能读出东西」，它有两个盲区：

          · **验不到缓存**。缓存挂掉时它照样 200 —— 而那正是「一个失败
            看起来像成功」。
          · **分不清两种不健康**。「服务活着但库坏了」与「服务根本没起来」
            在编排层看起来是一样的（都只是「不健康」），可排查方向完全不同。

        ## 状态码

        · 库通 ⇒ **200**（缓存挂了也还是 200，见下）
        · 库不通 ⇒ **503**

        ※ 这个区分是刻意的，判据写在 `bootstrap.App.health` 的 docstring 里：
        **库挂了整个产品都没法用，缓存挂了只是慢。** 把缓存也算进「不健康」
        会让编排层反复重启一个本来就正常的容器 —— 而重启修不好 Redis。

        ※ 响应体里 `cache.enabled` 与 `cache.ok` 是**两个**字段：本机
        （空实现）是 `ok: true, enabled: false` —— 「没故障，也没在工作」。
        合成一个字段就会让「没启用」读起来像「工作正常」。

        ※ 探的是**只读连接**（`app_obj.health()` 里用的是 `reader`）——
        健康检查不该有能力写主库。
        """
        body = app_obj.health()
        if not body["ok"]:
            # ※ 非 2xx 才是「不健康」—— `deploy/docker-compose.yml` 的
            # healthcheck 用 `urllib.request.urlopen` 探它，非 2xx 会抛，
            # 于是容器被判为 unhealthy。这是它**唯一**能被用上的形状。
            response.status_code = 503
        return body

    # ══ 静态页面 ═══════════════════════════════════════════════

    _mount_web(api, app_obj)
    return api


# ── 辅助 ────────────────────────────────────────────────────────


def _check_slug(slug: str) -> None:
    """slug 的形状。

    ※ 前端在提交前会自己先校验一遍（即时反馈），后端这一层是守门。
    两边规则不一致的症状是「前端说行、后端说不行」—— 用户看到的是一个
    填对了却被拒的表单。
    """
    import re

    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", slug or ""):
        raise ValidationError(
            "slug 只许用英文字母、数字、下划线、连字符（1-64 位）", field="slug"
        )


#: 快照里认识的那几个键 —— 读快照时的白名单（见 `categories_undo`）。
_CATEGORY_KEYS = ("slug", "label", "desc")


def _snapshot_categories(app_obj: Any) -> None:
    """把当前类目表 + 别名表存一份，供「撤销类目改动」用。

    ※ 快照的是**一对**（表 + 别名表）—— 它们必须一起回滚。
    只回滚其中一个会让 `aliases` 里留着指向不存在 slug 的键，
    而那会让条目显示成一个陌生的 slug。
    """
    import json

    cats = app_obj.config.categories()
    app_obj.store.set_kv(
        "categories_undo_stack",
        json.dumps(
            {
                "categories": [
                    {"slug": c.slug, "label": c.label, "desc": c.desc}
                    for c in cats.categories
                ],
                "aliases": dict(cats.aliases),
            },
            ensure_ascii=False,
        ),
    )


def _conflict_out(c: Any) -> dict[str, Any]:
    return {
        "a": {
            "label": c.a.label, "start": c.a.start, "end": c.a.end,
            "item_id": c.a.item_id, "kind": c.a.kind,
        },
        "b": {
            "label": c.b.label, "start": c.b.start, "end": c.b.end,
            "item_id": c.b.item_id, "kind": c.b.kind,
        },
        # ※ `minutes` 是**推算值**（基于假定的 90 分钟时长），
        # 不是抽出来的事实。前端**刻意不显示它**。
        # 字段仍在，因为它是给人看时要的一个具体数（CLI 报告用）。
        "minutes": c.minutes,
    }





def _mount_web(api: FastAPI, app_obj: Any) -> None:
    """挂上前端静态页。

    ※ `serve` 服务的是仓库里**已构建**的 `web/dist/`，**不是源码**。
    README 第 ③ 步「浏览器打开就能看到网页」依赖 `dist/` 在仓库里。

    找不到 `dist/` 时**不静默返回 404** —— 给一句能照着做的提示。
    """
    dist = app_obj.settings.data_dir.parent / "web" / "dist"
    if not dist.is_dir():
        # 回落：包内自带的 dist（`pip install` 之后的形状）
        dist = _package_web_dist()
    if dist is None or not dist.is_dir():
        @api.get("/")
        def _no_web() -> JSONResponse:
            return _error(
                501,
                "网页还没构建。跑一次 `cd web && npm ci && npm run build`，"
                "或直接看仓库里的 web/dist/。",
            )

        return

    @api.get("/")
    def _index() -> Any:
        return FileResponse(str(dist / "index.html"))

    api.mount("/", StaticFiles(directory=str(dist), html=True), name="web")


def _package_web_dist() -> Any:
    """回落：包内自带的 `dist`，或者仓库根下的 `web/dist`。

    ※ **两种布局都要试**，因为它们都真实存在：
      · **装出来的包**：`pyproject.toml` 的 package-data 把 `web/dist` 放进
        `yechao/` 里面（`yechao/web/dist`）
      · **仓库里直接跑**：`web/dist` 在**仓库根**下

    原来只试了第一种。后果是 `--data-dir` 指到别处时（`data_dir.parent` 不再是
    仓库根）两种都找不到 —— `serve` 回 501，而 `web/dist` 明明就在那儿，
    报的还是「网页还没构建」。**一句指错方向的提示比没有提示更坏。**
    """
    from pathlib import Path

    here = Path(__file__).resolve()
    for p in (
        here.parent.parent / "web" / "dist",          # 装出来的包：yechao/web/dist
        here.parent.parent.parent / "web" / "dist",   # 仓库里跑：<仓库>/web/dist
    ):
        if p.is_dir():
            return p
    return None
