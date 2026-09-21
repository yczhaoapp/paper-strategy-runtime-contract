# 作者与可选 Agent 的权限边界

2.0 的论文解析与策略编译属于本项目内部，见 [论文流水线](paper-pipeline.md)。默认执行不依赖模型服务或 API 密钥。

外部 LLM/Agent 可以协助提出 recipe 或 `PaperStrategySpec`，但只能提供待校验数据。`psrc author audit` 对结构化规格与 manifest 做确定性静态审计；`psrc author run --source <paper> --spec <spec> --strategy-dir <package>` 是外部实现的正式准入入口。它解析实际 PDF/HTML/UTF-8 文本，核对原文字节哈希、媒体类型、页码和规范化锚点，再绑定 manifest 与导入前的源码证据。通过准入后仍执行普通运行时编译、训练、重载、推理和回测。recipe 路径则通过同样的原文证据和受测算法编译器生成代码。自然语言并不是可信代码，也不能自行授予运行权限。

`AgentAuditReport.runtime_authority_granted` 固定为 false。作者或 Agent 不能绕过 Schema、危险 import 扫描、源码绑定、引擎能力、模型完整性、沙箱要求或有损转换授权。不能以主观分数提升支持等级，也不能删除失败样例来提高成熟度。

新增算法应同时给出公式定位、复现范围、反例和独立数学 oracle；仅有标题或生成代码不构成论文复现证据。

外部实现准入允许题目范围外的论文进入契约，但它验证的是可追溯性、声明一致性和可运行性。页内锚点不能自动证明研究解释正确，因此 `AgentAuditReport.human_review_required` 保持 true；未解决的 blocking ambiguity 会拒绝准入。该边界避免把“支持未知论文接入”误写成“无需审阅即可忠实复现任意论文”。
