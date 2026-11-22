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

## 鱼道声呐与净上溯界

渠口鱼道上下游各一台声呐。`sonar-import`/`echo-import` 登记 canal_id、岸位与设备本地 track_hint，`pack-import` 导入封签声呐包（鱼道几何、岸位、回波原件：波形摘要、方向、有效探测带、开机期时钟校准）。`fish_passage.provisional_count` 只是按 `sonar_id:track_hint` 去重的局部轨迹暂计数，不是净上溯鱼数，不能直接用于生态过鱼考核。

`net-run` 在全局一致的可行游动历史上给出每个时段净上溯的可证下界/上界与对应完整轨迹：枚举回波划分为鱼的全部方案，用波形相容、通道长度/最大游速的行程预算、每（设备，开机期）共用的时钟偏移（差分约束判环）与方向一致性过滤；镜像回波、两鱼交叉与探测盲区保留候选，不把相似波形强制合并，也不按每机 track_hint 一对一去重；折返从净量扣除，负净量不代表无通过事件。`video-import` 的侧视视频连续鳍条匹配把历史窗口净量收窄（遮挡并行鱼留作残余警示）,`pit-import`/`pit-run` 的 PIT 读头只确证新一轮有标签个体，不能给旧回波补身份。净量版本与证据另存 `fish_net`，不回写暂计数。

```powershell
python -m canalright pack-import --db fish.sqlite --file fixtures/fishway-return.json
python -m canalright net-run --db fish.sqlite --canal C1 --start 2026-11-22T08:00:00Z --end 2026-11-22T08:12:00Z
```

600 米鱼道、6 公里/小时最大游速下，B00/A06/B12 三条同尺寸回波对应两个可行世界：同一条鱼上溯后折返（净量 0)，或一条留上游、另一条未进鱼道（净量 1)，故界为 [0,1]；逐对贪心合并会错报 0，永不跨机合并会错报 1。
