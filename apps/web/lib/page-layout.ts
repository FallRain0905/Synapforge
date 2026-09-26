import type { CSSProperties } from "react";

/**
 * 页面级布局的两个"补丁"样式（自持，不写进 globals.css）。
 *
 * 为什么放这里而不是全局样式表：`globals.css` 同期正被另一个会话编辑，
 * 往里面加规则会两边互相覆盖。等并行编辑结束，把下面两条按同样口径搬进
 * globals.css（`#kb` / `#graph` 已有先例）即可，页面侧删掉引用。
 */

/**
 * 页面根容器用：行按内容取高、整体靠上。
 *
 * `.main-area > .page-content` 是 `flex: 1 1 auto`，而 `.page-content` 是 `display: grid`
 * 且默认 `align-content: stretch` —— 窗口一高，多出来的高度就被**平摊给每一个区块**，
 * 块内内容却留在原处：标题行被撑到 260px、指标卡 299px、面板标题行 148px，
 * 每一块里都出现"莫名其妙的大片空白"。带上这个之后实测 800/1000/1500 三档高度完全一致。
 *
 * 注意**不要**改成"主体吃剩余高度"（`gridTemplateRows: "auto auto 1fr auto"`）：
 * 实测那样只是把空白挪进面板内部——panel 自己也是 grid、`.list-item` 又是
 * `align-items: center`，会逐层摊白，内容会"浮"在面板正中。
 */
export const PAGE_GRID: CSSProperties = { alignContent: "start" };

/**
 * 「撑满剩余高度」的中间层用：显式参与 flex 布局的列容器。
 *
 * 真因（项目工作区对话区溢出）：`.panel` 是 flex 列、`.chat-stream` 也写了
 * `flex: 1 1 auto / min-height: 0`，但两者之间还夹着一层没有样式的中间 div。
 * 中间层是普通块级 flex item，`min-height: auto` 会顶住不让收缩，高度直接长到内容高度
 * （实测 2400px，面板只有 622px），子元素那套 `min-height: 0` 因此全部失效、面板
 * `overflow: visible` 又让它直接漏出去，而外层 `main` 不滚动 —— 结果就是消息被切在视口外。
 *
 * 给中间层补上 `flex: 1 1 auto + min-height: 0 + 列向 flex` 即可把约束传下去。
 * **不要**顺手加 `overflow: hidden`：输入框上方的 @ 提及浮层（`.mention-pop`）需要向外溢出。
 */
export const FILL_COLUMN: CSSProperties = {
  flex: "1 1 auto",
  minHeight: 0,
  minWidth: 0,
  display: "flex",
  flexDirection: "column",
};