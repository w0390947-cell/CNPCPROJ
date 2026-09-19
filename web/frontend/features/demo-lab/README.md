# 模拟设备演示

负责人：前端维护负责人；评审：设备控制维护负责人。

公开入口 `index.ts`，由 `app/demo-lab/page.tsx` 装配。所有 HTTP 请求经过
`shared/api/demo-client.ts`，使用 Python 生成的类型与 JSON Schema 校验结果。
页面只呈现后端的电气判断，不在浏览器推算安全、不填充缺失量测。
复用 `shared/ui/platform-chrome.tsx` 的平台品牌栏与主导航，功能内的面板、交互状态和展示格式保持在本目录。
遥测订阅使用串行轮询，卸载时停止；出错清除当前帧并提示无法确认安全。

前端验证执行 TypeScript 严格检查、受影响文件 lint、`tests/demo-schema.test.mjs`
契约测试与生产构建；安全判断及命令合法性由后端测试承担。
