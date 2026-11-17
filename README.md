# 河段水质预警采证台

完全离线的流域数据采录与预警决定工作台。四本业务账各有独立入口：仪器与测站资格、采样瓶保管链、沿河段事件、取水口通告与回执。仓内只含虚构样例，不连接真实测站或公众通告服务。

## 快速运行

在仓库根目录运行：

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m rivercase demo --db demo.sqlite
python -m rivercase audit --db demo.sqlite
python -m unittest discover -s tests -v
```

四本账可各自运行并报告当前决定：

```powershell
python -m rivercase station-import --db state.sqlite --input fixtures/station_valid.json
python -m rivercase sample-import --db state.sqlite --input fixtures/sample_valid.json
python -m rivercase incident-evaluate --db state.sqlite --input fixtures/incident_valid.json
python -m rivercase notice-publish --db state.sqlite --input fixtures/notice_valid.json
python -m rivercase report --db state.sqlite
```

正常 fixture 之外，`station_moved.json`、`sample_gap.json`、`incident_late.json`、`notice_missing.json` 分别演示调站失格、保管链冲突、迟到复测和漏收件人。先跑正常链再导入某个反例，才能观察跨账重裁；单独运行每本账也可看到其独立状态。

| 业务账 | 当前决定 | 跨账变化 |
| --- | --- | --- |
| 测站资格 | 仪器调站、校准时段、并列证书 | 读数失格使事件证据数变化 |
| 样本保管 | 瓶号、连续交接、实验方法 | 高值隔离或有效复测改变事件等级 |
| 河段事件 | 有向河边、传播时差、独立高值数 | 形成受影响取水口清单与事件版本 |
| 公众通告 | 区域完整性、逐人送达和撤回 | 事件降级后旧警示保留撤回义务 |

所有事件以原始 JSON 字节、SHA-256、来源顺序及业务时间存入 SQLite。同事件同内容重放不会重复占用或发回执；异内容拒绝覆盖。`incident_version` 追加历史事件版本，`notice_obligation` 保存未履行的投递或撤回责任。`audit` 在新内存数据库按 CLI 原始文件重放四账，比较最终资格、样本、事件、取水口和通知决定；若只直接调用 Python 函数而未通过 CLI，审计会报告缺少源文件日志。

当前范围是合成、单机、单污染阈值与确定性河道时差。真实传感器接入、跨行政区签发、外部消息送达和气象归因不在本版实现。
