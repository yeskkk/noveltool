# NovelTool v0.6.0

**本地小说辅助程序 · 已完成 M0–M6 · 当前目标：TXT 导入与可追溯分块**

每个压缩包都是完整源码，不是补丁。直接安装最新版本即可，不需要依次安装旧版本。
README 解释整个程序的代码结构、运行流程、持久化边界与后续小说工作流；版本差异另见 CHANGELOG.md。

## 1. 当前能做什么，不能做什么

现在可以创建/重开 SQLite 项目，编辑模型地址、A/B/n、上下文参数，主要在内存工作；
配置默认每 60 秒批量保存，也支持手动保存和正常退出保存。
正文底层支持导入、追加、Unicode 选区替换、多步撤销、重开验证和不可变历史。
正文浏览器界面已经接通：导入 UTF-8 TXT、选中替换、按逻辑行选区、整篇编辑、追加、撤销、导出。
已可向用户配置的模型地址发送小型测试请求；不会自动把整部小说发送给模型。
已提供小段事实抽取诊断：解析、Schema 校验、引用检查、本地修复和最多两次模型格式修复。诊断结果不修改设定。
已可预览 TXT 编码、章节与分块计划，保存原始文件和带正文位置引用的分块；尚不自动分析全书。

**尚未实现：正式人物/世界设定数据库、全书事实归并、人物时间状态、上下文检索装配、
n 个续写候选、候选拼接确认、AI 返修和后续一致性检查。** 不要把界面中的 A/B/n 设置当作已经能生成小说。
当前手工编辑不受 A/B 限制。

## 2. 安装、创建项目和再次打开

目标环境是 Linux / Python 3.11+，本次实际测试环境见 TEST_REPORT.md。Linux 文件锁使用 flock；
没有完成 Windows 原生适配。页面和脚本全在本地，无 CDN、npm、Node.js、遥测或自动模型下载。

```bash
unzip noveltool-v0.6.0-M6-source.zip
cd noveltool-v0.6.0
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

python -m noveltool --project ../noveltool-projects/my_novel.sqlite3 \
  --create --title '我的小说'
```

在浏览器打开 `http://127.0.0.1:8765`。第一次安装需要取得依赖；安装后应用本身可离线运行，
模型请求是否联网取决于你配置的 API 地址。只运行不测试可以安装 `python -m pip install -e .`。

再次打开同一项目：

```bash
python -m noveltool --project ../noveltool-projects/my_novel.sqlite3
```

**再次打开不要加 `--create`**，程序不会覆盖已有文件；路径拼错也不会自动建库。
项目放在源码目录外，升级时只更换代码，不移动或删除小说数据库。终端按 Ctrl+C 正常退出。
关闭浏览器不等于关闭服务器。

```bash
python -m noveltool --project ../noveltool-projects/second.sqlite3 --create --init-only
python -m noveltool --project ../noveltool-projects/my_novel.sqlite3 --port 8877
python -m noveltool --version
python -m noveltool --help
```

不使用多个 worker 或自动 reload。应用只允许绑定回环地址，不提供账号系统，不能暴露公网。

## 3. 整体代码结构

