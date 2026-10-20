# 夜抄 · 镜像
#
# ※ **不需要 Node** —— `web/dist/` 是**已构建**的产物且已入库
# （`.gitignore` 里专门放行了它）。这条是刻意的：评委 clone 下来不必装
# Node 就能看网页（README 的红线）。
FROM python:3.12-slim

# ※ 时区。产品语义里「本地日」是东八区（`domain/days.py`），
# 而容器默认是 UTC —— 不设的话，早上 00:00–08:00 之间生成的日报会落在
# **前一天**，而那个错在本地跑测试时**永远不会出现**。
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 先装依赖再拷源码 —— 让「只改了代码」时不必重装依赖。
#
# ※ `pymysql` / `redis` 是**云上那两个后端**的驱动，默认依赖里没有它们
# （理由见 `pyproject.toml` 的 `[project.optional-dependencies]`：
# 「clone 下来直接跑」不能要求先配环境）。
# 而**镜像**是部署产物 —— 它一定要能连 RDS 与 Redis，所以在这里显式装上。
# 不装的话，容器启动时会报「pip install pymysql」并拒绝启动（**不会**
# 悄悄退回 SQLite）—— 那是要的行为，但没必要让部署的人踩一次。
COPY pyproject.toml ./
RUN pip install --no-cache-dir fastapi uvicorn pymysql redis

COPY yechao ./yechao
COPY web/dist ./web/dist
COPY config.example ./config.example
COPY deploy/entrypoint.sh ./deploy/entrypoint.sh
# ⚠️ 定时那一路的入口。**必须拷进来** —— 它写好了而镜像里没有，症状是
# `docker compose up` 时 scheduler 容器报 `exec: not found`，而那是启动期
# 的一句英文报错，与「定时器没跑」这个真实病因隔着一层。
COPY deploy/scheduler.sh ./deploy/scheduler.sh
# ⚠️ 自动更新那一路的入口。**必须拷进来** —— 与 scheduler.sh 同一个理由：
# 写好了而镜像里没有，症状是 `docker compose up` 时 watcher 容器报
# `exec: not found`，而那是启动期的一句英文报错，与「信息流不更新了」
# 这个真实病因隔着一层。
COPY deploy/watcher.sh ./deploy/watcher.sh

RUN chmod +x ./deploy/entrypoint.sh ./deploy/scheduler.sh ./deploy/watcher.sh \
    && mkdir -p /app/data /app/config

# 入口脚本负责**首启初始化**：配置目录为空时把模板复制进去。
# ※ 这一步不是可有可无的 —— 没有它，容器起来后 `serve` 会因为
# 配置目录空而用默认值，而「用了默认值」与「用户配了默认值」看不出区别。
ENTRYPOINT ["./deploy/entrypoint.sh"]
