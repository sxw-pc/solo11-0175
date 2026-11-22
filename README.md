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

## 护岸倾角：热胀与滑移的可行界判定

原始倾角页与支架温度页只满足 `y(t)=s(t)+k·(T(t)−T0)`，未知批次热系数 `k∈[0,0.1]°/°C` 且同一材料批号跨 N1/N2 共用，单组两页无法同时解出真实墙体转角 `s` 与 `k`。因此 `import_tilt` 只把最新绝对倾角达到 0.5° 的段位暂列 `repair_review`（不是施工量），`canalright/slip_proof.py` 保留结构转角的可行区间并按段发布三态：

- `proven_displacement`：可行界整体达到 0.5°，按登记段长计入必须封闭长度，方可签发维修证书；
- `thermal_only`：可行界整体低于 0.5° 且无独立锚点松动记录，可免修；
- `pending_review`：可行界跨越 0.5°、温度/调零页缺失或锚点松动，不封闭并列出需要回访的传感器。

两条监理规程内的补证路径改变覆盖范围：基岩锚定全站仪观测直接证实单段墙体转角（洪水扰动水准点的观测会被拒绝，不能制造免修）；同批备用支架的多温点恒温箱标定把全批 `k` 钉死，同时重算所有共用批次段位，但独立锚点松动仍须现场复核。裂缝照片仅登记，不能证明裂缝形成时间。`bank_slip_review` 按段追加版本，旧 `repair_review` 通知与原页证据均不删除，`audit` 可按证据顺序完整重放。

```powershell
python -m pytest -q
```
