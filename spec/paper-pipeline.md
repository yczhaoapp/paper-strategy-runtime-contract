# 论文流水线契约 1.1

## 输入与对象

`PaperDocument` 保存原文 URI、字节 SHA-256、媒体类型、解析器版本和分页文本；每页有连续的一基页号和 UTF-8 文本哈希。HTML 去掉 script/style/noscript，正文仍是数据。扫描 PDF 不自动 OCR，返回错误要求显式处理。

`PaperRecipe` 保存论文身份、固定版本哈希、算法族、策略类型、执行引擎、数值参数、证据 claims、假设、偏差和映射审阅说明。每个 claim 包含页号、短锚点、公式/章节位置及实现解释。任何来源修改需要显式重新审阅 recipe。

`ReproductionSpec` 将 recipe、分页文档哈希、页内规范化字符偏移和页哈希绑定。`arbitrary_code_execution=false`，`empirical_results_reproduced=false`。算法复现不自动提升为论文实证结果复现。

`PaperStrategyBinding` 将 18 个契约示例逐一绑定到 `PaperSource`、至少两个页级 claim、实现文件与符号哈希、精确 pytest 节点、假设和偏差。1.2 要求每个 claim 恰好连接到一个已声明实现符号和验证节点，并保存确定性运行行为签名：输入及决策哈希、数据种类、粒度、动作、原因码、产物算法与 ID、决策/订单/成交计数。总验收从当次 JUnit 和运行包交叉核对，不接受仅存在但未执行的测试。`formula_reproduction` 与 `algorithm_reproduction` 强制 A2，`method_reproduction` 强制 A1，改变核心交易问题的 `method_adaptation` 强制 A0。A0 可以作为真实运行契约样例，但不计入论文复现。数据、实验结果和运行时分别用 D/E/R 轴声明。来源注册表固定 16 份 PDF，18 个策略实际使用其中 15 份。详见[复现等级政策](../docs/reproduction-fidelity-policy.md)。

`PaperStrategySpec` 和 `ExternalStrategyAdmission` 构成开放论文的外部实现路径。规格声明论文引用与原文字节哈希、策略类别、数据和动作契约、特征、标签或奖励、训练目标、推理规则、执行假设、歧义，以及带页码和短锚点的证据。`psrc author run` 从调用方提供的实际原文生成 `PaperDocument`，逐项定位证据，再把规格哈希、文档哈希、原文件哈希、manifest 哈希和策略源码证据哈希写进准入记录。准入记录只允许进入普通运行验证，`runtime_authority_granted` 固定为 false。

`DataSourceEvidence` 将数据真实性与算法忠实度分开。Ma 深度案例使用固定哈希的公开 AAPL 日线，另外三个使用确定性合成 L1；两者均声明不是原论文数据、没有复现实证结果。`PublicDatasetSource` 另外固定公开文件、来源登记和许可证文件三层哈希。详见[数据来源契约](data-provenance.md)。

上述对象均导出自包含 Draft 2020-12 JSON Schema，并用标准 jsonschema 对真实对象验证。

## 支持的编译后端

| 算法族 | 实现公式/训练 | 声明参数 |
| --- | --- | --- |
| avellaneda_stoikov | r=s-q*gamma*sigma²*tau；spread=gamma*sigma²*tau+2/gamma*ln(1+gamma/k) | gamma、sigma、k、tick、库存上限、事件时域 |
| queue_logistic | I=(bid_size-ask_size)/(bid_size+ask_size)；sigmoid(beta0+beta1*I) | 迭代预算、收敛阈值、显式交易阈值 |
| execution_dynamic_q | Q_t(s,a)=mean(reward+max Q_(t-1)(s',a'))；终止续值为零 | 时域、目标股数、tick |
| moving_average_band | 当前及前 n-1 收盘价均值；上/下百分比阈值与中间空仓 | 短窗口、长窗口、band |

来源抽取确定地定位这些算法声明的证据；算法含义来自审阅过的 recipe 和受测后端。系统没有声称从任意无配方论文独立发明或理解代码。增加新算法需新增严格参数验证、实现、最小数学 oracle、样例和失败测试。

不属于四个内置编译后端的算法不必修改运行时源码：作者或 LLM 可以提供结构化规格与包内 `strategy.py:Strategy`，通过外部准入路径执行。系统会验证其来源、声明、代码和运行行为，但研究含义仍需人工审阅；这条路径不能自动获得 A2 或 E1/E2 等论文保真等级。

## 生命周期与产物

1. 解析真实输入，计算原始和页级哈希。
2. 校验 recipe 的版本、算法类别、精确参数集合与预算。
3. 按规范化正文定位证据；证据页、锚点和哈希必须一致。
4. 生成 manifest、Python 入口、数据声明、事件、训练请求、规格、划分证据与 build manifest。
5. 用相同 `psrc run` 内核加载生成目录，能力协商在加载代码前完成。
6. 训练、原子保存、校验重载、推理、下个事件执行；输出标准 `RunBundle`。
7. 输出带内嵌标准 bundle 的 `paper-run.json` 与 HTML。独立验收器从原始 PDF 再解析和再编译。

输出包必须为空目录。每次复现应使用新目录，完整保存失败原因。编译器不会覆盖用户修改的包，也不会拼接未经校验的论文文本为代码。

## 错误

| 代码 | 条件 |
| --- | --- |
| PAPER_INPUT_INVALID | 格式、大小、加密或页数限制不满足 |
| PAPER_PARSE_FAILED | 原文缺失、损坏、编码错误、扫描件无文字 |
| PAPER_SOURCE_MISMATCH | 原文哈希与审阅配方不符 |
| PAPER_EVIDENCE_MISSING | 页、短锚点或页文本完整性不符 |
| PAPER_RECIPE_INVALID | 配方未知字段、算法类别/参数/资源预算不合法 |
| PAPER_COMPILATION_FAILED | 输出冲突或写入生成物失败 |
| TRAINING_FAILED | 数值非法、样本不足、完全分离、不可识别、未收敛、RL 状态不全 |
| INFERENCE_FAILED / BACKTEST_FAILED | 模型未加载、未知状态、执行不可完成等运行时失败 |

网络抓取仅在 `scripts/fetch-papers.py`，限公开学术 HTTPS 域名、限制体积、逐跳检查重定向、验证固定哈希，再原子发布缓存。缓存改变时明确失败，不自动更新配方。

## 可复核性边界

来源和代码哈希不是加密签名，不防御能同时修改源码、配方、测试和证据的恶意仓库作者。页内锚点证明原文存在，不单独证明解释正确；评审应固定提交 SHA、使用干净环境重跑，并人工检查关键论文映射。默认测试将模型拟合和回测准确性与原论文实证收益分开判定。
