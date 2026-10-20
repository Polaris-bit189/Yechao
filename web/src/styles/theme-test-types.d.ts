/// <reference types="vite/client" />

/*
 * 只为测试存在的一份声明。
 *
 * `theme.test.ts` 要读 `theme.css` 的**原文**并按块解析，还要用
 * `import.meta.glob` 把 src 下的 .ts/.tsx 扫一遍找裸色值。两样都要 Vite 的
 * 客户端类型，而 tsconfig 里写的是 `"types": []`。
 *
 * 用 triple-slash 引用而不是往 tsconfig 里加 "types"：tsconfig 不属于这一轮
 * 要动的文件；而且把 Vite 客户端类型挂成全局是测试的需要，不是生产代码的
 * 需要 —— 放在这个文件里，边界看得出来。
 *
 * 生产代码里一个 `import.meta` 都没有（扫过：src 下 0 处），所以这份声明
 * 不会改变任何非测试文件的类型。
 */

declare module 'node:fs' {
  /**
   * 只声明用得到的那一个重载。
   *
   * 为什么测试要碰文件系统：Vitest 默认不处理 CSS，**连 `?raw` 都被截成
   * 空串**（实测：src 下六个 `.css?raw` 全返回 ""），所以读 CSS 只能自己读
   * 文件。`.ts`/`.tsx` 仍然走 Vite 的 `?raw`，那边是好的。
   *
   * 这个仓库没装 `@types/node`。等哪天真的引进了，把这段删掉 —— 同名声明
   * 会合并成重载、不冲突，但留着就是多余的。
   */
  export function readFileSync(path: URL | string, encoding: 'utf8'): string
}
