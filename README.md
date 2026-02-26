
# OpenDeepResearch

**Tagline:** An interactive deep-research agent you can steer *mid-run* — inject instructions (steer), trigger interruption + replanning, prevent silent drift, and save time & tokens.

## Executive Summary
- Not a “prompt once → wait for a black-box report” workflow. You can intervene, correct, and re-plan while the run is executing.
- **ReAct-style graph orchestration:** `check_clarity` clarifies scope first; `supervisor` routes decisions; `planner` updates the plan; `researcher` runs ReAct loops for search/read/synthesize; `reporter` produces a deliverable `final_report`.
- Fully observable: **event-sourced runs (SQLite)** + **WebSocket** streaming tokens and step logs for replay and postmortems.
- Output-oriented: each run produces a **global `final_report`** (not just stepwise answers) while preserving structured evidence trails.

## Problem Statement
Traditional deep-research agents can be strong on raw output quality, but they often break down in real usage:

- **Uncontrollable process:** once it starts, you can only wait — small early misunderstandings get amplified into the final report.
- **Hard to correct:** new constraints or changed requirements usually mean abort-and-rerun, burning time and tokens linearly.
- **Not auditable:** it’s difficult to answer “where did this conclusion come from?”, “why these sources?”, or “which step introduced drift?”
- **Hard to operationalize:** without observability, replay, and evaluation hooks, iteration and benchmarking are painful.

**OpenDeepResearch** aims to turn “fire-and-forget research” into a **steerable, traceable, replayable research workflow**.

## Differentiators & Value Proposition
- **Steerable research (human-in-the-loop):** inject constraints/preferences/bans/replanning instructions while the run is live; the `supervisor` evaluates and applies them to future behavior.
- **Interruption Gateway:** incoming steer triggers `interrupt_requested`; the agent pauses and returns to `supervisor` instead of continuing to waste tokens down the wrong path.
- **Auditable + replayable runs:** event sourcing (`runs + events + steer_commands`) persisted to SQLite enables replay, comparisons, evaluation, and tuning.
- **More stable via multi-node graph:** `supervisor` decides and routes; `planner` decomposes and revises; `researcher` executes ReAct retrieval and evidence structuring; `reporter` writes the final deliverable — reducing single-agent runaway risk.
- **Retrieval quality controls:** query expansion, multi-provider aggregation + dedup, trusted-domain boosting, structured evidence, outline→report, self-checks (gaps / counterexamples / uncertainty).
- **Real-time observability:** token + step logs streamed via WebSocket; the UI shows timeline, role outputs, and a `final_report` panel.

## Highlights & Use Cases
### Traditional deep research vs OpenDeepResearch

| Dimension | Typical deep research | OpenDeepResearch |
|---|---|---|
| Interaction | One-shot prompt → one-shot output | **Steer mid-run**, interrupt + replan |
| Controllability | Low (black-box until the end) | High (human-in-the-loop steering via `supervisor`) |
| Observability | Output visible, process opaque | Token stream + step logs + timeline (WebSocket) |
| Audit / Replay | Weak or none | SQLite event sourcing: `runs/events/steer_commands` |
| Source quality | Mostly model-dependent | Expansion, dedup, trusted boosts, optional rerank, policy filters |
| Output format | Long single response | Deliverable **`final_report`** (global synthesis) + evidence trails |
| Best for | Quick one-off queries | Competitive/market research, diligence, tech selection, policy/lit reviews, team knowledge capture |

### Typical Scenarios
- **Research that must be corrected mid-run:** add new constraints (must cite certain sources / must show uncertainty / ban low-signal sites).
- **Team replay & postmortem:** replay the event stream to review evidence and improve strategies.
- **Prototype a controllable research pipeline:** minimal backend + UI that you can productize later.

## Quickstart

### Requirements
- Python 3.10+ (recommended 3.11)
- A working LLM key, plus at least one search / custom tool API key
- Virtualenv recommended

### Install & Run (macOS / Linux)
```bash
# Clone the repository
git clone https://github.com/DesignOps6ix9/OpenDeepResearch.git
cd OpenDeepResearch

# Create a virtual environment and install dependencies
python -m pip install -U pip uv
uv venv
source .venv/bin/activate
uv sync

# Configure environment variables
cp .env.example .env
# Edit .env and fill in your model key(s) and search/tool key(s)

# Start the local Agent service (with built-in LangGraph UI)
uvx --refresh --from "langgraph-cli[inmem]" --with-editable . --python 3.11 langgraph dev --allow-blocking
```

