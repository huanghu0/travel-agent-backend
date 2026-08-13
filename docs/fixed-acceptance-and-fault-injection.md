# 固定验收样本与故障注入

## 1. 目标

本阶段为旅行智能体建立不依赖实时高德或 LLM 的持续集成质量门，同时保留在稳定环境中录制真实端到端状态的能力。

固定验收包含杭州、北京、上海、成都、西安五个城市，每个城市覆盖：

- 1 日步行
- 3 日公共交通
- 5 日驾车

总计 15 个不可随运行日期漂移的场景。

## 2. 录制文件格式

每个场景保存为版本化录制包，而不是直接保存裸 `AgentState`：

```json
{
  "format_version": 1,
  "suite_name": "travel-agent-fixed-e2e-v1",
  "case_id": "hangzhou-3d-transit",
  "source": "live",
  "request_sha256": "...",
  "state_sha256": "...",
  "redacted_paths": [],
  "state": {}
}
```

目录中的 `manifest.json` 保存：

- 场景 ID 与文件名
- 样本来源：`live`、`synthetic` 或 `legacy`
- 请求摘要和状态摘要
- 脱敏字段数量
- 格式版本和套件名称

加载时会验证场景、请求摘要、状态摘要和 manifest，文件被手动修改后会直接拒绝回放。

## 3. 脱敏规则

录制前会递归检查状态：

- `api_key`、`token`、`authorization`、`password`、`secret`、`cookie` 等字段替换为 `[REDACTED]`。
- Bearer Token、URL 中的 `key`/`token` 参数和 `sk-...` 格式密钥会被替换。
- 脱敏完成后重新执行 `AgentState` 校验，避免生成无法恢复的样本。

不要把真实 `.env`、HTTP 原始认证头或未脱敏日志放入录制目录。

## 4. 离线契约样本

仓库内的：

```text
tests/fixtures/fixed_acceptance/v1/
```

包含 15 个 `synthetic` 样本。它们用于验证：

- 录制格式
- manifest 完整性
- SHA-256 防篡改
- 15 场景覆盖率
- 确定性质量门
- CI 回放链路

这些样本不代表真实高德结果，也不能用于评估真实路线和内容质量。

重新生成契约样本：

```powershell
python scripts/generate_fixed_acceptance_fixtures.py
```

## 5. 真实录制

先启动服务，再执行全量录制：

```powershell
python scripts/run_fixed_acceptance_baseline.py `
  --execute `
  --base-url http://127.0.0.1:8001 `
  --record-dir tests/fixtures/fixed_acceptance/live-v1 `
  --output build/reports/fixed-acceptance-live.json
```

单独补录某个缺失或失败场景时使用 `--case-id`，已有其他样本不会被覆盖：

```powershell
python scripts/run_fixed_acceptance_baseline.py `
  --execute `
  --base-url http://127.0.0.1:8001 `
  --record-dir tests/fixtures/fixed_acceptance/live-v1 `
  --case-id beijing-5d-driving `
  --output build/reports/fixed-acceptance-live.json `
  --summary-only
```

真实录制会标记为 `source=live`。建议在固定模型、固定高德配置和隔离数据库环境中执行，并在提交前人工检查脱敏结果。命令退出码为 1 不一定代表录制失败：如果 `recording_failure_count=0`，但某些业务质量检查未达标，录制仍然成功，只是固定基线未通过。

只允许真实样本离线回放：

```powershell
python scripts/run_fixed_acceptance_baseline.py `
  --replay-dir tests/fixtures/fixed_acceptance/live-v1 `
  --require-manifest `
  --allowed-source live `
  --output build/reports/fixed-acceptance-live-replay.json
```

### 5.1 2026-08-13 Live 基线结果

当前已经录制并回放 15 个真实供应商样本：

| 指标 | 结果 |
|---|---:|
| 场景总数 | 15 |
| 有效样本 | 15 |
| 缺失样本 | 0 |
| 无效样本 | 0 |
| Live 覆盖率 | 100% |
| 达到固定质量阈值 | 5 |
| 未达到固定质量阈值 | 10 |
| 固定基线通过率 | 33.33% |

15 个样本都由执行循环以 `completion_mode=full` 完成。覆盖率 100% 只说明录制、manifest 校验和回放链路完整，不能等同于业务质量全部达标。

未通过检查的分布：

| 检查代码 | 涉及场景数 | 含义 |
|---|---:|---|
| `plan.minimum_attractions` | 7 | 至少一天没有满足最低景点数量 |
| `commute.segment_limit` | 4 | 至少一段通勤超过固定阈值 |
| `route.available` | 1 | 至少一个真实路线分段不可用 |

同一场景可能同时命中多个检查。详细结果位于：

```text
build/reports/fixed-acceptance-live.json
build/reports/fixed-acceptance-live-replay.json
tests/fixtures/fixed_acceptance/live-v1/manifest.json
```

本轮 manifest 中 15 个样本全部为 `source=live`，录制状态中没有检测到需要替换的敏感字段，因此 `redacted_paths` 总数为 0。该数值只代表结构化录制内容未命中脱敏规则，不替代人工复核，也不能据此提交真实 `.env` 或原始认证日志。

## 6. 故障注入

`FaultInjector` 按“目标 + 调用次数”确定性触发故障，支持：

- `timeout`
- `upstream`
- `rate_limit`
- `authorization`
- `invalid_output`
- `sqlite_locked`

`ToolRegistry(call_injector=...)` 可覆盖高德和 LLM 工具；`FaultInjectingProxy` 可包装 SQLite Store、路线缓存和餐饮缓存。生产环境不注入时行为不变。

