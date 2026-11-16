# 工业实验数据账本

本项目是可在本地运行的 FastAPI 服务，用于管理实验规格、采集批次、质量门禁、组间对照分析、评审和不可变发布结果。仓库中的实验数据均为合成样本。

## 启动

在运行服务之前安装 requirements.txt 中锁定的依赖。设置 ATELIER_DB 为可写的 SQLite 路径，然后从项目根目录运行 uvicorn atelier.api:app --host 127.0.0.1 --port 8000。交互式接口文档位于 /docs。

实验规格定义分组、对照组、指标单位、数值边界、精度和质量阈值。批次使用稳定标识；相同内容重复提交会返回原结果，不同内容使用同一批次标识会被拒绝。无效行保留拒收原因。修正和作废样本时需提交当前内容摘要。有效数据变化会推进活动版本，旧分析仍可查阅，但不能作为新的评审或发布依据。

分析结果冻结来源版本与内容摘要。发布需两个不同评审人同意，由第三人执行；任何否决都会阻止发布。证据导出和对账接口可核对批次、样本、分析和发布内容。

## 跨实验室指标解释与换算

两家实验室可能用不同单位（mAh 与 Ah）报送同一材料指标，历史规格还带有不同的精度与有效范围。系统把"可换算"与"属于同一可比指标"作为两个独立判断：

- 测量入库时保留原值、原单位（`measurement_units`）和当时规格版本（`spec_version`），不做任何换算。显式单位报送形如 `{"value": "4850", "unit": "mAh"}`；纯数字字符串按规格单位解析，旧数据行为不变。按报送单位的历史接收范围在指标规则的 `intake_bounds` 中声明。
- **解释版本（interpretation）** 是不可变、内容寻址的依据，包含三部分：指标定义（indicator，统一单位与有效范围）、有证据的单位换算规则（conversion，必须带 basis 文本，只承认显式规则及其逆规则，不猜测多级链路）、按"指标名+规格版本+报送单位"给出的身份映射（mapping）。
- 同名但定义变更的指标通过 `equivalent=false` 标记，即使单位能换算也判 `definition_changed` 并排除，绝不合并。两级判定顺序为：先身份映射，后单位换算，再做换算后有效范围校验。
- 证据不足的数据带原因进入排除清单（`missing_measurement`、`no_identity_mapping`、`no_conversion_rule`、`out_of_harmonized_range`、`ambiguous_mapping`），不进入统计、不凑样本数。质量门禁只按实际可比样本计数（组内最低样本数、排除比例）。
- 换算分析冻结解释版本与请求摘要；解释版本或规格更新会推进账本修订，旧分析自动失效（stale），评审与发布被拦截，但已发布报告永久保留原依据。新旧分析对照把均值差分解为尺度效应（同一样本换算尺度变化）与人群效应（样本增减）；指标定义断裂时拒绝数值合并，只给定性说明。
- 批次重放不改变原始接收事实；相关接口位于 `/campaigns/{id}/interpretations` 与 `/campaigns/{id}/harmonized-*`，证据导出与对账覆盖解释版本、换算分析及其发布。

## 离线演示与检查

python -m atelier.cli --fixture fixtures/synthetic_experiment.json 会在临时数据库导入合成批次，计算对照指标，取得两份评审并输出发布摘要；演示不访问网络。跨实验室换算场景使用 python -m atelier.cli --fixture fixtures/cross_lab_experiment.json，它会注册解释版本、把 mAh 与 Ah 报送统一到同一指标后再走质量门禁与发布。业务测试命令是 python -m unittest discover -s tests -v；语法检查命令是 python -m compileall -q atelier。

测量值使用十进制文本，时间戳必须包含明确的 UTC 偏移，分析窗口采用左闭右开区间。SQLite 事务串行化写入操作，并保留审计事件。
