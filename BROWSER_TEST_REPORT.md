# 浏览器交互验证记录

## M3 正文页面

已运行 `tools/browser_smoke.py --chromium /usr/lib/chromium/chromium --bridge`。
实际使用 Chromium 渲染 DOM、执行源码中的 manuscript.js、操作 textarea 与按钮，
后端是实际启动的本地 Python 服务与临时 SQLite 项目。

通过：导入、emoji 的 UTF-16/code point 选区替换、撤销后逐字恢复、逻辑行选区、
整篇编辑、HTML/脚本作为普通小说文本呈现、双页面旧版本拒绝、被拒绝后保留草稿。
捕获的页面 JavaScript 异常为空；截图也已人工检查布局。

## 测试边界

环境中的 Chromium 直接请求回环地址被管理策略拒绝：ERR_BLOCKED_BY_ADMINISTRATOR。
因此启用了**仅测试脚本**中的 HTTP 桥接：浏览器 fetch 交给 Python 转发至实际本地 HTTP 服务。
生产网页和后端不使用这一桥接。本测试验证真实 DOM/JS 与后端配合，不验证浏览器直连网络、
真实来源的 CSP 执行或下载交互。不能称为完全原生浏览器网络端到端测试。

在用户本机正常环境可运行同一个脚本不加 `--bridge`，另需自行安装 Playwright 与 Chromium。
Playwright 不是应用运行依赖。

## M5 结构化输出页面

同一 Chromium/HTTP 桥接方式另行验证了 model.html/model.js：粘贴带单引号、尾逗号的结果，
点击“仅本地检查”后显示修复标记和结构化内容；粘贴重复键对象后拒绝接受并显示错误。
没有使用真实模型，本地检查也没有发出模型 API 请求。浏览器网络方面的上述限制仍然适用。

## M6 导入与分块页面

实际运行同一桥接脚本，验证 UTF-16 二进制 TXT 文件选择、自动 BOM 解码、预览、
分段选项变化使旧预览失效、确认导入、原始文件链接出现、创建并显示分块计划。
Chromium 捕获的页面异常仍为空，导入页截图已检查。原始字节下载一致性另由 HTTP 测试验证，
没有声称完成浏览器文件下载交互测试。桥接现在支持二进制请求体，生产代码不使用桥接。
