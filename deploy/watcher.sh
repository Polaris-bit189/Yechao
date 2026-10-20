#!/bin/sh
# 夜抄 · 信息流自动更新（每 10 分钟一轮）
#
# ※ 这个脚本是**容器的主进程**（compose 里 watcher 服务的 entrypoint
# 指向它）。它死了容器就退出，`restart: unless-stopped` 把它拉起来，
# 而重启这件事在 `docker compose ps` 里看得见。
#
# 为什么是「独立服务 + 主进程」而不是「在 entrypoint.sh 里起一个后台循环」：
# 与 `deploy/scheduler.sh` 逐字相同的理由 —— 后台循环挂在应用容器里时，
# 循环死了而 `serve` 还活着，容器照样 healthy，「信息流不更新了」
# 没有任何地方报出来。
#
# ## 为什么循环在 Python 里，而这个脚本只是入口
#
# 与 `scheduler.sh` 的差别就在这里，理由值得写下来：
#
#   · `scheduler.sh` 的循环**必须**在 shell 里 —— 它要算「下一个 07:30」，
#     那是一段日历算术，而 shell 的 `date` 做得了（还有 `next_epoch` 那段
#     注释解释为什么每次都从时钟重算而不是 `+86400`）。
#   · 这个循环要做的两件事 shell 表达不了：**按轮拿写者锁**（拿不到就让路）、
#     **把每一轮的结果说清楚**（几条消息、几条条目、为什么没成）。
#     在 shell 里做只能再起一个 `python -c` 去问，于是同一个问题有了
#     第二个读法 —— 而两份读法必漂移。
#
# ⇒ 循环在 `yechao/interfaces/watch.py` 里（可被测试穷举），
#   这个脚本只负责：把环境变量翻成参数、把心跳文件的目录准备好、
#   然后把进程交出去（`exec` —— 于是信号能直达那个 Python 进程，
#   而不是停在这个 shell 上）。
#
# ## 退出码
#
# ※ 这个脚本**不解释**退出码：含义只有一处答案，在
# `yechao/interfaces/watch.py` 顶部的「退出码」那一节，而它就在那些
# 逐轮打印的输出里（`状态 …（退出码 N）`）。脚本里再判一次会让两处漂移。
#
# 它**也不因为一轮失败就退出**：重启修不好一个连不上的模型端点，
# 只会让日志里多几行一样的话。失败已经打进日志了（那才是要的）。
set -eu

DATA_DIR="${YECHAO_DATA_DIR:-/app/data}"
CONFIG_DIR="${YECHAO_CONFIG_DIR:-/app/config}"
INTERVAL="${YECHAO_WATCH_INTERVAL:-600}"

echo "夜抄 · 信息流自动更新"
echo "  数据目录 $DATA_DIR"
echo "  配置目录 $CONFIG_DIR"
echo "  间隔 ${INTERVAL}s（每轮：取数 → 抽取）"
echo

mkdir -p "$DATA_DIR"
# ※ 先摸一次心跳：容器起来到第一轮结束之间可能有好几秒，而
# healthcheck 的 `start_period` 一过就会开始看它 —— 那一段空窗里
# 文件不存在，探活会抛（`os.path.getmtime` 对不存在的路径报 OSError），
# 容器被判 unhealthy。而这个脚本**只在容器启动时**跑得到，
# 所以那一次是唯一能补的地方。
touch "$DATA_DIR/.watcher-heartbeat"

exec python -m yechao.interfaces.watch \
    --data-dir "$DATA_DIR" \
    --config-dir "$CONFIG_DIR" \
    --interval "$INTERVAL"
