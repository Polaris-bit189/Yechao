#!/bin/sh
# 夜抄 · 容器入口 —— **首启初始化 + 起服务**
#
# ⚠️ 这个脚本**必须被 Dockerfile 的 ENTRYPOINT 引用**。
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

# ⚠️ **首启初始化**：配置目录为空时，把模板复制进去。
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

# ⚠️ **模型端点从环境变量注入**，写进 settings.toml。
# 这是容器场景与本地场景的差异：本地让用户改文件，容器里由编排层注入。
if [ -n "${YECHAO_LLM_BASE_URL:-}" ]; then
    echo "  模型端点 $YECHAO_LLM_BASE_URL"
    # ⚠️ 用 python 改 TOML 而不是 `sed` —— TOML 的结构对 sed 不友好，
    # 而写坏的配置会在**下一次启动**才报错（`load_toml` 会抛），
    # 那时根本看不出是这次改坏的。
    python - "$CONFIG_DIR/settings.toml" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8") if path.exists() else ""
lines = [ln for ln in text.splitlines() if not ln.strip().startswith(
    ("base_url", "model", "enable_thinking"))]
# 去掉可能存在的空 [llm] 段头，重建一份
out = [ln for ln in lines if ln.strip() != "[llm]"]
out += ["", "[llm]",
        f'base_url = "{__import__("os").environ["YECHAO_LLM_BASE_URL"]}"',
        f'model = "{__import__("os").environ.get("YECHAO_LLM_MODEL", "")}"']
path.write_text("\n".join(out).strip() + "\n", encoding="utf-8")
PY
fi

# ⚠️ 没有配置就**不假装配好了**：`refine` 会自己拒绝并指出去哪配。
# 网页仍然可打开 —— 那是 `serve` 与 `refine` 的职责分界。
exec python -m yechao serve --port 8000 --data-dir "$DATA_DIR" --config-dir "$CONFIG_DIR"
