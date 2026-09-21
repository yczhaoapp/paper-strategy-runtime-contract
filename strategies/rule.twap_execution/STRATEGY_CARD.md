# rule.twap_execution

- 策略类别：`rule`
- 策略版本：`1.0.0`
- 执行入口：`strategy.py:Strategy`
- 训练模式：`not_required`
- 允许动作：no_op, submit_order
- 确定性种子：`7`

## 数据契约

- `bar` / `bar` `PT1M`: close, high, low, open, volume

本策略仅用于可复现研究演示，不构成投资建议。

## 论文依据

- 来源 ID：`pardo2022`
- 忠实度：`formula_reproduction`
- 机器证据：`paper-binding.json`；包含页级声明、实现哈希、假设与偏差。
