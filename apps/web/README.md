# Web

React + TypeScript + Vite + Ant Design。需要 Node.js 22.12+；安装使用提交的锁文件，禁用第三方安装脚本。

```bash
npm ci --ignore-scripts
npm run dev
```

默认 `127.0.0.1:5173`，`/api` 转发至 `127.0.0.1:8767`，可用 `API_PROXY_TARGET` 修改开发代理。`npm run dev:mock` 使用仓库合成响应独立运行；这个模式不写数据库，也不支持真实导入。

`npm run build` 完成 TypeScript 检查并输出 `dist`；生产由 Caddy 提供静态资源、同源代理 API。`npm run types` 从 `contracts/openapi.json` 重新生成类型，生成文件应一同提交。

颜色与 S 标记属于展示层。工具顺序、时间与未知值来自 API，前端不补造事实；切片不改变原始编号。分析组件只在用户触发后请求结果。
