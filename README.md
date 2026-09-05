# NovelTool v0.1.0

**本地小说辅助程序 · M1：SQLite 项目存储与内存运行底座**

这个项目按小目标逐步实现。当前已经完成 M0 和 M1：可以启动本地网页、创建和打开项目、
修改项目配置、每分钟批量保存、手动保存，以及正常退出后重新打开。

**当前还不能导入小说、编辑正文、分析人物或生成续写；也没有发出任何模型 API 请求。**
模型名、A/B/n 和上下文大小在这一版只是已经可以保存的配置，不代表相应功能已经实现。

每个发行包都是完整源码快照，不是补丁。`v0.1.0` 已包含 `v0.0.1` 的基础功能，直接使用最新包即可。

## 1. 安装和第一次启动

要求 **Linux、Python 3.11+**。当前实际验证的是 Linux / Python 3.13.5，其他 Python 小版本
尚未在本轮实测。Windows 原生版本没有实现文件锁适配；可以考虑 WSL，但本轮未实测 WSL。

先解压，并进入能看到 `pyproject.toml` 的源码根目录：

```bash
unzip noveltool-v0.1.0-M1-source.zip
cd noveltool-v0.1.0

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

python -m noveltool \
  --project ../noveltool-projects/my_novel.sqlite3 \
  --create \
  --title '我的小说'
```

在浏览器打开 `http://127.0.0.1:8765`。要停止程序，回到启动它的终端按 `Ctrl+C`。

`../noveltool-projects/` 放在源码目录之外，是为了以后更换版本时不误删创作数据。
首次创建会建立缺失的父目录。这个路径只是示例，也可以换成自己的绝对路径。

首次安装需要能够取得 Python 依赖。依赖安装完成以后，**这一版本的运行不需要联网**：
网页资源全部随包提供，没有 CDN、遥测、模型下载或后台更新检查。
只运行而不测试，可以把安装命令改为 `python -m pip install -e .`。

### 再次打开同一个项目

```bash
python -m noveltool --project ../noveltool-projects/my_novel.sqlite3
```

**不要再加 `--create`。** 程序会拒绝覆盖已经存在的文件。
打开时路径拼错也不会悄悄创建一个新数据库，而是明确报错。

### 其他启动方式

```bash
# 只创建数据库，不启动网页服务
python -m noveltool --project ../noveltool-projects/second.sqlite3 \
  --create --title '第二个项目' --init-only

# 改用另一个本地端口
python -m noveltool --project ../noveltool-projects/my_novel.sqlite3 --port 8877

python -m noveltool --help
python -m noveltool --version
```

`--title` 只用于新项目。已有项目可以在网页里改名。
CLI 只允许绑定 `127.0.0.1` 或 `::1`，不允许 `0.0.0.0`。
不要用多个 Uvicorn worker、自动 reload 或多个进程同时打开同一个项目。

## 2. 在页面里怎样操作

页面刻意区分三个阶段：

| 阶段 | 内容在哪里 | 怎样进入下一阶段 |
|---|---|---|
| 修改表单 | 只在当前浏览器页面 | 点击对应的“应用配置到内存”或“更新名称到内存” |
| 应用到内存 | 本地 Python 进程的项目状态 | 等自动保存，或点击“立即写入磁盘” |
| 已写入磁盘 | SQLite 事务已成功提交 | 正常退出后可以重新打开 |

推荐先修改 A、B、n，点击“应用配置到内存”，观察状态变成“待写入磁盘”，
然后点击“立即写入磁盘”，确认状态变为“已保存到磁盘”。
重启程序后检查同样的配置是否仍在。

默认自动保存间隔是 60 秒，可在页面调整为 10～3600 秒。实际执行由事件循环调度，
不是实时系统的严格截止时间。如果磁盘故障或服务卡住，不能保证一分钟之内完成保存。

“从服务器重新载入表单”读取的是**服务器当前内存**，不是强制重新读取磁盘。
状态轮询只更新状态栏，不会自动覆盖你正在输入的表单。
有两个浏览器页面时，旧页面的提交会返回冲突提示，而不是静默覆盖新修改。

关闭浏览器不会停止 Python 服务。正常退出保存也只能保存已经提交到服务器内存的内容，
**不能保存尚未点击“应用”的浏览器输入**。

