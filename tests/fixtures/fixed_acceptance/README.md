# 固定端到端验收录制目录

此目录用于保存 `AgentState` JSON 录制文件，不保存 API Key 或请求头。

录制当前服务的 15 个固定场景：

```powershell
python scripts/run_fixed_acceptance_baseline.py --execute `
  --record-dir tests/fixtures/fixed_acceptance `
  --output data/fixed-acceptance-report.json
```

离线回放：

```powershell
python scripts/run_fixed_acceptance_baseline.py `
  --replay-dir tests/fixtures/fixed_acceptance `
  --output data/fixed-acceptance-report.json
```

建议只提交经过确认且不包含用户隐私的录制文件。Provider 原始响应如需录制，
应另行脱敏；当前基线以完整 `AgentState` 为稳定回放边界。
