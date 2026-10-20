#!/bin/sh
# 夜抄 · 容器入口 —— **首启初始化 + 起服务**
#
# ※ 这个脚本**必须被 Dockerfile 的 ENTRYPOINT 引用**。
# `docs/补遗` §三 第二种失败是：「首启初始化脚本写好了，但**没有任何地方
# 引用它**。于是它从不执行，而依赖它的服务单元全部起不来。」
# `tests/contracts/test_deploy_contract.py` 有一条断言钉这件事：
# 它从 Dockerfile 里读 ENTRYPOINT，确认指向的是这个文件。
set -eu

DATA_DIR="${YECHAO_DATA_DIR:-/app/data}"
CONFIG_DIR="${YECHAO_CONFIG_DIR:-/app/config}"

echo "夜抄 · 启动"
echo "  数据目录 $DATA_DIR"
echo "  配置目录 $CONFIG_DIR"

# ※ **首启初始化**：配置目录为空时，把模板复制进去。
#
# 只在**空的**时候复制 —— 已经有配置的是**用户的**，覆盖它会丢掉他的编辑，
# 而容器「启动成功」会让这件事看起来没问题。
if [ -z "$(ls -A "$CONFIG_DIR" 2>/dev/null || true)" ]; then
    echo "  配置目录是空的 —— 从 config.example 初始化"
    cp -r /app/config.example/. "$CONFIG_DIR"/
else
    echo "  配置目录已有内容 —— 保留不动（那是你的配置）"
fi

mkdir -p "$DATA_DIR"

# ※ **模型端点从环境变量注入**，写进 settings.toml。
# 这是容器场景与本地场景的差异：本地让用户改文件，容器里由编排层注入。
if [ -n "${YECHAO_LLM_BASE_URL:-}" ]; then
    echo "  模型端点 $YECHAO_LLM_BASE_URL"
    # ※ 用 python 改 TOML 而不是 `sed` —— TOML 的结构对 sed 不友好，
    # 而写坏的配置会在**下一次启动**才报错（`load_toml` 会抛），
    # 那时根本看不出是这次改坏的。
python - "$CONFIG_DIR/settings.toml" <<'PY'
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8") if path.exists() else ""

# ※ **只重建 [llm] 段。**
#
# 原来是在**全文**里过滤以 base_url / model / enable_thinking 开头的行 ——
# 于是 [cloud] 段里的同名键也被删掉了。而配置目录是**挂载卷**，损失是永久的：
# 网页上云端配置空了、云端那一步永远跳过、日报永远退回机械行，
# 而**没有任何地方报错**。
#
# 同一个 bug 还顺手删掉了 [llm] 里的 enable_thinking，且没有从任何环境变量
# 补回 —— 少这个字段，某些端点的模型行为会变，而没人知道。
#
# ⇒ 按段走：进到 [llm] 就把整段吃掉重写，其它段原样搬。
src = text.splitlines()
out: list[str] = []
k = 0
replaced = False
while k < len(src):
    head = src[k].strip()
    if head == "[llm]":
        k += 1
        keep: list[str] = []
        while k < len(src):
            s = src[k].strip()
            if s.startswith("[") and s.endswith("]"):
                break
            if s and not (s.startswith("base_url") or s.startswith("model")):
                keep.append(src[k])          # enable_thinking 之类，原样留
            k += 1
        out.append("[llm]")
        out.append('base_url = "' + os.environ["YECHAO_LLM_BASE_URL"] + '"')
        out.append('model = "' + os.environ.get("YECHAO_LLM_MODEL", "") + '"')
        out.extend(keep)
        replaced = True
        continue
    out.append(src[k])
    k += 1

if not replaced:
    out += ["", "[llm]",
            'base_url = "' + os.environ["YECHAO_LLM_BASE_URL"] + '"',
            'model = "' + os.environ.get("YECHAO_LLM_MODEL", "") + '"']

path.write_text("\n".join(out).strip() + "\n", encoding="utf-8")
PY
fi

# ※ 没有配置就**不假装配好了**：`refine` 会自己拒绝并指出去哪配。
# 网页仍然可打开 —— 那是 `serve` 与 `refine` 的职责分界。
# 容器里必须绑 0.0.0.0，否则 Docker 的端口发布够不到（宿主打不开，
# 而容器内探活仍是绿的）。
exec python -m yechao serve --host 0.0.0.0 --port 8000 --data-dir "$DATA_DIR" --config-dir "$CONFIG_DIR"
