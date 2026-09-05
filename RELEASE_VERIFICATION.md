# NovelTool v0.17.0 发布验收

## 版本与环境

完整源码：v0.17.0 / M17。项目 Schema：14。运行环境与实际依赖版本见 verification/environment.json；应用版本、数据库版本、测试环境版本是三个独立概念。

## 重现命令

```bash
python -m pytest -q -W error
python -m coverage run --source=noveltool -m pytest -q -W error
python -m compileall -q noveltool
for f in noveltool/static/*.js; do node --check "$f"; done
python -m pip wheel . --no-deps --no-build-isolation -w /tmp/noveltool-wheels
python -m pip install --no-deps --target /tmp/noveltool-installed /tmp/noveltool-wheels/noveltool-0.17.0-py3-none-any.whl
# 需要额外的 playwright 和 Chromium；桥接限制在脚本与测试报告中明确说明。
python tools/browser_smoke.py --browser /usr/bin/chromium --output /tmp/noveltool-ui-check
# --old-source 为此前发布的 v0.13.0 完整源码目录。
python tools/release_smoke.py --installed /tmp/noveltool-installed --old-source /path/to/noveltool-v0.13.0 --output /tmp/noveltool-release-check
```

## 实际完成

完整源码重新解压/复制后 330 项测试通过。wheel 的静态页面、JavaScript、CSS 和 14 个 SQL 脚本均实际载入；独立进程升级、旧备份重开、候选/草稿保留、撤销、真实模型 HTTP 测试桩、退出/强制终止及备份下载均验证。浏览器验收真实执行 JS，但网络经 Python 测试桥接，不宣称原生 CSP/网络完全验证。

原始机器可读验收记录位于 verification/。TEST_REPORT.md 说明覆盖范围和未验证部分。README.md 解释整体结构、每条流程及保存/恢复边界；CHANGELOG.md 单独记录版本差异。

## 包内容与排除

源码包含应用、测试、SQL、HTML/JS/CSS、可选验收脚本和文档。排除个人项目数据库、API key、虚拟环境、构建产物、Python 缓存、浏览器截图及字体文件。ZIP 是独立完整版本，不要求先安装旧包。SHA256 校验文件与下载包同时提供。

数据库升级单向。运行新版本前停止旧进程；升级备份自动生成，常规备份可通过维护页下载。不能把升级后的数据库直接交给旧程序；回退使用备份副本的新路径。工具中所有测试项目和临时模型服务均为可丢弃测试资料，不接触个人小说。
