# Redis 生产可观测性、压力基线与运行参数调优

更新日期：2026-08-21

## 1. 目标与边界

本阶段为 Redis 缓存和任务通知链路补齐生产化能力：

- Prometheus 指标与告警规则；
- 可选 OpenTelemetry OTLP Metrics 导出；
- Redis 健康、自动降级和恢复观测；
- 连接池并发压力基线；
- Worker/SSE 数据库兜底轮询间隔建议。

MySQL 仍是异步任务、事件、取消标志和 Worker 租约的事实来源。Redis 故障时，缓存回退 MySQL/Provider，任务通知回退数据库轮询，不能因为指标或告警组件异常而中断业务。

## 2. HTTP 端点

### 2.1 Prometheus

```http
GET /metrics
```

由 `PROMETHEUS_METRICS_ENABLED` 控制，路径可通过 `PROMETHEUS_METRICS_PATH` 修改。指标只使用固定低基数标签，不包含 `task_id`、Redis Key、城市、用户输入或路线内容。

核心指标：

- `travel_agent_redis_up`：最近一次 PING 是否成功；
- `travel_agent_redis_degraded`：Redis 客户端是否处于自动降级；
- `travel_agent_redis_health_latency_milliseconds`：PING 延迟；
- `travel_agent_redis_pool_connections{state=...}`：连接池容量和占用；
- `travel_agent_redis_pool_utilization_ratio`：连接池使用率；
- `travel_agent_redis_client_operations_total{result=...}`：命令成功、失败和绕过数；
- `travel_agent_redis_notification_degraded`：Pub/Sub 是否回退数据库轮询；
- `travel_agent_redis_notifications_total{direction,kind}`：通知发布和接收数；
- `travel_agent_task_notification_waits_total{consumer,result}`：Worker/SSE 唤醒与兜底轮询；
- `travel_agent_redis_alert{code,severity,state}`：结构化告警状态；
- `travel_agent_task_notification_fallback_poll_seconds{consumer}`：当前兜底间隔。

Prometheus 抓取示例位于：

```text
deploy/prometheus/prometheus.example.yml
```

告警规则位于：

```text
deploy/prometheus/redis-alerts.yml
```

### 2.2 脱敏运行快照

```http
GET /api/observability/redis
```

返回 Redis 健康、连接池、缓存、通知、Worker、告警和当前调优参数。`GET /api/health` 同时包含这些组件的简化状态。

## 3. OpenTelemetry

OpenTelemetry 默认关闭，避免开发机没有 Collector 时产生无意义的导出错误。部署 Collector 后配置：

```env
OTEL_METRICS_ENABLED=true
OTEL_SERVICE_NAME=travel-agent-backend
OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=http://otel-collector:4318/v1/metrics
OTEL_METRIC_EXPORT_INTERVAL_SECONDS=30
```

示例 Collector 配置：

```text
deploy/opentelemetry/otel-collector.example.yml
```

当前导出 Redis up、degraded、PING 延迟、连接池使用率和通知降级等核心 Gauge。多个回调共享短时快照，避免一次采集重复执行多次 Redis PING。

## 4. Redis 健康与降级告警

应用内结构化告警：

- `redis_unavailable`：Redis 客户端连续降级；
- `redis_notification_degraded`：Pub/Sub 订阅异常；
- `redis_pool_near_capacity`：连接池使用率超过阈值。

相关配置：

```env
REDIS_ALERTS_ENABLED=true
REDIS_ALERT_DEGRADED_AFTER_SECONDS=30
REDIS_ALERT_POOL_UTILIZATION_THRESHOLD=0.8
```

告警先进入 `pending`，持续达到阈值后才进入 `active`，短暂抖动不会立即触发正式告警。Prometheus 规则进一步使用 1～5 分钟 `for` 窗口抑制瞬时波动。

## 5. 并发压力测试

执行命令：

