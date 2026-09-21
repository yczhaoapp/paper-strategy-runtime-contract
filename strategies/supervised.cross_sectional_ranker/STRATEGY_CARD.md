# supervised.cross_sectional_ranker

- 策略类别：`supervised`
- 策略版本：`1.0.0`
- 执行入口：`strategy.py:Strategy`
- 训练模式：`required`
- 允许动作：no_op, prediction, target_weight
- 确定性种子：`7`

## 数据契约

- `bar` / `bar` `P1D`: close, high, low, open, volume

本策略仅用于可复现研究演示，不构成投资建议。

## 论文依据

- 来源 ID：`gu2019`
- 忠实度：`method_reproduction`
- 机器证据：`paper-binding.json`；包含页级声明、实现哈希、假设与偏差。