## 3. 整体代码结构

```text
noveltool-v0.1.0/
├── pyproject.toml              包元数据、依赖、安装入口、SQL/静态资源打包规则
├── README.md                   当前文件：代码结构、完整流程与使用说明
├── CHANGELOG.md                小版本更新记录
├── TEST_REPORT.md              此次发行的实际测试结果和未验证事项
├── requirements-tested.txt     本轮测试所用的依赖版本参考
├── LICENSE                     MIT 许可证
├── noveltool/
│   ├── __init__.py             运行时版本号
│   ├── __main__.py             CLI：解析参数、创建项目、启动单进程服务
│   ├── app.py                  Web 生命周期、HTTP 路由、本机请求保护
│   ├── domain.py               Pydantic 配置/元数据模型和请求模型
│   ├── runtime.py              内存项目、版本检查、dirty 集合、自动保存
│   ├── db.py                   SQLite 创建/加载/事务写入、进程锁
│   ├── sql/
│   │   └── 001_initial.sql     只包含当前已经实现的两张表
│   └── static/
│       ├── index.html         设置页面，明确显示当前阶段和保存状态
│       ├── app.js             获取配置、提交表单、状态轮询、冲突提示
│       └── app.css            本地样式和窄屏布局
├── tests/
│   ├── conftest.py             临时数据库、Web 客户端、写操作令牌 fixture
│   ├── test_domain.py          类型、范围、URL、非法字段与只读模型测试
│   ├── test_db.py              建库、重开、锁、SQL 约束、回滚与版本冲突
│   ├── test_runtime.py         内存修改、dirty、自动保存、失败后重试与退出
│   ├── test_app.py             HTTP 输入校验、状态、保存、安全边界测试
│   └── test_cli.py             命令行参数与创建/启动约束
└── tools/
    └── smoke_check.py          真正启动子进程，通过 HTTP 验证保存、重开与强制退出
```

没有提前建立 LLM、正文、分析器等空模块。当前运行依赖只有 FastAPI、Uvicorn、Pydantic
以及它们的依赖；SQLite、锁、计时器、UUID 等使用 Python 标准库。
当前页面使用原生 HTML/JavaScript，尚不需要引入 Jinja2 或 HTMX。

## 4. 每一步怎样运行：启动到内存就绪

```text
python -m noveltool
    → __main__.main()
    → 校验 CLI 参数
    → 如有 --create：创建并验证新项目，然后关闭建库连接
    → app.create_app(project_path)
    → Uvicorn 启动一个进程
    → FastAPI lifespan 进入
    → ProjectStore.open() 获取项目文件锁并打开 SQLite
    → 校验数据库标识、schema 版本、完整性及配置
    → ProjectSession 加载 ProjectData，建立 RuntimeProject
    → 启动一个 asyncio 自动保存任务
    → 开始处理浏览器请求
```

### 4.1 CLI 不负责日常业务

`__main__.py` 只解析路径、端口、新建参数，随后交给应用工厂。
导入 Python 包不会自动启动服务，也不会打开或改写数据库。

创建操作使用“只允许新建”的文件打开模式，已有文件绝不被覆盖。
新数据库文件权限设为 `0600`；已有父目录的权限不会被程序擅自修改。
如果端口被占用，已经成功创建的项目仍然保留，修正端口后不带 `--create` 重新打开即可。

### 4.2 项目锁覆盖整个运行期

`db.py` 中的 `ProjectLock` 使用标准库 `fcntl.flock`。
锁的意义不是“当前这一条 SQL 不能并发写”，而是“这个项目只能有一个内存所有者”。[5]

否则两个进程可以各自持有旧内存，轮流把旧数据覆盖回同一个数据库。
因此程序从打开项目到退出，一直持有独占锁。

锁文件可能保留为 `my_novel.sqlite3.lock`。**文件存在不等于锁仍被占用。**
进程退出后操作系统会释放锁；正常释放时不删除这个文件，避免删除再创建造成锁竞争。
不要在程序运行时手工删除 `.lock` 文件。

### 4.3 不信任“文件后缀是 sqlite3”

打开已有项目时检查：SQLite `application_id`、`user_version`、`quick_check`、
元数据单例、配置所属项目、外键和 Pydantic 业务规则。
普通文本、别的程序的数据库、无法识别的 schema 版本都会被拒绝。
不会把陌生数据库原地初始化成 NovelTool 项目。

