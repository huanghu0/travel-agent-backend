# DashScope Embedding 迁移设计

## 1. 目标

将分享广场与 RAG 的 Embedding 提供方从 Gemini 完全迁移到阿里云百炼 `qwen3.7-text-embedding`。应用通过百炼的 OpenAI 兼容地址生成固定 768 维向量，Docker 自建 Qdrant 继续只负责向量存储和检索。

本次迁移不保留 Gemini 配置、客户端、依赖、脚本参数或文档兼容层。当前工作只写入 `rag` 分支工作目录，不 commit、不 push。

## 2. 已确认约束

- Embedding 模型固定为 `qwen3.7-text-embedding`。
- 向量维度固定为 768，与当前 Qdrant Collection schema 保持一致。
- 使用项目已有的 `openai` Python SDK 请求百炼 OpenAI 兼容接口。
- 删除 `google-genai` 依赖。
- API Key 只从 `DASHSCOPE_API_KEY` 读取。
- OpenAI 兼容 Base URL 只从 `DASHSCOPE_BASE_URL` 读取。
- 不读取 `GEMINI_EMBEDDING_API_KEY`，不提供旧变量兼容。
- 不把真实 API Key 写入源码、配置样例、日志、文档或测试。
- 本次只更新测试代码，不运行测试、质量门禁、网络探测或真实模型请求；部署验证由用户在服务器完成。

## 3. 客户端设计

`app/rag/embedding.py` 保留现有 provider-neutral 异常类型，并用 `DashScopeEmbeddingClient` 替换 `GeminiEmbeddingClient`。

客户端通过以下参数构造：

- `api_key: str`
- `base_url: str`
- `model: str`
- `dimension: int`
- `timeout_seconds: float`
- `max_attempts: int`
- 可选注入的 OpenAI-compatible client，供测试隔离使用

未注入客户端时，使用 `OpenAI` 构造同步客户端。`max_attempts` 表示包含首次调用在内的总尝试次数，因此传给 OpenAI SDK 的 `max_retries` 为 `max_attempts - 1`。

`embed(text)` 调用兼容接口的 embeddings API，并固定传递：

```python
model=self.model
input=text
dimensions=self.dimension
```

响应只接受第一条 `data` 中的 `embedding`。结果转换为 `float` 列表后必须满足：

- 非空；
- 长度等于 768；
- 每一项都是有限数值。

客户端继续实现 `app.rag.interfaces.EmbeddingClient` 所要求的 `model`、`dimension` 和 `embed(text)` 边界，因此分享发布、索引 Worker 和 RAG 检索流程无需改变。

## 4. 配置与运行时

配置样例调整为：

```dotenv
DASHSCOPE_API_KEY=
DASHSCOPE_BASE_URL=
EMBEDDING_MODEL=qwen3.7-text-embedding
EMBEDDING_DIMENSION=768
EMBEDDING_TIMEOUT_SECONDS=10
EMBEDDING_MAX_ATTEMPTS=3
```

当 `SHARE_SQUARE_ENABLED=true` 或 `RAG_ENABLED=true` 时，运行时要求 API Key、Base URL、模型名和 768 维配置有效。缺失或非法配置只会让可选 RAG/分享写入能力进入 degraded 状态，不影响普通旅行攻略生成。

健康检查继续只验证配置是否存在，并对 Qdrant 做轻量探测；健康检查不调用百炼，不消耗免费额度。

`app.sharing.service` 使用 provider-neutral `EmbeddingClient` 协议作为类型边界，不再依赖具体供应商客户端类。实际客户端只在 `app.rag.runtime` 和维护脚本的组合根中构造。

## 5. 异常、重试与日志

- 超时、连接失败、HTTP 429 和 HTTP 5xx 映射为 `EmbeddingUnavailableError`。
- 其他 HTTP 4xx 映射为 `EmbeddingConfigurationError`。
- 空响应、缺少向量、维度错误、非数字或非有限值映射为 `InvalidEmbeddingError`。
- OpenAI SDK 负责有限重试；重试次数由 `EMBEDDING_MAX_ATTEMPTS` 控制。
- 日志事件名改为 `dashscope_embedding`。
- 日志只包含模型、结果、耗时、脱敏错误类别和 HTTP 状态码，不包含 API Key、Base URL、输入文本或响应正文。

## 6. Qdrant 与已有数据

Qdrant Collection 的维度和距离算法不变，因此不需要数据库 schema 迁移。Gemini 与 Qwen 即使维度相同，其向量空间也不兼容，不能在同一个有效索引中混用。

该功能尚未在服务器正式生成 Gemini 向量，因此继续使用当前 Collection 名称。若本地或部署环境中已经存在实验性 Gemini point，应在启用分享写入和 RAG 前，使用更新后的重建索引脚本从 MySQL 重新生成全部 Qwen 向量。

MySQL 中的 `embedding_model` 会在新发布或重建索引时记录 `qwen3.7-text-embedding`，`embedding_dimension` 继续记录 768。

## 7. 脚本、测试与文档迁移

- 重建索引和对账脚本改为构造 `DashScopeEmbeddingClient`。
- 检索评估脚本把 `--live-gemini` 改为 `--live-dashscope`，并读取 DashScope 配置。
- Embedding 客户端测试文件改为 `tests/test_dashscope_embedding.py`，使用假 OpenAI-compatible client 验证请求与响应边界。
- 配置、运行时和维护脚本测试中的 Gemini 配置改为 DashScope 配置。
- 测试夹具中仅表示模型元数据的值改为 `qwen3.7-text-embedding`。
- `.env.example`、运维手册、RAG 设计与既有实施计划中的 Embedding 提供方描述全部更新。
- 除本迁移设计与实施计划对旧状态的必要说明外，运行时代码、配置、依赖和活跃运维文档不再包含 Gemini Embedding 客户端、环境变量或模型名。

这些测试文件只为后续服务器验证保持可执行结构；本次修改过程不运行测试。

## 8. 安全与部署要求

此前发送到聊天中的 API Key 已视为泄露，部署只能使用重置后的新 Key。新 Key 由服务器环境变量或密钥管理服务注入，不写入 Git 工作目录。

部署时必须配置与新 Key 同一北京业务空间的 OpenAI 兼容 Base URL。免费额度用完即停保持开启时，额度耗尽会返回 403；应用会把该响应视为配置或额度不可用并进入现有降级路径。

## 9. 完成标准

- 运行时代码只构造 `DashScopeEmbeddingClient`。
- 所有 Embedding 请求显式指定 `qwen3.7-text-embedding` 和 768 维。
- Gemini Embedding 配置和 `google-genai` 依赖已删除。
- Qdrant 和分享/RAG 业务流程未改变。
- 测试、脚本和活跃文档已同步到 DashScope 命名。
- 没有真实密钥进入工作目录。
- 变更保持未提交、未推送、未经运行验证，等待用户在服务器测试。
