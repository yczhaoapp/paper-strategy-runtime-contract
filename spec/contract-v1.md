# PSRC Contract v1

## 目的

PSRC 在论文衍生交易策略、数据集、模型训练环境、回测引擎和执行场所之间定义稳定的语义边界。它不承诺每个策略都能在每个引擎上运行；它承诺：只有声明的要求能够被精确满足，或能够通过明确授权且可审计的转换满足时，运行才会被接受。

## 规范用语

本文中的**必须**、**不得**、**必需**、**应当**和**可以**为规范性要求，分别对应 **MUST**、**MUST NOT**、**REQUIRED**、**SHOULD** 和 **MAY**。

## 四份声明

一次运行必须由以下对象共同编译：

- `StrategyManifest`；
- `DatasetManifest`；
- `EngineCapabilities`；
- `RunPolicy`。

编译器必须返回 `ExecutionPlan` 或结构化 `ContractError`。编译成功之前，不得 import 或执行策略代码。

## 稳定核心与扩展

稳定核心包含带版本的策略、行情、标的、账户、决策、订单、执行事件、产物、错误和报告模型。供应商专属字段必须放在反向域名扩展命名空间中，并由拥有该命名空间的适配器验证。

Python 类型是参考 SDK。生成的 JSON Schema 与生命周期消息语义共同构成语言中立的公开契约。为保持生态兼容，机器字段名、枚举值和错误码使用英文；文档语言不影响线格式。

## 公开对象表

| 对象 | 必需语义 |
| --- | --- |
| `StrategyManifest` | 标识/版本、规则/监督学习/强化学习类别、import 入口、能力画像、生命周期、数据/动作/资源要求、确定性种子 |
| `DatasetManifest` | 不可变数据流清单、实际字段/标的/粒度、记录数、Schema 与数据 SHA-256 |
| `MarketEvent` | 唯一 ID、标的、序号、事件/可用/接收时间戳，以及可判别 payload |
| `AccountSnapshot` | 时间戳、基础货币、现金、权益、持仓和活动订单 |
| 规范 `Action` | no-op、prediction、目标仓位/权重、提交、撤销或替换订单 |
| `TrainingRequest` | 运行和数据集 ID、确定性种子，以及监督特征/标签或 RL transitions |
| `ArtifactManifest` | 模型/策略/状态文件、字节数、SHA-256、框架、数据集和种子来源 |
| `EngineCapabilities` | 支持等级、画像、数据/动作、成交/费用/滑点/队列/延迟/排序与沙箱模式 |
| `ExecutionPlan` | 四份声明的哈希，以及每项要求对应的兼容性记录 |
| `RunReport` / `FailureReport` | 带原始语义上下文的完整成功或失败证据 |
| `RunBundle` | 成功报告、四份原始声明及可选训练请求的单一聚合对象 |

原型中的 `MarketEvent` payload 覆盖 OHLCV bar、trade、L1 quote 和 L2 snapshot。公开枚举为 L2 delta、MBO、公司行动、标的状态及 custom event 预留可兼容的 minor 扩展。价格、数量、现金和仓位使用 Decimal 语义，不使用二进制浮点。时间必须含时区；事件可用时间不得早于市场事件时间，接收时间不得早于可用时间。

分钟和日线 bar 使用 `PT1M`、`P1D` 等 ISO-8601 duration，并同时声明时区、日历和对齐方式。tick 类数据使用 `mode=event`，不得声明 bar interval。

所有对象拒绝未知核心字段。JSON 是线传输/存储格式，YAML 可用于人工编写声明。`schemas/generated/` 中具有稳定 `$id` 的 Schema 是 Contract `1.1.0` 的规范机器表示；历史 `v1.0.0` Release 保持不可变。每份 Schema 明确声明 JSON Schema Draft 2020-12，所有内部 `$ref` 均指向本文档的 `#/$defs`，因此标准验证器无需网络或外部 registry 即可验证实例。

`StrategyCodeEvidence` 是 `1.1.0` 新增的可选线对象：它列出策略包 Python 文件的路径、大小和 SHA-256，并记录受信运行时源码树哈希。`ExecutionPlan.strategy_code_evidence_sha256` 绑定整个证据对象，当前完整验证要求新生成的 Bundle 包含它；字段保持可选是为了让 `1.0.0` 文档继续可读。

`TrainingInputEvidence` 对训练请求的完整规范 JSON、数据集标识、样本类型、记录数和输入宽度建立 SHA-256 绑定。当前策略包与新生成的可训练 Bundle 必须包含该对象，执行计划同时固定其哈希；字段在线格式中保持可选，以便读取 1.0/1.1 的既有 Bundle，但当前完整验证不会接受缺少训练输入证据的新生成物。

## 能力画像

v1 注册表初始包含：

- `core.bar.v1`
- `event.trade.v1`
- `event.l1.v1`
- `event.l2.v1`
- `event.l3-mbo.v1`
- `execution.basic.v1`
- `execution.advanced.v1`
- `portfolio.batch.v1`
- `training.supervised.v1`
- `training.rl.v1`
- `live.broker.v1`

画像采用加法扩展。策略必须声明所需画像及具体约束；引擎只能声明适配器已实际覆盖的画像。

引擎支持等级与能力形状是两个概念。`PROFILED` 不代表可运行。默认策略至少要求 `ADAPTER_AVAILABLE`，只有原生引擎自动化一致性证据才能标记为 `CONFORMANCE_VERIFIED`。

## 非目标

PSRC 不保证策略盈利，不保证不同成交模型下 PnL 相等，也不对第三方引擎作生产认证。跨引擎结果通过不变量和明确记录的执行语义比较。