### 4.4 载入后的读路径

`ProjectData` 包含两部分：`ProjectMeta` 和 `ProjectConfig`。
二者都是经过验证的只读 Pydantic 模型，修改时创建新对象，不在未知位置偷偷改字段。[6]

`RuntimeProject` 再附加运行期状态：已保存版本、dirty 分类和最后一次保存错误。
状态查看、配置查看和配置修改都通过这份内存数据完成，**不会为每次页面轮询查询 SQLite**。
这一点有 SQL trace 自动化测试覆盖。

## 5. 每一步怎样运行：修改配置

```text
浏览器表单
    → PUT /api/config
    → 本机 Host / Origin / 会话令牌检查
    → ConfigUpdate 与 ProjectConfig 严格校验
    → ProjectSession 获取 asyncio.Lock
    → 检查 expected_memory_version
    → 创建新的内存 ProjectData
    → 内存 data_version 加 1，标记 meta/config 为 dirty
    → 释放锁，返回“已更新到内存”
```

`domain.py` 不允许把字符串 `"600"` 默默转成数字，也不允许未定义字段。
会检查 A ≥ 1、B ≥ A、n 的范围、上下文参数、非有限浮点数、URL 和环境变量名。
校验失败返回 HTTP 422，不改变内存，也不写数据库。

没有实际变化的提交不会递增版本，也不会增加 dirty 项。
修改项目名称走同样的流程，只标记 `meta` 为 dirty。

### 三个版本概念不要混淆

| 名称 | 含义 |
|---|---|
| 程序版本 `0.1.0` | 当前源码发行版 |
| 数据库 `schema_version=1` | 数据表结构版本 |
| `memory_version` / `saved_version` | 当前配置的内存编辑序号 / 已持久化序号 |

配置编辑序号**不是**后续正文的 `revision_no`。
例如内存版本 3、磁盘版本 1，表示已有两次内存修改尚未一起提交。

## 6. 每一步怎样运行：批量写入 SQLite

以下操作会要求保存：自动保存到期、页面点击“立即写入磁盘”、正常退出。

```text
ProjectSession.save() 或 autosave_tick()
    → 获取内存锁
    → 如果 dirty 为空：直接返回，不执行写 SQL
    → ProjectStore.flush()
        → BEGIN IMMEDIATE
        → 用项目 ID 和 expected_version 检查磁盘版本
        → 更新项目元数据
        → 如果 config 为 dirty，再更新配置
        → COMMIT
    → 只有 COMMIT 成功，才推进 saved_version 并清除 dirty
    → 释放锁，返回保存状态
```

SQLite 写入使用参数绑定，不把用户名称、模型名拼成 SQL。
字段列名来自代码里固定的模型字段集合，不来自 HTTP 输入。

任何一条 SQL 失败都会回滚这个批次。测试通过数据库触发器故意让第二个更新失败，
验证第一个更新也没有留在磁盘上。
失败时内存数据、dirty 标记和旧的 `saved_version` 都保留，页面显示保存错误。
自动保存任务不会因此永久退出，会在后续周期重试。

### SQLite 设置

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

这里把前期草案的 `NORMAL` 收紧为 `FULL`：本项目本来就批量低频写入，因此优先考虑
已提交事务的持久性。WAL 模式下 `FULL` 会在每次提交后增加一次 WAL 同步；
`NORMAL` 在断电或系统崩溃时可能丢失最近已提交的事务。[3][4]
这不是对所有硬件、文件系统或供电故障的绝对保证。

SQLite 连接只在创建它的线程使用，没有关闭 `check_same_thread` 检查。
当前 async 路由、自动保存和短事务都在同一个事件循环线程执行。[2]
因为只有很小的配置写入，这一版允许短暂占用事件循环；数据库等待锁时最多可能受
`busy_timeout` 影响。以后若写入量变大，需要让专用工作线程自己持有连接，不能随意把现有
连接塞进 `asyncio.to_thread()`。未来的网络模型请求不得持有项目内存锁。

## 7. 自动保存和退出的详细边界

自动保存任务每秒检查一次单调时钟。达到当前配置的保存周期时，集中处理所有 dirty 分类。
更改保存周期后，下一个计时检查就采用新周期。
手动保存会重置下一次自动保存的计时基点。
没有修改时不会仅为“刷新保存时间”而制造数据库事务。

