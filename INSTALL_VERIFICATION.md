# v0.20.0 安装与升级实测

本记录对应本次实际执行，不沿用旧版本验收。

## 完整 wheel 安装

从本次源码运行 `pip wheel --no-deps --no-build-isolation`，再使用 `pip install --no-deps --target` 安装到独立目录。离开源码目录后，显式核对导入路径确实来自安装目录。86 个应用 Python/SQL/HTML/JavaScript/CSS 资源与源码逐个 SHA256 比较一致，其中 SQL 脚本 17 个。

在独立安装包上创建项目，经 FastAPI TestClient 设置分析模型并调用默认小问题诊断：4 次普通问答 + 1 次中文转换，结果附转换前文本、保持待审，正文始终为空；配置、模型、构思、生成、设定、时间线、分析、维护页面 HTTP 响应正常，草稿之外的项目保存成功。

该 HTTP 测试使用确定性 MockTransport，不是用户本机模型，也不是浏览器原生网络测试。当前容器缺少 Playwright 所需的 Chromium 可执行文件，本版未执行新的浏览器点击验收；只进行了前端 JS 语法检查、页面资源测试和后端工作流验证。

## v0.17.0 项目升级

使用原始 v0.17.0 源码创建项目（schema 14），保存自定义模型/字数配置、带 emoji/空白/英文专名/数字的正文，再做一次手工范围返修。新版打开生成升级前备份并升至 schema 17；旧配置保留、新模式默认为 small、中文转换默认开启。撤销旧返修逐字恢复初始正文，SQLite/外键/正文哈希检查通过。

升级前备份重新由 v0.17.0 打开，仍为旧 schema 和旧正文。回退只能用升级前备份；不要用旧程序直接打开升级后的数据库。

## 机器可读摘要

```json
{
  "installation": {
    "program": "0.20.0",
    "schema": 17,
    "installed_resource_hashes_match": 86,
    "sql_files": 17,
    "plain_answer_requests": 4,
    "translation_requests": 1,
    "pages_http_ok": true,
    "manuscript_unchanged": true,
    "real_browser_test": false,
    "real_user_model_test": false
  },
  "upgrade": {
    "from_program": "0.17.0",
    "to_program": "0.20.0",
    "from_schema": 14,
    "to_schema": 17,
    "backup_created": true,
    "old_config_retained": true,
    "old_rewrite_undo_exact": true,
    "new_default_protocol": "small",
    "auto_chinese": true,
    "integrity_ok": true,
    "backup_opens_in_old_program": true
  }
}
```
