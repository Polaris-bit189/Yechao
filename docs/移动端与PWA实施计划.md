# 移动端与 PWA 实施计划（CodeArts 执行规格）

> 执行者：华为云 CodeArts 码道代码智能体。
> 监督与验收：Claude（读 diff、跑命令、给放行/纠正结论）。
> 基线提交：`3c59c7a`，分支 `feat/mobile-first-pwa`，标签 `pre-mobile-pwa`。

## 目标

1. 手机端以 **375px 为设计基准**真正好用：顶部两行导航、44px 触控、安全区、无横向溢出。
2. 桌面端（≥768px）**不退化**：侧栏 224px、内容左距 224px、右侧抽屉行为不变。
3. 可安装、可离线读的 PWA：iOS Safari、Android Chrome、鸿蒙浏览器（ArkWeb）以 H5 为基线；安装是渐进增强。
4. 离线数据必须**可见地标注**「缓存于 HH:MM」，写操作必须失败可见，绝不静默展示旧数据。

## 非目标

- 不做底部 Tab（界面规格与用户已定：顶部两行）。
- 不做 Web Push、后台同步、写队列。
- 不引入新的运行时依赖；不新增主题 token；不加路由。
- 不改业务逻辑、数据库、数据源、模型调用。

## 设计约束（必须遵守）

- 产品是「早上读一次的纸」，不是花哨网站；日报首屏优先。
- 导航只有一套 DOM，桌面侧栏与手机顶栏是同一个 `.rail` 的 CSS 形态。
- 六套主题（`yoi/kiri/tsuki/sakura/yuu` + 彩蛋 `katsuji`）各 42 token；`theme.test.ts` 断言齐全、互不继承、对比度达标。
- **全站零 emoji**；图标用手写 SVG。
- 字号阶梯固定：32/22/18/15/14/13/12/11；移动端只允许日期 26、页标题 20 两个响应式变体。
- 卡片不加阴影；只有浮层用阴影；一个强调色；类目色语义固定。
- 动效只动 `transform/opacity`；`prefers-reduced-motion` 必须压制。
- 只在 `max-width:767px` 内改移动端；新增 `max-width:430px` 小屏档；≥768px 不动。
- 不手工编辑 `web/dist/`；只在 T9 用 `npm run build` 生成并提交。

---

## T1 — 外壳与导航

**目标**：viewport、安全区、顶部两行、44px 触控、标签自动滚入、浮层高度。

**改动文件**
- `web/index.html`
- `web/src/App.tsx`
- `web/src/App.css`
- `web/src/styles/mobile.css`（新建）
- `web/src/styles/base.css`
- `web/src/ui/misc.css`（remind / help 的安全区部分；细节在 T3）

**要求**
1. `index.html` viewport 改 `width=device-width, initial-scale=1, viewport-fit=cover`；不要禁用缩放。
2. 新建 `web/src/styles/mobile.css`，在 `App.tsx` 的 import 列表**最后**（`./ui/misc.css` 之后）引入，确保覆盖。
3. `@media (max-width:767px)`：
   - `.rail`：`position: sticky; top: 0; z-index: 40;` 两行；去掉 `backdrop-filter`（移动端滚动性能）；背景接近不透明。
   - `padding-top: calc(8px + env(safe-area-inset-top))`；左右 `max(12px, env(safe-area-inset-left/right))`。
   - 第一行：品牌、撤销/主题/帮助，每个可点击目标 ≥44×44；允许隐藏当前页徽标以省宽度。
   - `.rail__nav`：单行横滑，`overflow-x:auto`、`scroll-snap-type:x proximity`、隐藏滚动条、`overscroll-behavior-x:contain`；`.rail__link` 最小 44px 高、`scroll-snap-align:center`。
   - 只让 `.rail__link.is-on` 显示图标（15px）；未选中只显示文字。选中圆点 `::after` 保留且不被裁掉。
   - `.main`：左右安全区；底部 `padding-bottom: calc(var(--s6) + env(safe-area-inset-bottom))`。
   - Toast：`bottom: calc(var(--s5) + env(safe-area-inset-bottom))`；`max-width: calc(100vw - 2 * var(--s3))`。
