# 生命周期

## 运行状态机

```text
DISCOVERED
  -> VALIDATED
  -> CAPABILITIES_NEGOTIATED
  -> INITIALIZED
  -> TRAINING
  -> ARTIFACT_SAVED
  -> ARTIFACT_LOADED
  -> INFERENCE_BACKTEST
  -> FINALIZED

任一非终态 -> FAILED
```

非法状态转换必须以 `LIFECYCLE_TRANSITION_INVALID` 失败。

线格式状态名为 `DISCOVERED`、`VALIDATED`、`CAPABILITIES_NEGOTIATED`、`INITIALIZED`、`TRAINING`、`ARTIFACT_SAVED`、`ARTIFACT_LOADED`、`INFERENCE_BACKTEST`、`FINALIZED` 和 `FAILED`。规则策略从 `INITIALIZED` 直接进入 `INFERENCE_BACKTEST`；可训练策略在此之前必须依次经过三个产物状态。Python SDK 的 `LifecycleState.RUNNING` 保留为 `INFERENCE_BACKTEST` 的兼容别名。

实际公开调用边界为 `compile_run`、`TrainableStrategy.train/load`、`RuntimeStrategy.on_start/on_event/on_finish` 及 `BacktestAdapter.run`。其中 `on_event` 是逐事件推理入口；`BacktestAdapter.run` 是带最终棚栏的回测模板入口，直接调用仍校验上下文与源事件并执行兼容转换，具体引擎只实现已验证事件钩子。完整签名和失败语义见 [运行接口](runtime-interfaces.md)。不需要训练的规则策略不得伪造成功的模型产物，其产物列表为空。

`TrainingRequest` 携带运行 ID、数据集 ID、确定性种子和监督样本或 RL 转移。`TrainingInputEvidence` 固定其规范哈希、样本类型、记录数和输入宽度；执行计划再次绑定证据对象哈希。训练数据可以与留出回测数据使用不同的数据集标识，但不能只靠一个可任意填写的字符串建立来源关系。`DecisionRecord`、订单事件与运行日志分别携带确定性序号及其领域上下文；公开对象按各自 Schema 携带 Contract 版本。Contract v1 不虚构统一的 `request_id` 消息信封。批量数据可以通过不透明只读句柄传递，但不得把原始宿主机路径暴露给策略 worker。

训练返回持久化 `ArtifactManifest`，不能只返回报告外不可见的内存对象。加载阶段验证每个声明文件的哈希；只有加载成功后才可执行 `on_event` 推理。即使所选适配器拥有回测事件循环，报告也必须记录完整状态历史。
