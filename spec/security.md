# 策略安全模型

生成式或第三方策略代码均视为不可信。包内全部 Python 源码必须在 import 前接受 AST 检查，危险 import、相对 import、文件调用和反射被拒绝；允许列表由 manifest 声明，`psrc` 规范 SDK 是受信任运行时依赖。静态检查是强制预检，但不单独构成安全边界。严格执行使用非 root 容器：无网络、根文件系统只读、移除全部 capabilities、启用 no-new-privileges，限制内存/CPU/PID/时间，临时文件系统为 no-exec，数据只读挂载，产物和报告使用独立可写挂载。

开发子进程模式被明确标记为不安全。若请求严格模式但 Docker 或同等级隔离不可用，运行时返回 `SANDBOX_UNAVAILABLE`，绝不切换到开发模式。

严格模式除外层容器控制外，还要求环境证明、真实容器标记、非 root UID，并从 `/proc` 与网络接口核验仅回环网络、零有效 capability、`NoNewPrivs=1`、只读根挂载及 `/tmp` 的 `noexec/nosuid/nodev` 选项。manifest、输入、能力和源码必须在 import 前验证。证据报告记录实际 `sandbox_mode`；严格容器验证使用 `--require-strict`，开发模式只能生成预验收报告。

`StrategyManifest.resources.sandbox` 是策略接受的最低隔离级别，不是建议值。运行策略只能保持或提高它；降低时编译器返回 `SANDBOX_POLICY_DOWNGRADE`。第三方 manifest 默认最低为 `strict_container`，内置合成演示策略为了允许本地预验收才显式声明 `development`。

包发现阶段记录策略源码和受信运行时源码哈希，import 前再次计算并比较；差异返回 `SOURCE_HASH_MISMATCH`。宿主机单包入口 `psrc sandbox run` 调用同一 `DockerSandbox.execute` 边界，把策略目录只读挂载到 `/psrc/data`，并让容器内 `psrc run --require-strict` 再次执行证明与校验。

该模型旨在保护宿主机免受策略代码影响，但不声称能够抵御内核或容器运行时漏洞。生产部署还应增加镜像签名、最小 seccomp 画像、rootless Docker 或 gVisor，以及外部产物恶意软件扫描。
