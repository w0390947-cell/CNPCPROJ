# 算例与数据说明

容量与版本字段来自生成文件 `shared/api/generated/dataset-facts.json`。修改统一台账后运行 `python tools/export_unified_dataset.py`，使用 `--check` 检测漂移；不要在 content.ts 另行维护区域容量。

负责人：前端维护负责人；涉及模型口径时由建模维护负责人评审。

公开入口为 `index.ts`，由 `app/case-information/page.tsx` 装配。该功能集中呈现
Web 合成算例的数据身份、区域代码映射、研究参数、时间尺度、术语及结果适用边界。

区域展示名称只能读取 `shared/lib/region-presentation.ts`，不得维护第二套映射。页面
只说明后端既有模型口径，不在浏览器重新计算约束或安全结论。当前结果的动态校核
范围、错误和未知状态仍由各业务页面就地展示。

## 页面组织与兼容

- `ui.tsx` 组合公共平台外壳、页面标题与数据标签、章节目录和阅读主栏。
- `case-sections.tsx` 呈现数据身份、区域资产及算例构造；`study-sections.tsx`
  呈现研究参数、时间尺度、校核边界及术语。`reference-section.tsx` 统一本功能的章节样式。
- `content.ts` 保存说明页数据与章节元信息；目录和标题共享元信息，保留全部既有章节锚点。
- `style.module.css` 拥有本页局部样式。宽屏为阅读主栏与右侧常驻目录，窄屏目录置于正文前；
  表格在自身容器内横向滚动，并支持键盘聚焦。

仅依赖 React、现有图标/路由库及 `shared` 公开能力，不引入跨功能依赖、请求或计算。
路由 `/case-information`、公开导出 `CaseInformation`、区域名称来源及参数语义保持兼容。
模型依据见 [模型假设](../../../../docs/Model_Assumptions.md)；页面用途见
[使用说明第 12 节](../../../../docs2/Web仿真系统使用说明.md)。

资产表使用有名称的原生 `section` 和 `tabIndex={0}`，让键盘用户在需要时聚焦并横向滚动。
这遵循 [MDN 滚动容器可访问性说明](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/overflow#accessibility)。
仅在该容器处抑制 `no-noninteractive-tabindex` 的静态误报，采用
[规则文档明确允许的滚动容器处理](https://github.com/jsx-eslint/eslint-plugin-jsx-a11y/blob/main/docs/rules/no-noninteractive-tabindex.md)，
不改变全局规则，也不为静态正文增加 Tab 停靠点。

验证入口：前端严格 TypeScript 检查、oxlint、生产构建，以及
`node --test tests/region-presentation.test.mjs`。纯版式改动不增加物理/安全模型测试。
