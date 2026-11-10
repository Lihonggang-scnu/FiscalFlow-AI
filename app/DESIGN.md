---
name: "财税 Agent 工作台 · A/B/C 三页"
description: "同一 Chainlit 会话中的办件、对话与票据核对工作区"
colors:
  chainlit-primary: "hsl(229 48% 35%)"
  page-a-nav-primary: "#30223a"
  page-a-nav-secondary: "#392943"
  page-a-accent: "#6f4d7d"
  page-a-canvas: "#f6f3f7"
  nav-indigo: "#202e61"
  action-indigo: "#354b96"
  history-current: "#35477f"
  page-c-nav-primary: "#153b46"
  page-c-nav-secondary: "#1c4a55"
  page-c-accent: "#2e7180"
  page-c-canvas: "#f1f6f6"
  canvas: "#f3f5f9"
  surface: "#fff"
  ink: "#1d293e"
  divider: "#dce2eb"
  neutral-wash: "#edf0f6"
  nav-secondary: "#c1c9e3"
  dark-panel: "#2a3140"
  dark-ink: "#e5eaf5"
  dark-control: "#f8f9fc"
  dark-focus: "#b4c3ff"
typography:
  headline:
    fontFamily: '"Microsoft YaHei", "PingFang SC", "Segoe UI", system-ui, sans-serif'
    fontSize: "17px"
    fontWeight: 650
    lineHeight: 1.35
  title:
    fontFamily: '"Microsoft YaHei", "PingFang SC", "Segoe UI", system-ui, sans-serif'
    fontSize: "14px"
    fontWeight: 650
  body:
    fontFamily: '"Microsoft YaHei", "PingFang SC", "Segoe UI", system-ui, sans-serif'
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: '"Microsoft YaHei", "PingFang SC", "Segoe UI", system-ui, sans-serif'
    fontSize: "11px"
    fontWeight: 600
rounded:
  compact: "5px"
  control: "7px"
  rail: "8px"
  composer: "15px"
  composer-phone: "13px"
spacing:
  tight: "4px"
  compact: "8px"
  row: "12px"
  panel: "16px"
components:
  button-primary:
    backgroundColor: "{colors.action-indigo}"
    textColor: "{colors.surface}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "0 11px"
  button-primary-hover:
    backgroundColor: "#293d82"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "#2d3e70"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "0 10px"
  input-month:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "0 10px"
    width: "100%"
  history-current:
    backgroundColor: "{colors.history-current}"
    textColor: "#f8f9ff"
    rounded: "{rounded.control}"
    padding: "12px 9px 11px"
  context-tab-selected:
    backgroundColor: "{colors.surface}"
    textColor: "#26376e"
    rounded: "{rounded.compact}"
    padding: "0 5px"
---

# Design System: 财税 Agent 工作台 · A/B/C 三页

## Overview

**Creative North Star: "The Three-View Tax Desk"**

一个 Chainlit 会话切换三个完整页面：A 办件视图用深梅紫导航与目标票详情/进度；B 对话工作区保留深蓝导航、真实历史和原生聊天；C 票据核对台用石油蓝导航、真实发票表与规则详情。顶部始终显示当前用户、企业和显式选择的期间，左侧窄主导航旁的次级列表随页面变化。

页面切换只改变可见视图，聊天消息、输入草稿和 Agent 会话仍由当前 Chainlit 会话承载。A 的沟通预览来自该会话已保存且当前用户有权访问的近两轮对话；C 只展示业务库中的发票字段和程序返回的用途规则检查结果。

**Key Characteristics:**
- 左侧 64px 主导航加 232px 次级列表；顶部保留当前企业和期间。
- A 主区展示真实目标票字段和办件沟通，旁侧按 target/pending 展示进度。
- B 保留持久历史、Chainlit 原生 transcript 和 composer。
- C 用密集表格查看发票字段，右侧查看用途、备注与发票用途规则。
- 中文无衬线字体；关键业务数据和表格正文保持 12px 起，填写控件更大。

## Colors

每个页面用自己的深色导航和强调色标识工作状态；页面主体保持浅色，表格和细分隔线优先，方便逐项核对业务数据。

### Page Accents
- **A 深梅紫**（`page-a-nav-primary`、`page-a-accent`）：办件视图主导航、选中项和主要操作。
- **B 深蓝**（`nav-indigo`、`action-indigo`）：对话历史及当前对话入口。
- **C 石油蓝**（`page-c-nav-primary`、`page-c-accent`）：票据核对台导航和主要操作。
- Chainlit 自带组件另沿用其根主题主色（`chainlit-primary`）。

### Neutral
- **工作区浅底**（`canvas`）与**阅读面**（`surface`）：承托中央聊天和当前资料内容。
- **正文与分隔**（`ink`、`divider`）：用于深色正文和轻细区域边界。
- **状态面与导航辅助字**（`neutral-wash`、`nav-secondary`）：用于选项卡底面及深色历史栏的次级文字。