```powershell
.\.venv\Scripts\python.exe scripts\run_redis_load_test.py `
  --concurrency 20 `
  --operations-per-worker 50 `
  --notification-count 200 `
  --json-report build\reports\redis-load-test.json
```

测试只执行：

- 并发 `SET → GET → DELETE`；
- Redis Pub/Sub `task_event` 发布和接收；
- 连接池占用采样。

它不访问 MySQL、高德或 LLM。所有测试 Key 使用唯一批次前缀并设置短 TTL，报告不包含具体 Key 或消息正文。

验收阈值：

- Redis 操作错误数为 0；
- 逻辑操作 P95 不超过 100ms；
- Pub/Sub 接收率不低于 99%；
- Pub/Sub publish→receive 端到端 P95 不超过 200ms。

## 6. 2026-08-21 本机基线

初始 `REDIS_MAX_CONNECTIONS=20` 时，在 20 并发基础上还需保留 Pub/Sub 长连接和健康检查连接，出现 50 次 `Too many connections`，因此不能作为当前并发基线。

调整为 `REDIS_MAX_CONNECTIONS=32` 后结果：

| 指标 | 结果 |
|---|---:|
| Redis 命令数 | 3000 |
| 吞吐 | 11910.20 commands/s |
| 逻辑操作 P95 | 6.729ms |
| 逻辑操作 P99 | 8.994ms |
| 连接池峰值 | 21 / 32 |
| 通知接收率 | 100% |
| 通知端到端 P95 | 0.262ms |
| 通知端到端 P99 | 0.309ms |
| 错误数 | 0 |
| 结果 | PASS |

结构化报告：

```text
build/reports/redis-load-test.json
```

该结果是当前开发机的回归基线，不直接等同于生产 SLA。生产环境应使用实际 Worker 数量、并发任务量和网络拓扑重新执行。

## 7. 连接池容量结论

当前默认值调整为：

```env
REDIS_MAX_CONNECTIONS=32
```

调优原则：

1. 每个应用进程拥有独立 redis-py 连接池；总连接数约为 `实例数 × REDIS_MAX_CONNECTIONS`；
2. 连接池至少覆盖峰值并发命令、1 条 Pub/Sub 长连接、健康检查和抖动余量；
3. 压力脚本按观测峰值增加约 25% 余量给出建议；
4. 生产 Redis `maxclients` 必须大于所有实例连接池总和，并预留运维连接；
5. 连接池连续 5 分钟超过 80% 时再扩容，避免只根据瞬时峰值盲目放大。

## 8. 通知轮询间隔结论

当前压力基线中通知接收率 100%，端到端 P95 低于 1ms，因此继续使用：

```env
TRIP_TASK_NOTIFICATION_WORKER_FALLBACK_POLL_SECONDS=5
TRIP_TASK_NOTIFICATION_SSE_FALLBACK_POLL_SECONDS=5
```

这两个值是 Redis 丢消息或断线时查询 MySQL 的安全兜底上限，不是正常通知延迟。当前基线按 2 个 Worker、50 条活跃 SSE 估算，最坏约产生 624 次/分钟兜底检查。

生产调优建议：

- 接收率 ≥99.9% 且通知 P95 ≤50ms：Worker 5s、SSE 5s；
- 接收率 ≥99% 且通知 P95 ≤200ms：Worker 3s、SSE 5s；
- 低于上述可靠性：Worker 1s、SSE 2s，并触发告警；
- 不要只为减少 MySQL 查询无限增大间隔，否则 Redis 丢消息时会显著增加任务和页面恢复延迟。

## 9. 验收顺序

生产参数调整后建议依次执行：

1. Redis 单元与 API 契约测试；
2. `scripts/run_redis_load_test.py`；
3. `scripts/run_redis_runtime_acceptance.py`；
4. 完整 `unittest discover`；
5. `scripts/run_quality_gate.py`；
6. 检查 Prometheus 告警和 OpenTelemetry Collector 的实际采集结果。
