> 2.0 验证范围：默认运行 Reference 和 Backtrader。以下 Nautilus 适配设计保留，但其依赖独立为 `--extra nautilus`，默认支持等级为 ADAPTER_AVAILABLE，未计入本次默认动态验证。任何旧文档中的三引擎说明均以此处为准。

# 引擎适配器指南

适配器必须继承 `psrc.adapters.base.BacktestAdapter`，发布一份 `EngineCapabilities`，并实现 `_run_validated`；不得覆盖公共 `run` 模板。该模板在任何直接或 orchestrator 调用中统一绑定执行计划、实际策略、能力、沙箱和源事件，并在进入引擎前执行显式兼容转换。能力声明是一项可检验的承诺，因此必须保守，不能用“计划支持”冒充“已经支持”。训练画像由 `RuntimeCapabilities` 声明；适配器只负责行情、动作、执行和回测能力，不重复声明 orchestrator 的训练能力。

## 支持等级

| 等级 | 含义 |
| --- | --- |
| `PROFILED` | 已记录字段映射和语义缺口，但仓库没有可执行桥接 |
| `ADAPTER_AVAILABLE` | 存在可执行桥接，但尚无原生引擎一致性证据 |
| `CONFORMANCE_VERIFIED` | 已有原生引擎自动化测试及生成证据 |
| `PRODUCTION_CERTIFIED` | 需要外部生产运营认证；本项目不作此声明 |

`RunPolicy.minimum_engine_support` 默认为 `ADAPTER_AVAILABLE`。只有操作者显式降低策略时，`PROFILED` 引擎才可用于离线兼容性分析；本运行时不会执行它。

可执行引擎通过同一策略目录入口选择：

```bash
psrc run --strategy-dir <package> --engine backtrader --output <report-directory>
```

引擎解析是显式的；依赖缺失返回 `ENGINE_DEPENDENCY_MISSING`，能力不足返回 `ENGINE_CAPABILITY_UNSUPPORTED`，两者都不会回退到 Reference。

动作 Schema 表达跨引擎的规范并集，不代表每个引擎实现每一种订单语义。Reference 当前只接受 UTC/24×7 会话的 `day` 有效期，订单在下一 UTC 日期的第一条事件撮合前过期；`gtc`、`ioc` 和 `fok` 会以 `ORDER_REJECTED` 明确失败，其他日历/时区以能力不支持失败，不能退化为长期挂单。`max_abs_position` 对目标仓位和直接订单都表示成交后的总仓位上限；实现必须在接单、改单和成交前计入待成交敞口。

## 新增适配器的必要步骤

1. 继承 `BacktestAdapter` 并只实现 `_run_validated`；不得复制或绕过公共入口棚栏。
2. 映射每一种规范数据类型，不得伪造缺失信息。
3. 声明标的映射、时区、交易日历和时间戳含义。
4. 转换 `AccountSnapshot` 与已支持动作，拒绝其余所有动作。
5. 固定成交、费用、滑点、队列、延迟及同时间戳排序语义。
6. 保持 next-event/no-look-ahead 语义；若不同，必须声明并审计。
7. 把原生订单/成交规范化到 `RunReport`，不得吞掉拒单。
8. 增加引擎画像 YAML、直接入口负例及真实依赖上的原生一致性测试。
9. 只有生成证据中出现该原生测试后，才能提升支持等级。

## 当前清单

| 引擎 | 状态 | 已执行范围 |
| --- | --- | --- |
| Reference | `CONFORMANCE_VERIFIED` | bar、trade、L1、L2；Contract v1 全部动作 |
| Backtrader | `CONFORMANCE_VERIFIED` | 单标的 bar/基础执行画像；监督 Logistic 与 RL Tabular-Q 的完整训练—保存—重载—推理—回测 |
| NautilusTrader | `ADAPTER_AVAILABLE` | 单标的 bar/基础执行画像；默认门禁不动态认证 |
| QuantConnect LEAN | `PROFILED` | 仅设计映射 |
| Microsoft Qlib | `PROFILED` | 仅批量 ML/信号映射 |
| vn.py | `PROFILED` | 仅 CTA/事件/网关映射 |

仅画像 YAML 会列出尚未解决的语义决策。它们是下一步开发适配器的需求文档，不是引擎已经实际运行的证据。

## 扩展约束

引擎专属配置必须放在其拥有的反向域名命名空间中，例如 `org.backtrader.*`，不能污染稳定核心字段。新增可选能力使用 minor 版本；改变已有字段或生命周期语义必须升级 major 版本。适配器若不能忠实实现策略声明，应返回 `ENGINE_CAPABILITY_UNSUPPORTED`，而不是静默更换引擎、数据或订单类型。