```text
noveltool-v0.6.0/
  pyproject.toml             包版本、依赖、CLI 与静态资源/SQL 打包规则
  README.md                 本说明：整体结构和每一步工作原理
  CHANGELOG.md              各小版本变化
  TEST_REPORT.md            本版本实际测试、未验证范围
  requirements-tested.txt   本环境已测试的依赖版本参考
  noveltool/
    __init__.py             应用版本和当前小目标
    __main__.py             CLI：显式建库或打开、启动单进程服务
    app.py                  Web 生命周期、路由、Host/Origin/写令牌检查
    domain.py               严格 Pydantic 配置与元数据模型
    runtime.py              热数据、asyncio 锁、配置 dirty 集合、计时保存、正文提交协调
    db.py                   SQLite 连接、文件锁、加载、原子事务、失败回滚
    migrations.py           顺序 SQL 升级；升级前用 SQLite backup() 建可恢复备份
    manuscript.py           无损分段、Block/Span、字符计数、逻辑行到选区的映射
    revision.py             纯修改计划、持久化、历史块读取、逆向撤销
    manuscript_routes.py    正文请求模型、输入/选区校验和 HTTP 路由
    llm.py                  唯一模型 HTTP 适配层、错误分类、超时和完整响应检查
    llm_routes.py           测试请求与诊断接口；不自动提交正文
    llm_schemas.py          小段事实抽取的严格输出结构
    structured_llm.py       JSON 提取、重复键拒绝、受限修复、Schema/引用检查
    import_text.py          文本解码、规范化、章节预览
    import_service.py       内存预览、导入/计划事务、旧计划失效检查
    chunker.py              token 估计预算、段落切块、超长段落切片和重叠
    import_routes.py        导入预览、原始文件保留与分块计划保存
    sql/                    只包含已实现功能的逐版本 migration
    static/
      index.html, app.js    项目设置表单、内存/磁盘状态提示
      app.css               本地页面样式
      manuscript.html/js   正文阅读、选区、手工编辑、行范围、撤销和历史
      model.html/js        模型连接测试、结果与错误显示
      import.html/js       TXT 预览、编码、分块预算和计划详情
  tests/                    可重复的临时库、HTTP、算法、异常注入和回归测试
  tools/
    revision_demo.py       无模型、临时项目的导入→替换→撤销演示
    smoke_check.py         真正运行服务器的保存/重启/异常退出检查
```

没有 ORM、向量库、Agent 框架、云端服务或前端构建工具。浏览器使用原生 HTML/JavaScript，
数据在服务层处理，路由不拼 SQL。未来 TUI 可以复用 ProjectSession 和纯算法模块。

## 4. 启动时到底发生什么

1. `__main__.py` 解析参数。带 `--create` 才建立新项目；用排他建文件避免覆盖。
2. `ProjectStore.open()` 获取项目旁 `.lock` 文件上的操作系统锁；第二个进程会被拒绝。
3. 检查 SQLite application_id、schema 版本、元数据/配置、外键和完整性。未知新版本拒绝打开。
4. 老项目需要升级时先生成 `.before-schema-… .bak`，再在单个事务中顺序运行内置 SQL。
   备份通过 SQLite 的备份接口读取已提交状态，包括 WAL 内容，不直接复制活跃主文件。
5. 加载配置和**当前有效**正文块到内存。旧正文、完整修改历史按需读取，不全部常驻。
6. 验证当前块的顺序、内容哈希和最后一次 revision 的全文哈希；不一致时拒绝继续写作。
7. `ProjectSession` 创建 asyncio 锁和定时保存任务，FastAPI 开始响应浏览器。

升级是单向的：旧程序不能打开已经升级的新 schema。恢复旧版时先停止服务，再对备份文件的副本操作；
不要把旧 `.bak` 覆盖到仍运行的项目上。保留原库和备份，防止误删数据。

## 5. 配置从浏览器到磁盘的三步

**表单输入 → 点击应用后进入 Python 内存 → 到期/手工保存后进入 SQLite。**

浏览器提交 `expected_memory_version`。后端严格检查字段、类型、URL、A≤B 和上下文范围，
旧页面不能覆盖新配置。成功修改使 `data_version` 增加，并将 meta/config 标记为 dirty。
默认每 60 秒检查到期，把整个 dirty 批次放进一个 SQLite 事务；成功以后才清标记。
保存失败保留内存修改和错误信息，可重试。到期判断使用绝对时间截止值，避免浮点减法的边界误差。