4. `App.tsx`：给 `.rail__nav` 加 ref；`page` 变化时，如果标签条溢出，把 `.rail__link.is-on` 滚到中间（`scrollIntoView` 或 `offsetLeft` 计算）；`prefers-reduced-motion: reduce` 时用 `behavior:'auto'`。不新增第二套导航 DOM。
5. `base.css`：移动端 `-webkit-text-size-adjust:100%`；输入控件字号 ≥16px；`-webkit-tap-highlight-color: transparent`；全局 `prefers-reduced-motion: reduce` 压制动画/过渡；`html,body,#root` 移动端用 `min-height:100svh`（`100vh` 兜底），不要锁 `height:100%`。
6. `misc.css`：Help 浮层移动端用 `max-height: calc(100dvh - 2*var(--s4))`、内部滚动、安全区；（完整 Help 细节在 T3）。

**验收**
```bash
cd web && npm test && npm run build
python -m yechao serve    # 另开终端
cd web && node tools/verify.mjs
cd web && MOBILE=1 node tools/shot.mjs yoi t1-mobile
```
- 375 宽度：导航 sticky、两行；六个标签都能点到；当前标签在可视区内；无横向溢出（`document.documentElement.scrollWidth <= innerWidth + 1`）。
- 768/1440：`.rail` 仍为 fixed、224px；`.main` 左距 224px。
- 控制台零 error。

**提交**：`移动端 T1：顶部两行导航、安全区与 44px 触控`

---

## T2 — 日报与信息流

**目标**：日报首屏可读；信息流搜索不缩放、chips 单行、详情改底部抽屉。

**改动文件**
- `web/src/pages/DayPage.css`
- `web/src/pages/StreamPage.tsx`
- `web/src/pages/StreamPage.css`

**要求**
1. 日报（`DayPage.css`，`max-width:767px`）：
   - 日期保持 26px；页头与卡片间距收紧；`.paper` 内边距 16px，<360px 时 12px。
   - 正文、引用、链接 `overflow-wrap:anywhere`；`.day__head` 允许换行；星期不缩。
   - 不截断任何内容。首屏（375×812）能看到日期 + 摘要开头 + 第一个分区即可。
2. 信息流（`StreamPage`）：
   - 搜索框高度 ≥44px，`font-size:16px`。
   - 分类 chips：移动端单行横滑（`flex-wrap:nowrap`、`overflow-x:auto`、隐藏滚动条、snap）；人员筛选可换行。
   - 行内工具：移动端隐藏桌面 hover 工具，改为每行一个 44×44「操作」按钮，点击打开**现有的**详情抽屉（复用现有 state，不改业务逻辑）。
   - 详情抽屉：`max-width:767px` 时改为底部抽屉——`position:fixed; left:0; right:0; bottom:0; top:auto; width:100%; max-height:85dvh;` 顶部圆角、底部安全区、关闭/操作按钮 44px；打开时锁背景滚动；Esc/遮罩关闭；关闭后焦点回到触发按钮。
   - ≥768px 保持右侧抽屉与 hover 工具不变。

**验收**
```bash
cd web && npm test && npm run build
cd web && node tools/verify.mjs
cd web && MOBILE=1 node tools/shot.mjs yoi t2-mobile
```
- 375：chips 单行（内部可横滑）；搜索框字号 16px；行正文不被工具挤压；抽屉宽=视口、底=视口底、高≤85dvh；无横向溢出。
- 1440：抽屉仍在右侧，行为不变。

**提交**：`移动端 T2：日报首屏与信息流底部抽屉`

---

## T3 — 其余页面与浮层

**目标**：320/375 下六页全部无横向溢出，触控与可读性达标。

**改动文件**
- `web/src/pages/MonthPage.css`
- `web/src/pages/SetupPage.tsx`
- `web/src/pages/SetupPage.css`
- `web/src/pages/BoundaryPage.css`
- `web/src/pages/KindsPage.css`
- `web/src/ui/Status.css`
- `web/src/ui/Consent.css`
- `web/src/ui/Remind.tsx`
- `web/src/ui/Help.tsx`
- `web/src/ui/misc.css`

