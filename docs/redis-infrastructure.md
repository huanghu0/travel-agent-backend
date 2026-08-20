# Redis 通用缓存基础设施（阶段一与阶段二）

更新日期：2026-08-20

## 1. 当前定位

Redis 是旅行智能体的可选 L1 加速与协调层，不是业务事实来源：

- MySQL 保存 AgentState、行程版本、异步任务、Worker 租约、SSE 事件以及持久化路线/餐饮缓存；
- Redis 负责可丢失、可重建的热数据；
- Redis 关闭或故障时，业务继续回退 MySQL/Provider；
- 当前阶段完成通用缓存抽象，尚未把路线和餐饮查询切换到 Redis L1。

后续业务读取链路统一为：

```text
Redis L1 → MySQL L2 → 高德 Provider
```

## 2. 配置

`.env.example` 提供以下配置：

```env
REDIS_ENABLED=false
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_DB=0
REDIS_USERNAME=
REDIS_PASSWORD=
REDIS_SSL=false
REDIS_MAX_CONNECTIONS=20
REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS=3
REDIS_SOCKET_TIMEOUT_SECONDS=5
REDIS_HEALTH_CHECK_INTERVAL_SECONDS=30
REDIS_RETRY_ON_TIMEOUT=true
REDIS_DECODE_RESPONSES=false
REDIS_CLIENT_NAME=travel-agent-backend
REDIS_KEY_PREFIX=travel-agent:dev
REDIS_DEFAULT_TTL_SECONDS=1800
REDIS_CACHE_SCHEMA_VERSION=1
REDIS_CACHE_MIN_TTL_SECONDS=1
REDIS_CACHE_MAX_TTL_SECONDS=604800
REDIS_CACHE_DELETE_INVALID_ENTRIES=true
REDIS_DEGRADE_COOLDOWN_SECONDS=5
```

本机开发可在被 Git 忽略的 `.env` 中设置 `REDIS_ENABLED=true`。修改 `REDIS_CACHE_SCHEMA_VERSION` 会让旧信封被视为未命中并按配置清理，不会修改 MySQL 数据。

## 3. 连接管理与自动降级

`app/infrastructure/redis/client.py` 提供共享的 `RedisClientManager`：

- 线程安全 redis-py 连接池；
- 连接数、连接超时、命令超时和健康检查间隔；
- Redis 异常后的短暂冷却；
- 健康检查绕过冷却，实现恢复后的立即自愈；
- 日志和健康信息中的密码擦除；
- 应用退出时释放连接池。

非关键 Redis 操作统一通过 `execute()` 执行。它只吞掉连接、协议、超时等 Redis 基础设施故障，不吞掉业务校验和编程错误。

## 4. 通用 CacheStore

`app/infrastructure/cache/` 定义了与 Redis 解耦的 `CacheStore` 协议：

```python
lookup = cache_store.get(key)
write = cache_store.set(key, value, ttl_seconds=300)
deleted = cache_store.delete(key)
metrics = cache_store.metrics_snapshot()
```

当前实现：

- `RedisCacheStore`：使用 Redis 保存版本化 JSON 信封，故障时返回显式降级结果；
- `NoOpCacheStore`：Redis 关闭时返回 bypass/skipped，业务层不需要散落 `if REDIS_ENABLED`；
- `create_cache_store()`：根据配置选择实现，创建过程不会连接 Redis。

读取状态：

- `hit`：命中，包括 payload 本身为 `null`；
- `miss`：正常未命中，或缓存内容损坏、过期、版本不兼容；
- `bypass`：缓存关闭；
- `degraded`：Redis 当前不可用。

写入状态：

- `stored`：写入成功；
- `skipped`：缓存关闭、TTL 非正数或调用方明确不缓存；
- `degraded`：Redis 当前不可用。

## 5. JSON 序列化规范

缓存值统一编码为 UTF-8 JSON，不使用 pickle。信封格式固定为：

```json
{
  "schema_version": 1,
  "created_at": "2026-08-20T08:30:00+00:00",
  "expires_at": "2026-08-20T09:00:00+00:00",
  "payload": {}
}
```