正常退出顺序：通知计时任务停止 → 等待任务结束 → 保存剩余 dirty 数据 → 关闭连接 → 释放项目锁。
生命周期使用 FastAPI 的 lifespan，而不是在模块导入时创建后台任务。[1]

**延迟写盘意味着未保存内存不是持久化数据。** `kill -9`、掉电或进程崩溃可能丢失上次成功保存
以后的修改。真实进程测试明确验证了这一点：强制终止后能重开最后已提交的版本，但未保存的
新名称没有保留。

如果退出时磁盘仍无法写入，程序会记录退出保存错误，不能把这次退出当成成功保存。
**本版没有应急恢复文件或恢复未写盘内存的能力。** 保存失败时应先保持服务运行，检查磁盘空间、
权限或外部修改，成功重试后再退出；必要时先手动记录页面上的配置。

## 8. 数据库实际包含什么

当前只有两张业务表，没有提前创建十几张尚未实现的小说表。

| 表 | 主要内容 | 为什么这样存 |
|---|---|---|
| `project_meta` | 单例 ID、标题、schema 版本、数据版本、创建/修改/保存时间 | 标识项目并控制保存版本 |
| `project_config` | API 地址、密钥环境变量名、模型名、A/B/n、上下文和保存参数 | 每个配置字段可单独通过 SQLite CHECK 约束 |

UUID 由 Python 生成。时间以带时区的 ISO 8601 字符串保存，程序生成的时间采用 UTC；
页面显示时转换成浏览器所在时区。

`PRAGMA user_version` 和元数据中的 `schema_version` 都要求等于当前支持的版本。
这一版遇到其他版本会拒绝打开，不执行猜测性迁移。
**下一小目标新增表时再增加真正的迁移，并测试旧项目升级，不预先堆放空表。**

### 主要配置及默认值

| 配置 | 默认值 | 当前校验与作用 |
|---|---|---|
| `api_base_url` | `http://127.0.0.1:8000/v1` | http/https 地址；不允许凭据、查询参数、片段或空白 |
| `api_key_env` | `NOVELTOOL_API_KEY` | 只保存环境变量名，不保存或读取实际密钥 |
| `writer_model` / `analysis_model` | 空字符串 | 表示未配置；M1 不调用模型 |
| `context_window` | 20000 | 2048～1000000；尚未实现 token 预算计算 |
| `context_safety_ratio` | 0.85 | 大于 0 且不超过 1 |
| `min_chars` / `max_chars` | 600 / 1000 | 正整数，B ≥ A；当前只是配置 |
| `candidate_count` | 4 | 1～32，防止误填非常大的请求数量 |
| `writer_temperature` / `analysis_temperature` | 0.7 / 0.1 | 0～10；是否被具体模型支持要到 API 层检查 |
| `api_timeout_seconds` | 600 | 1～86400；尚无网络请求 |
| `autosave_seconds` | 60 | 10～3600，当前实际生效 |
| `retain_llm_logs` | true | 未来日志开关；当前没有模型日志 |

Pydantic 负责类型和跨字段业务规则，SQLite CHECK/外键负责额外的存储约束。
**SQLite 不会修复模型写错的 JSON。** 模型输出的提取、修复、Schema 校验和语义校验属于 M5，
本包没有安装 `json-repair`，也没有用空实现假装完成它。

## 9. HTTP 接口和冲突处理

| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/` | 本地设置页 |
| GET | `/health` | 进程健康状态与软件版本，不等同于“磁盘已保存” |
| GET | `/api/session` | 当前进程的写操作令牌 |
| GET | `/api/status` | 项目、内存/磁盘版本、dirty 和最后保存错误 |
| GET | `/api/config` | 当前配置、标题和内存版本 |
| PUT | `/api/config` | 校验后整体替换配置，只改内存 |
| PUT | `/api/project` | 修改标题，只改内存 |
| POST | `/api/save` | 立即写盘；没有修改时不重复写 |

写请求必须带 `X-Noveltool-Token`，修改请求必须带 `expected_memory_version`。
`PUT /api/config` 的 `config` 是**整体替换语义**，不是局部 PATCH；省略字段会取模型默认值。
调用者应先 GET 当前完整配置，再修改需要的字段并整体提交。

可以用标准库直接操作 API，不必额外安装 requests：

```python
import json
from urllib.request import Request, urlopen

