# 运行接口

本文件定义 Contract v1 在参考 Python SDK 中的真实调用边界。其他语言和引擎可以使用不同函数名，但输入、输出、顺序及失败语义必须等价。

## 策略包统一入口

```bash
psrc run --strategy-dir <package> \
  --engine <reference|backtrader|nautilus-trader> \
  --output <report-directory>
```

该入口从目录读取策略 manifest、数据 manifest、精确输入事件及可选训练请求。它必须在 import 前依次完成声明解析、输入内容哈希校验、所选引擎能力编译和源码安全扫描，再根据生命周期使用同一 orchestrator 执行训练、内容寻址保存、完整性校验加载、推理和回测。`--engine` 默认为 `reference`；只有选定的引擎依赖和实现会被加载，能力不匹配时禁止替换引擎。目录格式见 [策略包规范](package.md)。

论文来源驱动的外部包使用 `psrc author run`。该入口额外要求 `--source` 和 `--spec`，将原文、页内证据、结构化方法说明和策略源码绑定后，再调用同一个运行入口。

## 行情与时间粒度

规范行情联合类型包含 `BarPayload`、`TradePayload`、`QuoteL1Payload` 和 `BookSnapshotL2Payload`。它们分别表达 OHLCV、逐笔成交、Level-1 买卖报价和 Level-2 多档快照。当前公共 v1 不声明 L2 增量簿、逐笔委托或任意 custom payload。

逐事件 tick 数据声明为 `Timeframe(mode="event", interval=null)`；分钟线和日线分别使用 `Timeframe(mode="bar", interval="PT1M")` 与 `P1D`。策略在 `DataRequirement` 中同时声明 stream、kind、粒度、标的、必需/可选字段、lookback、深度和最大陈旧时间。数据集用对应 `DatasetStream` 声明实际供给，编译器在代码导入前比较二者。编译期用记录数排除必然无法满足的 lookback；兼容转换完成后，运行时按每个必需标的核对实际观察数。`max_staleness_ns` 的机器语义固定为 `available_time - event_time`，任一事件超过上限即返回 `DATA_STALENESS_EXCEEDED`。包加载和 orchestrator 还会核对实际 bar 的 `event_time`：当前 UTC/epoch 网格要求时间戳为声明周期的绝对整数倍，允许跨过任意数量的合法周期。

## 账户、动作与输出

`AccountSnapshot` 每次推理提供时间、基础币种、现金、权益、持仓和活动订单；持仓包含数量、均价、已实现和未实现损益，活动订单包含类型、方向、数量、目标数量、限价和状态。`realized_pnl` 是本次运行中该标的平仓部分累计的毛价格损益，平仓后仍保留；`unrealized_pnl = quantity × (当前可用行情标记价格 − 持仓均价)`。二者不扣佣金，佣金反映在现金、权益和成交的 `fee` 中；不能把未知损益静默写为零。策略返回 no-op、prediction、target position、target weight、submit、cancel 或 replace 七种规范动作。Adapter 必须将订单生命周期写成 `OrderEventRecord`，成交写成 `Fill`，账户逐事件写成快照。

成功运行同时输出执行计划、决策、订单、成交、账户、模型产物、生命周期和 `RuntimeLogRecord`；`RunBundle` 再绑定实际输入、manifest、运行时能力、引擎能力、运行策略、源码及可选训练/外部准入证据。字段清单与实际支持边界见[公开接口覆盖](../docs/public-interface-coverage.md)。

## 编译入口

```python
compile_run(
    *,
    run_id: str,
    strategy: StrategyManifest,
    dataset: DatasetManifest,
    engine: EngineCapabilities,
    runtime: RuntimeCapabilities | None = None,
    policy: RunPolicy,
) -> ExecutionPlan
```

编译发生在策略 import 和运行之前。`training.supervised.v1` 与 `training.rl.v1` 由 `RuntimeCapabilities` 提供；数据、动作和执行画像由 `EngineCapabilities` 提供。成功结果包含五份声明的哈希、两类能力画像的无重叠分工及逐项兼容性记录；不满足要求时抛出带 `ContractError` 的 `ContractViolation`，不得尝试替代引擎、运行时或隐藏转换。

## 策略推理入口

```python
class RuntimeStrategy(Protocol):
    manifest: StrategyManifest

    def on_start(self) -> None: ...
    def on_event(
        self,
        event: MarketEvent,
        account: AccountSnapshot,
    ) -> tuple[Action, ...]: ...
    def on_finish(self) -> None: ...
```