**要求**
1. 日历：`grid-template-columns: repeat(7, minmax(0,1fr))`；格子 `aspect-ratio:1`，≥360px 时 54px、<360px 时 48px；月份切换按钮 ≥44px；320 下文档不溢出。保留「今天默认选中、空日显示最近有内容的日子」。
2. 配置：8 个标签改为单行横滑条（44px、snap、当前项滚入视野；或 <360px 用 `<select>`）；输入/下拉/文本域 16px；`.field{min-width:0}`；长 slug/路径 `overflow-wrap:anywhere`；320 下 `documentElement.scrollWidth === 320`。
3. 隐私：统计卡 `repeat(2,minmax(0,1fr))`，<340px 单列；数字不被裁；免责声明原文**一字不改**。
4. 类目：标题 `min-width:0` + 省略号；截止时间 `flex-shrink:0`；<360px 收紧行距；行本身不是误导性的可点区域。
5. 状态：网格 `minmax(0,1fr)`；长来源/原因 `overflow-wrap:anywhere`。
6. 授权（Consent）：文档面板 `max-height:46dvh`（`46vh` 兜底）；内边距 16px；按钮 ≥44px；表格只在自身容器内横向滚动。
7. 提醒条：移动端保持静态流内；两个按钮 ≥44px；文案可换行；一次性关闭逻辑不变。
8. Help：全宽卡片、`max-height:calc(100dvh - 2*var(--s4))`、内部滚动、安全区、关闭 ≥44px、`aria-modal`、焦点陷阱、Esc/遮罩关闭。

**验收**
```bash
cd web && npm test && npm run build
cd web && node tools/verify.mjs
cd web && MOBILE=1 node tools/shot.mjs yoi t3-mobile
```
- 320/375：六页 `documentElement.scrollWidth <= innerWidth + 1`；配置页 320 严格相等。
- 日历 7 列不横向滚动；Help 在 568px 高可滚；所有列出的按钮 ≥44px。

**提交**：`移动端 T3：日历、配置、隐私、类目与浮层`

---

## T4 — manifest、图标、主题色

**目标**：可安装；图标齐全；浏览器 UI 颜色跟主题走。

**改动文件**
- `web/public/manifest.webmanifest`（新建）
- `web/public/icon-source.svg`、`web/public/icon-maskable.svg`（新建）
- `web/public/icons/*.png`（生成）
- `web/tools/icons.mjs`（新建）
- `web/package.json`（加 `pwa:icons` 脚本）
- `web/index.html`
- `web/src/lib/theme.ts`

**要求**
1. manifest 字段：`name/short_name` 夜抄、`description`、`lang:"zh-CN"`、`dir:"ltr"`、`id:"."`、`start_url:"./"`、`scope:"./"`、`display:"standalone"`、`orientation:"any"`、`background_color:"#0a0c12"`、`theme_color:"#0a0c12"`、`categories:["news","productivity"]`。
2. 图标：192、512（`purpose:"any"`），192、512 maskable（`purpose:"maskable"`）；`apple-touch-icon.png` 180（不透明背景）。
3. `web/tools/icons.mjs`：用仓库已有的 `playwright` Chromium，渲染 SVG 到指定尺寸截图；`any` 透明背景，maskable/apple 铺默认底色；`package.json` 加 `"pwa:icons": "node tools/icons.mjs"`。
4. `index.html`：`<link rel="manifest" href="./manifest.webmanifest">`；`apple-touch-icon`；`apple-mobile-web-app-capable`、`mobile-web-app-capable`、`apple-mobile-web-app-status-bar-style=black-translucent`、`apple-mobile-web-app-title=夜抄`；`theme-color` meta 加 `id="theme-color"`。
5. `lib/theme.ts` 的 `paint()`：主题应用后读 `getComputedStyle(document.documentElement).getPropertyValue('--bg')`，写入 `meta#theme-color` 的 `content`。TS 里不写颜色字面量。View Transitions 路径在 `apply()` 后也同步。

