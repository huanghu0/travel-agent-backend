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

先启动服务，再执行：

```powershell
python scripts/run_fixed_acceptance_baseline.py `
  --execute `
  --base-url http://127.0.0.1:8000 `
  --record-dir recordings/fixed-acceptance-v1
```

真实录制会标记为 `source=live`。建议在固定模型、固定高德配置和隔离数据库环境中执行，并在提交前人工检查脱敏结果。

只允许真实样本回放：

```powershell
python scripts/run_fixed_acceptance_baseline.py `
  --replay-dir recordings/fixed-acceptance-v1 `
  --require-manifest `
  --allowed-source live
```

## 6. 故障注入

`FaultInjector` 按“目标 + 调用次数”确定性触发故障，支持：

- `timeout`
- `upstream`
- `rate_limit`
- `authorization`
- `invalid_output`
- `sqlite_locked`

`ToolRegistry(call_injector=...)` 可覆盖高德和 LLM 工具；`FaultInjectingProxy` 可包装 SQLite Store、路线缓存和餐饮缓存。生产环境不注入时行为不变。

预置故障场景覆盖：

- 高德景点查询首次超时
- LLM 首次返回无效结构
- SQLite 首次保存被锁
- 高德酒店查询首次返回 429

故障记录保存在 `FaultInjector.events`，可以断言故障是否真正触发，避免“测试看似通过但没有执行故障分支”。

## 7. 质量门

本地运行：

```powershell
python scripts/run_quality_gate.py
```

质量门依次执行：

1. Python 编译检查
2. 完整 Orchestrator 六类故障恢复验收
3. 全量单元测试和故障注入测试
4. 15 个固定样本 manifest 校验和离线回放
5. 六类故障场景和 15/15 固定样本全部通过时才返回退出码 0

GitHub Actions 配置位于：

```text
.github/workflows/quality-gate.yml
```

## 8. 完整 Orchestrator 故障恢复验收

完整验收位于：

```text
app/evaluation/orchestrator_faults.py
tests/test_orchestrator_fault_recovery.py
scripts/run_orchestrator_fault_recovery.py
```

该套件使用真实 `TripOrchestrator`、`ExecutionPolicy`、`ToolRegistry`、SQLite 检查点、校验器和确定性优化器，只替换高德与 Planner 外部边界，不访问网络，也不读取真实 API Key。

固定覆盖六类故障：

1. 景点查询首次超时，第二次调用恢复。
2. 酒店查询首次 429，保留限流语义且熔断器不误打开。
3. Planner 首次返回无效结构，在 `generate_plan` 原动作重试，不错误进入 `repair_plan`。
4. 鉴权失败不可重试，立即安全失败，不继续天气、酒店或 LLM 调用。
5. SQLite 第二次保存短暂锁定，由独立检查点策略有限重试，且不重复已完成工具。
6. 一条真实路线分段超时，保留其他成功分段，并由确定性路线重排后重新查询恢复。

SQLite 检查点只重试 `database is locked` 和 `database is busy`。连续三次仍失败时抛出 `AgentCheckpointError`，接口返回 503；其他 `OperationalError` 不重试，避免掩盖表结构或 SQL 错误。

单独运行：

```powershell
python scripts/run_orchestrator_fault_recovery.py
python -m unittest tests.test_orchestrator_fault_recovery -v
```

验收要求：故障必须真实触发；可恢复场景最终完成并可从 SQLite 恢复；不可恢复场景必须进入明确终态；步骤、工具和 LLM 预算不能越界；已完成会话再次 `resume()` 不产生外部调用。

## 9. 后续维护

- `synthetic` 样本负责验证框架，不能替代真实端到端回归。
- 高德或 LLM 协议发生变化时，应在稳定环境重新录制 `live` 样本。
- 更新固定场景或录制格式时必须提升版本，不能静默覆盖旧语义。
- 真实录制进入仓库前必须确认不含用户自由文本、密钥、个人联系方式和内部地址。
