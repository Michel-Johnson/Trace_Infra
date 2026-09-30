# Eval Template 合同

执行前必须具备：`spec_id`、语义版本、`Approved` 状态、评测问题、总体、分析单位、revision 策略、证据契约、方法、指标/判定、unknown 规则、查询计划、校准案例和输出 Schema。

以下情况必须拒绝执行：

- 使用 `latest` 且未在运行开始时解析成固定 revision 集合。
- 分母、阈值或 Cohort 匹配条件不明确。
- Judge 可以看到 Spec 未声明的信息。
- 必要证据缺失时没有 unknown/insufficient 语义。
- 查询计划要求当前权限之外的 analysis scope 或原件访问。
- 将搜索命中、错误文本或工具名称直接定义为业务失败。

若用户只有自然语言需求或 Deep Dive 假设，先交给 `trace-eval-designer`，不得在执行阶段临时补齐标准。
