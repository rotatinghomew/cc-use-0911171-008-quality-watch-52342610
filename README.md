# quality-watch

质量检验抽样核对、固定规则警报与人工复核。

## 当前产品

这是可独立运行的本地分析应用，提供业务记录核对和确定性基线结果。各业务职责可直接作为 Python 接口调用，`app.py` 负责组合一次只读业务检查。结果可保存为 JSON，不依赖远端服务。

- `measurements.py`：抽检分母和测量更正。
- `reference.py`：可比批群及参照资格。
- `incidents.py`：固定规则事件跟踪和人工复核（安全红线）。
- `monitoring.py`：分层漂移监测——同层参照、样本量自适应检验、FDR 误报控制。
- `events.py`：漂移事件引擎——跨窗口/跨指标聚合、学习护栏、证据修订与冲突留痕。

## 运行

使用 Python 3.11.14，无需安装第三方包。

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
python3 app.py demo
```

`inspect` 与 `demo` 使用相同业务入口；`--as-of` 选择已知日期，`--data` 可选本地数据文件，`--output` 可将本次业务核对结果写入文件。默认数据位于 `fixtures/data.json`，默认日期为 `2026-10-30`。这些命令仅展示当前已经存在的能力。

## 容器

`Dockerfile` 固定到 Python 基础镜像内容摘要。构建后运行不联网：

```bash
docker build -t quality-watch:local .
docker run --rm --network none quality-watch:local python -m unittest discover -s tests -v
docker run --rm --network none quality-watch:local
```

## 数据与业务契约

样例记录全部为本地合成数据，不代表真实农户、市场成交或实际试验结果。日期采用 `YYYY-MM-DD`，有记录时点的条目以 `recorded_at` 表示系统已知日期；同一业务键存在多个修订时选已知范围内最新记录，同日由 `revision` 区分，`withdrawn` 表示撤回。可修改样例验证业务边界，不能将演示数据的统计方向视为真实结论。

测量修订按sample_id和recorded_at选择，可用withdrawn撤回。checked_kg为抽检分母，不是整批货重。固定规则警报按日期、产线、产地、等级形成独立提醒，作为安全红线保留；漂移监测按产线×产地×等级×班次分层，只与同层历史比较，样本量不足给 `evidence_insufficient` 而非确认正常。复核只追加记录；未经 `normal_batch` 放行的事件证据保持隔离，不会被自动学成正常，复核与统计冲突时只标记不覆盖。设计依据见 `docs/monitoring_design.md`，接口契约见 `docs/contracts.md`。

接口细节见 `docs/contracts.md`。测试使用各自的内存副本和临时目录，不修改原始数据或依赖固定端口。
