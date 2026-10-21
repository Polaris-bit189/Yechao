# AGENTS.md — 夜抄项目规则（CodeArts）

这份文件是 CodeArts 代码智能体在本仓库工作时**每次会话都自动加载**的项目规则。

## 先读什么

1. `README.md` —— 产品是什么、三步复现、数据来源。
2. `docs/交接.md` —— 当前进度、已知问题、不能忘的规矩。
3. `docs/移动端与PWA实施计划.md` —— 当前任务的执行规格（T1–T9）。

本仓库的全局说明在仓库之外（`D:\github\CLAUDE.md`），CodeArts 看不到；以本文件和上面三份为准。

## 项目结构

四层，依赖方向**只有一个：外 → 内**：

```
interfaces/      HTTP 路由、CLI
application/     用例编排、端口 Protocol
infrastructure/  SQLite / MySQL、模型客户端、配置、数据源
domain/          纯逻辑，零 I/O
bootstrap.py     组合根
```

- 内层不得 import 外层；`domain/` 不碰 I/O。
- `application/ports.py` 的返回类型写 `Any` 是刻意的：类型精度让位于依赖方向。
- 分层护栏在 `tests/contracts/test_layering.py`，白名单式，带空转自检。

## 当前任务的范围

- 只改 `web/`；PWA 服务端部分只允许改 `yechao/interfaces/api.py` 的 `_mount_web`（T8）。
- 不改 `domain/`、`application/`、持久化、数据源、业务路由。
- **不手工编辑 `web/dist/`**；它只由 `npm run build` 生成，T9 统一提交。
- 一个任务一次提交；测试变红立即停下报告，不带红往下走。

## 硬规则

1. **失败要响。** 不静默使用默认配置、不静默跳过、不静默返回空/假数据。占位必须显式报错（501）。
2. **导航只有一套 DOM。** 桌面侧栏与手机顶栏是同一个 `.rail` 的 CSS 断点形态，禁止两套 DOM。
3. **全站零 emoji。** 图标是手写 SVG（`web/src/ui/icons.tsx`），未知类目回落 `IcTag`。
4. **主题 token。** 六套主题（含彩蛋 `katsuji`）各 42 个 token，`theme.test.ts` 断言齐全、互不继承、对比度达标。**不要新增 `:root` token**；需要尺寸就直接写值（如 44px）。
5. **TS/TSX 不写原始颜色。** 颜色只出现在 `theme.css`；主题色同步读运行时计算值，不写 hex。
6. **测试绿 ≠ 对。** 每个任务结束跑真流程：`python -m yechao serve` 打开六页看实际产出。
7. **不提交任何账号、密钥、AK/SK、代金券码。** 连接串只走环境变量。
8. 提交信息用中文，写清这一任务做了什么；不要 `git add -A`，按路径提交。

## 命令

```bash
# 前端
cd web && npm test                 # vitest
cd web && npm run build            # tsc -b && vite build，产物 web/dist

# 后端
python -m pytest -q
python -m pytest tests/contracts/test_deploy_contract.py -q

# 跑起来看
python -m yechao demo
python -m yechao digest
python -m yechao serve             # 默认 http://127.0.0.1:9000
```

前端测试工具（需要先起 `serve`）：

```bash
cd web && node tools/verify.mjs
cd web && MOBILE=1 node tools/shot.mjs yoi mobile
```

## 本任务的验收口径

以 `docs/移动端与PWA实施计划.md` 每个任务卡里的「验收」为准。通用要求：

- 320 / 375 / 390 / 414 / 767 宽度下，六个页面**文档无横向溢出**。
- 移动端可点击目标 ≥44×44；输入框字号 ≥16px。
- 768 / 1440 宽度下，桌面侧栏固定 224px、内容左距 224px，行为与改造前一致。
- 离线时缓存数据必须显示「缓存于 HH:MM」，写操作必须失败可见。
