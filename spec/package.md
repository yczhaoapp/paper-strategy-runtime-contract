# 策略包目录规范

Contract v1 的可执行策略包是一个可移动目录，最小内容如下：

```text
<strategy-package>/
├── strategy.yaml
├── strategy.py
├── dataset-manifest.json
├── input-events.json
├── training-request.json   # 仅监督学习和强化学习必需
├── training-input-evidence.json # 仅监督学习和强化学习必需
└── STRATEGY_CARD.md
```

`strategy.yaml` 的 `entrypoint` 使用安全相对文件入口 `strategy.py:Strategy`。入口不得是绝对路径、不得包含 `..`，导出的 18 个示例均遵守这一约定。外部论文策略可以替换 `strategy.py` 的实现，但实例公开的 `manifest` 必须与目录声明逐字段一致。

## 强制加载顺序

运行器必须依次执行：

1. 只读取并校验 `strategy.yaml`，对包内 Python 文件逐一计算 SHA-256，并计算受信 `psrc` 运行时源码树哈希；此时不得 import 策略代码；
2. 校验数据 manifest、精确事件、训练请求及 `TrainingInputEvidence`，并核对行情和训练输入 SHA-256；
3. 使用策略、源码证据、训练输入证据、数据、引擎和运行策略编译不可变 `ExecutionPlan`；
4. import 前重新计算源码证据，拒绝发现后修改（TOCTOU），再扫描包内全部 Python 源码，解析 import 别名和属性调用链；运行时命名空间只能使用公开 `psrc.strategy_api`；
5. 在开发模式或已证明的严格容器中加载相对文件入口；导入、构造和全部回调都受运行时审计围栏约束，直接文件/进程/网络操作失败；
6. 核对代码实例的 manifest 与目录 manifest 完全相同；
7. 训练回调只收到不含根路径和验证方法的 `ArtifactIO` 能力对象；主机 `ArtifactStore` 独立保存并验证物理字节，加载回调必须留下读取已验证产物的受信记录。随后执行推理和回测，生成含 `strategy-code-evidence.json`、`runtime-capabilities.json` 与训练输入证据的聚合 `RunBundle`。

包源码树哈希由按路径排序的 `{path,size_bytes,sha256}` 数组进行规范 JSON 编码后计算；路径必须是包内非符号链接的相对 `.py` 路径。运行时树使用相同算法。`ExecutionPlan.strategy_code_evidence_sha256` 绑定整个证据对象，因此 manifest 哈希、策略代码哈希和运行时实现哈希具有不同职责。

`training-input-evidence.json` 绑定 `training-request.json` 的完整规范内容，而不是假设训练数据与回测数据使用同一个 `dataset_id`。训练请求或证据缺失、数据集标识/样本/标签/转移/种子/元数据被单独改动时，必须在 import 和训练前返回 `TRAINING_DATA_MISMATCH` 或 `MANIFEST_INVALID`。规则策略不得携带这两个文件。

任一步失败都返回机器可读 `ContractError`，不得改用内置同名策略、替代引擎或宿主机执行。`psrc run --strategy-dir --engine` 是单包统一入口，可显式选择 `reference`、`backtrader` 或 `nautilus-trader`；不支持的数据或动作必须在策略 import 前失败。`psrc sandbox run --strategy-dir --engine` 从宿主机把单包只读挂载进严格 Docker 边界；`psrc demo all` 只负责发现多个目录并逐个调用同一执行链路。直接入口支持 `--require-strict`；要求不能满足时返回 `SANDBOX_UNAVAILABLE`，禁止退回开发模式。