### Install & Run （Windows） 
```bash
git clone https://github.com/DesignOps6ix9/OpenDeepResearch.git
cd OpenDeepResearch

python -m pip install -U pip uv
uv venv
.venv\Scripts\Activate.ps1
uv sync

Copy-Item .env.example .env
# Use a text editor to fill in the keys in .env

uvx --refresh --from "langgraph-cli[inmem]" --with-editable . --python 3.11 langgraph dev --allow-blocking
```

## Run Example

After the service starts, open `http://127.0.0.1:2024/docs` in your browser to view the API documentation.

In the LangGraph Studio input box, enter a question such as:

> Please research: “What do the RACE and FACT frameworks in DeepResearch Bench evaluate?”, and output a summary with citations.

The system will automatically:
- Clarify the question scope
- Generate a research brief
- Collect sources via search and custom tools
- Compress and synthesize findings
- Output a structured report with traceable citations

You can inject preferences (e.g., preferred sources, citation format, or additional constraints) at different stages to dynamically adjust the research path.

---

## Configuration & Extensions

### Configuration Options

#### 1) Required (the system cannot run without these)

| Variable | Description | Default |
|---|---|---|
| `LLM_BASE_URL` | LLM gateway URL (OpenAI-compatible) | `https://ark.cn-beijing.volces.com/api/v3` |
| `LLM_API_KEY` | LLM API key | (empty) |
| `LLM_MODEL_SUPERVISOR` | Supervisor model | `doubao-seed-1-6-flash-250828` |
| `LLM_MODEL_PLANNER` | Planner model | `doubao-seed-1-6-flash-250828` |
| `LLM_MODEL_RESEARCHER` | Researcher model | `doubao-seed-1-6-flash-250828` |
| `RESEARCH_MODEL_TEMPERATURE` | Temperature for research | `0` |

#### 2) Strongly recommended (determines retrieval/reading quality)

| Variable | Purpose |
|---|---|
| `TAVILY_API_KEY` | Semantic search |
| `SERPER_API_KEY` | Broad Google-style search |
| `FIRECRAWL_API_KEY` | Stable webpage content extraction |
| `JINA_READER_API_KEY` | Fallback content extraction |
| `COHERE_API_KEY` | Reranking results (rerank) |
| `COHERE_RERANK_MODEL` | Rerank model (default `rerank-v3.5`) |

#### 3) Optional enhancements

| Variable | Purpose | Default |
|---|---|---|
| `CROSSREF_BASE_URL` | Academic metadata source | `https://api.crossref.org` |

#### 4) Source policy configuration

| Variable | Description |
|---|---|
| `DEFAULT_ALLOWED_DOMAINS` | Default allowlist domains (comma-separated; empty = no filtering) |
| `DEFAULT_BLOCKED_DOMAINS` | Default blocklist domains (default: `reddit.com,quora.com`) |
| `TRUSTED_DOMAINS` | Trusted domains boost list (comma-separated) |

#### 5) Retrieval scale / performance configuration

| Variable | Description | Default |
|---|---|---|
| `SEARCH_MAX_RESULTS_PER_QUERY` | Max results per query | `5` |
| `MAX_SOURCES_PER_STEP` | Max candidate sources kept per step | `6` |
| `MAX_READ_SOURCES_PER_STEP` | Max sources to read per step | `4` |
| `CROSSREF_MAX_RESULTS_PER_QUERY` | Crossref max results per query | `4` |

#### 6) Compatibility aliases (choose either)

| Primary variable | Alias |
|---|---|
| `LLM_BASE_URL` | `ARK_BASE_URL` |
| `LLM_API_KEY` | `ARK_API_KEY` |
| `LLM_MODEL_SUPERVISOR/PLANNER/RESEARCHER` | `ARK_MODEL` (overrides all three) |
| `JINA_READER_API_KEY` | `JINA_API_KEY` |

#### 7) Runtime configuration (API request parameters)

| Endpoint | Field | Description |
|---|---|---|
| `POST /api/runs` | `goal` | Research objective (required) |
| `POST /api/runs` | `max_steps` | Max iterations (1–50, default 4) |
| `POST /api/runs` | `config` | Extra runtime config (stored in run record) |
| `POST /api/runs/{run_id}/steer` | `content` | Mid-run steering content |
| `POST /api/runs/{run_id}/steer` | `scope` | Steering scope (default `global`) |