当前配置包括 base URL、密钥环境变量名、写作/分析模型、A/B/n、温度、请求超时、上下文大小和保存周期。
API key 的值不进入配置表或日志。表单状态轮询不覆盖正在编辑的输入。
`data_version` 是项目数据变化计数，**不是**正文的 `revision_no`；二者分别用来检查不同操作的冲突。

## 6. 正文为什么能精确撤销

`Block` 有稳定 UUID，`text` 不可变。**文本块连同它后面的实际空白和分隔符一起保存**；
呈现全文只做 `''.join(block.text)`，不擅自插入/删除空行、缩进或末尾换行。

`split_text()` 支持 auto / blankline / line。auto 有空行时按空行分，否则按行分；
分块改变结构但不改变文本本身。`RenderedManuscript.spans` 记录每块的 Unicode code point 起止位置。
计数字数为去掉 Unicode 空白后的 code point 数，标点计数；它不是英文单词数，也不是 tokenizer。

### 替换流程：先计划，再提交，最后发布

1. 验证请求的 `expected_revision_no`；旧 revision 的选区直接拒绝。
2. 验证 `0 ≤ start ≤ end ≤ 全文长度`，空选区可插入，空替换文本可删除。
3. 计算受影响的首尾 Block；保留首块未选中的前缀和尾块未选中的后缀。
4. 用前缀 + 新文本 + 后缀产生新块。未涉及的块保持原 UUID，旧块内容不被覆盖。
5. 生成 `RevisionPlan`，包含旧/新块 ID、拼接位置、前后全文哈希、选区和修改说明。
6. 获取项目锁，在 **一个 SQLite 事务** 中一起保存待存配置、revision、块和当前顺序。
7. COMMIT 成功后才替换内存里的当前正文；失败时内存正文、磁盘正文和待存配置均不被误报为已提交。

SQL trigger 阻止修改/删除历史 Block 内容和 revision。块的 `seq` 非空表示当前有效，
`seq=NULL` 表示历史块；保存当前排序时先清活动序号再统一赋值，避免 UNIQUE 序号碰撞。
追加作为新段落加入，必要的分隔符归新块所有，Undo 会一并撤回；不会改写前一段的内容。

### 撤销流程

查找最近一条尚未被撤销的非 undo revision → 按需读取其旧块 → 校验当前块和全文哈希 →
生成一条 `kind=undo` 的逆向 revision → 原子提交 → 旧块重新激活、新块退出活动序列。
正文 revision 编号始终递增，即使内容回到旧状态也不倒退。
可以连续撤销，不会在“撤销上次撤销”之间反复切换；当前没有 redo、分支合并或任意历史 cherry-pick。

```bash
python tools/revision_demo.py
python -m pytest tests/test_manuscript.py tests/test_revision.py -q
```

## 7. 正文网页的操作流程

导航进入“正文”。空项目可导入 UTF-8 TXT，文件换行统一为 LF，UTF-8 BOM 被去掉；
除此之外不整理空格和空行。先预览导入文本再确认，已存在正文时禁止覆盖式导入。

上方正文框只读，选中内容后点击“用选区建立替换草稿”；也可输入起止**逻辑行号**。
逻辑行按真实 `\n` 计算，不随浏览器窗口宽度换行。中文扩展字、emoji 在 JavaScript 中可能占两个 UTF-16 单元，
脚本会把 selectionStart/End 转成 Unicode code point 再提交，避免切错位置。

下方草稿可自由编辑；“确认替换”才产生 revision 并立即写盘。整篇编辑会先转成最小连续变更区间，
尽量保留无关块身份。“追加为新段落”写入末尾，“撤销”恢复最近一次尚未撤回的修改。
“重新载入正文”不会自动丢弃未提交草稿，需要确认；关闭页面时浏览器会对未提交内容给出提示。
**未提交的浏览器草稿不会被服务器自动保存**，不要将状态栏的“正文已保存”理解成草稿也已保存。

