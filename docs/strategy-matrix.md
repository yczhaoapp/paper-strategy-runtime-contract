# 策略覆盖矩阵

| 类别 | 策略 | 主要差异 | 论文来源 | 忠实度 |
| --- | --- | --- | --- | --- |
| 规则 | SMA 交叉 | 分钟 OHLCV、目标仓位 | Ma et al. 2015 | 公式复现 / A2 |
| 规则 | Donchian 突破 | 日线 OHLCV 突破 | Ma et al. 2015 | 公式复现 / A2 |
| 规则 | 配对 z-score | 双标的日线均值回归 | Khizbullin 2023 | 公式复现 / A2 |
| 规则 | L1 微价格 | 报价与对手方数量加权中价 | Stoikov 2017 | 公式复现 / A2 |
| 规则 | L2 库存做市 | 保留价、最优价差、双边限价单 | Avellaneda & Stoikov 2008 | 公式复现 / A2 |
| 规则 | TWAP 执行 | 定时切片与直接订单 | de Meer Pardo et al. 2022 | 公式复现 / A2 |
| 监督学习 | Logistic 方向 | 五维 OHLCV、sigmoid、1000 步梯度下降 | Chipwanya 2023 | 算法复现 / A2 |
| 监督学习 | Ridge 收益率 | 正则化连续收益率 | Moodi & Jahangard-Rafsanjani 2023 | 等价方法复现 / A1 |
| 监督学习 | Gaussian 成交量突破 | 生成式类别似然 | Saifan et al. 2021 | 等价方法复现 / A1 |
| 监督学习 | L1 逆向选择 | 队列不平衡 Logistic MLE | Gould & Bonart 2015 | 算法复现 / A2 |
| 监督学习 | L2 成交概率 | 常数死亡率 Erlang 队列竞争 | Lokin & Yu 2026 | 算法复现 / A2 |
| 监督学习 | 截面排序器 | pooled OLS 预测并截面排序 | Gu, Kelly & Xiu 2019 | 等价方法复现 / A1 |
| 强化学习 | Tabular Q 库存 | 离策略离散库存控制 | Spooner et al. 2018 | 等价方法复现 / A1 |
| 强化学习 | SARSA 趋势 | 在策略 bar 状态控制 | Spooner et al. 2018 | 等价方法复现 / A1 |
| 强化学习 | 风险厌恶 Contextual Bandit | 线性后验 Thompson 抽样、均值方差目标 | Lin, Wang & Zhou 2022 | 等价方法复现 / A1 |
| 强化学习 | Double-Q 订单簿库存 | 拆分估计器的 L2 库存控制 | Spooner et al. 2018 | 等价方法复现 / A1 |
| 强化学习 | A2C 配对 | 固定 1:-2 spread 状态、优势 Actor–Critic 更新 | Yang & Malik 2024 | 等价方法复现 / A1 |
| 强化学习 | 线性 Actor–Critic 配置 | 连续目标权重、策略梯度与价值更新 | Li, Wang & Cao 2023 | 算法复现 / A2 |

每个可训练示例都依次执行训练、内容寻址保存、完整性校验重载，再通过同一 Contract 进入推理/回测。每个策略目录包含完整机器 manifest、包内入口源码、数据 manifest、精确输入事件、可选训练请求、`paper-binding.json` 与中文 Strategy Card；`psrc demo all` 从这些目录发现并运行策略，而不是绕过目录调用内置 factory。包内入口在 manifest、输入哈希、能力和源码策略全部验证成功后才允许 import。

“18 个”不是把同一 SMA 参数复制 18 次。证据门禁会对策略类别、数据种类、周期、必要字段和动作空间生成契约指纹，并要求至少 12 种有实质差异的形状；当前矩阵还覆盖单/双/多标的、bar/L1/L2 及信号/目标仓位/直接订单等差异。

`formula_reproduction` 与 `algorithm_reproduction` 必须达到 A2；`method_reproduction` 对应 A1 等价方法复现。方法适配已从合法值中删除，不能计入 18 个策略。数据与实验结果另行评级；本表的 A2 不代表原论文行情或收益已复现。完整判定见[复现等级政策](reproduction-fidelity-policy.md)。

数据轴中，Donchian、Pairs Z-Score、Logistic 方向、Ridge Return、A2C Pairs 和线性 Actor–Critic 使用固定哈希的公开 AAPL 或 AAPL/MSFT 日线，规则/监督/RL 各 2 个达到 D1；其余 12 个策略为 D0。所有策略均为 E0。R2由严格容器的当次动态收据判定，不写入静态算法等级。

## 深度论文案例（额外验证路径）

| 案例 | 类别 | 数据/动作 | 引擎 |
| --- | --- | --- | --- |
| paper.avellaneda2008 | 规则 | L1、双边限价、撤单、库存风险 | Reference |
| paper.gould2015 | 监督 | L1、下一中价标签、MLE 概率、目标仓位 | Reference |
| paper.nevmyvaka2006 | RL | L1、私有时间/库存、限价/市价、截止执行 | Reference |
| paper.ma2015 | 规则 | 公开 AAPL 2015–2017 日线、百分比 band、目标仓位 | Backtrader |

这 4 个包由真实 PDF 与审阅配方在验证时生成，独立验证原文解析、编译与运行链路。它们不额外计入“18 个”数量，也不将同一算法跨引擎运行重复计数。