**验收**
```bash
cd web && npm run pwa:icons && ls -la public/icons
cd web && npm test && npm run build && npm run verify:pwa   # verify:pwa 在 T7 建立；此处先手工核对 dist
```
- `web/dist/manifest.webmanifest` 与四个 PNG + apple-touch 存在；PNG 尺寸正确。
- 六套主题切换后 `meta#theme-color` 等于计算出的 `--bg`。
- 无新增 `:root` token；`theme.test.ts` 全绿。

**提交**：`PWA T4：manifest、图标与主题色同步`

---

## T5 — Service Worker 生成、注册、更新

**目标**：构建产出 `dist/sw.js`；生产注册；新版本可见提示，不静默接管。

**改动文件**
- `web/pwa/sw-template.js`（新建）
- `web/pwa/vite-plugin.mjs`（新建）
- `web/pwa/vite-plugin.d.mts`（新建，类型声明）
- `web/vite.config.ts`
- `web/src/pwa/register.ts`（新建）
- `web/src/main.tsx`
- `web/src/App.tsx`（更新提示 UI 的挂载点；可先留空壳，T6 完善）

**要求**
1. 插件在 `closeBundle`：枚举 `dist` 实际文件（index、assets、manifest、icons、favicon），计算版本哈希，替换模板 `__VERSION__`/`__PRECACHE__`，写 `dist/sw.js`。缺预期文件时**构建失败**。
2. 预缓存应用壳；静态资源 cache-first；导航 network-first（3s 超时）回落 `index.html`；无缓存返回可见 503 HTML。
3. 非 GET 一律不拦截。此任务先只做壳与静态资源；API 缓存在 T6。
4. 新 SW 安装后**不 skipWaiting**；页面检测到 `registration.waiting` 显示「新版本可用」；点击后 `postMessage({type:'SKIP_WAITING'})`，`controllerchange` 后只刷新一次。
5. 注册仅在 `import.meta.env.PROD` 且 `window.isSecureContext`；`register('./sw.js',{scope:'./',updateViaCache:'none'})`；失败只记 info，不 `console.error`。

**验收**
```bash
cd web && npm run build && ls -la dist/sw.js
# 构建两次，确认第二次生成不同的版本串
cd web && npm test
```
- `dist/sw.js` 包含当前全部 hashed assets。
- 生产（`python -m yechao serve`）下 `navigator.serviceWorker.controller` 在二次刷新后非空。
- 连续两次构建后可观察到 waiting SW 与提示（手工截图）。

**提交**：`PWA T5：Service Worker 构建与更新提示`

---

## T6 — 离线数据层、横幅、安装入口

**目标**：离线可读且时间可见；写失败可见；安装入口只在 Help。

**改动文件**
- `web/pwa/sw-template.js`（API 策略）
- `web/src/api.ts`
- `web/src/pwa/offline.ts`（新建）
- `web/src/pwa/install.ts`（新建）
- `web/src/ui/OfflineBanner.tsx`（新建）
- `web/src/ui/OfflineBanner.css` 或 `misc.css`
- `web/src/ui/Help.tsx`
- `web/src/App.tsx`

**要求**
1. SW 对 `/api` **仅 GET** 白名单网络优先（3s 超时）：`/api/digests`、`/api/digests/:id`、`/api/items`（仅无 `offset` 或 offset=0、`limit<=50`、无搜索 `q`）、`/api/items/:id`（曾打开过）、`/api/calendar`、`/api/categories`、`/api/persons`、`/api/groups`、`/api/urgent`。
2. 不缓存 `/api/status`、`/api/consent`、`/api/privacy`、`/api/llm`、`/api/config/*`、`/api/jobs*`；永不缓存非 GET；4xx/5xx 原样返回，不拿缓存顶替。
3. 命中缓存时响应加 `X-Yechao-Cache: hit` 与 `X-Yechao-Cached-At: <epoch 秒>`；API 缓存上限约 40 条，淘汰最旧。
4. `api.ts` 在统一请求函数里读取这两个头，记录**本次会话最早的缓存时间**，用订阅机制暴露给 UI；正常在线请求不清除它，只有页面重新加载/用户点「重新加载」才清。
5. `OfflineBanner` 文案：
   - 离线且有缓存：「离线 · 缓存于 HH:MM · 只读」
   - 离线无缓存：「离线 · 没有可读缓存」
   - 在线但本次会话用过缓存：「当前显示缓存数据（缓存于 HH:MM）」+「重新加载」
   - 横幅是流内静态条，推开内容，不遮挡；正常在线时不显示。