后端还会核对选区原文与当前正文的切片，拒绝错误偏移；XSS 风险文本使用 textarea.value/textContent 呈现，
不会当作 HTML 执行。导出只输出已确认正文，UTF-8/LF，不包含草稿或历史。

## 8. 模型调用是怎样接入的

在“项目设置”填兼容服务的 base URL（通常结尾是 `/v1`）和模型名，并先应用配置。
密钥写在启动程序的终端环境变量里，例如：

```bash
export NOVELTOOL_API_KEY='你的密钥'
python -m noveltool --project ../noveltool-projects/my_novel.sqlite3
```

到“模型测试”发送一段短请求。`llm_routes.py` 先在锁内取得配置快照，随后释放锁；
`llm.py` 用 HTTPX 发送 POST `/chat/completions`，只使用 model/messages/temperature/max_tokens/stream=false。
不依赖 OpenAI SDK、tools、response_format 或供应商专属 JSON 模式。兼容服务不支持某个参数时明确报错，
不悄悄换模型或参数。请求不跟随重定向、不读系统代理，不把密钥发往重定向地址。

模型返回必须是正常 Chat Completions envelope，检查 choices、message.content 和 finish_reason。
截断、拒绝、空文本、HTTP 失败、超时和无法解析的响应分别反馈；不把部分截断当成功。
请求总超时、响应体积和同时运行请求数都有边界。本阶段默认串行，不对真实服务做隐藏自动重试。

调用完成后元数据/经密钥脱敏的日志先进入内存，按保存周期一起落盘；失败也留状态。
关闭日志选项会停止保存后续请求/响应正文，不自动清除过去日志。
调用日志不会包含 Authorization；远程服务器是否保存请求由该服务负责。
模型返回只显示在诊断页，**不会加入小说正文，也不会建立事实**。

## 9. 小模型结构化结果的检查流程

“模型测试”另有小段事实抽取诊断。程序临时给文本分配 B001 等引用；模型不能生成数据库 UUID。
这不是全书分析，诊断只验证当前模型是否能遵循格式。

1. 正常文本响应进入 `StructuredLLM`，先拒绝超过大小/深度边界的输出。
2. 优先解析完整 JSON；必要时用识别引号与转义的扫描器找到外层对象，识别 Markdown 包裹。
   重复 JSON 键、多份互相竞争的对象、截断响应都不能静默接受。
3. 原始 JSON 语法不合法时才尝试自有的受限修复器：只转换单引号字符串和删除尾逗号；不增加缺失值或括号。
   不依赖 `json-repair` 等额外包，本地修复结果仍需严格 Schema 检查，并标记修复来源。
4. Pydantic 使用 strict=True、extra=forbid、必需顶层字段。`{}` 不会被默认空数组伪装成“分析成功”。
5. 语义检查确认 evidence 只引用本次给过的 B 编号、证据摘录确实在该块、名字非空、长度/项目数有效。
6. 类型/额外字段等格式错误可最多调用两次“只修结构”的模型请求；缺失字段、超长列表和语义错误直接拒绝。只给原始输出、Schema 和错误，不再给整部小说。
   修复不能证明内容正确；无证据的实体、坏引用等语义错误直接失败，需要重新分析，而不是编造引用。
7. 返回校验后的对象、原始响应、修复标记和调用次数。失败保留错误，不更新正文或设定。

Schema 通过只说明结构/引用可用，**不证明文学理解或推断为真**。本地修复也可能改变含义，
因此所有修复结果明确显示“需人工核对”，不会悄悄成为正式设定。测试使用 FakeLLM/HTTPX MockTransport，
故障用例不依赖真实小模型响应是否碰巧合格。

## 10. 导入预览和分块怎样工作