当前固定覆盖 14 类故障，分为 7 个可恢复场景和 7 个不可恢复安全终止场景。缓存持续读写失败被定义为非关键依赖降级：只要实时供应商查询仍可用，行程应在不使用缓存的情况下完成，而不是错误终止整个任务。

不可恢复场景必须在有限预算内进入明确 `failed` 状态，不能退化成最大步骤、预算耗尽或无收益循环终止。故障记录保存在 `FaultInjector.events`，可以断言故障是否真正触发，避免“测试看似通过但没有执行故障分支”。

## 7. 质量门

本地运行：

```powershell
python scripts/run_quality_gate.py
```

确定性质量门依次执行：

1. Python 编译检查。
2. 完整 Orchestrator 14 类故障恢复与安全终止验收，并生成 JSON/JUnit 报告。
3. 全量单元测试和故障注入测试。
4. 仓库内 15 个 `synthetic` 固定样本的 manifest 校验和离线回放。
5. 14 类故障场景和 15/15 synthetic 契约样本全部通过时才返回退出码 0。

Live Provider 回放当前作为人工/发布前质量基线单独执行，不放入不允许访问真实供应商的默认 CI。Live 样本即使覆盖完整，只要业务阈值未通过，回放命令仍返回非零退出码。

GitHub Actions 配置位于：

```text
.github/workflows/quality-gate.yml
```

## 8. 完整 Orchestrator 故障恢复与不可恢复终止验收

完整验收位于：

```text
app/evaluation/orchestrator_faults.py
app/evaluation/fault_reporting.py
tests/test_orchestrator_fault_recovery.py
tests/test_fault_reporting.py
scripts/run_orchestrator_fault_recovery.py
```

该套件使用真实 `TripOrchestrator`、`ExecutionPolicy`、`ToolRegistry`、SQLite 检查点、校验器和确定性优化器，只替换高德与 Planner 外部边界，不访问网络，也不读取真实 API Key。

### 8.1 可恢复场景（7 类）

1. 景点查询首次超时，第二次调用恢复。
2. 酒店查询首次 429，保留限流语义且熔断器不误打开。
3. Planner 首次返回无效结构，在 `generate_plan` 原动作重试，不错误进入 `repair_plan`。
4. SQLite 第二次保存短暂锁定，由独立检查点策略有限重试，且不重复已完成工具。
5. 一条真实路线分段超时，保留其他成功分段，并由确定性路线重排后重新查询恢复。
6. 路线缓存持续读写失败，降级为实时路线查询并正常完成。
7. 餐饮缓存持续读写失败，降级为实时餐厅查询并正常完成。

### 8.2 不可恢复安全终止场景（7 类）

1. 鉴权失败不可重试，立即失败，不继续天气、酒店或 LLM 调用。
2. Planner 连续返回无效结构，在动作重试上限处终止。
3. SQLite 会话检查点连续锁定，在独立重试耗尽后抛出 `AgentCheckpointError`。
4. 路线分段持续不可用，在重排和有限修复耗尽后保留 `route.unavailable` 并明确失败。
5. 长通勤且没有可用的近距离替换景点，以 `commute_replacement_exhausted` 终止。
6. 日程持续超时且无法通过移动、压缩或移除景点解决，以 `schedule_optimization_exhausted` 终止。
7. 景点候选不足，在确定性回填和修复均无法满足最低景点数时，以 `attraction_candidates_exhausted` 终止。

SQLite 会话检查点只重试 `database is locked` 和 `database is busy`。连续三次仍失败时抛出 `AgentCheckpointError`，接口返回 503；其他 `OperationalError` 不重试，避免掩盖表结构或 SQL 错误。路线缓存和餐饮缓存属于可降级依赖，其持续失败不会被误判为会话持久化失败。

单独运行：

```powershell
python scripts/run_orchestrator_fault_recovery.py `
  --json-report build/reports/orchestrator-faults.json `
  --junit-report build/reports/orchestrator-faults.junit.xml
python -m unittest tests.test_orchestrator_fault_recovery tests.test_fault_reporting -v
```

验收要求：故障必须真实触发；可恢复场景最终完成并可从 SQLite 恢复；不可恢复场景必须进入明确 `failed` 终态并生成稳定的 `termination_code`；步骤、工具和 LLM 预算不能越界；已完成会话再次 `resume()` 不产生外部调用。

### 8.3 2026-08-13 故障恢复基线

`build/reports/orchestrator-faults.json` 最新结果：

| 指标 | 结果 |
|---|---:|
| 故障场景总数 | 14 |
| 可恢复场景 | 7/7 通过 |
| 安全终止场景 | 7/7 通过 |
| 总通过率 | 100% |

结构化 JSON 报告包含恢复率、安全终止率、异常类型、终止代码、问题代码、故障事件、步骤、工具/LLM 调用、重试次数和预算检查。JUnit XML 报告可由 GitHub Actions 或其他 CI 平台直接展示；默认质量门将两份报告写入 `build/reports/`。

## 9. 后续维护

- `synthetic` 样本负责验证框架和 CI 确定性，不能替代真实端到端回归。
- `live` 样本负责暴露真实供应商数据质量问题；当前优先修复最低景点保障、过长通勤替换和路线不可用降级。
- 高德、LLM 协议、标准化模型或质量阈值发生变化时，应在稳定环境增量录制受影响的 Live 场景，再执行 15 样本全量离线回放。
- 更新固定场景或录制格式时必须提升版本，不能静默覆盖旧语义。
- 真实录制进入仓库前必须确认不含用户自由文本、密钥、个人联系方式和内部地址。
- 建议把 Live 基线刷新记录、模型版本、高德配置版本和人工脱敏复核结果写入发布清单。
