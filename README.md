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

项目采用 `.env` 文件管理模型及搜索 API 的配置，可在 `config.py` 中定义提示词模板和 Hook。常见的可配置项包括：

- `allow_clarification`: 是否在研究前澄清问题范围。
- `search_api`: 选择使用的搜索服务或禁用搜索。
- `models`: 研究、压缩、终稿三个阶段的模型配置，可根据成本和效果自定义。
- `concurrency` 与 `max_iterations`: 控制并发数量和最大迭代次数。

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