#### 8) Minimal viable `.env` (example)

```env
LLM_BASE_URL=your_gateway_url
LLM_API_KEY=your_llm_key
LLM_MODEL_SUPERVISOR=your_model_id
LLM_MODEL_PLANNER=your_model_id
LLM_MODEL_RESEARCHER=your_model_id
RESEARCH_MODEL_TEMPERATURE=0
```




# OpenDeepResearch

一句话定位：研究过程可交互的深度研究 Agent——运行中随时注入指令（steer），触发中断与重规划，避免黑盒跑偏，节省 token 与时间。

执行摘要：
- 不是“输入一次 → 等结果”的黑盒研究：你能在研究过程中介入、纠偏、重规划。
- ReAct 驱动的多节点编排：check_clarity 先做问题澄清；supervisor 负责统筹决策；planner 负责拆解与更新计划；researcher 用 ReAct 执行检索/阅读/归纳；reporter 汇总为可交付的 final_report。
- 全程可观测：事件溯源（SQLite）+ WebSocket 实时推送 token 与步骤日志，支持回放与复盘。
- 产出导向：run 结束自动生成 final_report（全局汇总，而非单步答案），并保留结构化证据线索。

## 核心问题陈述

传统 deepresearch 在“效果”上往往很强，但工程落地时常见痛点是：

- 过程不可控：一旦开始就只能等待，细节偏差会被放大到最终报告。
- 不可纠偏：需求变化/补充约束只能中断重跑，时间与 token 成本线性上升。
- 不可审计：你难以追踪“某个结论来自哪里”“为什么选择这些来源”“哪一步引入偏差”。
- 难工程化：缺少可观测、可回放、可复盘的运行记录，难以做迭代与评测。

OpenDeepResearch 的核心目标是：把 deepresearch 从“fire-and-forget 黑盒”变成“可被操控、可追溯、可复盘的研究流程”。

## 创新点与价值主张

- 深度研究过程可控制（steer）：运行中可随时注入约束/偏好/禁止项/重规划指令；由 supervisor 评估后真正改变后续行为。
- 中断网关（Interruption Gateway）：steer 到达即触发 interrupt_requested，研究员即时暂停并回到 supervisor，避免“跑偏继续跑”造成浪费。
- 可审计与可回放：事件溯源（runs + events + steer_commands）落盘到 SQLite，便于复盘、对比、评测与调参。
- 多节点协作更稳：supervisor 做决策与路由；planner 做拆解与计划更新；researcher 以 ReAct 执行搜索与证据整理；reporter 负责最终成稿，降低单体 Agent 失控概率。
- 检索增强与质量策略：query expansion、多 provider 聚合去重、可信域名加权、结构化证据、outline → report、自检（缺失点/反例/不确定性）。
- 实时可观测：WebSocket 推送 token 与步骤日志；前端提供 timeline、角色输出与 final_report 面板。

## 功能亮点与使用场景

### 传统 deepresearch vs OpenDeepResearch（对比表）

| 维度 | 传统 deepresearch（常见形态） | OpenDeepResearch |
|---|---|---|
| 交互模式 | 一次性提问 → 一次性输出 | 运行中可 steer 注入指令；支持即时中断与重规划 |
| 过程可控性 | 低：更像“黑盒跑完再看” | 高：人在回路，supervisor 审核 steer 并影响后续行为 |
| 可观测性 | 结果可见、过程不可见 | token 流 + 步骤日志 + timeline；WebSocket 实时推送 |
| 可审计/复盘 | 多数不支持或很弱 | SQLite event sourcing：runs/events/steer_commands 可回放 |
| 资料质量控制 | 依赖模型自觉 | query expansion、去重、可信域名 boost、低信号域名默认屏蔽、可选 rerank |
| 输出形态 | 常见为“单次长回答” | 自动生成 final_report（全局汇总），更像可交付研究简报 |
| 适用场景 | 快问快答、一次性研究 | 竞品/行业研究、投资/采购尽调、技术选型、政策/学术综述、团队知识沉淀 |

### 典型使用场景

- 需要“边跑边改”的研究：中途新增约束（例如必须覆盖某些来源/必须输出不确定性/禁止某类网站）。
- 团队研究复盘：把一次研究过程沉淀为可回放事件流，方便复查证据链与改进策略。
- 研究管线原型：快速搭建“可控 deepresearch”的后端与最小前端，用于后续产品化。

## 快速开始

### 环境要求

