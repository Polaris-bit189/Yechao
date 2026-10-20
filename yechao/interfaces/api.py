"""HTTP 路由 —— 实现前端那一侧的**冻结契约**。

## 路由注册顺序是**承重的**

`/api/items/kinds` 必须排在 `/api/items/{id}` **前面**，否则前者会被更宽的
前缀吃掉（前端注释逐字警告过这件事）。同理：
  · `/api/digests/{id}` 要排在 `/api/digests` 前
  · `/api/categories/undo` 要排在 `/api/categories/{slug}` 前

FastAPI 按注册顺序匹配，所以下面每一条的**位置**都是有理由的。

## 为什么 GET 全部用只读连接

**不变量 ①**：网页侧不得写入主库。`app.reader` 是一个只读连接
（`mode=ro` + `PRAGMA query_only`），写操作由 SQLite 自己拒绝。

⚠️ 写端点（POST/PUT/PATCH/DELETE）走 `app.store`（读写连接）——
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

⚠️ **4xx 不是「服务不可达」** —— 前端的连通性状态机据此保持在线。
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
#: ⚠️ 前端知道这个数（`snapshot.ts` 逐字：「它就是 `api.py` 的 `MAX_LIMIT`」）
#: —— 它拉「最近 N 天」时用 200。改这个数要同步前端。
MAX_LIMIT = 200

DEFAULT_LIMIT = 50


def _error(code: int, detail: str) -> JSONResponse:
    """⚠️ 前端读 `body.detail` 当错误消息**原样显示**。

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
        # ⚠️ 501 而不是 500 —— 「还没做」与「做了但出错」对排查者意味着
        # 完全不同的方向。
        return _error(501, str(exc))

    @app.exception_handler(YechaoError)
    async def _ye(_r: Request, exc: YechaoError) -> JSONResponse:
        return _error(500, str(exc))


