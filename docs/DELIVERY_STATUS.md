# 交付状态 · 2026-09-22

当前工作区已完成 2.6.2 / Runtime Contract 1.2 的契约收口，状态为 **S 级候选实现，宿主机和 Linux/arm64 严格容器完整验收通过**。最终档位仍由独立评审决定。

## 实际执行结果

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 2.6.2 macOS / Python 3.13.7 完整门禁 | 11 个步骤全部通过；收据绑定最终输入树哈希 | `evidence/release/host-verification.json` |
| 2.6.2 pytest | 205 通过，0 失败，0 错误，0 跳过 | `evidence/release/host-acceptance.json`；完整 JUnit 位于本地报告/CI artifact |
| 2.6.2 核心覆盖率 | 宿主与严格容器均 ≥92.6% | 两份发布 acceptance 记录；可选 Nautilus 单独排除 |
| 2.6.2 Linux/arm64 / Python 3.12.14 严格 Docker | 当前源码重建、16 份论文获取校验、断网运行通过；18 个运行包均为 R2 | `evidence/release/strict-verification.json` 与 `strict-acceptance.json` |
| Ruff + 严格 mypy | 通过 | 完整验证日志 |
| 硬验收项 | 19/19 通过 | acceptance-report.json |
| Schema | 31 份，自包含引用、真对象验证与生成物漂移检查通过 | `schemas/generated/`；另有 1 份 catalog.json |
| 契约覆盖策略 | 18/18；规则/监督/RL 各 6 | runs/all/summary.json |
| 论文依据绑定 | 18/18；15 个实际来源；6 公式 + 3 算法 + 9 等价方法复现；9 个 A2；6 个 D1 且三类各 2 | acceptance-report.json |
| 深度论文算法案例 | 4/4；均有真实解析、代码、订单与成交 | runs/papers/summary.json |
| 默认原生引擎 | Reference + Backtrader 差分通过 | runs/adapters/comparison.json |
| 强制运行失败 | 8 类官方关键失败均实际触发 | runs/failures/summary.json |
| 边界契约反例 | 累计仓位、day 到期、bar 网格、无效版本、输出复用、lookback、staleness、策略/引擎/沙箱绑定共 10 个精确测试节点通过 | JUnit 与 `runtime_boundary_contract_enforcement` |
| 兼容转换 | 实际 10→2 事件及可核查哈希 | runs/compatibility/bundle.json |

## 环境边界

2.6.2 已在 Docker Desktop 的 Linux/arm64 VM 内从排除本机 `papers/sources` 缓存的构建上下文重新构建；构建阶段实际获取并校验 16 份注册论文。运行阶段实测断网、只读根目录、非 root、capabilities 清零、`NoNewPrivs`、PID/CPU/内存限制和 `noexec` 临时目录。严格运行调用镜像构建期安装的解释器，不在断网阶段调用 uv、pip 或项目构建。

Linux/Windows/macOS 的 GitHub Actions CI 已配置并通过静态检查，但当前工作区尚无远程仓库运行记录，**未声称 GitHub `ubuntu-latest` 或 `windows-latest` 已通过**。本地平台回归不能替代 Windows 真机结果。

原生 Nautilus 适配器保留为独立可选 extra，默认没有安装或动态认证。默认依赖和报告只计 Reference 与 Backtrader。

## 论文范围

发布注册表固定 16 份公开论文 PDF；18 个策略实际由其中 15 个来源覆盖，并逐一绑定页级声明、实现文件哈希和精确测试。保真分布为 6 个公式复现、3 个算法复现、9 个等价方法复现；9 个达到 A2，规则/监督/RL 分别为 6/3/0。线性 Actor–Critic 因单资产线性实现与论文多资产神经网络实验存在实质差异降为 A1；A2C Pairs 也因固定 proxy spread 保持 A1。6 个策略使用公开历史行情达到 D1，其余 12 个是 D0；全部保持 E0。4 个案例进一步执行受审配方驱动的原文解析、代码编译和统一运行。项目不声称复现原论文收益、显著性或完整实验。

许可证与第三方声明见 `LICENSE` 和 `NOTICE`。当前版本尚未推送或提交外部评审，也未向任何人发送消息。
