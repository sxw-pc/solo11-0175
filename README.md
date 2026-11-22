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

泵站电量分两条独立证据路径，交付结果不同：

```powershell
python -m canalright meter-import      --db pump.sqlite --file meter.json
python -m canalright sample-import     --db pump.sqlite --file sample.json
python -m canalright acquisition-import --db pump.sqlite --file acquisition.json
python -m canalright run-import        --db pump.sqlite --file run.json
python -m canalright waveform-import   --db pump.sqlite --file waveform.json
python -m canalright total-meter-import --db pump.sqlite --file total.json
```

- 旧 RMS 页（`pump_sample`）只产生 `pump_bill` 暂计账：`V_rms × I_rms × 小时` 是视在功率，缺少相位，不能作实功结算。230 V、10 A、1 元/kWh 仍得 2.3 kWh / 2.3 元暂计。
- 同期同钟的电压、电流配对波形（`pump_waveform`）按 `mean(v·i)` 积分：同相为 2.3 kWh，滞后 60° 为 1.15 kWh。缺相、饱和、时钟漂移、时窗缺口或非整周期采样使整段波形作废并记为 `unmeasured`，绝不按零功率补齐。一个采集器在一个开机期的相位偏差由 `pump_acquisition` 统一登记，逐张波形不得改调。
- 供电公司签名总表（`pump_total_meter`）只能确认泵站总额与损耗界。分泵得到可行区间 `[lower, upper]`；未被配对波形覆盖的差额进入 `pending_allocation` 分摊池，绝不按总表差数指定给某一台泵。若总表与分泵合计在损耗界内无法闭合，整站记为 `loss_out_of_bounds`。
- `real_settlement` 逐次版本保留：暂计账 `pump_bill`、原 RMS 页、总表和各版真实账单同时可查；审计按证据顺序重放。

`fixtures/pump-phase.json` 给出同一 RMS 页下 2.3 / 1.15 kWh 两过程及 3.5 kWh 签名总表的全部数字。

