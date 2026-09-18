# 飞柚 GEO 辅助工具（geo-query-agent）

面向 B 端客户的 GEO/SEO 分析与交付平台，单 FastAPI 应用、无前端框架（页面 HTML/JS 内嵌在 `main.py`）。提供三种分析方法与一套官网诊断引擎：

| 方法 | 做什么 | 产出 |
|------|--------|------|
| GEO搜索问句挖掘 | 挖掘目标客群各决策阶段的高频搜索问句与意图 | 报告 json + html |
| 客户市场调研分析 | 联网多轮采集 + 交叉验证 + 工商数据核验，输出企业画像、调研报告、情报简报（deck/brief）、组织架构实名图 | 报告 json + html |
| 网站 SEO/GEO 诊断 | 对目标站点做 66 项检测（技术可抓取、AI 友好度、内容质量、权威信任、商业转化），出带评分与问题卡的人工报告 | 报告 json + html + 页面快照 |

三种方法共享：大模型客户端、工商 API 客户端、企业信息档案库、异步任务管理器。

---

## 1. 目录总则：代码区 vs 数据区

2026-09-16 起实行目录重组，**唯一原则**：

> **根目录 = 代码/配置/文档，发版可整体覆盖服务器；`var/` = 运行数据，是服务器资产，发版、上传、重部署永远不碰。**

所有数据落盘路径只允许定义在一个文件：`paths.py`。改数据位置只改它。

```
geo-query-agent/
├── paths.py                 # 全部可变数据路径的唯一来源（输出→var/）
├── main.py                  # FastAPI 服务端 + CLI 入口 + 全部管理页面渲染（单文件大模块）
├── methods.yaml             # 分析方法注册表：每个方法 = 一组 skill + 输入表单（管理端可编辑）
├── config.yaml              # 运行配置：大模型、网页抓取器、服务端口、输出开关（含密钥，勿外传）
├── run.sh                   # 启动脚本（macOS 下自动 caffeinate 防休眠）
├── requirements.txt         # Python 依赖
├── Dockerfile / docker-compose.yml   # 容器化部署（compose 把 ./var 挂到宿主机）
│
│  ── 核心引擎 ──
├── agent.py                 # 通用编排器：加载 skills/*.yaml，按 order 执行（single/parallel/iterate/iterate_parallel）
├── pipeline.py              # 问句挖掘 8 步管线编排
├── profile_pipeline.py      # 客户市场调研的企业画像三层数据管线（查询分解→证据池→汇聚→机检→补查）
├── llm_client.py            # 大模型客户端（百炼，联网/思考双开关，token 用量统计）
├── web_fetcher.py           # 网页全文抓取层（重试 + UA 池 + 内存缓存，补百炼搜索只有摘要的短板）
├── business_api_client.py   # 工商数据 API 客户端（惊贤所；详情/模糊搜/股东/分支机构，响应落盘缓存）
├── org_graph.py             # 组织架构实名图：纯工商 API 构建 org.json，0 次大模型调用
├── company_cache.py         # 企业信息档案库（SQLite）：画像/挖掘/诊断产出统一沉淀，字段级合并
├── company_ui.py            # 企业资料页渲染（列表+详情，含企业性质单标签推导）
├── audit_company_bind.py    # 官网诊断 → 企业身份精确绑定（站内全称提取→工商核验→唯一才绑，绝不猜绑）
├── task_manager.py          # 异步任务管理：后台执行、断线恢复、状态查询（任务记录落 var/tasks/）
├── prompts.py / reference_data.py   # 提示词与参考数据
│
│  ── 诊断引擎 ──
├── site_audit/              # SEO/GEO 检测引擎（core 检测项实现、report 渲染、runner 编排）
│
│  ── 可编辑的"内容配置"（注意：在代码区，人工改过要 git 提交，见 §5 发版） ──
├── skills/                  # 16 个方法步骤定义 yaml（挖掘 8 步 + 调研 8 模块）+ mr_report_template.html 报告版式；管理端可在线编辑，直接写回本目录
├── references/              # 检测项组合规则表 yaml
├── priority_overrides.json  # 检测项优先级人工覆盖（诊断引擎读写，当前为空）
│
│  ── 文档与记录 ──
├── docs/                    # 规格与规则文档：报告展示规则、检测逻辑清单、V6 修订落地、项目铁律清单等（改对应功能前先读）
├── versions/                # 版本说明，每发一版一个 `<版本号>.md`（Version 格式 YY.MDDDi.N：年后两位+月日当日序号连写+累计发布次数，如 26.09163.6），记录当版全部功能变更
├── AGENTS.md                # 66 个检测项人工核验清单（对话纪律也在其头部）
│
├── scripts/                 # 运维/回灌/自检脚本，用法见 §6
├── archive/                 # 旧版代码与模板备份（不参与运行，git 不跟踪）
├── geo-audit-report-brand/  # 报告品牌设计稿（Figma 导出物，仅参考）
│
└── var/                     # ★ 全部运行数据，见 §2，git 与发版均不触碰
```