6. 写操作（撤销/删除/改类目/授权等）离线时走现有失败路径，必须显示错误，不显示成功。
7. 安装入口只放 Help：Android 捕获 `beforeinstallprompt`（`preventDefault`）并在点击时 `prompt()`；`appinstalled` 后隐藏；iOS 显示「分享 → 添加到主屏幕」；鸿蒙/其他显示浏览器菜单说明。不要常驻首屏。
8. Help 可选加「清除离线缓存」按钮（会删 API 缓存），并在文案说明离线缓存是明文。

**验收**
```bash
cd web && npm test && npm run build
cd web && node tools/verify.mjs --offline --widths 375 --browsers chromium
```
- 断网刷新：应用壳与日报可读；横幅含「缓存于 HH:MM」；写操作报错可见。
- 恢复网络点「重新加载」：横幅消失，数据刷新。
- 非 GET 未被缓存命中。

**提交**：`PWA T6：离线数据层、缓存时间横幅与安装入口`

---

## T7 — 验证脚本与前端测试

**目标**：把验收变成可重复、非零退出的命令。

**改动文件**
- `web/tools/verify.mjs`
- `web/tools/check-pwa.mjs`（新建）
- `web/tools/shot.mjs`（加 WIDTH/HEIGHT）
- `web/src/pwa/pwa.test.ts`（新建）
- `web/package.json`（`verify`、`verify:pwa` 脚本）

**要求**
1. `verify.mjs` 参数：`--url`、`--widths`（逗号分隔）、`--browsers`（chromium,webkit）、`--offline`。默认 `http://127.0.0.1:9000/`、`320,375,390,414,767,768,1440`、`chromium`。
2. 断言：每宽度每页 `documentElement.scrollWidth <= innerWidth + 1`；控制台 error/pageerror 为 0；无 emoji；导航形态（<768 sticky 两行；≥768 fixed 224px）；触控目标（nav、Setup 标签、抽屉关闭/操作、Help、月份切换、提醒按钮）≥44×44；输入 ≥16px；320 配置页严格无溢出；日历 7 列；信息流抽屉几何；主题色六套同步。
3. 违规时打印表格并 `process.exitCode=1`；截图存 `web/tools/shots/verify/`。
4. `check-pwa.mjs`：解析 manifest 字段；读 PNG IHDR 校验尺寸；校验 dist/index.html 的 viewport/manifest/apple-touch-icon/theme-color；`dist/sw.js` 存在、含版本与全部 assets、含非 GET 守卫。
5. `pwa.test.ts`：manifest 字段与相对路径；index 不变量；离线状态机（最早时间、清除）。
6. 负控：临时制造一个 375 溢出和一个小字号，确认 verify 非零退出，再撤销。

**验收**
```bash
cd web && npm test && npm run build && npm run verify:pwa
cd web && node tools/verify.mjs --browsers chromium,webkit
```
- 全矩阵通过；负控能红。

**提交**：`测试 T7：移动端与 PWA 验收脚本`

---

## T8 — 服务端 MIME、缓存与契约

**目标**：FastAPI 正确服务 manifest 与 SW；更新不被 HTTP 缓存掩盖。

**改动文件**
- `yechao/interfaces/api.py`（只改 `_mount_web` 及其辅助）
- `tests/interfaces/test_pwa_contract.py`（新建）

**要求**
1. 在 `StaticFiles` 挂载前显式路由：
   - `GET /manifest.webmanifest` → `FileResponse(..., media_type="application/manifest+json")`
   - `GET /sw.js` → `FileResponse(..., media_type="text/javascript", headers={"Cache-Control":"no-cache","Service-Worker-Allowed":"/"})`