“导入与分块”先接收 TXT 原始字节。自动模式遇到 UTF-8/UTF-16 BOM 使用对应编码，否则严格尝试 UTF-8；
没有 BOM 的 UTF-16、GB18030/GBK 等需手动选择。**有些旧编码字节同时也是有效 UTF-8，解码成功不代表判断正确**，
必须核对预览。程序不使用静默替换乱码的 `errors="replace"`。UTF-32、NUL、二进制控制字符拒绝导入。
文件上限 20 MiB、规范化正文上限 500 万 code point、分段上限 100000；不把上传的文件名当本地路径。
服务器最多保存两份未确认的内存预览，15 分钟过期；重启后预览丢失，需要重新上传。
预览只返回开头 6000 字符、最多 200 个章节标题候选；标题识别只是提示，不是强制章节结构。
原始字节与哈希保留在库里；正文只规范化换行/BOM，不抹掉有意义的空白。导入预览不改变正文。
确认导入以后生成 import revision，原始文件与正文在同一事务中保存。

分块按当前 revision 计算：Block 提供稳定引用，过长单段会切成带 block_id + code point 起止位置的片段；
优先在换行/句末切，必要时安全按字符切。每块区分“本块新内容”和“前块重叠上下文”，
让未来事实抽取只处理新内容，避免重复记事件。

预算把系统指令、Schema、输出和安全余量预留在外；每块文本按保守估计装入。
估计不是精确 tokenizer，不能宣称对所有模型绝不超限；未来接入本地 tokenizer 可以替换估计器。
`PlanSettings` 默认目标块 6000、重叠 400、提示词预留 4096、输出预留 1536。
可用块上限是 `min(目标块, floor(context_window × safety_ratio) - 提示预留 - 输出预留)`。
每个片段用 UTF-8 字节数加 24 的引用开销，每块再加 64 的封装开销；核心、重叠都计入预算。
提示词预留目前是人工指定的预算，不是已经组装的实际分析 prompt；未来 M7 发请求前仍须重新核算完整 messages。
`make_plan()` 按块与句末边界构造核心切片，再取前一核心的后缀作重叠；
`verify_plan()` 验证核心按正文顺序无遗漏/无重复、引用和哈希正确、重叠紧邻且不超预算。
最多 4096 个 chunk、100000 个切片，超限明确报错，不静默截断小说。
计划记录所依据的正文 revision、预算和片段引用。正文或模型上下文预算改变后旧计划标记过期，不用于分析。
SQLite 只保存切片引用/偏移和预算 JSON，不为每个 chunk 复制一份全文。当前计划启动时加载，旧计划按需读库。派生计划损坏时停止使用并提示重建，不阻止打开已确认正文。
原始导入文件作为冷数据按需从库里读取，下载时验证原始字节哈希。正文页旧式直接粘贴/读取文件不会补造历史原始字节。
网页只展示前 200 块详情，其中前 80 块包含短摘录；计划接口/数据库保留全部分块。
本阶段只建立可恢复的计划，不调用模型分析全书，不提供事实归并。

### 一次导入到分块的调用链

`import.js` 发送原始字节 → `import_routes.preview` → `decode_import` 严格解码/规范化 →
`ImportService.remember` 暂存在内存并返回预览编号 → 用户确认 → `RevisionEngine.import_text` 生成修改计划 →
`ProjectSession._commit_snapshot_locked` 同时保存原始字节、revision、Block 和已有待存配置/日志 →
成功后发布内存正文 → 用户请求计划 → `make_plan` / `verify_plan` → 原子保存 `chunk_plans`。

主要接口：`POST /api/import/preview`（原始二进制 body；filename/encoding/mode 是 query）、
`POST /api/import/commit`（preview_id 和 expected_revision_no）、`POST /api/import/plan`（正文版本和 settings）、
`GET /api/import/plan`、`GET /api/import/sources`、`GET /api/import/sources/{id}/raw`。
所有写接口仍需当前会话令牌。预览和诊断不污染正文，只有明确确认才进入版本历史。

