# 策略安全模型

生成式或第三方策略代码均按不可信输入处理。包内全部 Python 源码必须在 import 前接受 AST 检查，危险 import、相对 import、文件调用和反射被拒绝；只允许最小 `psrc.strategy_api`，不允许导入整个运行时命名空间。静态检查和进程内审计围栏用于执行资源契约及关闭已知绕过，不构成针对任意恶意 Python 的独立进程隔离。manifest 属性解析和策略方法查找也在该围栏内执行。严格执行使用非 root 容器：无网络、根文件系统只读、移除全部 capabilities、启用 no-new-privileges，限制内存/CPU/PID/整次容器运行时间，临时文件系统为 no-exec，数据只读挂载，产物和报告使用独立可写挂载。

策略不会收到受信 `ArtifactStore`。orchestrator 为每次运行创建独立的最小产物能力对象，只开放 `save_bytes` 和 `load_bytes`，不开放存储根或主机验证方法。进入受信 I/O 前，所有标量、字节和 metadata 必须是内建基础类型，避免在审计豁免区执行调用方对象的方法；策略修改该能力对象的类只影响本次非受信对象。策略返回产物声明后，orchestrator 使用未暴露的服务对象独立读取磁盘上的规范 manifest，核对目录边界、符号链接、文件存在性、大小、哈希、策略身份和训练来源。`load` 回调必须通过能力对象读取这份已验证产物，受信侧记录该读取并在回调后再次复核；空 `load`、类方法替换和缺失模型均不能生成成功报告。

开发模式只提供契约预检，不提供恶意代码的宿主隔离。若请求严格模式但 Docker 或同等级隔离不可用，运行时返回 `SANDBOX_UNAVAILABLE`，绝不切换到开发模式。

严格模式除外层容器控制外，还要求环境证明、真实容器标记、非 root UID，并从 `/proc` 与网络接口核验仅回环网络、零有效 capability、`NoNewPrivs=1`、只读根挂载及 `/tmp` 的 `noexec/nosuid/nodev` 选项。manifest、输入、能力和源码必须在 import 前验证。证据报告记录实际 `sandbox_mode`；严格容器验证使用 `--require-strict`，开发模式只能生成预验收报告。

`StrategyManifest.resources.sandbox` 是策略接受的最低隔离级别，不是建议值。运行策略只能保持或提高它；降低时编译器返回 `SANDBOX_POLICY_DOWNGRADE`。第三方 manifest 默认最低为 `strict_container`，内置合成演示策略为了允许本地预验收才显式声明 `development`。

包发现阶段记录策略源码和受信运行时源码哈希，import 前再次计算并比较；差异返回 `SOURCE_HASH_MISMATCH`。宿主机单包入口 `psrc sandbox run` 调用同一 `DockerSandbox.execute` 边界，把策略目录只读挂载到 `/psrc/data`，并让容器内 `psrc run --require-strict` 再次执行证明与校验。

严格容器边界旨在限制策略代码对宿主机的影响；进程内围栏本身不承担这一保证。当前实现也不声称抵御内核或容器运行时漏洞，或提供多租户级 Python 解释器隔离。生产部署还应增加独立策略 worker、最小 IPC、镜像签名、最小 seccomp 画像、rootless Docker 或 gVisor，以及外部产物恶意软件扫描。Docker 客户端超时时会按随机容器 ID 显式执行强制清理并记录结果。