## 2. 系统怎么运转：取数与执行链路

### 2.1 企业基本信息取数——一律"先本地档案库，缺了才调工商接口"

三个页面对新客户/已有客户取企业基本信息，共享同一个档案库（`var/data/company_info.db`），但取数行为各有分工：

| 页面 | 读档案库 | 调工商接口的条件 | 接口返回后 |
|------|----------|------------------|------------|
| 客户市场调研 | 建画像前必查：库里有核心字段且不点"强制刷新"就直接复用（报告标注"历史沉淀+日期"） | 库内有效核心字段 < 5 个，或用户勾强制刷新 | **34 个原始字段全量入库**（字段级合并：存在即更新、不存在即创建），再参与画像合并 |
| GEO搜索问句挖掘 | 跑分析前把库内已知事实（基本信息+挖掘板块）注入提示词做校准 | **从不调工商接口**，也不写工商字段 | ——（产出写挖掘板块表） |
| 官网诊断 | 诊断完成后先拿站内公司全称/品牌名/别名/曾用名匹配档案库，命中即绑（免接口） | 本地比不中才查接口：现名精准查 → 模糊搜 → 曾用名桥接（字符集合等价比对，消化"（深圳）↔深圳市"形态差）→ 再核验 | 绑定成功即全量字段入库，站点品牌名增量并入 brands/aliases |

### 2.1b 企业性质 = 档案存储字段 nature（单一事实源）

判定规则只有一份：`company_cache.derive_nature()`（互斥优先级：上市→国有→港澳台→外资→集团→员工规模十档→未标注）。**结论随每次档案写入自动重算并存储**（upsert_info 咽喉点保鲜），画像卡片、企业资料页、三页分布图一律读存储值，禁止再写第二套规则。规格与验收样例：`docs/specs/SPEC-company-nature-unify.md`。

红线：诊断绑定**宁可不绑绝不错绑**——唯一核验通过才自动绑；多候选/英文站落候选，页面红标「企业待绑」人工选定（人工提交仍过工商核验）。

### 2.2 深度分析的执行载体——三个页面各不相同，别按统一心智找代码

| 页面 | 深度分析由什么驱动 | 代码位置 |
|------|--------------------|----------|
| GEO搜索问句挖掘 | **skills 配表驱动**：`methods.yaml` 注册 8 步，`skills/*.yaml` 定义每一步，`agent.py` 按 order 编排执行 | agent.py + pipeline.py + skills/ |
| 客户市场调研 | **Python 硬编码管线**：画像三层构建（查询分解→证据池→汇聚→机检→补查）、组织图纯接口 0 大模型、基本信息字段合并全在代码里；skills 只负责报告/简报/幻灯片的**版式渲染**，不产出分析内容 | profile_pipeline.py + org_graph.py + audit_company_bind.py；渲染层 skills/15、16 |
| 网站 SEO/GEO 诊断 | **独立检测引擎**（66 检查项规则+AI 判定），与 skills 体系完全无关 | site_audit/ |

一句话交接：**改"分析什么/怎么判"看管线代码，改"报告长什么样"才碰 skills 配表和模板。**

### 2.3 模块独立性铁律（2026-09-17 定版）

每个分析方法有自己的**独立执行链**：任务入口按方法分发到各自的执行器——问句挖掘 `_run_mining_task`（只做：档案已知事实注入 → skills 8 步 → 挖掘报告渲染），市场调研 `_run_research_task`（完整链：建档校验 → 调研 skills → 画像合并 → 组织图 → 简报机检 → 三页签渲染）。**禁止把 A 方法的环节挂进 B 方法的链**（曾发生过：调研的建档校验/组织图误挂挖掘，造成挖掘集体失败）。新增方法 = 注册新执行器 + 入口加一行分发 + 自己的渲染模板。

---

## 3. 动态数据存储清单（var/ 下）

运行时会产生/变化的数据全部在 `var/`。**备份/迁移 = 整包拷 var/。**