def create_app(app_obj: Any) -> FastAPI:
    """建一个 FastAPI 应用。

    `app_obj` 是 `bootstrap.App` —— ⚠️ 它在这里只被**读**，
    本层不做任何业务判断。
    """
    api = FastAPI(title="夜抄", docs_url="/api/docs", openapi_url="/api/openapi.json")
    install_error_handlers(api)

    # ── 展示用的查找表 ────────────────────────────────────────
    #
    # ⚠️ 每次请求现读配置而不是缓存：配置可以被网页改（新增类目），
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
    # ⚠️ **顺序**：`/api/items/kinds` 必须在 `/api/items/{item_id}` **之前**，
    # 否则它会被后者当成「item_id = 'kinds'」吃掉。

    @api.post("/api/items/kinds")
    def bulk_set_kind(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """人工多选批量改类目。

        ⚠️ 返回的 `actor` 是**撤销的键** —— 前端丢了它就再也撤不回这一批。
        """
        ids = payload.get("item_ids") or []
        kind = str(payload.get("kind") or "")
        if not isinstance(ids, list) or not ids:
            raise ValidationError("item_ids 不能为空", field="item_ids")
        if not kind:
            raise ValidationError("kind 不能为空", field="kind")
        actor = f"bulk:{app_obj.clock.now()}:{len(ids)}"
        actor, count = app_obj.store.bulk_set_kind(
            [int(i) for i in ids], kind, actor=actor, ts=app_obj.clock.now()
        )
        return {"actor": actor, "count": count}

    @api.post("/api/bulk-undo")
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

        ⚠️ `kind` / `person` 是**重复参数**（`kind=a&kind=b`），
        同一维度内**并集**、维度之间**交集**。
        空数组与不传同义（= 不筛选）。
        """
        if limit > MAX_LIMIT:
            # ⚠️ 不静默截断 —— 那会让调用方以为拿到了更多。
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

    @api.post("/api/items/{item_id}/kind")
    def set_item_kind(item_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        kind = str(payload.get("kind") or "")
        if not kind:
            raise ValidationError("kind 不能为空", field="kind")
        edit_id = app_obj.store.set_kind(
            item_id, kind, actor="web", ts=app_obj.clock.now()
        )
        return {"edit_id": edit_id}

    @api.delete("/api/items/{item_id}")
    def delete_item(item_id: int) -> dict[str, Any]:
        """⚠️ **软删** —— 界面上消失，库里的行仍在，且不会被下一轮抽取复活。"""
        edit_id = app_obj.store.soft_delete(
            item_id, actor="web", ts=app_obj.clock.now()
        )
        return {"edit_id": edit_id}

    @api.post("/api/undo")
    def undo(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """⚠️ `edit_id` 缺省时显式发 `null` —— 后端「撤最近一步」的判据是
        `None`，而「缺字段」与「显式 null」在 Python 侧是两件事。
        """
        raw = payload.get("edit_id", None)
        edit_id = None if raw is None else int(raw)
        ok = app_obj.store.undo(edit_id, ts=app_obj.clock.now())
        return {"undone": ok}

    # ══ 日历 / 紧急 ═══════════════════════════════════════════

    @api.get("/api/calendar")
    def calendar(since: str | None = None, until: str | None = None) -> dict[str, Any]:
        """日历。**不分页、默认全量**。

        ⚠️ 它**不走 `/api/items`** —— 后者的 limit 上限是 200，
        而日历要全量（前端注释逐字）。
        """
        from yechao.domain.conflicts import course_conflicts, find_conflicts, find_duplicates

        rows = app_obj.reader.items_with_deadline(since=since, until=until)
        dup = find_duplicates(rows)
        dup_ids = set(dup)
        # ⚠️ 算冲突时**排除被折叠的** —— 否则同一件事的两个副本会互相「撞车」
        # （那不存在的冲突会占满警示条）。
        visible = [i for i in rows if i.item_id not in dup_ids]
        conflicts = find_conflicts(visible)
        courses = app_obj.load_courses()
        cc = course_conflicts(visible, courses)
        return {
            "items": [_out(i) for i in rows],
            "total": len(rows),
            # ⚠️ 被折叠的**仍在 items 里** —— 前端据此把它们**收起来**，
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

        ⚠️ **刻意不复用 `/api/calendar`**：那个端点每次都要算重复几何 +
        读课表算课冲突，是「翻着看」的用途；本端点每 30 秒被调一次，
        必须最廉价。

        ⚠️ **不返回 `level` 字段** —— 分级在前端按**浏览器本地日**算。
        在这里加一个 `deadline_level` 会造出第三份阈值，
        而且它与浏览器算的时点不同，**跨午夜会漂移**。
        """
        # 窗口是 `[今天-1, 今天+2)` —— 过期项最长就是「昨天」。
        day = app_obj.clock.today()
        start = app_obj.clock.day_start_from_str(day) - 86400
        end = app_obj.clock.day_start_from_str(day) + 2 * 86400
        rows = app_obj.reader.items_with_deadline(
            since=since or str(start), until=until or str(end - 1)
        )
        return {"items": [_out(i) for i in rows], "total": len(rows)}

    # ══ 类目 ═══════════════════════════════════════════════════
    #
    # ⚠️ `/api/categories/undo` 要在 `/api/categories/{slug}` 之前。

    @api.post("/api/categories/undo")
    def categories_undo() -> dict[str, Any]:
        """撤销上一次类目改动。

        ⚠️ 空栈时返回 **409**（不是 400、不是 500）—— 前端据此说
        「没有可撤销的类目改动（撤销栈是空的）」。这个码是**契约**：
        前端有 `.catch()` 专门接它。
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
                categories=[Category(**c) for c in snapshot["categories"]],
                aliases=snapshot["aliases"],
            )
        )
        # ⚠️ 撤销是**一次性的**：用掉就清空，否则再点一次会「撤销撤销」。
        app_obj.store.set_kv(key, "")
        return {"undone": True}

    @api.get("/api/categories")
    def categories() -> dict[str, Any]:
        """⚠️ **一个端点两种形状**：它必须同时给出 `desc`（编辑器要）
        与 `aliases`（改名留痕要），一次往返满足两个调用方。
        """
        cats = app_obj.config.categories()
        counts = app_obj.reader.counts_by_kind()
        return {
            "categories": [
                {
                    "slug": c.slug,
                    "label": c.label,
                    "icon": c.icon,
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
                icon=str(payload.get("icon") or ""),
                desc=str(payload.get("desc") or ""),
            )
        )
        app_obj.config.set_categories(cats)
        return {"slug": slug}

    @api.put("/api/categories/{slug}")
    def update_category(slug: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """改名 / 改判据 / 改图标。

        ⚠️ 改名走的是**别名** —— 旧 slug 的条目不改 `item_id`（前端注释逐字）。

        ⚠️ `CategorySet` 是 **frozen 的值对象**（值对象纪律：不可变 =
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
            if payload.get("icon") is not None:
                changes["icon"] = str(payload["icon"])
            cats[i] = _replace(c, **changes)
            break

        from yechao.domain.config_models import CategorySet

        app_obj.config.set_categories(CategorySet(categories=cats, aliases=aliases))
        return {"slug": slug}

    @api.delete("/api/categories/{slug}", status_code=204, response_model=None)
    def remove_category(slug: str, migrate_to: str | None = None) -> Response:
        """删类目。`migrate_to` 是**查询参数**，不是 body 字段。

        ⚠️ 有条目指着它时**必须**给 `migrate_to` —— 否则那些条目会指向一个
        不存在的类目。
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
                f"「{target.label}」还有 {n} 条条目指着它，删之前必须选一个迁移目标",
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
        """⚠️ `count` 由**后端算好** —— 前端不要在拿到全部条目后自己数：
        那需要把整个库拉下来，而且计数会随筛选条件漂移。
        """
        counts = app_obj.reader.counts_by_person()
        return {
            "persons": [
                {
                    "uin": p.uin,
                    "label": p.label,
                    "note": p.note,
                    "count": counts.get(p.uin, 0),
                }
                for p in app_obj.config.persons()
            ]
        }

    @api.post("/api/persons", status_code=201)
    def add_person(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        from yechao.domain.config_models import PersonConfig

        try:
            uin = int(payload.get("uin"))
        except (TypeError, ValueError):
            raise ValidationError(
                "QQ 号必须是正整数（0 不是有效账号）", field="uin"
            ) from None
        label = str(payload.get("label") or "").strip()
        if not label:
            raise ValidationError(
                "给它起个名字 —— 群昵称会变，这个名字不会", field="label"
            )
        people = app_obj.config.persons()
        if any(p.uin == uin for p in people):
            raise ValidationError(f"这个 uin 已经在名单里了：{uin}", field="uin")
        people.append(
            PersonConfig(uin=uin, label=label, note=str(payload.get("note") or ""))
        )
        app_obj.config.set_persons(people)
        return {"uin": uin, "label": label}

    @api.delete("/api/persons/{uin}", status_code=204, response_model=None)
    def remove_person(uin: int) -> Response:
        people = [p for p in app_obj.config.persons() if p.uin != uin]
        app_obj.config.set_persons(people)
        return Response(status_code=204)

    # ══ 群 ═════════════════════════════════════════════════════

    @api.get("/api/groups")
    def groups() -> dict[str, Any]:
        """群清单。

        ⚠️ `available: false` 与「一个群都没有」是**两句不同的话** ——
        混起来说的后果是用户以为自己的 QQ 库是空的。
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
                    # ⚠️ `watched` 与 `known` 是两个字段：
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
        """⚠️ **名可以为空，服务端不许收紧。**

        `docs` 与前端注释都写明：快照里「名字读不出来」是**正常状态**，
        快照的 collect 刻意留 `""`。从前后端要求 name 非空的话，
        这个群**必然**拿回 400，在网页上**加不进来**。
        """
        from yechao.domain.config_models import GroupConfig

        try:
            gid = int(payload.get("id"))
        except (TypeError, ValueError):
            raise ValidationError("群号必须是正整数", field="id") from None
        groups_cfg = app_obj.config.groups()
        if any(g.id == gid for g in groups_cfg):
            # ⚠️ 409 的文案要指出去哪改 —— 「已经在配置里了」加上下一步。
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
            # ⚠️ `None` = **不发这个字段**（通用 OpenAI 端点最安全的形状）。
            "enable_thinking": llm.enable_thinking,
        }

    @api.put("/api/llm")
    def put_llm(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """⚠️ **响应是回填源**：三个字段全部按服务端归一化后的值回给前端。

        `trim()` 是写侧归一化，界面上必须看得见它的结果 ——
        否则下一次 GET 回来会「变样」。
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

    # ══ 重排 ═══════════════════════════════════════════════════

    @api.post("/api/reorder", status_code=202)
    def start_reorder() -> Any:
        """起重排任务。

        ⚠️ **409 = 已有 job 在跑**（唯一的 `JobBusy` 名额）。这不是「出错了」，
        而是「等一下」—— 后端那句 detail 已经把话说清楚了，前端原样显示。
        """
        from yechao.application import reorder as reorder_uc

        if app_obj.store.any_job_running():
            return _error(409, "已经有一个后台任务在跑了，等它结束再试")
        if not app_obj.model.config.configured:
            return _error(400, "还没配模型端点 —— 先在「配置 → LLM」里填上")

        job_id = app_obj.store.create_job("reorder", 0, ts=app_obj.clock.now())
        # ⚠️ 同步跑：本项目不做后台线程（Python 没法从外面杀线程，
        # 而后台跑会让「取消」变成一个说不清的语义）。任务量在演示规模下
        # 是秒级的，而同步跑让「进度」是**真的**。
        summary = reorder_uc.run(app_obj.reorder_deps())
        app_obj.store.finish_job(job_id, state="done", ts=app_obj.clock.now())
        return view.job_out(app_obj.store.get_job(job_id))

    @api.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        j = app_obj.store.get_job(job_id)
        if j is None:
            # ⚠️ 404 = job 早没了（服务重启 / 被挤掉）。前端据此清掉
            # localStorage 里那个 job_id。
            raise NotFound(f"没有这个任务：{job_id}")
        return view.job_out(j)

    @api.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        """⚠️ **返回体不是 `JobRow`** —— 它是 `{cancelled, state}`。

        与 `fetchJob` 那个端点**不同形**。混用会让调用方以为拿到的是完整
        job：`job.state` 会是 `undefined`，而在 TS 上**一个字都不报**。

        ⚠️ 对**已经结束**的 job 返回 200 + `cancelled: false`，**不是 409**
        —— 那是「手慢了一步」。
        """
        ok, state = app_obj.store.request_cancel(job_id)
        return {"cancelled": ok, "state": state}

    @api.post("/api/jobs/{job_id}/undo")
    def undo_job(job_id: str) -> Any:
        """撤销这次重排。

        ⚠️ 409 = 它还在跑（先取消再撤销）；404 = job 早没了；500 = 锁住。
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

    @api.get("/api/config/pack")
    def export_pack() -> dict[str, Any]:
        return {"toml": app_obj.build_pack()}

    @api.post("/api/config/pack")
    def import_pack(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """导入配置包。

        ⚠️ 走的是 **JSON body 里的字符串**，不是 multipart ——
        后端只为这件事引一个 `python-multipart` 不划算。

        ⚠️ `apply: false` = **预校验，一个字节都不写**。
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
            app_obj.config.set_groups([
                GroupConfig(
                    id=int(r["id"]), name=str(r.get("name") or ""),
                    tier=str(r.get("tier") or "normal"),
                    enabled=bool(r.get("enabled", True)),
                )
                for r in groups if isinstance(r, dict) and "id" in r
            ])
            app_obj.config.set_categories(CategorySet(
                categories=[
                    Category(
                        slug=str(r["slug"]),
                        label=str(r.get("label") or r["slug"]),
                        icon=str(r.get("icon") or ""),
                        desc=str(r.get("desc") or ""),
                    )
                    for r in cats if isinstance(r, dict) and "slug" in r
                ],
                aliases={str(k): str(v) for k, v in dict(aliases).items()},
            ))
            app_obj.config.set_persons([
                PersonConfig(
                    uin=int(r["uin"]), label=str(r.get("label") or ""),
                    note=str(r.get("note") or ""),
                )
                for r in people if isinstance(r, dict) and "uin" in r
            ])
            if not keep_settings and "settings" in data:
                notes.append("已采用包里的 [settings]")
            elif keep_settings:
                notes.append("保留了本机那份 [settings]（包里那份没采用）")

            # ⚠️ 检查包里的路径是不是别台机器的 —— 那条红线：
            # 「不要指引用户做一件做不到的事」。一条指向别处 QQ 库的配置
            # 会让用户照着做**连接超时**（不是报错，是超时）。
            pkg_dir = str((data.get("settings") or {}).get("qq_db_dir") or "")
            if pkg_dir and not pkg_dir.startswith(("/", "C:", "D:")):
                warnings.append(f"包里的 qq_db_dir 看起来指向别台机器：{pkg_dir}")

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

    # ══ 静态页面 ═══════════════════════════════════════════════

    _mount_web(api, app_obj)
    return api


# ── 辅助 ────────────────────────────────────────────────────────


def _check_slug(slug: str) -> None:
    """slug 的形状。

    ⚠️ 这条判据必须与前端 `uiState.ts` 的那一条**同形** ——
    前端做即时反馈、后端做守门。两边不一致的症状是「前端说行、后端说不行」。
    """
    import re

    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", slug or ""):
        raise ValidationError(
            "slug 只许用英文字母、数字、下划线、连字符（1-64 位）", field="slug"
        )


def _snapshot_categories(app_obj: Any) -> None:
    """把当前类目表 + 别名表存一份，供「撤销类目改动」用。

    ⚠️ 快照的是**一对**（表 + 别名表）—— 它们必须一起回滚。
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
                    {"slug": c.slug, "label": c.label, "icon": c.icon, "desc": c.desc}
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
        # ⚠️ `minutes` 是**推算值**（基于假定的 90 分钟时长），
        # 不是抽出来的事实。前端**刻意不显示它**。
        # 字段仍在，因为它是给人看时要的一个具体数（CLI 报告用）。
        "minutes": c.minutes,
    }





def _mount_web(api: FastAPI, app_obj: Any) -> None:
    """挂上前端静态页。

    ⚠️ `serve` 服务的是仓库里**已构建**的 `web/dist/`，**不是源码**。
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
    from pathlib import Path

    p = Path(__file__).resolve().parent.parent / "web" / "dist"
    return p if p.is_dir() else None
