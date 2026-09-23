# 独立复验

## 标准流程

1. 使用 Python 3.12/3.13 和 uv 0.8.22。
2. 执行 `uv run --frozen --extra dev --extra adapters python scripts/verify.py --fetch`。
3. 打开 `reports/generated/verification.json` 和 `acceptance-report.json`，确认均通过；检查逐步日志。
4. 如需离线验证，保留 `.venv` 与 `papers/sources`，直接使用虚拟环境 Python 执行 `scripts/verify.py --offline`。
5. 严格隔离使用 `python scripts/verify-container.py`；以 `reports/strict` 的实测结果为准。启动时旧成功收据立即失效；构建、镜像检查、容器运行和报告出版失败都留下本次 `attempt_id` 的失败状态。只有同一尝试的镜像、验证和验收文件全部成功才可发布。

没有 GNU Make 不影响流程。Windows 调用 `.venv\Scripts\python.exe`。Shell 脚本通过 `.gitattributes` 固定 LF，容器内执行的门禁脚本不调用依赖管理器。

## 报告路径

| 路径（相对于证据输出目录） | 内容 |
| --- | --- |
| verification.json | 本次每一步退出码、软件/Python 版本、完整验收输入树 SHA-256、作用范围和失败信息 |
| acceptance-report.json | 逐条硬门禁 |
| junit.xml / coverage.json | 真实测试和覆盖率结果 |
| runs/all/summary.json | 18 个论文可追溯契约策略与类别计数；每个策略包含保真分级的 `paper-binding.json` |
| runs/failures/summary.json | 8 类强制结构化失败 |
| runs/compatibility/bundle.json | 转换前后事件与审计记录 |
| runs/adapters/comparison.json | Reference 与 Backtrader 的实际差分 |
| runs/papers/summary.json | 4 个真实论文案例 |
| runs/papers/{paper}/paper-run.json | 原文、公式映射、代码、划分和标准运行 bundle |
| runs/papers/{paper}/index.html | 论文假设、偏差、诊断与运行报告链接 |

## 复核而非相信摘要

论文证据分两层检查。首先重算 18 个策略的来源 PDF、页级 claim、实现文件哈希、声明到符号映射、精确测试节点和策略类别，要求三类各 6 个且一对一覆盖；再从本轮 JUnit 证明这些节点实际通过，并将新生成 RunBundle 的输入/决策哈希、动作、原因码、产物和计数与审阅签名逐项对照。然后对 4 个深度案例从原 PDF 重新解析，重新计算 recipe/spec 并生成独立策略目录，逐文件比较生成物，加载标准 RunBundle，并对照源码证据、数据来源证据和实际模型文件哈希。可以修改一行生成策略代码、一个模型字节、一个行为原因码、公开数据或一个源 PDF 字节，观察验收明确失败。该行为有自动测试。

新论文案例的完整解析结果属于本地证据，不随源代码库分发。打包提交时保留锁文件、来源哈希及获取脚本；如评审环境从始至终无外网，应在获准的准备环境构建 Docker 镜像并传送镜像，验证阶段仍然断网。

## 可选 Nautilus

只有明确选择 `--extra nautilus` 才安装该引擎。其固定版本 1.231.0 的 macOS wheel 要求 macOS 26；其他环境可能需要 Rust 源码编译。默认验收不依赖它。选择 `--engine nautilus-trader` 且未安装时应出现 `ENGINE_DEPENDENCY_MISSING`，不能换成 Reference。

## 成功判据

退出码为 0；质量门禁无失败；核心测试无跳过；覆盖率至少 90%；18 个契约案例均有有效论文绑定和实际订单成交；4 个深度论文案例有实际订单成交，且同时覆盖公开历史与合成数据来源；两引擎差分一致项通过；8 类错误有实际失败证据；兼容转换可追踪。严格门禁还必须证明内核控制及严格模式报告，不能以环境变量或静态文件文字代替。
