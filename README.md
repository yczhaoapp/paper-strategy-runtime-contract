# Paper Strategy Runtime Contract 2.8.2

[![PSRC 完整验证](https://github.com/yczhaoapp/paper-strategy-runtime-contract/actions/workflows/ci.yml/badge.svg)](https://github.com/yczhaoapp/paper-strategy-runtime-contract/actions/workflows/ci.yml)

Paper Strategy Runtime Contract（PSRC）是面向 [SX-CH-003](https://github.com/SingularityX-Evolution/.github/blob/main/profile/challenge-board/tasks/SX-CH-003-paper-strategy-runtime-contract.md) 的独立维护实现，用统一契约连接论文策略、训练、推理和最小回测。软件版本 2.8.2；稳定运行契约为 1.4，论文绑定契约为 1.2。

**交付范围：18 个论文可追溯的契约策略（三类各 6 个）+ 4 个 PDF 到代码的深度纵向案例、32 份 JSON Schema、统一训练/保存/重载/推理/回测、结构化失败、可关闭的兼容转换、沙箱和机器验收。** 每个契约策略都带来源 PDF 哈希、页级声明、声明到实现符号和已执行测试的连接、实现文件哈希、确定性运行行为签名、假设与偏差；第四个深度案例直接运行于 Backtrader。

18 个策略中有 7 个公式复现、2 个算法复现、5 个等价方法复现和 4 个方法适配；9 个达到 A2，5 个达到 A1，4 个明确标为 A0。Spooner 的三个学习算法样例和 A2C Pairs 保留为差异化契约样例，但因动作、奖励或训练闭环改变，不再计入论文复现。算法、数据、实验结果和运行时采用四个独立保真轴。Donchian、Pairs Z-Score、Logistic、Ridge、A2C Pairs 和线性 Actor–Critic 共 6 个策略使用固定哈希的公开 AAPL 或 AAPL/MSFT 历史数据达到 D1，三类各 2 个；其余 12 个保留 D0 确定性夹具。全部维持 E0，不宣称论文收益；严格 Linux 容器的 R2 必须由当前版本动态收据证明。详见 [复现等级政策](docs/reproduction-fidelity-policy.md)。

## 一键复现

需要 Python 3.12 或 3.13，以及 uv 0.8.22。Windows、macOS、Linux 使用相同命令，不依赖 GNU Make：

```bash
uv run --frozen --extra dev --extra adapters python scripts/verify.py --fetch
```

第一次运行安装锁定依赖，并从作者/学术站点下载来源注册表中的哈希固定论文 PDF。随后执行格式、类型、Schema、生成物漂移、全量测试、覆盖率、18 个策略/论文绑定、8 类错误、兼容转换、两个引擎和 4 个深度论文案例。后续断网复验直接使用已安装解释器：

```bash
# macOS / Linux
.venv/bin/python scripts/verify.py --offline
# Windows
.venv\Scripts\python.exe scripts\verify.py --offline
```

结果在 `reports/generated/verification.json`，总验收在 `reports/generated/acceptance-report.json`。单步日志也在该目录。失败返回非零退出码，不沿用旧的成功验收结果。

严格容器验收（需要可工作的 Docker daemon）：

```bash
python scripts/verify-container.py
```

构建阶段使用 `--no-cache --pull` 安装依赖和取得论文；验证阶段为 `--network none`、只读根目录、非 root、无 capabilities、`no-new-privileges`、CPU/内存/PID 限制和 `noexec` 临时目录。验证脚本直接调用镜像内解释器，**不会在断网阶段运行 uv sync、pip 或项目构建**。产物在 `reports/strict/`，`image.json` 固定本次镜像内容 ID 与构建策略。

当前宿主机和严格 Docker 的实测范围见 [交付状态](docs/DELIVERY_STATUS.md)。Windows、Linux 与 macOS 的远端结果以 [GitHub Actions](https://github.com/yczhaoapp/paper-strategy-runtime-contract/actions/workflows/ci.yml) 及相应 artifact 为准；本机通过不替代远端平台证据，也不等于评审方的最终档位认定。

## 真实论文链路

```text
PDF / HTML / UTF-8 正文
  → 原文件 SHA-256 + 分页文本 + 文本哈希
  → 人工审阅过的声明式 recipe + 页码/短锚点/公式解释
  → 来源、证据、参数与歧义校验
  → ReproductionSpec + 确定性 Python 策略包
  → 契约编译 + 源码扫描 + 引擎能力协商
  → 训练 → 原子保存 → 校验重载 → 留出集推理 → 回测
  → 论文/数据/代码/模型/订单/报告证据链
  → 独立重新解析、重新编译和哈希复核
```

| 论文 | 实现 | 引擎 | 复现边界 |
| --- | --- | --- | --- |
| Avellaneda–Stoikov, 2008 | 库存保留价与最优价差近似，公式 29–30 | Reference | 确定性 L1 撮合；不复现 Poisson 成交实验 |
| Gould–Bonart, 2015 | 队列不平衡、下次中价变化标签、逻辑回归 MLE | Reference | 时间切分；交易阈值是显式新增策略 |
| Nevmyvaka–Feng–Kearns, 2006 | 时间/库存/市场状态、经验 Bellman 反向更新、截止执行 | Reference | 简化 L1 全额成交与三个动作 |
| arXiv:1504.04254, 2015 | VMA(1,20,0.01)，公式 1–4 | **Backtrader** | 复现规则；不复现原指数、显著性检验和收益 |

Ma 案例使用公开 AAPL 2015–2017 日线 OHLCV，原始 CSV、来源记录、许可文本和 SHA-256 随仓库固定，并把每根日线的可用时间设为下一 UTC 日以阻断同 bar 前视。它证明公开真实数据轨可运行，但不是原论文的中国指数数据，因此仍不构成原论文实证结果复现。其他三个深度案例使用确定性合成 L1 行情，以隔离公式、训练和执行语义。

```bash
uv run --no-sync psrc paper ingest --source paper.pdf --output document.json
uv run --no-sync psrc paper reproduce \
  --source papers/sources/gould2015.pdf \
  --recipe papers/recipes/gould2015.json --output runs/gould
uv run --no-sync psrc paper suite --output runs/papers
```

每次编译要求空输出包目录，防止旧文件混入结果。案例生成 `document.json`、`package/reproduction-spec.json`、`split-evidence.json`、`build-manifest.json`、策略源码、训练请求、`training-input-evidence.json`、模型、`paper-run.json` 和 HTML 报告。

18 个绑定还固定一次确定性统一运行的输入哈希、决策哈希、实际动作和原因码、训练算法与内容寻址产物，以及决策/订单/成交计数。总验收只接受本轮 JUnit 中确实执行过的精确测试节点；运行行为、测试选择或策略源码任一变化都要求显式重新审阅绑定。

**新增论文有两条路径：**受审内置算法可进入 registry、recipe 和确定性编译器；其他论文可由人或 LLM 形成 `PaperStrategySpec` 与独立策略包，再走通用外部接入门禁。后者会把调用方提供的 PDF/HTML/文本、页内锚点、规格、manifest 与策略源码绑定，支持规则、监督和 RL。当前自动化测试使用三类确定性作者夹具验证这条接口；它们不是额外的真实论文复现案例，也不证明系统能在无人审阅时理解任意论文。详见 [论文契约](spec/paper-pipeline.md)与[公开接口覆盖](docs/public-interface-coverage.md)。

```bash
uv run --no-sync psrc author run \
  --source paper.pdf --spec paper-spec.yaml \
  --strategy-dir my-strategy --output runs/my-strategy
```

该入口先审计后导入。来源不符、证据锚点不存在、规格与 manifest 不一致或训练/推理语义缺失时返回非零状态，不会退回内置策略或猜测字段。

## 运行契约与扩展

- 统一声明覆盖规则、监督和 RL；tick/L1/L2、分钟线、日线；账户、订单、模型、日志和生命周期。
- tick 在 Contract v1 中用 `event` 粒度表达；当前可执行行情类型严格限定为 OHLCV bar、trade、L1 quote 和 L2 snapshot。
- 18 个契约示例是不同算法，覆盖配对、截面、预测、目标仓位、订单和撤改；每个包内含 `paper-binding.json`。[策略矩阵](docs/strategy-matrix.md) 列明来源和忠实度。
- Tabular Q、SARSA、Double Q 同时要求多参数公式、终止/零学习率性质、每种算法 64 组固定 seed 随机单步公式，以及 16 组动态生成训练请求的完整多轮差分训练。后者核对整张持久化 Q 表、训练来源并通过公开事件入口验证训练集中未见状态；这些证据证明算法实现和生命周期真实，不把已改变的市场问题提升为论文复现。
- `RuntimeCapabilities` 单独声明 orchestrator 提供的监督/RL 训练能力；行情、动作和撮合画像仍由 `EngineCapabilities` 提供。`Reference` 与 `Backtrader` 为默认实测引擎，Backtrader 另有监督与 RL 的训练—重载—推理—回测门禁。NautilusTrader 是单独的可选适配器：`uv sync --extra dev --extra adapters --extra nautilus`；它的安装平台有额外要求，不计入默认验证。选择缺失或不支持的能力提供者会失败。
- 兼容转换默认关闭；显式开启后记录依据、是否有损、源/目标字段、影响范围及哈希。
- 训练产物按内容和来源寻址，先在临时目录完整写入再原子发布。策略只收到每次运行独立的最小产物读写能力对象，不会收到受信 `ArtifactStore`、存储根或验证方法；输入必须先归一化为内建字符串、字节、整数和字符串字典。运行时在 `train` 返回后独立重读规范 manifest，在 `load` 阶段要求策略实际读取同一份已验证字节，随后再次核对路径、大小、哈希和训练来源。
- 包内策略只能导入 manifest 白名单模块和最小 `psrc.strategy_api`。扫描器与进程内审计钩子执行资源契约并覆盖已知文件、进程和网络反例；它们不是抵御任意恶意 Python 的独立安全边界。严格模式的宿主隔离由非 root、断网、只读根目录和资源受限的 Docker 容器提供；`--require-strict` 不可降级。

```bash
uv run --no-sync psrc run --strategy-dir strategies/rule.sma_cross \
  --engine backtrader --output runs/sma
uv run --no-sync psrc sandbox run --strategy-dir strategies/rule.sma_cross \
  --engine backtrader --output runs/isolated
```

## 阅读顺序

1. [独立审计修复](docs/audit-remediation.md)、[既有评审问题闭环](docs/review-closure.md)、[S 级验收映射](docs/acceptance-audit.md)、[复现等级政策](docs/reproduction-fidelity-policy.md) 和 [模糊标准的严格解释](docs/interpretation-register.md)
2. [架构与信任边界](docs/architecture.md)、[论文流水线](spec/paper-pipeline.md)
3. [运行契约](spec/contract-v1.md)、[接口](spec/runtime-interfaces.md)、[错误](spec/errors.md)
4. [复验说明](docs/reproduction.md)、[引擎扩展](docs/adapter-guide.md)
5. [许可证与第三方声明](NOTICE)

代码采用 Apache-2.0。论文原文保存在忽略提交的本地缓存中；仓库分发来源链接、哈希、短证据锚点与实现，第三方论文不因本项目而改变许可。
