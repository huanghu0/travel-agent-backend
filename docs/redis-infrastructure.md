# Redis 阶段一基础设施

更新日期：2026-08-20

## 1. 当前定位

Redis 是旅行智能体的可选加速与协调层，不是业务事实来源：

- MySQL/SQLite 继续保存 AgentState、行程版本、异步任务、Worker 租约和可恢复事件；
- Redis 阶段一只提供配置、连接池、健康检查、Key 规范和自动降级；
- Redis 故障时 API、同步规划和后台 Worker 必须继续使用持久化后端运行。

当前阶段尚未把高德路线、餐饮、任务通知或 SSE 事件写入 Redis。

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
REDIS_DEGRADE_COOLDOWN_SECONDS=5
```

本机开发可以在被 Git 忽略的 `.env` 中设置 `REDIS_ENABLED=true`。生产环境应使用独立凭据和环境前缀。

## 3. 连接管理

`app/infrastructure/redis/client.py` 提供：

- 线程安全的 redis-py `ConnectionPool`；
- 连接数、连接超时、命令超时和健康检查间隔；
- `RedisClientManager.execute()` 非关键操作包装；
- Redis 异常后的短暂冷却；
- 健康检查绕过冷却，实现 Redis 恢复后的立即自愈；
- 应用关闭时释放连接池；
- 日志和健康信息中的密码擦除。

未来业务代码不能直接创建 Redis 客户端，应通过共享的 `RedisClientManager` 使用连接池。

## 4. 自动降级

非关键 Redis 操作应使用：

```python
value = redis_client_manager.execute(
    lambda client: client.get(key),
    fallback=None,
)
```

发生 Redis 连接、超时或协议错误时：

1. 关闭故障连接池；
2. 进入短暂冷却期；
3. 返回调用方声明的 fallback；
4. 调用方继续查询 MySQL/SQLite 或 Provider；
5. 后续健康检查可以立即尝试恢复 Redis。

不能用该自动降级包装吞掉业务代码的 `ValueError`、校验错误或编程错误。

## 5. Key 规范

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

复杂查询条件使用稳定 JSON 和 SHA-256，避免把地址、偏好等原始用户输入写入 Key。可信 ID 使用 literal Key，并限制字符集和长度。

## 6. 健康检查

命令行检查：

```powershell
.\.venv\Scripts\python.exe scripts\check_redis.py --require-enabled
```

API 检查：

```text
GET /api/health
```

Redis 正常时组件状态：

```json
{
  "status": "ok",
  "degraded": false,
  "components": {
    "redis": {
      "enabled": true,
      "status": "ok",
      "healthy": true,
      "degraded": false
    }
  }
}
```

Redis 不可用时，HTTP 健康接口仍返回主服务可用，同时 Redis 组件显示 `degraded`，避免非关键缓存故障使旅行规划整体下线。

## 7. 验收命令

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_redis_infrastructure -v
.\.venv\Scripts\python.exe scripts\check_redis.py --require-enabled
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 8. 下一阶段

阶段二建议实现通用缓存接口：

- `CacheStore`；
- `RedisCacheStore`；
- `NoOpCacheStore`；
- 缓存数据 schema version；
- TTL 和序列化规范；
- 路线、餐饮接入前的缓存命中/未命中指标。
