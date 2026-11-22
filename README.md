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

## 入田口离子确证

EC 反映总导电贡献，不是有害钠离子浓度：EC=1200 的同一原页可对应 Na=900/Ca=300（水稻禁灌）与 Na=300/Ca=900（可灌）两种水团，因此 `crop_advice` 的 `hold` 只是暂存状态，最终作物适宜性另存 `crop_window`，按水团与采样时段分栏。

```powershell
python -m canalright sodium-import --db demo.sqlite --file fixtures/sodium-intake.json
python -m canalright report --db demo.sqlite
python -m canalright audit --db demo.sqlite
```

合成校准液规定 EC 指数 = Na + Ca（同一单位），`ion_calibration_batch` 从校准液求出每个仪器批次唯一的共用基线漂移；同批测站的离子分析必须与这一个参数相容，否则整批拒绝。每条水样记录采样体积、原瓶封签、仪器批次与混水时段；只有同水团、同时段的 Na/Ca 分析才能与 EC 互相约束，相邻日的新水样不能替代旧水团（`parcel_reuse_forbidden`）。

两条补证路线用途不同：留存且封签完好的原瓶走离子色谱（`ion_analysis`），逐项定出历史水团组成，可补签该旧时段的进水证；封签破损则旧水团不可恢复，原瓶留在 `pending_bottle`，只能用钠选择电极对下一轮水团连续巡测（`electrode_patrol`），每个瞬时点都与 EC 和同批校准液交叉核验，且只能签发巡测覆盖到的时段交集，不扩张为全天可灌。后到的实验室结果只重算其覆盖的取水时段；原 EC 页、离子样、作物适宜性与改判来源全部保留在 evidence 与 decision_log 中，可按原顺序审计重放。
