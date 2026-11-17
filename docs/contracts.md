# 业务数据契约

测量修订按sample_id和recorded_at选择，可用withdrawn撤回。checked_kg为抽检分母，不是整批货重。固定规则警报按日期、产线、产地、等级形成独立提醒，作为安全红线保留；漂移监测在其之上分层运行，两者输出分开。

## 现有组合入口

`app.inspect(data, as_of)` 是只读检查，输入为本地记录集合，输出为 JSON 可表示的字典；不修改输入（事件台账在内部副本上按日重放生成）。领域接口会对不符合业务口径的数据抛出 `ValueError`。缺失、不足和不适用不应伪装成零数量。

## 漂移监测契约

- 分层：层 = 产线 × 产地 × 等级 × 班次（样本可选 `shift`，缺省 `default`）。参照仅取同层历史窗口，新产地/门控不足输出 `no_comparable_baseline`，禁止跨层比较。
- 判定状态：`signal` / `normal` / `evidence_insufficient` / `no_comparable_baseline` / `structural_change` 语义互斥；证据不足不等于确认正常。
- 信号门控：同一评估日内，层×指标检验构成一个 FDR 检验族（`test_family_size` 显式输出，BH 过程，水平 `fdr_q`），且效应量必须达到最小实际偏移；不参与检验族的状态不占用 FDR 预算。
- 事件台账 `data["events"]` 只追加：状态变迁、复核、证据修订全部留痕；事件 ID 由层与序号指纹确定，重放幂等。
- 学习护栏：未经 `normal_batch` 复核放行的事件证据窗口一律隔离，不进入参照；放行仅覆盖复核时已存在的窗口。复核结论与最新统计冲突时写 `statistical_conflict`，不覆盖人工判断。
- 证据溯源：事件证据保存 `evidence_fingerprint` 与 `reference_fingerprint`；测量更正/撤回会生成 `revisions` 条目并把事件置为 `evidence_revised`。
- 设计细节见 `monitoring_design.md`。
