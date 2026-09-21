# 兼容性与审计

兼容性默认严格。每项解析只有四种公开结果：

- `EXACT`；
- `TRANSFORMED_LOSSLESS`；
- `TRANSFORMED_LOSSY`；
- `UNSUPPORTED`。

所有转换都必须由 `RunPolicy` 明确列入白名单；有损转换还必须设置 `allow_lossy=true`。每次转换都记录标识符、版本、理由、参数、输入/输出 Schema 哈希、受影响记录数、损失分类，以及是否可禁用。

以下行为绝不能隐式发生：字段别名、时区变更、标的映射、fill-forward、缺失值填补、tick 聚合为 bar、深度降级、订单数量取整、订单类型模拟、替换引擎以及降低沙箱级别。

引擎支持等级也参与协商。`PROFILED` 表示已有需求与映射记录，但没有可执行适配器。默认运行策略至少要求 `ADAPTER_AVAILABLE`，因此仅画像引擎不会被误认为可运行集成。`CONFORMANCE_VERIFIED` 还要求真实原生引擎自动化测试。本研究原型明确不声称 `PRODUCTION_CERTIFIED`。

当前实际实现两个转换：`symbol.map.v1` 要求显式的一对一映射，属于无损、可逆转换；`bar.resample.v1` 将更细 bar 聚合为较粗 bar，属于有损、不可逆转换。二者可以在一份计划中按记录顺序组合。

兼容性记录属于 `ExecutionPlan`，先于策略 import 生成。运行时必须在进入适配器前应用该计划，并在 `RunInputEvidence` 中同时保存源事件、有效事件、两者 SHA-256 和实际转换标识；报告日志记录转换前后数量。关闭某个转换后，同一不满足输入必须稳定失败，而不能寻找另一条未声明路径。`psrc demo compatibility` 提供 `SYNTH.TEST → SYNTH.MAPPED`，再把 10 条一分钟事件实际变为 2 条五分钟事件的端到端证据；`10 → 2` 表示事件聚合数量，不是成功率。
