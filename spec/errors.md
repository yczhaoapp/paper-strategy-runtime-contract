# 结构化错误

每次失败都表示为稳定的 `ContractError`，其中包含 Contract 版本、运行 ID、阶段、错误码、人类可读消息、是否可重试、机器可读细节及可选原因链。

初始稳定分类如下：

- 契约：`CONTRACT_VERSION_UNSUPPORTED`、`MANIFEST_INVALID`、`LIFECYCLE_TRANSITION_INVALID`；
- 数据与源码完整性：`DATA_STREAM_MISSING`、`DATA_KIND_MISMATCH`、`DATA_FIELD_MISSING`、`DATA_TIMEFRAME_MISMATCH`、`DATA_RECORD_COUNT_MISMATCH`、`DATA_ORDERING_INVALID`、`DATA_HASH_MISMATCH`、`SOURCE_HASH_MISMATCH`、`TRAINING_DATA_MISMATCH`；
- 映射：`SYMBOL_MAPPING_FAILED`、`ENGINE_CAPABILITY_UNSUPPORTED`；
- 适配器环境：`ENGINE_DEPENDENCY_MISSING`；
- 产物：`ARTIFACT_NOT_FOUND`、`ARTIFACT_HASH_MISMATCH`；
- 动作：`ACTION_INVALID`、`ORDER_REJECTED`；
- 运行阶段：`COMPATIBILITY_TRANSFORM_FAILED`、`TRAINING_FAILED`、`INFERENCE_FAILED`、`BACKTEST_FAILED`；
- 安全：`SANDBOX_UNAVAILABLE`、`SANDBOX_POLICY_DOWNGRADE`、`SANDBOX_POLICY_DENIED`、`SANDBOX_TIMEOUT`、`SANDBOX_RESOURCE_EXHAUSTED`、`SANDBOX_EXECUTION_FAILED`。

实现不得因为某条 fallback 路径成功，就把原本失败的运行改写为成功。错误必须保留原始阶段、原因链和 `fallback_used: false` 等可审计上下文。

题目要求的失败可通过 `psrc demo failures` 执行。证据将非法策略动作与非法订单拒绝分开，因此一共生成 8 个场景。每个场景都写出同一种 `FailureReport` 信封和单独的 `error.json`；只有实际稳定错误码与预期值相等时才构成证据。意外成功本身就是测试失败。

普通 `psrc run` 也使用同一失败信封，并尽可能保留已经解析到的策略、数据、引擎及实际事件摘要。Contract v1 的 `MarketEvent` 不含 `stream_id`，因此目录输入只接受一个可明确归属的 Dataset stream；多流声明不会绕过哈希和语义核对，而是返回 `DATA_STREAM_MISSING`。

## 论文层错误

新增 PAPER_INPUT_INVALID、PAPER_PARSE_FAILED、PAPER_SOURCE_MISMATCH、PAPER_EVIDENCE_MISSING、PAPER_RECIPE_INVALID、PAPER_COMPILATION_FAILED；语义见 [论文流水线](paper-pipeline.md)。复现入口把 ContractViolation 同时写入 failure.json 并返回非零退出码。