深色模式只覆盖定制资料/转录内容、窄屏身份提示和主聊天滚动条。资料区的月份与发票原生控件、规则原文仍保留浅色控件面；不要把这些局部覆盖概括成全应用深色主题。

**The Three-Rail Rule.** A/B/C 导航色区分页面；业务数据的有效、待办、非必填与未判定状态保持一致的颜色语义。

## Typography

**Display Font:** 无独立展示字体。  
**Body Font:** "Microsoft YaHei", "PingFang SC", "Segoe UI", system-ui, sans-serif。  
**Label/Mono Font:** 标签沿用中文无衬线；仅剪贴板失败后的原文框使用 ui-monospace / Consolas。

**Character:** 紧凑、清楚、以工作信息为主，不用大幅展示字级替代信息层级。金额、日期及计数在实现标记处使用 tabular numerals。

### Hierarchy
- **Headline:** 用于工作区及资料/转录面板标题。
- **Title:** 用于资料类别等次级标题。
- **Body:** 用于对话摘要、说明和逐项业务事实。
- **Label:** 用于控件名称、操作文字和紧凑元信息。

## Layout

桌面宽度从 1120px 起显示 296px 左侧工作栏：64px 主导航与 232px 次级列表。顶部工作栏显示当前用户、企业和显式选择的期间。A/C 页面占据主工作区；B 页面显示 Chainlit 原生对话，并保留 ElementSidebar 中的当前资料。

1119px 及以下把三页导航移到屏幕底部；次级列表按需作为抽屉打开，A/C 主内容纵向滚动，C 表格可横向滚动。B 隐藏其他页面时仍保留原生聊天输入及未发送内容，回到 B 后可以继续。

间距以紧凑控件组为主，列表项和面板之间留出清楚分组；YAML 中的 spacing 步进取自重复出现的源码值。

## Elevation & Depth

分隔线和色面是主要层次。阴影仅用于输入区、选中资料页签和移动端抽屉等局部抬升。

### Shadow Vocabulary
- **聊天输入区**（`box-shadow: 0 5px 16px #25375b0b`）：为固定输入区域提供轻柔分离。
- **输入聚焦**（`box-shadow: 0 0 0 3px #5268b21c, 0 5px 16px #25375b0b`）：保留明确焦点环。
- **移动历史抽屉**（`box-shadow: 8px 0 24px #14204426`）：仅用于打开的抽屉边界。
- **选中资料页签**（`box-shadow: 0 1px 3px #1d2b4c12`）：轻微区分白色选中项。

**The Local Elevation Rule.** 历史和资料内容以底色及分隔线分组；柔和阴影只标示输入焦点、选中页签或移动抽屉。

## Shapes

控件使用紧凑圆角，历史导航标记略柔和；主输入区采用更大的圆角，手机宽度下另有收紧值。内容区以直边和分隔线为主，不套用重复的卡片轮廓。实际半径见 YAML 的 `rounded` scale。

## Components

### Buttons
主要操作以靛蓝填充，次要操作采用白面与描边，安静操作为无边框文字按钮。普通按钮有 hover、focus-visible 和 disabled 状态；焦点使用清楚的外轮廓，禁用控件降低不透明度并显示不可操作光标。输入区发送/停止按钮另有原生 Chainlit 样式。

### Inputs / Fields
月份输入使用浏览器原生 `type="month"`，发票选择使用原生 `select`；两者均有明确描边和 focus-visible 轮廓。深色覆盖中两种原生控件明确保留浅色 `color-scheme`，以维持原生日期/选票交互。

### Navigation
主导航提供 A 办件、B 对话、C 票据三个页面；次级列表分别显示当前期间办件、对话历史或发票行。身份与期间显示在顶部；切换用户/企业走 Chainlit 原有设置入口与权限校验。

### A/B/C Page Content
A 从真实任务的 target/pending 和选中目标发票构成办件进度，不展示内部任务编号或多人消息。B 保留 Chainlit 历史查看、继续、复制、折叠和原生对话。C 表格只显示发票编号、日期、销售方、项目、金额和程序判定的用途状态；用途/备注需要用户明确点击保存。

### Composer
Chainlit 输入区只属于 B。A/C 暂时隐藏原生聊天表面但保留 DOM 和未发送草稿；返回 B 后继续使用原输入与当前 Agent 会话。

## Do's and Don'ts

### Do:
- **Do** 在未选择月份时显示空态，不默认当前月。
- **Do** 只展示当前业务库返回的字段和规则判定；区分用途非必填、检查越界和规则未判定。
- **Do** 保留 hover、focus-visible、disabled、loading、error 和 selected 等实际控件状态。

### Don't:
- **Don't** 恢复欢迎宣传大标题，或用重复圆角卡片和装饰渐变取代工作区；这与已选 B 方向及 PRODUCT.md 品牌承诺冲突。
- **Don't** 将独立企业工作台表现成已与候选 Chainlit 共用账号、业务库或审批流程。
- **Don't** 增加当前业务库没有的票据影像、税率、预付款/支付状态、多人沟通或猜测用途。
- **Don't** 在月份未选时暗示已有期间，或用虚构票据内容填充页面。