2. `/` 与 `/index.html` 响应加 `Cache-Control: no-cache`。
3. dist 缺失时仍返回原来的显式 501，不回落。
4. 契约测试：manifest/SW 的 MIME 与头；index 的 no-cache；缺 dist 的 501；dist 中存在 manifest/sw/icons。

**验收**
```bash
python -m pytest tests/interfaces/test_pwa_contract.py tests/contracts/test_deploy_contract.py -q
python -m pytest -q
python -m yechao serve &
curl -sI http://127.0.0.1:9000/sw.js
curl -sI http://127.0.0.1:9000/manifest.webmanifest
```

**提交**：`PWA T8：manifest 与 Service Worker 的服务端响应`

---

## T9 — 全量回归、构建、证据

**目标**：一次可复现的交付。

**要求**
1. `cd web && npm test && npm run build && npm run verify:pwa`
2. `python -m pytest -q`
3. `cd web && node tools/verify.mjs --widths 320,375,390,414,767,768,1440 --browsers chromium,webkit`
4. 离线用例：`node tools/verify.mjs --offline --widths 375 --browsers chromium`
5. 截图：`MOBILE=1 node tools/shot.mjs yoi t9-mobile`；`node tools/shot.mjs yoi t9-desktop`；另跑 `kiri`、`katsuji` 各一组移动端。
6. 提交 `web/dist/`（由构建生成，不手改）；确认 `git ls-files web/dist` 含 manifest、sw.js、icons。
7. 写 `docs/移动端与PWA.md`：构建命令、缓存策略、离线范围、真机清单（未执行写 pending）、已知限制（无路由导致返回键退出、iOS 独立存储、鸿蒙 PWA 不确定）。

**验收**：以上命令全绿；`git status` 干净；桌面 1440/768 与基线截图对比无几何变化。

**提交**：`移动端与 PWA：全量回归与构建产物`

---

## 停止条件（任一满足就停下报告，不要继续）

- 任何测试变红。
- 需要改本规格之外的文件（尤其是 `domain/`、`application/`、持久化、数据源）。
- 需要新增依赖、修改主题 token、改对外路由/表名/CLI 契约。
- `web/dist` 与源码不一致，或构建失败。
- 发现另一个进程/会话正在同时改 `web/`。

## 统一验收口径

- 320 / 375 / 390 / 414 / 767：六页文档无横向溢出；可点击目标 ≥44×44；输入 ≥16px。
- 768 / 1440：侧栏 fixed 224px、main 左距 224px；信息流右侧抽屉不变。
- 离线：应用壳与日报可读；横幅含「缓存于 HH:MM」；写操作失败可见。
- 主题：六套切换后 `meta#theme-color` 同步；`theme.test.ts` 全绿。
- 零 emoji；零控制台 error；`npm test`、`pytest`、`verify` 全绿。

## 沙箱网络限制与验收兜底

CodeArts 终端沙箱会**间歇性**拦截 Chromium 对 `127.0.0.1` 的访问：`curl` 返回 200、`netstat` 显示端口 `LISTENING`，但 Playwright 报 `net::ERR_ADDRESS_UNREACHABLE`。**这不是代码故障，也不是停机的理由。**

因此：

- **浏览器 HTTP 验收由监督方在沙箱外执行**，以监督方的结果为准。CodeArts 不得用 `file://` 的结果宣称「HTTP 验收通过」。
- **T2–T4**（布局/静态资源）：允许用 `file://` 加载 `web/dist` 做几何辅助自检，但报告里必须标注「file:// 几何自检」，不能写成 HTTP 验收。
- **T5–T6**（Service Worker / 离线）：**不能**用 `file://` 验证——它不是安全上下文、没有 SW、没有真实网络回落。这两项必须由监督方在 HTTP/HTTPS 下验收。
- CodeArts 不要为验收反复启动 `serve`；它的职责是**改代码 + 单测 + `npm run build` + 提交**。网络渠道问题交给监督方。
