"""建表（MySQL 方言）—— 表名仍是**对外契约**，写法是方言的。

## 与 `schema.py`（SQLite）逐条对照，差异只有下面这些

不是「把 SQL 抄一遍换个引擎」—— 抄一遍会漏掉下面每一条，而它们**都不会
在本地跑测试时暴露**（本地跑的是 SQLite）。

| 项 | SQLite | MySQL | 为什么必须改 |
|---|---|---|---|
| 自增 | `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGINT NOT NULL AUTO_INCREMENT` + `PRIMARY KEY` | 语法不同；且 SQLite 的 `INTEGER` 是 64 位而 MySQL 的 `INT` 是 32 位 |
| 索引 | `CREATE INDEX IF NOT EXISTS` | 表内 `KEY` 子句 | MySQL **不支持** `CREATE INDEX IF NOT EXISTS`，重复执行会报错 |
| 唯一列 | 列上 `UNIQUE` | 具名 `UNIQUE KEY` | MySQL 要求索引名在**表内**唯一，匿名唯一键会由服务端生成名字，重复建表时难以对账 |
| 布尔 | `INTEGER` | `TINYINT` | MySQL 的 `BOOLEAN` 只是 `TINYINT(1)` 的别名，写 `TINYINT` 更直白 |
| 默认值 | `TEXT ... DEFAULT '[]'` | 不给默认值 | MySQL 的 TEXT/BLOB 列**不能有**默认值（8.0.13 之前一律不行）⇒ 插入路径必须显式写这一列 |
| 字符集 | 默认 UTF-8 | 每张表 `CHARSET=utf8mb4` | 不写会跟服务端/库的默认走，而默认可能是 latin1 —— 中文会**报错或乱码** |
| 保留字 | `key` 不是保留字 | `key` / `groups` 是保留字 | 不加反引号就是语法错误 |
| 长度上限 | `TEXT` 无上限 + `CHECK (length(x) <= n)` | `VARCHAR(n)` | SQLite 的 `TEXT` 没有上限，所以「不许塞正文」这条只能靠 `CHECK`；MySQL 靠列宽。**上限值两边一样** |

## ※ 一律给标识符加反引号

MySQL 的保留字表比 SQLite 长得多，而且**跨版本增长**（`groups`、`rank`、
`system` 都是 8.0 才进来的）。逐个判断「这个是不是保留字」漏一个的症状是
语法错误 —— 那是**响的**，不会静默，但会让人在部署当天才发现。
所以这里一律加反引号，不逐个判断。

## ※ 没有的两张表

· **`items_fts`**：SQLite 的 FTS5 全文索引。MySQL 没有对应物。
  MySQL 那边 `search(q=...)` 走 `LIKE` —— 这是**有代价的取舍**，
  见 `mysql_store.py::search` 的注释。
· **`groups`**：它在 SQLite 那份 schema 里是历史遗留 —— 仓储层**从不**
  读写它（群配置走 TOML，见 `TomlConfigStore`）。所以不在这里建一张
  没人用的表：一张空表会让人以为「群里那些数据是从这儿来的」。

## 长度限制是有意的

SQLite 的 `TEXT` 没有长度上限，MySQL 的 `VARCHAR(n)` 有。这里按字段的
**真实语义**给长度：`YYYY-MM-DD` 给 10、job id 给 32、自由文本给 `TEXT`。
超长时 MySQL 在严格模式下**报错**，不静默截断 —— 这正是要的行为。
"""

from __future__ import annotations

