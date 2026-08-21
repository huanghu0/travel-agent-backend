# Redis 第三阶段：业务增强

更新日期：2026-08-21

## 1. 目标与边界

本阶段让 Redis 承担跨实例供应商限流、可重建查询缓存和前端只读快照加速。Redis 仍然不是业务事实来源：

- AgentState、行程版本、异步任务、SSE 事件、取消状态和 Worker 租约仍以 MySQL/SQLite 为准；
- Redis 故障默认 `fail-open`，旅行规划继续执行，并通过指标暴露降级；
- 缓存只保存标准化后的 Pydantic JSON，不保存高德原始响应，不使用 pickle；
- 测试和验收不会调用真实高德或 LLM。

## 2. 已实现能力

### 2.1 跨实例供应商限流

`app/infrastructure/redis/rate_limit.py` 使用 Redis Lua 固定窗口原子计数。不同 API 实例、Worker 进程和连接池使用同一环境前缀时共享额度。

当前策略：

- 高德：全局每秒、每分钟和每日请求数；
- LLM：按模型摘要隔离的每分钟和每日请求数；
- Redis Key 不包含 API Key、模型原文、城市或用户输入；
- Redis 不可用时默认放行，`degraded_allowed` 指标递增；
- 高德额度耗尽会转为可重试的本地 `RATE_LIMIT` Provider 错误；
- LLM 额度在网络调用前检查，Anthropic 纠正重试会作为一次新的真实请求计数。

限流只发生在真实供应商网络请求入口。Redis/MySQL 缓存命中不会消耗高德配额。

### 2.2 高德与 LLM 配额配置

```env
REDIS_PROVIDER_RATE_LIMIT_ENABLED=true
REDIS_PROVIDER_RATE_LIMIT_FAIL_OPEN=true

AMAP_RATE_LIMIT_REQUESTS_PER_SECOND=5
AMAP_QUOTA_REQUESTS_PER_MINUTE=300
AMAP_QUOTA_REQUESTS_PER_DAY=100000

LLM_QUOTA_REQUESTS_PER_MINUTE=30
LLM_QUOTA_REQUESTS_PER_DAY=10000
```

数值 `<= 0` 表示关闭对应窗口。当前是请求次数配额，不是 LLM token 配额；当前也没有用户级额度，需要在认证用户体系建立后再增加。

### 2.3 高德业务缓存

缓存链路分为两类：

| 领域 | 读取链路 | 默认 TTL |
|---|---|---:|
| 路线 | Redis L1 → MySQL/SQLite L2 → 高德 | 3600 秒 |
| 餐饮候选 | Redis L1 → MySQL/SQLite L2 → 高德 | 21600 秒 |
| 天气 | Redis → 高德 | 1800 秒 |
| 景点/周边景点 | Redis → 高德 | 21600 秒 |
| 酒店 | Redis → 高德 | 3600 秒 |
| 地理编码/地点解析 | Redis → 高德 | 604800 秒 |
| 通用 POI/POI 详情 | Redis → 高德 | 21600 秒 |

`app/providers/amap/business_cache.py` 统一完成：

1. 使用标准化查询参数摘要生成 Key；
2. 命中后重新执行 Pydantic 校验；
3. 损坏数据自动删除并回源；
4. Redis 读写失败时直接回源 Provider；
5. 记录每个领域的命中、未命中、降级、写入和 Provider 调用次数。

配置：

```env
AMAP_WEATHER_CACHE_TTL_SECONDS=1800
AMAP_ATTRACTION_CACHE_TTL_SECONDS=21600
AMAP_HOTEL_CACHE_TTL_SECONDS=3600
AMAP_GEOCODE_CACHE_TTL_SECONDS=604800
AMAP_POI_CACHE_TTL_SECONDS=21600
```

### 2.4 execution-view 与任务进度快照

`app/infrastructure/cache/read_models.py` 提供前端只读模型缓存：

- `execution-view`：Redis 命中直接返回；未命中时从 AgentState 检查点重建；
- 活动会话的 execution-view 最多缓存 5 秒，终态按配置缓存；
- 草稿新版本确认或会话恢复成功后主动删除旧 execution-view；
- 任务进度：任务创建、领取、进度、取消、成功和失败后刷新 Redis 快照；
- 任务查询先读 Redis，未命中或损坏时回退数据库；
- 取消判断始终直接读取数据库，避免缓存延迟导致 Worker 继续调用高德或 LLM；
- SSE 历史事件始终从数据库按 `event_id` 回放，Redis 只负责唤醒和最新快照。

配置：

```env
EXECUTION_VIEW_CACHE_TTL_SECONDS=1800
TASK_PROGRESS_CACHE_ACTIVE_TTL_SECONDS=3600
TASK_PROGRESS_CACHE_TERMINAL_TTL_SECONDS=86400
```

## 3. Redis Key 规范

```text
{prefix}:quota:amap:{policy}:{identity_sha256}
{prefix}:quota:llm:{policy}:{model_sha256}
{prefix}:cache:weather:{query_sha256}
{prefix}:cache:attraction:{query_sha256}
{prefix}:cache:hotel:{query_sha256}
{prefix}:cache:geocode:{query_sha256}
{prefix}:cache:poi:{query_sha256}
{prefix}:snapshot:execution-view:{session_id}
{prefix}:task:progress:{task_id}
```

复杂查询与模型标识使用 SHA-256；可信会话/任务 ID 使用受字符集和长度约束的 literal 段。

## 4. 可观测性

`GET /api/health` 和 `GET /api/observability/redis` 增加：

- `provider_quota_metrics`：检查、放行、拒绝、Redis 故障和按供应商结果；
- `amap_business_cache_metrics`：各领域命中、未命中、降级、写入和 Provider 调用；
- 通用缓存、路线/餐饮 L1/L2、连接池和通知指标保持不变。

Prometheus 新增：

```text
travel_agent_provider_quota_checks_total{provider,outcome}
travel_agent_provider_quota_degraded_total{outcome}
travel_agent_amap_business_cache_operations_total{domain,outcome}
```

标签只使用固定供应商、领域和结果，不使用 session_id、task_id、城市、模型原文或用户输入，避免高基数和隐私泄露。

## 5. 验收

单元与回归测试：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_redis_business_enhancements -v
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

真实 Redis 业务验收：

```powershell
.\.venv\Scripts\python.exe scripts\run_redis_business_acceptance.py `
  --json-report build\reports\redis-business-acceptance.json
```

脚本不会调用高德或 LLM，使用唯一 Redis 前缀，并验证：

1. 当前 Redis 健康；
2. 三个独立进程共享同一高德额度，原子放行数不超过上限；
3. 两个独立连接池高并发共享同一 LLM 模型额度；
4. 独立临时 Redis 中断时 fail-open，恢复后重新协调；
5. 输出脱敏 JSON 报告，不执行 `FLUSHDB`。

## 6. 尚未包含

- LLM 输入、输出和总 token 配额；
- 已认证用户、租户或 API Key 维度的独立额度；
- 滑动窗口、令牌桶和按供应商返回头动态校准；
- 热点 Key 分片和 Redis Cluster 专用 Lua/Hash Tag 策略；
- execution-view 的主动事件驱动预热；
- 天气、景点、酒店和地理编码的 MySQL L2 持久缓存。

这些能力应依据真实 Prometheus 数据和用户认证模型继续实施，而不是提前增加无依据的复杂度。