- Python 3.10+（推荐 3.11）
- 准备一个可用的 LLM Key，并配置至少一种搜索或自定义工具的 API Key
- 推荐使用虚拟环境管理依赖

### 安装与运行（macOS/Linux）

# OpenDeepResearch

## 项目定位
可配置、可审计、可交互的深度研究 Agent。通过实时注入提示词与工具、动态规划搜索路径，让研究过程透明可控，生成结构化、可追溯的高质量报告。

## 背景与问题
传统 Deep Research 工作流往往一跑就是几十分钟，用户只能等待黑盒输出。一旦模型对早期模糊指令的理解出现偏差，长周期的研究就会跑偏，既浪费时间又消耗大量 token。更糟的是，任务开始后无法纠偏，修改需求只能停止并重来。

OpenDeepResearch 希望解决这些痛点：提供一个可观察、可调节、可扩展的研究工作流，用户可以在运行过程中注入新的指令或工具提示，实时重规划研究路径，保障结果的准确性和复现性。

## 核心特性

- 可审计工作流：将研究过程拆解为清晰阶段（澄清 → 简报 → 研究 → 压缩 → 终稿），每一步都有日志，可查看和复用。
- 提示词可注入：用户可以在关键节点向 Agent 注入附加约束，如指定来源、格式规范、合规要求等，使结果更贴近业务需求。
- 工具可插拔：支持接入多种搜索 API 和自定义工具，既可以使用通用搜索，又可以接入内部知识库、爬虫或文档解析服务。
- 人在回路：研究前自动澄清问题范围，运行中提供人工纠偏窗口，避免因为早期指令不准确而导致严重偏差。
- 可评测与可复现：支持固定配置进行基线评测，帮助您迭代优化研究流程并与其他 Agents 对比。

## 快速开始

### 环境要求

- Python 3.10+（推荐 3.11）
- 准备一个可用的 LLM Key，并配置至少一种搜索或自定义工具的 API Key
- 推荐使用虚拟环境管理依赖

### 安装与运行（macOS/Linux）

```bash
# 克隆仓库
git clone https://github.com/DesignOps6ix9/OpenDeepResearch.git
cd OpenDeepResearch

# 创建虚拟环境并安装依赖
python -m pip install -U pip uv
uv venv
source .venv/bin/activate
uv sync

# 配置环境变量
cp .env.example .env
# 编辑 .env，填入模型 key 和搜索/工具 key

# 启动本地 Agent 服务（内置 LangGraph UI）
uvx --refresh --from "langgraph-cli[inmem]" --with-editable . --python 3.11 langgraph dev --allow-blocking

```
### 安装与运行（Windows PowerShell）

```powershell
git clone https://github.com/DesignOps6ix9/OpenDeepResearch.git
cd OpenDeepResearch

python -m pip install -U pip uv
uv venv
.venv\Scripts\Activate.ps1
uv sync

Copy-Item .env.example .env
# 使用文本编辑器填入 .env 中的 key

uvx --refresh --from "langgraph-cli[inmem]" --with-editable . --python 3.11 langgraph dev --allow-blocking

```

### 运行示例

启动服务后，打开浏览器访问 `http://127.0.0.1:2024/docs` 查看 API 文档。  
在 LangGraph Studio 的输入框中输入问题，例如：

> 请研究“DeepResearch Bench 的 RACE 与 FACT 框架分别评估什么”，并输出带引用的总结。

系统会自动澄清问题范围，生成研究简报，调用搜索和自定义工具收集资料，压缩整理结果，并最终输出结构化报告和可追溯引用。您可以在不同阶段注入偏好来源、引用格式或其他限制，从而动态调整研究路径。

## 配置与扩展

可配置项清单

## 1) 必填（没有就无法正常跑研究）

| 变量名 | 说明 | 默认值 |
|---|---|---|
| `LLM_BASE_URL` | LLM 网关地址（OpenAI-compatible） | `https://ark.cn-beijing.volces.com/api/v3` |
| `LLM_API_KEY` | LLM 密钥 | 空 |
| `LLM_MODEL_SUPERVISOR` | supervisor 模型 | `doubao-seed-1-6-flash-250828` |
| `LLM_MODEL_PLANNER` | planner 模型 | `doubao-seed-1-6-flash-250828` |
| `LLM_MODEL_RESEARCHER` | researcher 模型 | `doubao-seed-1-6-flash-250828` |
| `RESEARCH_MODEL_TEMPERATURE` | 研究温度 | `0` |

