# 灌区水权轮灌采录台

离线记录地块水权、渠段轮灌和闸口实供。所有主体与计量数据均为合成 fixture，不连接真实闸口。签发机构的范围与有效期、水权生效区间和重叠额度决定轮灌资格；渠段容量、优先级、封渠与复开决定预留和候补；独立校准凭据、见证读数及欠供复核改变实供与补偿责任。每类证据都有独立 CLI 入口、持久状态和审计重放。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m canalright right-import --db demo.sqlite --file fixtures/right-ok.json
python -m canalright rotation-import --db demo.sqlite --file fixtures/rotation-ok.json
python -m canalright gate-import --db demo.sqlite --file fixtures/gate-ok.json
python -m canalright report --db demo.sqlite
python -m pytest -q
```

完整的跨账示例：

```powershell
python -m canalright issuer-import --db verified.sqlite --file fixtures/issuer-ok.json
python -m canalright right-import --db verified.sqlite --file fixtures/right-verified.json
python -m canalright rotation-import --db verified.sqlite --file fixtures/rotation-verified.json
python -m canalright calibration-import --db verified.sqlite --file fixtures/calibration-ok.json
python -m canalright gate-import --db verified.sqlite --file fixtures/gate-verified.json
python -m canalright shortage-review --db verified.sqlite --file fixtures/review-drought.json
python -m canalright audit --db verified.sqlite
```

实供 25、计划 30 导致欠供 5；裁定前由渠段运营方暂列应补，复核干旱责任后转至水权管理方。失败证据在 SQLite 事务内回滚，可更正证据并重试。审计按原始证据顺序重建权证、排程、校准、实供与复核结果。

失败 fixture 分别演示同地块超水权、同渠同窗超配和表计回拨。证据原字节与 SHA、各业务状态、逐次决定保存在 SQLite；同 ID 同文件重放幂等，异内容拒绝。签发人与校准人的身份仅是离线登记与不同角色校验；密码学签名、水权交易、遥测网关和跨灌区调度尚未实现。

## 堤岸水尺与越堤事件判定

值守站每 15 分钟上传一页水位（`stage_reading`，含采样时刻与上传时刻），`flood.update_warning` 仍只对最新页记 `high_stage`，它只是值守信号。`canalright/overtop.py` 另采录站点堤顶高程、压力探头带宽与最大变率、采样/上传时钟、停电区间、签名高频缓冲页、堤顶水痕和校准偏差，按物理可行包络把每个时段分为可确定未越堤、可确定越堤、仍可能越堤三档——缺页或采样过宽的夹缝保持未决，不做线性插值填平；一次校准偏差对同站整段共同生效；停电空白不算安全时段；越堤事件按证据可分辨的时间连通性计为区间。每次发布保存证据版本与原始字节并版本化，新采样资料只修订其覆盖时段。`fixtures/overtop-alias.json` 给出三张相同原件（均 1.2 米、堤顶 1.4 米）下两条合法水位史：A(t)=1.2+0.3·sin²(πt/15) 两次越堤、B(t)=1.2−0.3·sin²(πt/15) 零次越堤；`tests/test_overtop.py` 复算全部验收项。