base = 'http://127.0.0.1:8765'

def get(path):
    with urlopen(base + path, timeout=5) as response:
        return json.load(response)

token = get('/api/session')['csrf_token']
data = get('/api/config')
config = data['config']
config.update(min_chars=800, max_chars=1400, candidate_count=3)
body = json.dumps({
    'expected_memory_version': data['memory_version'],
    'config': config,
}).encode('utf-8')
request = Request(base + '/api/config', data=body, method='PUT', headers={
    'Content-Type': 'application/json',
    'X-Noveltool-Token': token,
})
with urlopen(request, timeout=5) as response:
    print(json.load(response))  # 此时只是更新到内存。

request = Request(base + '/api/save', method='POST', headers={
    'X-Noveltool-Token': token,
})
with urlopen(request, timeout=5) as response:
    print(json.load(response))  # written=true 才表示这次确实提交了写入。
```

常见错误：400 为非法 Host；403 为跨来源或令牌问题；409 为旧页面编辑冲突；
422 为输入格式/业务校验错误；503 为存储错误。
外部程序改变数据库版本导致的保存冲突也属于 503，内存修改不会因此自动清除。
不支持使用外部 SQLite 编辑器同时修改项目；版本检查不能识别所有绕过程序的任意 SQL 修改。

## 10. 本地安全和备份注意事项

程序没有账户系统，回环地址和会话令牌不能替代多用户认证。
Host 检查、Origin 检查、写操作令牌和不开放 CORS 用于降低浏览器跨站误操作风险；
拥有同一台机器访问权限的其他程序仍可能调用本地接口。

页面使用 `textContent` 显示服务端文字，不把项目名当 HTML 插入。
静态脚本和样式均来自本程序，设置了基本的 CSP、禁止嵌入和禁止缓存响应头。

实际 API key 不存入项目。`api_key_env` 是为 M4 准备的配置约定，M1 连该环境变量的值都不读取。
也不要把 API key 填进模型名或标题等普通文本字段；这些普通字段当然会照常保存。

请把项目放在普通本地磁盘，不要放在网络文件系统或正在同步的目录里同时跨机器打开。
WAL 有自己的文件和共享内存要求。[4]
运行时可能看到 `.sqlite3-wal`、`.sqlite3-shm`，不要随意删除。
没有在线备份功能时，先确认保存成功并正常关闭程序，再复制项目文件；
不要在运行中只复制主数据库并假定它包含了 WAL 中的所有更新。

## 11. 怎样测试、验证和定位问题

```bash
# 自动化单元/集成测试，不调用任何模型
python -m pytest -q