## 2) 强烈建议（决定检索/阅读质量）

| 变量名 | 用途 |
|---|---|
| `TAVILY_API_KEY` | 语义搜索 |
| `SERPER_API_KEY` | Google 广度检索 |
| `FIRECRAWL_API_KEY` | 网页正文抽取（稳定） |
| `JINA_READER_API_KEY` | 正文抽取回退 |
| `COHERE_API_KEY` | 结果重排（rerank） |
| `COHERE_RERANK_MODEL` | 重排模型（默认 `rerank-v3.5`） |

## 3) 可选增强

| 变量名 | 用途 | 默认值 |
|---|---|---|
| `CROSSREF_BASE_URL` | 学术元数据源 | `https://api.crossref.org` |

## 4) 来源策略配置

| 变量名 | 说明 |
|---|---|
| `DEFAULT_ALLOWED_DOMAINS` | 默认允许域名（逗号分隔，空表示不过滤） |
| `DEFAULT_BLOCKED_DOMAINS` | 默认屏蔽域名（默认 `reddit.com,quora.com`） |
| `TRUSTED_DOMAINS` | 可信域名加权名单（逗号分隔） |

## 5) 检索规模/性能配置

| 变量名 | 说明 | 默认值 |
|---|---|---|
| `SEARCH_MAX_RESULTS_PER_QUERY` | 每个 query 的最大检索条数 | `5` |
| `MAX_SOURCES_PER_STEP` | 每轮最多保留多少候选来源 | `6` |
| `MAX_READ_SOURCES_PER_STEP` | 每轮最多阅读多少来源 | `4` |
| `CROSSREF_MAX_RESULTS_PER_QUERY` | Crossref 每 query 返回上限 | `4` |

## 6) 兼容别名（可二选一）

| 主变量 | 兼容别名 |
|---|---|
| `LLM_BASE_URL` | `ARK_BASE_URL` |
| `LLM_API_KEY` | `ARK_API_KEY` |
| `LLM_MODEL_SUPERVISOR/PLANNER/RESEARCHER` | `ARK_MODEL`（会同时覆盖三者） |
| `JINA_READER_API_KEY` | `JINA_API_KEY` |

## 7) 运行时可配置（API 请求参数）

| 接口 | 字段 | 说明 |
|---|---|---|
| `POST /api/runs` | `goal` | 研究目标（必填） |
| `POST /api/runs` | `max_steps` | 最大轮次（`1-50`，默认 `4`） |
| `POST /api/runs` | `config` | 额外运行配置（会写入 run 记录） |
| `POST /api/runs/{run_id}/steer` | `content` | 中途干预内容 |
| `POST /api/runs/{run_id}/steer` | `scope` | 干预范围（默认 `global`） |

## 8) 最小可用 `.env`（示例）

```env
LLM_BASE_URL=你的网关地址
LLM_API_KEY=你的LLM密钥
LLM_MODEL_SUPERVISOR=你的模型ID
LLM_MODEL_PLANNER=你的模型ID
LLM_MODEL_RESEARCHER=你的模型ID
RESEARCH_MODEL_TEMPERATURE=0
```


欢迎通过编写自己的工具函数接入内部知识库、API 或爬虫，以满足特定业务需求。

## 贡献指南

我们鼓励社区共同完善 OpenDeepResearch。如果您发现 bug、希望新增功能或改进文档，请参考以下流程：

1. 提交 Issue：先查找是否有类似问题，若不存在，请按模板提供重现步骤、期望行为、实际表现以及环境信息。
2. 提交 Pull Request：Fork 仓库，在分支中进行修改，小步提交，完成后打开 PR 并简要说明改动动机、实现方式及影响。
3. 代码规范：保持一致的代码风格，建议使用 `ruff`、`mypy` 等工具进行格式化和静态检查。新增功能请编写最小测试，修复 Bug 时补充回归测试。
4. 讨论与反馈：欢迎在 Discussions 中分享使用经验或提出功能建议。

## 许可证

本项目采用 MIT License。这意味着您可以自由使用、复制、修改和分发本项目代码，但须保留版权声明和许可条款。

## 致谢

- 感谢社区中其他开源 Deep Research 项目的启发，例如一些项目的 README 采用了“简洁介绍 + 清晰分步说明 + 可运行 Quickstart”的结构，用户上手成本更低，也更利于传播。
- 感谢所有贡献者提供的改进建议和实践经验。



