# 公开接口覆盖与可执行边界

本文区分“Schema 中可声明”“参考引擎已执行”和“尚未承诺”三种状态。只有被源码、自动化测试和当次验收收据同时支持的能力，才计为已覆盖。

## 大模型如何使用运行契约

运行契约是机器可读的目标接口，不是自动猜测器。LLM 或开发者先把论文中的数据需求、训练目标、推理规则和动作写入 `PaperStrategySpec`，再实现 `strategy.py:Strategy` 与 `strategy.yaml`。`psrc author run` 同时接收原始论文、结构化规格和外部策略包，在导入策略代码前校验原文字节哈希、页内证据锚点、规格与 manifest、源码证据和引擎能力。通过后仍走普通的训练、保存、校验重载、推理和回测路径；作者审计不能授予绕过运行门禁的权限。

数据适配分成两层：调用方把来源数据明确转换为规范 `MarketEvent`；引擎 Adapter 再把规范事件和动作映射为目标引擎对象。运行时不会根据名称相似度猜字段，也不会静默补值。当前唯一实现的兼容转换是显式一对一标的映射和 bar 重采样。

## 覆盖矩阵

| 范围 | 公开对象或入口 | 可执行证据 | 当前边界 |
| --- | --- | --- | --- |
| 规则、监督、强化学习 | `StrategyKind`；`RuntimeStrategy`；`TrainableStrategy` | 18 个包三类各 6 个；外部未知规格三类黑盒运行各 1 次 | 未实现开放域论文的无审阅自动理解 |
| 训练入口 | `train(TrainingRequest, ArtifactStore)`；`RuntimeCapabilities` | 监督与 RL 全矩阵训练、原子保存和重载；实际请求与计划、产物及 Bundle 哈希链负例；训练画像与引擎画像分开协商 | 规则策略声明 `not_required` |
| 推理入口 | `on_event(MarketEvent, AccountSnapshot)` | 18 个策略统一回调与决策记录 | 单次目录输入在 Contract v1 中只允许一个可归属 stream |
| 最小回测 | `BacktestAdapter.capabilities`、`BacktestAdapter.run` / `psrc run` | Reference 全矩阵；Backtrader 规则差分及监督/RL 完整生命周期；直接 Adapter 也核对上下文、真实字段、L2 深度，并结构化封装异常 | NautilusTrader 为可选依赖，不能作为默认实测能力 |
| 模型产物 | `ArtifactManifest`、内容寻址文件、训练输入证据 | 保存后由运行时独立重读规范 manifest，按路径、大小和 SHA-256 在加载前后校验；产物固定训练请求规范哈希；根权限不可由策略改写 | 不把内存对象或策略自报成功当作可交付模型 |
| 日志与报告 | `RuntimeLogRecord`、`RunReport`、`FailureReport`、`RunBundle` | 成功和失败均输出机器可读文件 | 日志不能替代结构化订单、成交和错误对象 |
| tick / 事件 | `Timeframe(mode="event", interval=null)` | Trade、L1 quote、L2 snapshot 在 Reference 可运行 | “tick”在 v1 表示逐事件，不虚构固定 tick 周期 |
| 分钟线 / 日线 | `Timeframe(mode="bar", interval="PT1M" | "P1D")` | 18 个目录包同时覆盖 PT1M 与 P1D；实际时间错位反例失败 | UTC/epoch 逐 bar 校验；允许周期整数倍的缺口 |
| OHLCV | `BarPayload` | Reference 与 Backtrader 路径 | 字段为 open/high/low/close/volume，价格和 OHLC 关系受校验 |
| 成交 | `TradePayload` | tick 订单生命周期集成测试 | price、size、aggressor_side、trade_id；不声明逐笔委托 MBO |
| Level 1 | `QuoteL1Payload` | L1 规则、监督和深度论文案例 | bid/ask price 与 size；拒绝倒挂报价 |
| Level 2 | `BookSnapshotL2Payload` | L2 规则、监督和 RL 策略 | 仅快照 bids/asks；不声明增量簿或 MBO |
| 账户 | `AccountSnapshot` | 每事件保存 cash、equity、positions、open_orders | Reference 是单币种基础账户模型 |
| 订单输出 | 七种规范 `Action` | 目标仓位、权重、预测、提交、撤单、改单和 no-op 均有执行/失败测试；同标的重复目标及目标派生超限订单在两个 Adapter 上拒绝 | Reference 直接订单只实现 market/limit 和 UTC/24×7 day；累计仓位在接单、改单和成交前校验 |
| 订单状态 | `OpenOrder.status`、`OrderEventRecord.status` | trade tick 测试验证 accepted → replaced → filled | Reference 不支持 partial fill；Schema 保留 `partially_filled` 供具备能力的 Adapter 使用 |
| 结构化失败 | `ContractError` / `FailureReport` | 题目场景与无效 SemVer、实际时间错位等边界动态核对 | 失败不能改写为 no-op 或成功报告；失败目录不保留旧成功 Bundle |
| 兼容记录 | `CompatibilityRecord`、`RunInputEvidence` | 映射 + 重采样端到端、关闭后的失败、迟到 bar 与重复市场时间测试 | 默认关闭；有损转换还需 `allow_lossy=true`；OHLC 由市场时间决定，可用时间取最晚输入 |
| 数据窗口与陈旧度 | `DataRequirement.lookback`、`max_staleness_ns` | 编译期记录数负例、转换后逐标的检查、真实事件陈旧度负例 | 陈旧度定义为 available time 减 event time；不猜测来源系统时钟 |

## 失败和兼容性的硬约束

题目列出的关键失败均有稳定机器码：字段缺失 `DATA_FIELD_MISSING`、粒度不符 `DATA_TIMEFRAME_MISMATCH`、标的映射失败 `SYMBOL_MAPPING_FAILED`、不可交易动作 `ACTION_INVALID` 或 `ORDER_REJECTED`、训练失败 `TRAINING_FAILED`、回测失败 `BACKTEST_FAILED`。报告保留阶段、策略、引擎、原因链和 `fallback_used: false`。

每条兼容记录包含结果等级、转换 ID 和版本、理由、参数、输入/输出 Schema 哈希、影响记录数、是否可逆，以及固定为 true 的可关闭标志。源事件和转换后事件分别保存并计算 SHA-256。没有白名单的转换在策略代码导入前失败。

`tests/integration/test_public_interface_coverage.py` 直接执行 trade tick、账户现金/权益/持仓、挂单状态、改单和成交；同一文件还验证三类策略及 event/PT1M/P1D 三种要求确实存在于可运行目录。`tests/integration/test_strategy_packages.py` 用未注册规格和独立源码分别执行规则、监督和 RL 外部包，证明接入入口不依赖内置策略目录。
