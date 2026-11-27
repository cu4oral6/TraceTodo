# 🧠 有迹 TraceTodo：可追溯、自演化的融合记忆与四象限待办引擎

## 评委体验链接

1. 在工作台服务中设置 `TRACE_MEMORY_AUTH_REQUIRED=true` 和 `TRACE_MEMORY_JUDGE_DEMO_ENABLED=true`，并确认已有可用管理员。部署后从无痕窗口打开 `/home?demo=1`，应直接进入工作台。普通访客也可以从 `/home` 点击「评委一键免密体验」。
2. 将 `deploy/qoder-site/` 作为静态站点发布到现有的 Qoder Sites 作品。入口会立即跳转到 `https://todo.531party.cn/home?demo=1`。若工作台域名变化，同步更新入口文件里的两个网址。

每个首次访问的浏览器会获得独立的普通用户工作台和 24 小时会话。已有有效登录会话时复用当前工作台。评审结束后可关闭 `TRACE_MEMORY_JUDGE_DEMO_ENABLED` 停止发放体验会话，已发放的会话会正常到期。

> 深度融合 **随心记 (`suixinji`)**、**`basic-memory`** 与 **阿里 `ReMe`** 三大架构流派的核心精髓，去除繁重的 IM 渠道耦合，开箱即用的个人与多 Agent 跨会话长期记忆系统。

---

## ✨ 核心特性

1. **三层真值分离与全链路证据溯源（源自 `suixinji`）**
   - **原始记录（`RawNote`）** 与 **四类结构化记忆（`task` / `preference` / `semantic` / `episodic`）** 严格分离。
   - 每一次记忆演进均生成不可变的 `MemoryVersion` 并绑定支撑它的 `RawNote` 修订号与原文引述；支持**因果时间栅栏**（晚补录的旧历史不覆盖当前状态）、**原文修改级联失效**与**片段级精准遗忘（Partial Forget）**。
2. **记忆驱动的待办事项与艾森豪威尔四象限分析专页（特色创新模块）**
   - 自动评估待办事项的 **重要程度（Importance 1–5）** 与 **紧急程度（Urgency 1–5）**，映射至 **Q1（重要且紧急）/ Q2（重要不紧急）/ Q3（紧急不重要）/ Q4（不重要不紧急）**。
   - **动态截止时间提权**：临近截止日期（48h / 24h / 逾期）自动动态升级紧急度与象限，无需天天手工改状态。
   - **长期记忆联动校准**：结合用户的长期偏好记忆（`preference`）自动提权相关主题待办的重要度，并在手动调整象限后启用 **CAS 乐观锁 + `user_locked` 防覆盖保护**。
3. **可插拔向量数据库 + FTS5 BM25 混合检索与向量生命周期管理**
   - 支持 **Qdrant 本地嵌入式 HNSW / 远程 Qdrant Server** 与 **内置零依赖余弦向量引擎** 一键切换。
   - 通过 **RRF（倒数秩融合）** 联合稀疏关键词检索（BM25）与稠密语义相似度匹配（Cosine Similarity），并在记忆被替代（`superseded`）或遗忘（`forgotten`）时同步物理清理旧向量，彻底杜绝过期记忆“死灰复燃”。
4. **标准 MCP 协议表面 + Obsidian 可读的 Markdown 双向视图（源自 `basic-memory`）**
   - 提供带安全注解（`readOnlyHint` / `destructiveHint`）的 stdio MCP Server，供 Claude Desktop、Claude Code、Cursor 直接接入。
   - 支持一键将正式记忆导出为带 YAML Frontmatter、`memory://` 固定链接和 `[[wikilinks]]` 的 Markdown 知识目录（`knowledge/`），并通过 `.manifest.json` 实现安全的外部编辑回流与版本冲突草稿保护（`*.conflict-vN.md`）。