# 真实子进程与 HTTP 检查，只使用临时目录，不碰你的项目
python tools/smoke_check.py
```

第一组测试包括配置验证、建库重开、SQL 约束、整个批次回滚、进程锁、内存读路径、
失败保留 dirty、自动保存重试、旧页面冲突和 HTTP 安全边界。
定时逻辑测试通过注入单调时钟值完成，不需要每个测试等待一分钟。

第二组会启动真实服务器，将测试项目的自动保存间隔设为 10 秒，实际等待自动保存发生，
再验证重复进程被拒绝、SIGINT 退出保存、SIGKILL 后只能恢复最后已提交版本。
脚本中的强制终止仅针对它自己创建的临时测试子进程。

`TEST_REPORT.md` 列出真实结果。不能把这些存储测试当成模型效果测试。
此次环境的 Chromium 阻止访问本地测试服务器，未完成真实浏览器交互验收；
已做 HTTP 页面/资源测试和 JavaScript 语法检查，但没有把它们冒称为浏览器点击测试。

`requirements-tested.txt` 记录本轮使用的依赖版本，便于复现问题，不是跨平台永久锁文件：

```bash
python -m pip install -r requirements-tested.txt
python -m pip install -e . --no-deps --no-build-isolation
```

遇到问题先检查终端输出和页面保存状态。此阶段没有复杂的文件日志系统。
`/health` 正常只说明服务在响应；请看 `/api/status` 的 `dirty` 和 `last_save_error` 判断保存状态。

## 12. 全程序最终会有哪些流程

以下是整体路线图，除“项目存储”外均未在这个版本实现。

### 原作入口

导入 TXT → 保留来源并拆成正文块 → 按上下文预算切分析块 → 调用模型提取事实 →
本地解析/修复/类型/引用校验 → 暂存 observation → 消歧与归并 → 用户查看和修改基础设定。

### 想法入口

记录创意 → 模型提出人物、地点、关系和风格建议 → 用户审阅/修改 → 建立初始设定。
尚未写进正文的计划不能假装成已经发生的剧情事件。

### 续写流程

确定写作位置和用户指令 → 检索相关状态与摘要 → 在输入/输出预算内组装上下文 →
独立请求 n 个候选 → 按规则计数字数 → 用户选择或拼装草稿 → 确认正文 revision →
立即保存已确认正文 → 再分析新增正文并同步设定。
未确认候选不进入小说事实；同步分析失败不能撤销用户已经确认的正文。

### 返修流程

把行范围/选区映射到稳定文本锚点 → 加入选区前后文和修改位置的状态 →
生成多个替换候选 → 用户确认 → 建立新 revision、退役旧正文块 →
使受影响的自动分析失效 → 局部重分析 → 提示可能受影响的后文，不自动连锁改写。

### 开发顺序与验收点

| 小目标 | 交付内容 | 状态 |
|---|---|---|
| M0 | 可启动/关闭的本地服务与健康检查 | v0.0.1 已完成 |
| M1 | SQLite、内存配置、dirty、自动/手动/退出保存 | v0.1.0 已完成 |
| M2 | 不可变正文 Block、选区映射、追加/替换、精确 Undo | 下一目标 |
| M3 | TXT 导入与最小手工正文 WebUI | 未实现 |
| M4 | 最薄的 OpenAI-compatible 模型客户端 | 未实现 |
| M5 | 结构化输出提取、修复、类型/语义校验、有限重试 | 未实现 |
| M6 | 导入来源、章节识别、分析分块与覆盖检查 | 未实现 |
| M7 | 单分析块提取与 observation | 未实现 |
| M8 | 全书分析、恢复进度、归并与消歧 | 未实现 |
| M9 | 设定和时间线编辑界面 | 未实现 |
| M10 | 从想法建立初始设定 | 未实现 |
| M11 | 状态归约与有预算的上下文构建 | 未实现 |
| M12 | 多候选续写与字数检查 | 未实现 |
| M13 | 拼装草稿、版本检查、确认正文 | 未实现 |
| M14 | 正文确认后的设定同步 | 未实现 |
| M15 | AI 范围返修与派生数据失效处理 | 未实现 |
| M16 | 后续一致性提示 | 未实现 |
| M17 | 更完整的任务恢复、导出与收尾 | 未实现 |

M2 会先测试“原文 → 替换 → Undo → 逐字恢复”，不会提前接模型。
新增正文表必须同时提供从 M1 数据库升级的迁移和测试，不能要求用户删除已有项目重建。

## 13. 相比设计草案，已经明确的实现取舍

只有四项有意收紧：只创建已实现的两张表；保存使用 WAL + FULL；密钥只配置环境变量名；
M1 页面暂用原生 HTML/JS，避免安装尚不需要的 UI/LLM 依赖。

内存读取、一分钟批量保存、确认内容的强保存边界、后续不可变正文块、模型输出隔离校验、
逐目标验收的总体方向没有改变。

## 14. 实现参考资料

这些是实现时核对的官方资料；具体程序行为仍以本包代码和测试结果为准。

[1] FastAPI lifespan 与生命周期测试：
https://fastapi.tiangolo.com/advanced/events/
https://fastapi.tiangolo.com/advanced/testing-events/

[2] Python sqlite3：连接线程限制、显式事务与标准库接口：
https://docs.python.org/3/library/sqlite3.html

[3] SQLite PRAGMA，特别是 synchronous、foreign_keys、user_version 和 application_id：
https://sqlite.org/pragma.html

[4] SQLite WAL：同步策略、WAL/SHM 文件与文件系统限制：
https://www.sqlite.org/wal.html

[5] Python fcntl / flock：
https://docs.python.org/3/library/fcntl.html

[6] Pydantic strict mode：
https://pydantic.dev/docs/validation/latest/concepts/strict_mode/