规范：

- JSON Key 排序、紧凑分隔符，保证相同输入生成稳定字节；
- 保留中文，不转义为 ASCII；
- 禁止 `NaN`、`Infinity` 和任意 Python 对象；
- 支持 Pydantic、dataclass、日期时间、`Decimal` 和 `Enum`；
- 所有信封时间必须带时区，读取后转换为 UTC；
- 信封字段必须与当前规范完全一致；
- `schema_version` 必须严格等于当前配置版本；
- 损坏、旧版本或绝对过期条目按 miss 处理，可自动删除。

## 6. TTL 规范

所有 Store 使用同一 `CacheTTLPolicy`：

1. `ttl_seconds=None` 使用 `REDIS_DEFAULT_TTL_SECONDS`；
2. TTL `<= 0` 时跳过写入；
3. 小于 `REDIS_CACHE_MIN_TTL_SECONDS` 时提升到最小值；
4. 大于 `REDIS_CACHE_MAX_TTL_SECONDS` 时压缩到最大值；
5. TTL 必须是整数秒，布尔值和浮点数不接受；
6. Redis 原生 TTL 与信封中的绝对 `expires_at` 双重限制陈旧数据。

默认最大 TTL 是 604800 秒（7 天）。具体路线、餐饮、天气等业务接入时仍应声明更短的领域 TTL。

## 7. 指标

每个 CacheStore 暴露线程安全的进程内快照：

- 读取：`read_requests`、`hits`、`misses`、`bypasses`、`degraded_reads`；
- 数据质量：`invalid_entries`、`expired_entries`；
- 写入：`write_requests`、`writes`、`skipped_writes`、`degraded_writes`；
- 删除：`delete_requests`、`deletes`、`degraded_deletes`；
- `hit_rate = hits / (hits + misses)`，bypass 与 degraded 不进入命中率分母。

指标不记录 Key、地址、偏好或缓存内容。当前为单进程计数，后续可映射到 OpenTelemetry/Prometheus。

## 8. Key 规范

`app/infrastructure/redis/keys.py` 统一生成 Key：

```text
travel-agent:dev:cache:route:{sha256}
travel-agent:dev:cache:restaurant:{sha256}
travel-agent:dev:cache:place:{sha256}
travel-agent:dev:cache:weather:{sha256}
travel-agent:dev:task:progress:{task_id}
travel-agent:dev:session:{session_id}
travel-agent:dev:lock:{namespace}:{sha256}
```

复杂查询条件使用稳定 JSON 和 SHA-256，避免把地址与偏好原文写入 Key。可信 ID 使用 literal Key，并限制字符集、段长度和总字节数。

## 9. 健康检查与冒烟测试

API：

```text
GET /api/health
```

响应的 `components` 同时包含：

- `redis`：连接健康、目标、延迟和降级状态；
- `cache`：当前 backend、enabled、schema version 和缓存指标。

Redis 不可用时顶层服务仍返回 `status=ok`，并使用 `degraded=true` 表示非关键组件降级。

命令行连接检查：

```powershell
.\.venv\Scripts\python.exe scripts\check_redis.py --require-enabled
```

完整缓存冒烟测试：

```powershell
.\.venv\Scripts\python.exe scripts\check_redis.py --require-enabled --cache-smoke-test
```

冒烟测试会使用唯一 Key 完成 delete → set → get → TTL → delete，不输出 Key 或缓存内容，也不会留下测试数据。

## 10. 验收命令

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_redis_infrastructure -v
.\.venv\Scripts\python.exe -m unittest tests.test_cache_infrastructure -v
.\.venv\Scripts\python.exe scripts\check_redis.py --require-enabled --cache-smoke-test
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

## 11. 下一阶段

- 将高德路线缓存接入 Redis L1 → MySQL L2 → Provider；
- 将餐饮候选缓存接入相同分层，并保持各自领域 TTL；
- 增加 L1/L2 分层命中率和 Provider 节省调用量；
- 再接入任务通知、取消/SSE 唤醒、共享限流和分布式协调。
