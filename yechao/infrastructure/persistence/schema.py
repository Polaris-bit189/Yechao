"""建表 —— 表名是**对外契约**，结构是自己设计的。

## 锁住的表名

`items` / `messages` / `item_sources` / `digests` / `item_digests`

## 两条不变量在 schema 上的落点

**不变量 ①「网页侧不得写入主库」。**
落点不在 schema，在**连接**：只读连接用 `file:...?mode=ro` 打开，
由 SQLite 自己拒绝 `INSERT`。见 `sqlite_store.py::open_readonly`。

为什么不靠代码自觉：靠自觉的约束迟早被静默绕开，而且绕开时没有声音。
让数据库拒绝，写错了会**立刻报错**。

**不变量 ②「删除是软的」。**
`items.deleted` 是一个**标记**，不是 `DELETE FROM`。而且 `edits` 表是
**append-only** 的事件流 —— 状态是事件的折叠结果。

## 为什么 `item_sources` 是单独一张表

一个条目可以来自**多条**消息（跨群转发同一件事）。那是本项目最该保留、
最该合并的信号，所以「来源数」是一个有意义的量，不是冗余。
"""

from __future__ import annotations

#: ⚠️ 这些表名是**对外契约**。改了就失效。
SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id      INTEGER PRIMARY KEY,
    name    TEXT    NOT NULL DEFAULT '',
    tier    TEXT    NOT NULL DEFAULT 'normal',
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS messages (
    msg_id      INTEGER PRIMARY KEY,
    group_id    INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    sender      TEXT    NOT NULL DEFAULT '',
    content     TEXT    NOT NULL,
    actor_uin   INTEGER,
    source      TEXT    NOT NULL DEFAULT 'qq',
    -- ⚠️ 规范化之后的文本才是**唯一真相**。原样存一份 raw 是为了
    -- 出问题时能比对，但下游一律读 content。
    raw_content TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_group ON messages(group_id, ts);

CREATE TABLE IF NOT EXISTS items (
    item_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind               TEXT    NOT NULL,
    title              TEXT    NOT NULL,
    detail             TEXT,
    event_ts           INTEGER NOT NULL,
    deadline_ts        INTEGER,
    group_id           INTEGER NOT NULL,
    actor              TEXT,
    actor_uin          INTEGER,
    place              TEXT,
    amount             TEXT,
    links              TEXT    NOT NULL DEFAULT '[]',
    start_ts           INTEGER,
    -- ⚠️ 来源数是**算一次写进列**，不是每次查询用子查询现算：
    -- 列表页一次返回 200 条时，子查询就是 200 次查找。
    -- 代价是插入/删改来源后要刷新它 —— 而刷新只有一处（insert_items）。
    source_count       INTEGER NOT NULL DEFAULT 0,
    -- ⚠️ 核验旗标：**必须与对应的 ts 字段一致**。
    -- `deadline_ts IS NULL` ⇒ `deadline_supported = 0`。
    -- 这个不变式由 `verify.py` 的 `gate()` 保证，由测试断言。
    deadline_supported INTEGER NOT NULL DEFAULT 0,
    start_supported    INTEGER NOT NULL DEFAULT 0,
    -- 不变量 ②：删除是**软的**。界面上消失，行仍在。
    deleted            INTEGER NOT NULL DEFAULT 0,
    deleted_at         INTEGER,
    -- ⚠️ 用户手改过的类目**不被重排覆盖**。
    kind_locked        INTEGER NOT NULL DEFAULT 0,
    created_at         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_items_event ON items(event_ts DESC);
CREATE INDEX IF NOT EXISTS idx_items_deadline ON items(deadline_ts);
CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind);

CREATE TABLE IF NOT EXISTS item_sources (
    item_id INTEGER NOT NULL,
    msg_id  INTEGER NOT NULL,
    quote   TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (item_id, msg_id)
);

CREATE INDEX IF NOT EXISTS idx_sources_msg ON item_sources(msg_id);

CREATE TABLE IF NOT EXISTS digests (
    digest_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    day        TEXT    NOT NULL UNIQUE,
    window_from INTEGER NOT NULL,
    window_to   INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    body_md     TEXT    NOT NULL,
    preview     TEXT,
    item_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS item_digests (
    digest_id INTEGER NOT NULL,
    item_id   INTEGER NOT NULL,
    PRIMARY KEY (digest_id, item_id)
);

-- ⚠️ **append-only**。人工干预以事件追加记录，状态是事件的折叠结果。
-- 为什么不是直接改字段：**撤销需要「原来是什么」**。直接改掉之后，
-- 撤销就无从下手。
CREATE TABLE IF NOT EXISTS edits (
    edit_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT    NOT NULL,
    item_id   INTEGER,
    payload   TEXT    NOT NULL,
    actor     TEXT    NOT NULL DEFAULT '',
    ts        INTEGER NOT NULL,
    undone    INTEGER NOT NULL DEFAULT 0,
    undo_of   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_edits_ts ON edits(ts DESC);

CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'running',
    total       INTEGER NOT NULL DEFAULT 0,
    -- ⚠️ 「**最后一条成功批次**的序号」，不是「已完成批次数」。
    done        INTEGER NOT NULL DEFAULT 0,
    message     TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    actor       TEXT NOT NULL DEFAULT '',
    started_at  INTEGER NOT NULL,
    finished_at INTEGER,
    cancel_requested INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kvs (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ⚠️ 全文检索：`/api/items` 的 `q` 参数在 `len(q) >= 3` 时走这一条。
-- trigram 分词器让中文子串搜索可用（默认的 unicode61 不切中文词）。
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, detail, content='items', content_rowid='item_id', tokenize='trigram'
);
"""