#: ※ 表名仍是**对外契约**（`items` / `messages` / `item_sources` /
#: `digests` / `item_digests`），改了就失效。写法是方言的，名字不是。
SCHEMA_MYSQL = """
CREATE TABLE IF NOT EXISTS `messages` (
    `msg_id`        BIGINT       NOT NULL,
    `group_id`      BIGINT       NOT NULL,
    `ts`            BIGINT       NOT NULL,
    `sender`        TEXT         NOT NULL,
    `content`       MEDIUMTEXT   NOT NULL,
    `actor_account` BIGINT       NULL,
    `source`        VARCHAR(64)  NOT NULL DEFAULT '',
    `raw_content`   MEDIUMTEXT   NULL,
    PRIMARY KEY (`msg_id`),
    KEY `idx_messages_ts` (`ts`),
    KEY `idx_messages_group` (`group_id`, `ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `items` (
    `item_id`            BIGINT      NOT NULL AUTO_INCREMENT,
    `kind`               VARCHAR(64) NOT NULL,
    `title`              TEXT        NOT NULL,
    `detail`             MEDIUMTEXT  NULL,
    `event_ts`           BIGINT      NOT NULL,
    `deadline_ts`        BIGINT      NULL,
    `group_id`           BIGINT      NOT NULL,
    `actor`              TEXT        NULL,
    `actor_account`      BIGINT      NULL,
    `place`              TEXT        NULL,
    `amount`             TEXT        NULL,
    -- ※ 没有 DEFAULT：MySQL 的 TEXT 列不能有默认值。插入路径
    -- （`insert_items`）**总是**显式写这一列，所以默认值本来也用不上。
    `links`              TEXT        NOT NULL,
    `start_ts`           BIGINT      NULL,
    `source_count`       INT         NOT NULL DEFAULT 0,
    `deadline_supported` TINYINT     NOT NULL DEFAULT 0,
    `start_supported`    TINYINT     NOT NULL DEFAULT 0,
    `deleted`            TINYINT     NOT NULL DEFAULT 0,
    `deleted_at`         BIGINT      NULL,
    `kind_locked`        TINYINT     NOT NULL DEFAULT 0,
    `created_at`         BIGINT      NOT NULL DEFAULT 0,
    PRIMARY KEY (`item_id`),
    KEY `idx_items_event` (`event_ts`),
    KEY `idx_items_deadline` (`deadline_ts`),
    KEY `idx_items_kind` (`kind`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `item_sources` (
    `item_id` BIGINT     NOT NULL,
    `msg_id`  BIGINT     NOT NULL,
    `quote`   MEDIUMTEXT NOT NULL,
    PRIMARY KEY (`item_id`, `msg_id`),
    KEY `idx_sources_msg` (`msg_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `digests` (
    `digest_id`   BIGINT      NOT NULL AUTO_INCREMENT,
    `day`         VARCHAR(10) NOT NULL,
    `window_from` BIGINT      NOT NULL,
    `window_to`   BIGINT      NOT NULL,
    `created_at`  BIGINT      NOT NULL,
    `body_md`     MEDIUMTEXT  NOT NULL,
    `preview`     TEXT        NULL,
    `item_count`  INT         NOT NULL DEFAULT 0,
    PRIMARY KEY (`digest_id`),
    UNIQUE KEY `uq_digests_day` (`day`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `item_digests` (
    `digest_id` BIGINT NOT NULL,
    `item_id`   BIGINT NOT NULL,
    PRIMARY KEY (`digest_id`, `item_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `edits` (
    `edit_id` BIGINT       NOT NULL AUTO_INCREMENT,
    `kind`    VARCHAR(64)  NOT NULL,
    `item_id` BIGINT       NULL,
    `payload` MEDIUMTEXT   NOT NULL,
    `actor`   TEXT         NOT NULL,
    `ts`      BIGINT       NOT NULL,
    `undone`  TINYINT      NOT NULL DEFAULT 0,
    `undo_of` BIGINT       NULL,
    PRIMARY KEY (`edit_id`),
    KEY `idx_edits_ts` (`ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `jobs` (
    `job_id`           VARCHAR(32)   NOT NULL,
    `kind`             VARCHAR(64)   NOT NULL,
    `state`            VARCHAR(16)   NOT NULL DEFAULT 'running',
    `total`            INT           NOT NULL DEFAULT 0,
    -- ※ `done` 记的是**最后一批跑成功的批号**，不是已经成功了几批。
    `done`             INT           NOT NULL DEFAULT 0,
    -- ※ 长度 2000：它装得下 `jobs._message()` 拼出来的那几句（最多 3 条
    -- 原因 + 计数），装不下整段 traceback。SQLite 那边 TEXT 无上限，这里
    -- 超长会**报错** —— 报错比截断好，但知道这个上限存在很重要。
    `message`          VARCHAR(2000) NOT NULL DEFAULT '',
    `error`            VARCHAR(2000) NOT NULL DEFAULT '',
    `actor`            VARCHAR(255)  NOT NULL DEFAULT '',
    `started_at`       BIGINT        NOT NULL,
    `finished_at`      BIGINT        NULL,
    `cancel_requested` TINYINT       NOT NULL DEFAULT 0,
    PRIMARY KEY (`job_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ※ 失败记账（见 `schema.py` 里那两张表的说明：为什么是两级、为什么不存原文）。
--
-- ※ 与 SQLite 那份的差别只有**长度上限的表达方式**：
--   · SQLite 是 `TEXT` + `CHECK (length(x) <= n)`
--   · 这里是 `VARCHAR(n)`
-- 上限**一样**（reason 64 / kind 32 / status 16），因为「两个后端对同一种
-- 输入必须给出同一种行为」—— 一边拒绝、一边接受，那个差异只在换后端时出现。
-- 严格模式下超长会**报错**（不是静默截断），这正是要的。
CREATE TABLE IF NOT EXISTS `runs` (
    `run_id`         VARCHAR(64) NOT NULL,
    `kind`           VARCHAR(32) NOT NULL,
    `started_at`     BIGINT      NOT NULL,
    `finished_at`    BIGINT      NOT NULL DEFAULT 0,
    `status`         VARCHAR(16) NOT NULL DEFAULT 'ok',
    `seen`           INT         NOT NULL DEFAULT 0,
    `produced`       INT         NOT NULL DEFAULT 0,
    -- ※ 只有 refine 会填这两列（覆盖率的两半）。别的命令填 0 —— 见
    -- `schema.py` 里那段：列名带 `model_` 前缀，就是为了让那个 0 读作
    -- 「这一轮没有模型那一侧的事」，而不是「一条都不够格」。
    `model_eligible` INT         NOT NULL DEFAULT 0,
    `model_sent`     INT         NOT NULL DEFAULT 0,
    PRIMARY KEY (`run_id`),
    KEY `idx_runs_started` (`started_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `run_entries` (
    `entry_id` BIGINT      NOT NULL AUTO_INCREMENT,
    `run_id`   VARCHAR(64) NOT NULL,
    `kind`     VARCHAR(32) NOT NULL,
    -- ※ 主体四列都可空：它们不是同一类主体（一条消息 / 一批 / 一条日报行）。
    -- 用 NULL 而不是 0 表示「不适用」—— 0 是合法的群号/条目号。
    `msg_id`   BIGINT      NULL,
    `group_id` BIGINT      NULL,
    `batch_no` INT         NULL,
    `item_id`  BIGINT      NULL,
    `reason`   VARCHAR(64) NOT NULL DEFAULT '',
    PRIMARY KEY (`entry_id`),
    KEY `idx_run_entries_run` (`run_id`),
    KEY `idx_run_entries_msg` (`msg_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ※ `key` 是 MySQL 保留字 —— 不加反引号就是语法错误。
-- ※ VARCHAR(191)：utf8mb4 下一个字符最多 4 字节，191×4 = 764，
-- 落在**所有** InnoDB 行格式的索引前缀上限之内（老格式是 767 字节）。
-- 给 255 在老格式上会报「索引键太长」。
CREATE TABLE IF NOT EXISTS `kvs` (
    `key`   VARCHAR(191) NOT NULL,
    `value` MEDIUMTEXT   NOT NULL,
    PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""
