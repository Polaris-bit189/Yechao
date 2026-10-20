#!/bin/sh
# 夜抄 · 定时跑「今天的日报」
#
# ※ 这个脚本是**容器的主进程**（compose 里 scheduler 服务的 entrypoint
# 指向它）。它死了容器就退出，`restart: unless-stopped` 把它拉起来，
# 而重启这件事在 `docker compose ps` 里看得见。
#
# 为什么是「独立服务 + 主进程」而不是「在 entrypoint.sh 里起一个后台循环」：
# 见 `deploy/docker-compose.yml` 里 scheduler 那一节的注释。一句话 ——
# 后台循环挂在应用容器里时，循环死了而 `serve` 还活着，容器照样 healthy，
# 「今天没有日报」没有任何地方报出来。
#
# 这个脚本做三件事：
#   ① 算出下一次该跑的时刻，睡过去；
#   ② 到点了跑 `python -m yechao.interfaces.daily`，把退出码打进日志；
#   ③ 每 30 秒摸一次心跳文件 —— 循环卡住时 healthcheck 看得出来。
#
# ※ 一次跑失败**不让脚本退出**：重启修不好一个连不上的模型端点，只会让
# 日志里多几行一样的话。失败本身已经打进日志了（那才是要的）。
set -eu

DATA_DIR="${YECHAO_DATA_DIR:-/app/data}"
CONFIG_DIR="${YECHAO_CONFIG_DIR:-/app/config}"
RUN_AT="${YECHAO_DAILY_AT:-07:30}"

#: 心跳文件。healthcheck 读它的 mtime —— 见 compose 里 scheduler 的探活。
HEARTBEAT="$DATA_DIR/.scheduler-heartbeat"

#: 循环醒来的间隔（秒）。它决定「最多晚多久跑」与「心跳多久更新一次」。
#: 日报是一天一次的东西，晚半分钟没有意义；而 30 秒的粒度让心跳足够新。
TICK=30


# 今天 `HH:MM` 的 Unix 秒。
#
# ※ 时刻的算术交给 python 而不是 `date -d @…`：那是 GNU 的扩展，换一个
# 基础镜像（alpine / busybox）就没有它 —— 而那种失败**只在部署那天**出现，
# 本地跑测试永远撞不到。
#
# ※ 配错了**当场报错**，不取整、不回落：把 `25:00` 悄悄算成「明天 01:00」
# 会让「我配错了」看起来像「配好了」。
epoch_at() {
    python - "$1" <<'PY'
import sys
import time

raw = sys.argv[1]
parts = raw.split(":")
if (
    len(parts) != 2
    or len(parts[0]) != 2
    or not all(p.isdigit() for p in parts)
):
    raise SystemExit(
        f"YECHAO_DAILY_AT 要写成 HH:MM（24 小时制），现在是 {raw!r}"
    )
hh, mm = (int(p) for p in parts)
if not (0 <= hh <= 23 and 0 <= mm <= 59):
    raise SystemExit(
        f"YECHAO_DAILY_AT 不是合法时刻：{raw!r}（小时 0-23，分钟 0-59）"
    )
lt = time.localtime()
print(int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))))
PY
}

# 下一次该跑的时刻：今天 `HH:MM`；已经过了就是明天同一时刻。
#
# ※ 每次都从**时钟**重算，不是 `next=$((next + 86400))`：
# 机器挂起过之后 `sleep` 会照常走完，而墙上时钟跳了一大段 ——
# 累加会让这个循环连着跑好几次，去「补上」那些早就过去的日子。
next_epoch() {
    at="$(epoch_at "$RUN_AT")"
    if [ "$(date +%s)" -ge "$at" ]; then
        at=$((at + 86400))
    fi
    echo "$at"
}

stamp() {
    date '+%F %T'
}

# 跑一次，把**退出码**打进日志。
#
# ※ 这里**不解释**退出码的含义（只列出来给人看）：含义只有一处答案，
# 在 `yechao/interfaces/daily.py` 顶部的「退出码」那一节 —— 而它就在上面
# 那几行输出里（`状态 …（退出码 N）`）。脚本里再判一次会让两处漂移，
# 而漂移的症状是「日志说没跑成，日报其实已经生成了」。
run_once() {
    echo "  [$(stamp)] 开始跑今天的日报"
    touch "$HEARTBEAT"
    set +e
    python -m yechao.interfaces.daily --data-dir "$DATA_DIR" --config-dir "$CONFIG_DIR"
    code=$?
    set -e
    echo "  [$(stamp)] 今天这一次结束：退出码 $code"
    echo "      （0 = 跑成了；3 = 跑成了但模型没跑，日报是机械行兜底的；1 = 没跑成）"
    echo "  ----------------------------------------------------------------"
}

mkdir -p "$DATA_DIR"
touch "$HEARTBEAT"

echo "夜抄 · 定时日报"
echo "  数据目录 $DATA_DIR"
echo "  配置目录 $CONFIG_DIR"
echo "  每天 $RUN_AT 跑一次（容器本地时区，见 Dockerfile 的 TZ）"

today_at="$(epoch_at "$RUN_AT")"
now="$(date +%s)"
next="$today_at"

# ※ **补跑**：容器可能是在「今天的时刻之后」才起来的（部署、重启、扩缩容）。
# 那时不能等到明天 —— 今天的日报会**静默地**永远不出现。
#
# 判据：今天的时刻已经过了 ⇒ 立刻跑一次。跑重复了不要紧：同一天跑两次是
# **覆盖**（理由在 `yechao/interfaces/daily.py::run_today`），不是插第二篇。
if [ "$now" -ge "$today_at" ]; then
    echo "  今天（$(date +%F)）的 $RUN_AT 已经过了 —— 先补跑一次"
    run_once
    next="$(next_epoch)"
fi

echo "  下一次 $(python -c 'import sys,time;print(time.strftime("%F %T", time.localtime(int(sys.argv[1]))))' "$next")"
echo

while true; do
    # ※ 心跳**每一轮都摸**：它是「这个循环还活着」的唯一证据。
    # 没有它，一个卡死在 `sleep` 里的调度器与一个正常工作的调度器
    # 在 `docker compose ps` 里长得一模一样（都是 Up）。
    touch "$HEARTBEAT"
    if [ "$(date +%s)" -ge "$next" ]; then
        run_once
        next="$(next_epoch)"
        echo "  下一次 $(python -c 'import sys,time;print(time.strftime("%F %T", time.localtime(int(sys.argv[1]))))' "$next")"
    fi
    sleep "$TICK"
done