`on_event` 是统一推理回调。它只能读取当前已可用的规范行情事件和调用时账户快照，并返回声明动作空间内的规范动作。每个决策批次中，同一标的最多有一个最终目标仓位或目标权重；重复目标会被拒绝，不能按顺序累加。目标动作转成原生订单后，派生数量仍必须满足单笔订单上限。非法动作以 `ACTION_INVALID` 或 `ORDER_REJECTED` 失败；异常不得被改写成 no-op 成功。

Reference 的直接订单仅支持 market/limit + `day`。`day` 使用运行时 `available_time` 的 UTC 日期作为 24×7 会话：订单在下一 UTC 日期的第一条事件参与撮合前过期。不属于 UTC/24×7 的数据流若声明直接下单能力，将以 `ENGINE_CAPABILITY_UNSUPPORTED` 失败，不能把未知交易所会话猜成 UTC 日。直接订单的接单、改单和成交都核对累计仓位上限。

## 训练与模型入口

```python
class TrainableStrategy(Protocol):
    def train(
        self,
        request: TrainingRequest,
        store: ArtifactIO,
    ) -> ArtifactManifest: ...

    def load(
        self,
        manifest: ArtifactManifest,
        store: ArtifactIO,
        *,
        run_id: str,
    ) -> None: ...
```

`TrainingRequest` 包含运行 ID、数据集 ID、确定性种子，以及监督学习的特征/标签或强化学习 transitions。orchestrator 在训练开始前重算 `TrainingInputEvidence` 并与 `ExecutionPlan.training_input_evidence_sha256` 比较；缺失或不同都以 `TRAINING_DATA_MISMATCH` 失败。`ArtifactIO` 是每次运行独立的最小能力对象，只开放字节保存和读取；它不暴露主机 `ArtifactStore`、存储根或验证方法，并在进入受信 I/O 前拒绝非内建标量和字节类型。`train` 返回带 `training_request_sha256` 的 `ArtifactManifest` 后，orchestrator 使用独立主机服务核对请求哈希、数据集、seed、路径、大小和 SHA-256。`load` 必须通过同一能力对象实际读取该规范产物；空回调或被替换的能力方法没有受信读取记录，不能进入推理。产物缺失或哈希不符分别返回 `ARTIFACT_NOT_FOUND`、`ARTIFACT_HASH_MISMATCH`。

## 回测引擎入口

```python
class BacktestAdapter(ABC):
    capabilities: EngineCapabilities

    @final
    def run(
        self,
        *,
        plan: ExecutionPlan,
        strategy: RuntimeStrategy,
        events: tuple[MarketEvent, ...],
        sandbox_mode: SandboxMode,
    ) -> RunReport:
        # 公共模板：绑定上下文和源事件、执行兼容计划、验证有效事件。
        ...

    @abstractmethod
    def _run_validated(..., events: tuple[MarketEvent, ...]) -> RunReport: ...
```

适配器实例必须暴露本次运行的稳定 `EngineCapabilities`。`run` 是基类提供的最终公共模板，不允许具体引擎覆盖：它核对实际策略 ID/manifest 哈希、Adapter 引擎 ID/能力哈希、沙箱等级和源事件内容哈希，执行计划中的显式兼容转换，再验证转换后的数据要求，包括实际 payload 字段与 L2 深度。具体引擎只实现 `_run_validated`。orchestrator 在训练前调用同一棚栏预检；`RunReport` 与 `RunBundle` 再做防御性一致性校验。适配器驱动 `on_start -> on_event* -> on_finish`，把规范动作映射到原生引擎，并把订单、成交、账户、日志和指标还原为 `RunReport`。它必须遵守 `ExecutionPlan` 中的成交与时间语义，不得吞掉拒单。策略或原生引擎泄漏的异常统一封装为结构化 `BACKTEST_FAILED`，同时保留原因链。

## 失败出口

所有阶段失败均以 `ContractError` 表示，并可写成 `FailureReport`。失败报告包含原始策略、数据集、引擎能力、运行策略及实际输入上下文；任何 fallback 成功都不能覆盖原始失败。运行生命周期进入 `FAILED` 后不可再次转换。

一个 `--output` 目录只表示最近一次运行尝试。新运行会清理该目录中列入公开清单的 PSRC 自有报告、Bundle、事件、成交和 artifact store，再写入本次结果；未知用户文件不会删除。失败发布还会再次清理成功专属文件，因此 `report.json=failed` 时同一目录不得保留旧 `bundle.json`、fills、orders 或模型产物。