5. **受预算保护的睡眠归纳引擎 `Dream` + 受控双链图谱展开（源自 `ReMe`）**
   - 聚合零散日常陈述记忆提炼长期规律（强制标记 `assertion_kind="inferred"` 绝不冒充原话），内置 `BudgetGuard`（调用次数与 Token 上限熔断）；双链图谱支持**悬空前向引用自动连线**与有预算的多跳上下文展开。

---

## 🚀 快速开始（零外部依赖即可秒级运行）

### 1. 运行全套自动化与 C01–C12 业务不变量测试

```bash
cd trace-memory
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

### 2. 启动多视图 Web 控制台（含【待办事项与四象限分析专页】）

```bash
cd trace-memory
PYTHONPATH=src python3 -m trace_memory web --port 18090
```

浏览器打开 `http://127.0.0.1:18090`，点击右上角 **「✨ 一键载入演示数据」** 即可体验：
- **📋 待办事项与四象限分析**：2×2 四象限看板视图 / 多维优先级排序列表切换、AI 负载诊断、一键完成/改象限、原文溯源。
- **🗂️ 四类记忆与混合向量检索**：实时展示每条命中的 `RRF Score`、`BM25 Rank` 与 `Vector Cosine` 得分。
- **🕸️ 双链知识图谱上下文**：查看 `[[wikilink]]` 有向边、悬空前向引用解析与跳数预算状态。
- **💬 双源证据问答 & Trace**：区分“当前状态”与“历史变迁”问答，查看全链路耗时与 Token 审计。

### 3. 命令行（CLI）常用操作

```bash
# 记录一句话并自动抽取记忆与四象限待办
PYTHONPATH=src python3 -m trace_memory record "明天必须修复线上微信支付回调超时 bug"

# 查看四象限矩阵与按紧急度/重要度排序的待办
PYTHONPATH=src python3 -m trace_memory tasks --sort priority

# 运行 BM25 + 向量相似度 RRF 混合检索（测试跨字面语义召回）
PYTHONPATH=src python3 -m trace_memory search "线上结算隐患"

# 双源证据问答
PYTHONPATH=src python3 -m trace_memory ask "我现在住在哪个城市？"

# 导出 Markdown 视图 / 同步外部编辑 / 运行 Dream 睡眠归纳
PYTHONPATH=src python3 -m trace_memory export
PYTHONPATH=src python3 -m trace_memory sync
PYTHONPATH=src python3 -m trace_memory dream --max-calls 3
```

### 4. 接入 Claude Code / Cursor (MCP Server)

在 `.cursor/mcp.json` 或 Claude 配置中添加：

```json
{
  "mcpServers": {
    "trace-memory": {
      "command": "python3",
      "args": ["-m", "trace_memory", "mcp"],
      "env": {
        "PYTHONPATH": "/path/to/trace-memory/src"
      }
    }
  }
}
```

### 5. 切换真实大模型（DeepSeek / OpenAI / Qwen）与 Qdrant 向量数据库（可选）

复制 `.env.example` 为 `.env` 并安装可选扩展依赖：

```bash
pip install -e ".[full,dev]"
cp .env.example .env
# 在 .env 中设置 TRACE_MEMORY_LLM_PROVIDER=openai、API Key 以及 TRACE_MEMORY_VECTOR_BACKEND=qdrant
```

---

## 📚 深入阅读文档

- **[架构设计详解 (`docs/ARCHITECTURE.md`)](docs/ARCHITECTURE.md)**：模块分层、数据流、向量生命周期与一致性模型。
- **[测试用例设计与执行手册 (`docs/TEST_CASES.md`)](docs/TEST_CASES.md)**：对标 `suixinji`、`basic-memory`、`ReMe` 三大 GitHub 仓库设计的 33 项自动化测试与 Web 手工验收脚本。
- **[项目简历写法与面试高频问答指南 (`docs/INTERVIEW_GUIDE.md`)](docs/INTERVIEW_GUIDE.md)**：专为面试准备的项目话术与源码走读索引。
