# DeepDeepResearch (M2 + M3 Skeleton)

这个仓库现在实现了可运行的 `M2 + M3` 最小闭环：

- M2: 多角色协作（`supervisor -> planner -> researcher`）
- M3: 检索增强（query expansion / 去重 / 高质量来源优先 / 结构化证据 / 自检）
- 运行中可注入 steer，且 steer 会被 supervisor 审核并真正改变后续行为
- run 结束自动生成 `final_report`（全局汇总，不是仅单步输出）
- ReAct 风格执行（Researcher: Thought -> Action -> Observation）
- Interruption Gateway（用户 steer 到达后即时中断并回到 Supervisor）

## 当前能力

- 后端循环：`collect_steer -> supervisor -> planner -> researcher -> decide_next`
- 事件溯源：`runs` + `events` + `steer_commands`
- 实时可观测：WebSocket 推送 token 与步骤日志
- 中途干预：steer 到达即触发 `interrupt_requested`，Researcher 会即时暂停并跳回 Supervisor
- 质量策略：
  - query expansion
  - 多 provider 检索结果聚合与去重
  - 可信来源优先（trusted domain boost）
  - 结构化 notes（claim / evidence / citations）
  - outline -> report
  - 自检（missing points / counterexamples / uncertainties）

## 技术栈

- 后端：FastAPI + asyncio
- 存储：SQLite（event sourcing）
- 前端：原生 HTML/JS（MVP）
- LLM：OpenAI-compatible Chat Completions（已适配 ARK）
- Search：Tavily / Serper / Crossref
- Reader：Firecrawl / Jina Reader
- Rerank：Cohere Rerank

## 目录结构

```text
backend/
  app/
    config.py      # 环境变量与默认策略
    db.py          # SQLite event store
    llm.py         # OpenAI-compatible LLM client
    providers.py   # search/reader provider 与质量打分
    engine.py      # M2/M3 运行图与角色逻辑
    main.py        # REST + WebSocket API
  static/
    index.html     # 控制台 + steer 输入 + 角色视图
    app.js
    styles.css
```

## 快速启动

```bash
cd /Users/zhouyafan/code/deepresearch/deepdeepresearch
cp .env.example .env

python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt

# 会自动读取 .env
uvicorn app.main:app --app-dir backend --reload --port 8000
```

浏览器打开：

- 用户页（黑色极简）：http://127.0.0.1:8000

## 环境变量

详见 `.env.example`。

最关键：

- `LLM_BASE_URL`
- `LLM_API_KEY`
- `LLM_MODEL_SUPERVISOR`
- `LLM_MODEL_PLANNER`
- `LLM_MODEL_RESEARCHER`
- `TAVILY_API_KEY` / `SERPER_API_KEY`
- `FIRECRAWL_API_KEY` / `JINA_READER_API_KEY`
- `COHERE_API_KEY`
- `CROSSREF_BASE_URL`
- `TRUSTED_DOMAINS`
- `DEFAULT_BLOCKED_DOMAINS`

## API

- `GET /api/system/capabilities`
- `GET /api/runs?limit=50&offset=0`（任务历史）
- `POST /api/runs`
  - body: `{ "goal": "...", "max_steps": 8 }`
- `POST /api/runs/{run_id}/steer`
  - body: `{ "content": "constraint: ...; ban source: ...", "scope": "global" }`
- `GET /api/runs/{run_id}`
- `GET /api/runs/{run_id}/events`
- `WS /ws/runs/{run_id}`
  - 服务端持续推送 events
  - 客户端可发送 `{"type":"steer","content":"...","scope":"global"}`

关键事件：
- `researcher_output`：单步输出
- `final_report`：全局最终报告
- `run_finished`：运行结束，payload 包含 `has_final_report`
- `interrupt_requested` / `research_paused_for_steer`：中断网关
- `researcher_react_thought` / `researcher_react_action` / `researcher_react_observation`：ReAct 过程

## steer 指令示例

- `change goal: 重点比较开源agent框架`
- `constraint: 必须标注不确定性`
- `allow source: arxiv.org`
- `ban source: reddit.com`
- `replan`
- `stop`

## Key 需求评估（想把效果做强）

最低必需（必须有）：

1. `LLM_API_KEY`

强烈建议（质量提升最大）：

1. `TAVILY_API_KEY`（语义检索质量高）
2. `SERPER_API_KEY`（补充 Google 广度）
3. `FIRECRAWL_API_KEY`（稳定正文抽取，减少噪声）
4. `COHERE_API_KEY`（对检索结果做语义重排，显著提高前几条相关性）
5. `JINA_READER_API_KEY`（作为正文抽取回退，提高可读率）

可选增强（后续可以补）：

1. 学术专用 API（Semantic Scholar）增强论文召回
2. 多语言检索 provider（根据目标区域补充）

## 说明

- 已默认屏蔽部分低信号来源（如 `reddit.com`、`quora.com`）。
- 若未配置外部 key，系统会自动降级为 fallback 模式（流程仍可运行，但检索质量会明显下降）。