| 存储 | 位置 | 作用 | 写入方 |
|------|------|------|--------|
| 问句挖掘报告 | `var/output/问句挖掘_*.json/.html` | 挖掘方法交付物，json 是数据源、html 是渲染件 | pipeline + main |
| 市场调研报告 | `var/output/市场调研_*.json/.html`（含 `_deck`/`_brief` 简报件） | 调研交付物；简报按页专属版式 | profile_pipeline / agent |
| 官网诊断报告 | `var/output/audit/*.json/.html` | 每站点每次诊断一份，文件名含域名+时间戳；企业绑定结果写回 json | site_audit/runner |
| 诊断页面快照 | `var/output/audit/snapshots/<站点_时间>/pages.json` | 诊断当时各页面实际抓到的正文/结构，**存量报告回绑企业靠它**，删了老报告就无法再自动绑定 | site_audit/core |
| 企业信息档案库 | `var/data/company_info.db`（SQLite） | 全系统企业身份与工商事实的唯一沉淀处，表见 §4 | company_cache |
| 任务记录 | `var/tasks/<id>.json` | 每个后台任务的状态/进度/耗时，重启后可查历史、断线可恢复 | task_manager |
| 工商接口缓存 | `var/api_cache/*.json` | 工商 API 原始响应留档，重跑报告免重复计费查询 | business_api_client |
| 对话会话 | `var/output/chat_sessions/*.json` | GEO 问答页的会话持久化 | main |
| 服务日志 | `var/log/server.out` | 进程 stdout/stderr 重定向（以 shell 重定向方式启动服务时写入） | 操作系统 |

另：token 用量统计只在大模型客户端内存里，重启即失，不属持久存储。

## 4. 档案库 `company_info.db` 表结构

| 表 | 主键 | 存什么 |
|----|------|--------|
| `company` | 企业身份 id（全称归一/信用代码对齐） | 企业全量档案（工商字段 + 画像字段 + brands/aliases/history_names），data 为 JSON 文本，字段级合并、非空覆盖、首次信息来源只记一次 |
| `mining` | 同上 | 问句挖掘板块对该企业的产出 |
| `audit_snapshot` | 域名_时间 | 诊断结果按站点的摘要行 |
| `op_log` | 自增 id | **操作日志**：各页面的关键操作事件（时间、页面、动作、品牌、详情），管理端有查询接口 |

## 5. 两种"日志"的区别

- **操作日志**（`op_log` 表）：业务语义，记录"哪个页面干了什么"，入库可查，是审计与排障第一现场。
- **系统日志**（`var/log/server.out`）：进程输出的流水文本，排障看异常栈用；体积大即轮转/清空，无结构无查询。

## 6. 运行与部署

```bash
# 本地
pip install -r requirements.txt        # 依赖：fastapi/uvicorn/httpx/pyyaml/jinja2/requests/bs4/lxml
./run.sh --mode server                 # 起服务，端口 8777（config.yaml server.port）
./run.sh --brand "好莱客"               # CLI 单次执行

# 服务器（Docker）
docker compose up -d --build
# 关键：compose 已把 ./var:/app/var 挂载，容器重建数据不丢；镜像强制 TZ=Asia/Shanghai（否则报告时间差 8 小时）
```

`config.yaml` 四段：`llm`（百炼 base_url/模型/联网与思考开关/并发上限）、`web_fetcher`（抓取开关与缓存 TTL）、`server`（host/port）、`output`（json/html 落盘开关）。

**发版纪律**：
1. 整体覆盖只允许动根目录代码，**永远不上传/不覆盖 `var/`**。
2. `skills/`、`methods.yaml`、`priority_overrides.json`、`skills/mr_report_template.html` 位于代码区但**管理端可在线改**——服务器上人工改过而未 git 提交的，发版会被冲掉。发版前先确认服务器与仓库一致，有差异先拉回提交。

## 7. scripts/ 脚本速查

| 脚本 | 用途 |
|------|------|
| `backfill_company.py` | 存量市场调研报告回灌企业档案库（网页「JSON数据补录入库」同款逻辑的命令行版；预览/`--apply`） |
| `backfill_company_raw.py` | 档案库逐家补齐工商全量原始字段（预览/`--apply`） |
| `backfill_audit_company.py` | 存量诊断报告一次性回补企业绑定（读快照→工商核验→唯一才绑；预览/`--apply`，多候选落人工） |
| `regen_report_from_json.py` | 用已存报告 json 重渲染 html（模板改版后批量重出，不重跑分析） |
| `rerun_profiles.py` / `rerun_mr_deck.py` | 重跑画像 / 重出简报（版式迭代时用） |
| `patch_task_method.py` | 给旧任务记录补方法字段 |
| `check_*.py` / `light_scan.py` | 自检：组织架构图、渲染回归、页面覆盖、流程视图、轻量扫描 |

在线绑定与回灌是同一套代码（`audit_company_bind.bind_report`），新诊断自动触发，存量报告必须手动跑回灌一次。

## 8. 文档地图（忘了先看这里）

- 改报告展示 → `docs/报告展示规则.md`（定版话术与卡片规则）
- 检测项口径/人工核验 → `AGENTS.md` + `docs/检测逻辑清单.md` + `docs/修订规格-V6落地.md`
- 企业绑定/档案设计 → `docs/specs/SPEC-audit-company-bind.md`、`docs/specs/SPEC-org-api.md`、`docs/工作计划-企业信息获取流程重构.md`
- 每版改了什么 → `versions/*.md`（按日期升序即演进史）
- 不做的事/红线 → `docs/项目铁律清单.md`
