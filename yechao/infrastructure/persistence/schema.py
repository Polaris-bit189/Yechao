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

## ※ `CREATE TABLE IF NOT EXISTS` 不会改一张已经存在的表

所以表结构变了之后，旧库文件配上新代码，症状是查询报 `no such column`
—— **响的**，不是静默的。这是刻意的：一个静默的「自动迁移」失败起来更难查。

重来一次的命令是 `python -m yechao demo --fresh`（它会删掉旧库重建）。
不在这里写自动 `ALTER TABLE`：那种迁移失败一半时，库会处在两种结构之间，
而它照样能读、照样能写。

## 为什么 `item_sources` 是单独一张表

一个条目可以来自**多条**消息（跨群转发同一件事）。那是本项目最该保留、
最该合并的信号，所以「来源数」是一个有意义的量，不是冗余。
"""

from __future__ import annotations

#: ※ 这些表名是**对外契约**。改了就失效。
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
    -- 账号：发送者在源系统里的标识（群里的号码、工号、学号）。匿名消息为 NULL。
    actor_account INTEGER,
    -- ※ 默认值**不锚在某一个具体源的名字上**：空串 = 这条消息没标来源。
    -- 写死一个源名会让别的源进来的消息也自称来自它 ——
    -- 一条记错的来源在审计里看不出来，因为它格式完全正常。
    source      TEXT    NOT NULL DEFAULT '',
    -- ※ 规范化之后的文本才是**唯一真相**。原样存一份 raw 是为了
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
    -- ※ 见 messages.actor_account。匿名条目为 NULL，且**不该被人物筛选命中**。
    actor_account      INTEGER,
    place              TEXT,
    amount             TEXT,
    links              TEXT    NOT NULL DEFAULT '[]',
    start_ts           INTEGER,
    -- ※ 来源数是**算一次写进列**，不是每次查询用子查询现算：
    -- 列表页一次返回 200 条时，子查询就是 200 次查找。
    -- 代价是插入/删改来源后要刷新它 —— 而刷新只有一处（insert_items）。
    source_count       INTEGER NOT NULL DEFAULT 0,
    -- ※ 核验旗标：**必须与对应的 ts 字段一致**。
    -- `deadline_ts IS NULL` ⇒ `deadline_supported = 0`。
    -- 这个不变式由 `verify.py` 的 `gate()` 保证，由测试断言。
    deadline_supported INTEGER NOT NULL DEFAULT 0,
    start_supported    INTEGER NOT NULL DEFAULT 0,
    -- 不变量 ②：删除是软的 —— 打标记，不物理删（理由见 soft_delete）。
    deleted            INTEGER NOT NULL DEFAULT 0,
    deleted_at         INTEGER,
    -- ※ 用户手改过的类目**不被重排覆盖**。
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

-- ※ **append-only**。人工干预以事件追加记录，状态是事件的折叠结果。
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
    -- ※ 记的是**最后一批跑成功的批号**，不是已经成功了几批。
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

-- ※ **失败记账落库的地方。** 在此之前，「预筛丢弃了几条、哪一批失败了、
-- 哪几条没跑到」只活在 `FailureLedger` 那个**内存列表**里 ——
-- `refine` 一退出，证据就没了。而产品的承诺是「每一条消息都能回答为什么」，
-- 承诺不能只在进程活着的时候成立。
--
-- 两张表、两级：抬头（这一次跑）+ 账（一件事为什么没成）。
-- 「为什么是这个形状」写在 `domain/errors.py` 的「账的单位是什么」那一段，
-- 那里也是这两个值对象的定义处。
--
-- ※ **表里不存原文。** `reason` 是短码，上限 64 —— 由下面的 `CHECK` 让
-- **数据库自己拒绝**超长的 reason。这条与不变量 ① 同一个取向：靠自觉的
-- 约束迟早被静默绕开，而绕开时没有声音（往 reason 里塞正文正是这种绕开）。
--
-- ※ 旧库（本次改动之前建的）里**没有这两张表** —— 症状是
-- `no such table: runs`，**响的**，不是静默的。重建方式与其它结构变更
-- 一致：`python -m yechao demo --fresh`。
CREATE TABLE IF NOT EXISTS runs (
    -- ※ TEXT 而不是自增整数：id 由**用例层**生成（它要把 id 打进 CLI 的
    -- 输出里），而自增整数只能在写入之后才知道 —— 那条路要么多一次查询，
    -- 要么把抬头写两次。
    run_id     TEXT    PRIMARY KEY,
    kind       TEXT    NOT NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER NOT NULL DEFAULT 0,
    -- ok / failed，闭集见 `domain/errors.py` 的 RUN_OK / RUN_FAILED。
    status     TEXT    NOT NULL DEFAULT 'ok',
    seen       INTEGER NOT NULL DEFAULT 0,
    produced   INTEGER NOT NULL DEFAULT 0,
    -- ※ 这两列只有 refine 会填（覆盖率的两半）。别的命令填 0，
    -- 含义是「这一轮没有模型那一侧的事」—— 所以列名带 `model_` 前缀。
    model_eligible INTEGER NOT NULL DEFAULT 0,
    model_sent     INTEGER NOT NULL DEFAULT 0,
    -- 长度上限：见 `schema_mysql.py` 那张方言对照表（两个后端的上限必须一样）。
    CHECK (length(run_id) <= 64),
    CHECK (length(kind) <= 32),
    CHECK (length(status) <= 16)
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);

CREATE TABLE IF NOT EXISTS run_entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   TEXT    NOT NULL,
    kind     TEXT    NOT NULL,
    -- ※ 主体四列**都可空**：它们不是同一类主体（一条消息 / 一批 /
    -- 一条日报行）。哪一笔填哪几个由 `kind` 决定，见 `application/extract.py`
    -- 顶部那张表。可空而不是「填 0」：0 是一个合法的群号/条目号，
    -- 用它表示「不适用」会让「第 0 条」与「没有这一维」分不开。
    msg_id   INTEGER,
    group_id INTEGER,
    -- 第几批（**1-based**，与 `jobs.done` 同一种数法）。
    batch_no INTEGER,
    item_id  INTEGER,
    -- ※ 短码，**不是正文**。上限 64 由数据库自己拒绝 —— 见 `runs` 上面那段。
    reason   TEXT    NOT NULL DEFAULT '',
    CHECK (length(kind) <= 32),
    CHECK (length(reason) <= 64)
);

CREATE INDEX IF NOT EXISTS idx_run_entries_run ON run_entries(run_id);
CREATE INDEX IF NOT EXISTS idx_run_entries_msg ON run_entries(msg_id);

-- ※ 全文检索：`/api/items` 的 `q` 参数在 `len(q) >= 3` 时走这一条。
-- trigram 分词器让中文子串搜索可用（默认的 unicode61 不切中文词）。
--
-- ※ **谁写这张表**：`insert_items` 在**它自己的那个事务里**增量写
-- （条目 + 来源 + 记账 + 索引一起提交）。不在别处写 —— 尤其不在事务外，
-- 那会留下「条目在、索引不在」，症状是刚抽出来的条目搜不到，且不报错。
-- `init_db()` 会整表 `rebuild` 一次，那是给**存量库回填**用的，不是主路径。
--
-- ※ 它是 **external content** 表（`content='items'`）：无 `WHERE` 的
-- `SELECT COUNT(*) FROM items_fts` 会被改写成读**内容表**，于是恒等于
-- `items` 的行数 —— 判断索引齐不齐必须用 `MATCH`，不能用行数。
--
-- ※ 软删的条目**留在索引里**（没有触发器去删它），这不是漏洞：
-- `search` 只拿它当候选集，可见性由外层的 `i.deleted = 0` 决定。
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, detail, content='items', content_rowid='item_id', tokenize='trigram'
);
"""
