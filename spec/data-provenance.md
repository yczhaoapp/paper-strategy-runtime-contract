# 数据来源与经验声明契约 1.0

题目允许公开样例数据或模拟行情。本契约不把两者混为一个布尔值：每个深度论文案例和每个 D1 契约策略生成 `DataSourceEvidence`，并嵌入 `DatasetManifest.extensions["org.singularityx.data-provenance"]`。

## 来源等级

| `origin` | 含义 | 允许声明 |
| --- | --- | --- |
| `public_historical` | 可公开取得、内容哈希固定的历史市场观察 | 证明真实数据解析、因果可用时间和回测接入；不能自动证明原论文结果 |
| `synthetic_fixture` | 项目确定性生成的可重复事件 | 证明字段、生命周期、训练、动作和撮合语义；不能声明历史市场表现 |

所有证据记录数据 ID、SHA-256、标的、时间范围、记录数、转换、许可/权利说明，并固定 `original_paper_dataset=false` 与 `empirical_results_reproduced=false`。若未来加入原论文数据和经验结果，应发布新的契约版本及单独的统计验收，不得修改这两个固定字段伪装升级。

## 时间和前视

事件同时声明 `event_time`、`available_time` 和 `receive_time`。公开 AAPL 日线在交易日期之后一个 UTC 日才可用，因此策略无法在同一根 bar 尚未完成时交易其收盘值。监督标签必须完全落在训练分区；规则策略也必须先完成 lookback warm-up。

## 公开 AAPL 样例

`data/public/finance-charts-apple.csv` 含 2015-02-17 至 2017-02-16 的 506 根 AAPL 日线 OHLCV。`data/public/source.json` 通过 `PublicDatasetSource` Schema 固定来源、哈希、列映射、许可和权利边界；运行证据另外保存来源登记文件本身的哈希。许可证文本也随仓库保存并独立校验 SHA-256。源仓库采用 MIT 许可，但其 README 未单独说明该行情文件的上游来源，因此本项目只声明它是公开、可复验的样例，不声明其属于公共领域。

## 验收

独立验收重新读取本地数据证据和实际运行 bundle，要求深度论文套件同时出现 `public_historical` 与 `synthetic_fixture`。18 个契约策略还要求至少 6 个 D1，且规则、监督、RL 各至少 2 个；D1 训练型策略必须同时声明公开训练数据 ID 与来源元数据。公开文件字节、记录数、可用时间、训练来源或数据证据被修改时，测试或哈希检查必须失败。
