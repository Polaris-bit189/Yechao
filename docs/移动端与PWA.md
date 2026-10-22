# 移动端与 PWA

> 2026-10-22。对应分支 `feat/mobile-first-pwa`，执行规格见
> `docs/移动端与PWA实施计划.md`。

## 做了什么

- **手机端以 375px 为设计基准**：顶部两行导航（品牌/操作一行，页面标签横滑一行），
  44px 触控，刘海与 home 指示条安全区，输入控件 ≥16px（防 iOS 聚焦缩放）。
- **桌面不退化**：≥768px 仍是 224px 固定侧栏、内容左距 224px；信息流详情仍是右侧抽屉。
- **PWA**：manifest、192/512 与 maskable 图标、apple-touch-icon；Service Worker
  预缓存应用壳；只读 API 的 GET 走网络优先 + 缓存回落；可安装、可离线读。
- **离线是可见的**：缓存数据顶部显示「离线 · 缓存于 HH:MM · 只读」；
  写操作离线时走原有失败路径，**不显示成功**。

## 构建与验收

```bash
cd web
npm ci
npm test                 # 62 条
npm run build            # tsc + vite + 生成 dist/sw.js
npm run verify:pwa       # manifest / 图标 / SW 预缓存静态检查

# 先起服务（从仓库根，避免加载到已安装的旧副本）
cd ..
python -m yechao serve --port 9000

# 布局矩阵（需要 Playwright Chromium）
cd web
node tools/verify.mjs --url http://127.0.0.1:9000/ \
  --widths 320,375,390,414,767,768,900,1440 --browsers chromium

# 离线：壳 + 缓存时间头 + 横幅
node tools/verify.mjs --url http://127.0.0.1:9000/ \
  --offline --widths 375 --browsers chromium

# 负控：注入溢出，必须退出 1
NEGATIVE_CONTROL=1 node tools/verify.mjs --widths 375 --browsers chromium
```

WebKit 本机未安装（`npx playwright install webkit` 可补），verify 会打印
`[skip]` 并**退出 1**，不会把「没测」说成「通过」。

## Service Worker 缓存策略

| 对象 | 策略 |
|---|---|
| 应用壳（index、CSS/JS、manifest、图标、favicon） | 安装时预缓存；静态资源 cache-first |
| 导航请求 | network-first（3s 超时），失败回落缓存的 `index.html`；无缓存返回可见 503 页 |
| 只读 API GET 白名单 | network-first；只缓存 200；4xx/5xx **原样返回**，不拿旧缓存顶替 |
| 写操作 / 非 GET | **不拦截**，失败按原有错误路径显示 |
| 新版本 | 不静默 `skipWaiting`；页面提示「新版本可用」，用户点击后接管并刷新一次 |

API 白名单：`/api/digests`、`/api/digests/:id`、`/api/items`（仅首页、无搜索）、
`/api/items/:id`、`/api/calendar`、`/api/categories`、`/api/persons`、
`/api/groups`、`/api/urgent`。

**不缓存**：`/api/status`、`/api/consent`、`/api/privacy`、`/api/llm`、
`/api/config/*`、`/api/jobs*`。

缓存命中时响应带 `X-Yechao-Cache: hit` 与 `X-Yechao-Cached-At: <epoch 秒>`；
前端读取这两个头，显示**本次会话最早**的一次缓存时间。API 缓存上限约 40 条。

## 离线能读什么 / 不能读什么

- **能**：应用壳；最近一次日报；信息流第一页；已访问过的日历、类目、人物、群、紧急条目；
  打开过的条目详情。
- **不能**：搜索、信息流后续页、从未打开过的条目详情、状态/授权/隐私/LLM/配置/任务，
  以及**所有写操作**。

离线缓存是明文的，且可能含个人数据；Help 里有「清除离线缓存」。

## 安装

- Android / 桌面 Chrome：Help 里出现「安装到桌面」（`beforeinstallprompt`）。
- iOS Safari：Help 给「分享 → 添加到主屏幕」的文字指引。
- 鸿蒙浏览器：Help 给「浏览器菜单 → 添加到主屏幕」；**不承诺** standalone，
  PWA 安装/离线能力以真机版本为准。

不做 Web Push。日报的每日回访仍靠打开页面；推送是后续独立任务。

## 验收结果（2026-10-22）

| 项 | 结果 |
|---|---|
| `npm test` | 62 passed |
| `python -m pytest -q` | 1047 passed |
| `npm run build` | 通过，生成 `dist/sw.js` |
| `npm run verify:pwa` | 通过 |
| 布局矩阵 8 档（Chromium） | 0 违规 |
| 离线用例 | 通过（壳、缓存时间头、横幅） |
| 负控注入溢出 | 按预期退出 1 |
| WebKit | **未安装，跳过**；iOS 真机待测 |

截图在 `web/tools/shots/`（gitignored），本轮前缀 `t9-`：
`t9-mobile-*`（375）、`t9-320-*`、`t9-768-*`、`t9-desktop-*`。

## 真机矩阵（未执行，写 pending）

| 设备/浏览器 | 状态 |
|---|---|
| iOS Safari（iPhone） | **pending**：无真机/HTTPS 环境 |
| Android Chrome | **pending** |
| HarmonyOS 浏览器 / ArkWeb | **pending**：需记录系统与浏览器版本 |

真机要检查：添加到主屏幕、独立窗口启动、飞行模式离线读与时间戳、横竖屏、
键盘弹出、刘海与 home 指示条。**未执行就写 pending，不编造。**

## 已知限制

- **没有路由**：浏览器返回键会退出应用；PWA 每次打开都落在「日报」。
- **iOS**：独立窗口与 Safari 的存储是分开的；系统可能回收缓存。
- **鸿蒙**：PWA 安装与离线支持不确定；H5 浏览器是基线。
- **无 Web Push**：没有后台推送能力。
- **离线缓存明文**：见上「清除离线缓存」。

## 开发过程（如实记录）

- **T1–T3（移动端外壳、日报/信息流、其余页面与浮层）由 CodeArts 码道完成**：
  提交 `711acd9`、`ab5d01a`、`2c08f53`；对应 CodeArts 会话记录
  `ses_e9eb…`（T1）、`ses_e9df…`（T2）、`ses_e9dc…`（T3）。
- **T4–T9（PWA、验收脚本、服务端契约）由监督方（Claude）完成**：CodeArts 在
  T3 后长时间闲置，为保证进度由监督方接手；T1–T3 的 CodeArts 使用记录保留在
  本机 `%USERPROFILE%\.codeartsdoer\codearts-data\`，并已导出到
  `D:\github\docs\证据\codearts\`。
- `AGENTS.md`、`docs/移动端与PWA实施计划.md` 与验收脚本由监督方编写。