```bash
python tools/chunk_demo.py       # 自造文本，无模型；验证核心切片无损覆盖
python tools/structured_demo.py  # 好/坏 JSON、本地修复与证据引用拒绝
python tools/llm_smoke.py        # 真 HTTPX 请求到临时模拟端点，不是真实小模型
```

## 11. 整部小说助手的工作流程与边界

以下是整体目标，不表示后面几步已经实现：

| 步骤 | 怎样运作 | 当前状态 |
|---|---|---|
| 建立项目 | 校验配置→SQLite→内存项目→定时保存 | 已实现 |
| 管理正文 | 无损 Block→选区→Revision→原子提交→撤销 | 已实现 |
| 导入准备 | 保留原作→规范化换行→分段/分块→可追溯引用 | 以本文开头当前能力为准 |
| 小模型边界 | 普通文本调用→结构化解析/校验→失败有界处理 | 以本文开头当前能力为准 |
| 全书分析 | 各 chunk 提取 Observation→名字消歧→合并事实与事件 | 尚未实现 |
| 设定编辑 | 人工事实锁定，AI 提议不覆盖人工锁定项 | 尚未实现 |
| 按位置取状态 | 依据叙述位置和故事时间，区分当时状态与后来得知的事实 | 尚未实现 |
| 上下文装配 | 当前要求/邻近正文/相关设定/摘要按 token 预算取舍 | 尚未实现 |
| 续写 | A/B/n 任务快照→n 次独立纯文本生成→候选 | 尚未实现 |
| 确认候选 | 用户拼草稿→检查 base revision→提交正文 | 尚未实现 |
| 同步设定 | 只分析已确认文字；失败保留正文并标记待同步 | 尚未实现 |
| AI 返修 | 指定范围及前后文→候选→确认→旧证据失效→局部重分析 | 尚未实现 |
| 一致性检查 | 新旧事实差异与后文摘要比对，只提示，不自动改正文 | 尚未实现 |

尤其不能把不可靠叙述、倒叙中的过去状态或未来获知的信息简单按“最后出现”覆盖；
这一时间语义问题留到 StateReducer 阶段专门设计和测试，当前没有伪装成已实现的状态系统。

## 12. 数据、安全和恢复

SQLite 使用 WAL、foreign_keys=ON、synchronous=FULL。配置按周期写；确认正文/撤销立即事务保存。
一次正文确认也会顺带保存已在内存里的配置，但不保存尚未发送的浏览器输入。
文件系统故障、断电、强制终止仍有风险：未写盘的内存修改可能丢失；不承诺绝对零丢失。

进程锁是建议锁，只约束遵守协议的程序。不要在服务运行时用外部编辑器直接改库；不要放在网络共享盘。
不要只复制运行中的 `.sqlite3` 主文件当完整备份，应停止应用后复制，或使用 SQLite 备份工具。
`.lock` 文件保留是正常的，进程退出时系统锁自动释放；不要运行中删除它。

HTTP 只接受本地 Host，写操作需要会话令牌且检查 Origin，不开启宽泛 CORS。
这不是密码登录，无法防止同机恶意程序；也不允许通过反向代理暴露公网。
升级/保存失败保留旧库，界面错误不能当作确认成功。磁盘不足时先解决存储问题，再重试。

## 13. 测试与可复现验收

```bash
python -m pytest -q -W error
python tools/revision_demo.py
# 安装 coverage 后可另外运行覆盖率，不是正常运行依赖：
python -m coverage run --source=noveltool -m pytest -q
python -m coverage report -m
```

测试覆盖配置严格校验、锁与冲突、故障回滚、升级前备份、随机替换/逐字撤销、历史不可变、
UTF-16/code point 边界、HTTP 写令牌以及已实现的模型/分块错误路径。
详细数量和哪些浏览器/真实模型未实测见 TEST_REPORT.md；测试通过不等于文学质量已经验证。

## 14. 本次小目标

本次完成 **M6：TXT 导入与可追溯分块**。后续目标继续逐个验收、各自打完整源码包。
