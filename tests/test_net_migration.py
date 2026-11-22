"""Acceptance: provable net-migration bounds over globally consistent worlds.

The greedy baseline (merge per-device tracks by time, then pair across devices
by nearest timestamp under max swim speed) chains B00->A06->B12 into one fish
and reports net 0 -- wrong in the world where the 08:12 echo is a second fish.
Never merging across devices reports net 1 -- wrong in the world where the same
fish returned. Only the min/max over all globally consistent feasible worlds is
sound for both, so that is what these tests pin down.
"""
import json
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from canalright.net_migration import import_pit, import_video, run_net, run_pit_net
from canalright.sonar import import_pack
from canalright.store import connect, report

PACK = json.loads((Path(__file__).resolve().parent.parent / 'fixtures' / 'fishway-return.json').read_text())
START, END = '2026-11-22T08:00:00Z', '2026-11-22T08:12:00Z'
PACK_ID = 'pack-fishway-return-2026-11-22'


def write(tmp, name, doc):
    p = tmp / f'{name}.json'
    p.write_text(json.dumps(doc))
    return p


def echo(eid, sonar, hint, at, **kw):
    doc = {'id': eid, 'sonar_id': sonar, 'track_hint': hint, 'at': at, 'range_m': 5,
           'direction': 'unknown', 'waveform': {'size_class': 'M'}, 'band': [2, 30],
           'clock': {'session': f'{sonar}-boot1', 'offset_s': [-3, 3]}}
    doc.update(kw)
    return doc


def pack(echoes, pack_id='pack-x'):
    return {'kind': 'fish_pack', 'id': pack_id, 'canal_id': 'C1',
            'fishway': {'id': 'fishway-C1', 'up_sonar': 'A', 'down_sonar': 'B',
                        'length_m': 600, 'max_speed_kmh': 6},
            'sonars': [{'sonar_id': 'A', 'bank': 'upstream'}, {'sonar_id': 'B', 'bank': 'downstream'}],
            'echoes': echoes}


def fish_id_sets(world):
    return {frozenset(e['id'] for e in f['echoes']) for f in world['fish']}


def test_return_trip_bounds_two_feasible_worlds(tmp_path):
    db = tmp_path / 'net.sqlite'
    out = import_pack(db, write(tmp_path, 'pack.json', PACK))
    # B 追踪重启给出 T1/T2 两条局部轨迹：暂计数是 3 条局部轨迹，不是净上溯
    assert out['provisional_count'] == 3
    result = run_net(db, 'C1', START, END)
    assert (result['lower'], result['upper']) == (0, 1)
    worlds = {frozenset(fish_id_sets(w)) for w in result['worlds']}
    # 世界一：同一条鱼 B→A→B，上溯一次又折返一次，净量 0
    world1 = frozenset({frozenset({'echo-B00', 'echo-A06', 'echo-B12'})})
    # 世界二：一条鱼 B→A 留在上游，08:12 的 B 回波是另一条未进鱼道的鱼，净量 1
    world2 = frozenset({frozenset({'echo-B00', 'echo-A06'}), frozenset({'echo-B12'})})
    assert world1 in worlds and world2 in worlds
    nets = {w['net'] for w in result['worlds']}
    assert nets == {0, 1}
    # 三条回波同尺寸仍不被强制合并（世界二存在），B 机两个 track_hint 也不被强制拆分（世界一存在）
    witness = result['worlds'][result['upper_witness']]
    assert fish_id_sets(witness) == {frozenset({'echo-B00', 'echo-A06'}), frozenset({'echo-B12'})}


def test_clock_and_travel_budget_block_teleport_merges(tmp_path):
    db = tmp_path / 'net.sqlite'
    echoes = [echo('e-b0', 'B', 'T1', '2026-11-22T08:00:00Z', direction='up'),
              echo('e-a4', 'A', 'T1', '2026-11-22T08:04:00Z', direction='up')]
    import_pack(db, write(tmp_path, 'pack.json', pack(echoes)))
    result = run_net(db, 'C1', START, END)
    # 4 分钟游不完 600 米（6 km/h 需 6 分钟）：任何可行世界都不并成同一条鱼
    assert all(len(w['fish']) == 2 for w in result['worlds'])
    assert (result['lower'], result['upper']) == (0, 0)
    assert report(db)['fish_passage'][0]['provisional_count'] == 2


def test_shared_session_offsets_are_global_not_per_pair(tmp_path):
    db = tmp_path / 'net.sqlite'
    clock_a = {'session': 'Aa', 'offset_s': [-10, 10]}
    clock_b = {'session': 'Bb', 'offset_s': [-10, 10]}
    echoes = [echo('e-b0', 'B', 'T1', '2026-11-22T08:00:00Z', direction='up', clock=clock_b),
              echo('e-a1', 'A', 'T1', '2026-11-22T08:06:00Z', clock=clock_a),
              echo('e-b2', 'B', 'T2', '2026-11-22T08:12:04Z', clock=clock_b),
              echo('e-a3', 'A', 'T2', '2026-11-22T08:17:59Z', direction='up', clock=clock_a)]
    import_pack(db, write(tmp_path, 'pack.json', pack(echoes)))
    result = run_net(db, 'C1', '2026-11-22T08:00:00Z', '2026-11-22T08:18:00Z')
    # 逐对看每条腿都可行（偏移 0 / -4 / +5 秒），逐对贪心会把四条串成一条；
    # 但同一开机期的偏移须共用：x>=0、x<=-4 与 x>=5 矛盾，四条同鱼的世界不可行
    assert all(len(f['echoes']) < 4 for w in result['worlds'] for f in w['fish'])
    pairs = {frozenset(p) for p in (('e-b0', 'e-a1'), ('e-a1', 'e-b2'), ('e-b2', 'e-a3'))}
    merged = {p for w in result['worlds'] for f in w['fish']
              for p in pairs if p <= {e['id'] for e in f['echoes']}}
    assert merged == pairs


def test_mirror_echo_keeps_both_candidate_readings(tmp_path):
    db = tmp_path / 'net.sqlite'
    echoes = [echo('e-m1', 'B', 'T1', '2026-11-22T08:00:00Z', direction='up'),
              echo('e-m2', 'B', 'T2', '2026-11-22T08:00:05Z')]
    import_pack(db, write(tmp_path, 'pack.json', pack(echoes)))
    result = run_net(db, 'C1', START, END)
    # 镜像回波：同一条鱼被重复采录与两条鱼并存为候选，不强制合并也不强制拆分
    assert sorted(len(w['fish']) for w in result['worlds']) == [1, 2]
    assert (result['lower'], result['upper']) == (0, 0)


def test_incompatible_waveforms_are_never_merged(tmp_path):
    db = tmp_path / 'net.sqlite'
    echoes = [echo('e-b0', 'B', 'T1', '2026-11-22T08:00:00Z', direction='up'),
              echo('e-a6', 'A', 'T1', '2026-11-22T08:06:00Z', direction='up',
                   waveform={'size_class': 'L'})]
    import_pack(db, write(tmp_path, 'pack.json', pack(echoes)))
    result = run_net(db, 'C1', START, END)
    # 尺寸档不同，即使时间与游速可行也不合并
    assert all(len(w['fish']) == 2 for w in result['worlds'])


def test_negative_net_is_representable_and_not_absence_of_passage(tmp_path):
    db = tmp_path / 'net.sqlite'
    echoes = [echo('e-a0', 'A', 'T1', '2026-11-22T08:00:00Z', direction='down'),
              echo('e-b6', 'B', 'T1', '2026-11-22T08:06:00Z', direction='down')]
    import_pack(db, write(tmp_path, 'pack.json', pack(echoes)))
    result = run_net(db, 'C1', START, END)
    # 一条鱼自上游端进入、下游端离开：净量可为负，但鱼道确有通过事件
    assert result['lower'] == -1
    down = [w for w in result['worlds'] if w['net'] == -1]
    assert down and down[0]['fish'][0]['legs'][0]['kind'] == 'down_crossing'


def video_doc():
    return {'kind': 'fish_video', 'id': 'video-night-1', 'canal_id': 'C1',
            'window': {'start': START, 'end': END},
            'links': [['echo-B00', 'echo-A06', 'echo-B12']], 'occluded_parallel': True}


def pit_doc():
    return {'kind': 'fish_pit', 'id': 'pit-round-2', 'canal_id': 'C1',
            'window': {'start': '2026-11-23T08:00:00Z', 'end': '2026-11-23T08:12:00Z'},
            'reads': [
                {'tag': 'T001', 'end': 'down', 'at': '2026-11-23T08:01:00Z'},
                {'tag': 'T001', 'end': 'up', 'at': '2026-11-23T08:07:00Z'},
                {'tag': 'T002', 'end': 'down', 'at': '2026-11-23T08:02:00Z'},
                {'tag': 'T002', 'end': 'up', 'at': '2026-11-23T08:08:00Z'},
                {'tag': 'T003', 'end': 'up', 'at': '2026-11-23T08:03:00Z'},
                {'tag': 'T003', 'end': 'down', 'at': '2026-11-23T08:09:00Z'},
                {'tag': 'T004', 'end': 'down', 'at': '2026-11-23T08:04:00Z'}]}


def test_side_video_narrows_historical_window(tmp_path):
    db = tmp_path / 'net.sqlite'
    import_pack(db, write(tmp_path, 'pack.json', PACK))
    before = run_net(db, 'C1', START, END)
    assert (before['lower'], before['upper']) == (0, 1)
    import_video(db, write(tmp_path, 'video.json', video_doc()))
    after = run_net(db, 'C1', START, END)
    # 尾鳍缺口连续匹配确认回游：历史 12 分钟净量收至 0
    assert (after['lower'], after['upper']) == (0, 0)
    assert after['version'] == 2
    # 视频遮挡的并行鱼仍留范围：作为残余警示随净量版本保存
    assert after['caveats'] == ['video_occluded_parallel_fish_remain_in_range']
    assert set(after['evidence']) == {PACK_ID, 'video-night-1'}


def test_pit_readers_confirm_next_round_but_not_old_echoes(tmp_path):
    db = tmp_path / 'net.sqlite'
    import_pack(db, write(tmp_path, 'pack.json', PACK))
    before = run_net(db, 'C1', START, END)
    assert (before['lower'], before['upper']) == (0, 1)
    import_pit(db, write(tmp_path, 'pit.json', pit_doc()))
    # 新一轮跨端匹配有标签个体：T001/T002 上溯、T003 下行折返、T004 单侧未确证
    confirmed = run_pit_net(db, 'C1', '2026-11-23T08:00:00Z', '2026-11-23T08:12:00Z')
    assert (confirmed['lower'], confirmed['upper']) == (1, 1)
    assert confirmed['coverage'] == 'tagged_only'
    assert 'unmarked_fish_not_determined' in confirmed['caveats']
    # 新 PIT 不能给旧日三条回波补身份：历史窗口净量界不变
    after = run_net(db, 'C1', START, END)
    assert (after['lower'], after['upper']) == (0, 1)
    assert after['version'] == 2
    assert 'pit-round-2' not in after['evidence']


def test_raw_echoes_provisional_and_net_versions_are_traceable(tmp_path):
    db = tmp_path / 'net.sqlite'
    import_pack(db, write(tmp_path, 'pack.json', PACK))
    v1 = run_net(db, 'C1', START, END)
    v2 = run_net(db, 'C1', START, END)
    rep = report(db)
    # 旧 provisional_count 保留为采录结果，净量版本另存、不回写
    assert rep['fish_passage'] == [{'canal_id': 'C1', 'provisional_count': 3}]
    assert [r['source_id'] for r in rep['fish_echoes']] == [PACK_ID] * 3
    versions = [(r['window_start'], r['method'], r['version'], r['lower'], r['upper'])
                for r in rep['fish_nets']]
    assert versions == [(START, 'sonar', 1, 0, 1), (START, 'sonar', 2, 0, 1)]
    assert v1['id'] != v2['id']
    with closing(connect(db)) as dbh:
        ev = dbh.execute('SELECT sha,length(raw) FROM evidence WHERE id=?', (PACK_ID,)).fetchone()
    assert ev and ev['sha'] and ev[1] > 0
    from canalright.audit import audit
    assert audit(db)['status'] == 'matched'


def test_cli_pack_import_and_net_run(tmp_path):
    db = tmp_path / 'cli.sqlite'
    pack_file = write(tmp_path, 'pack.json', PACK)
    r1 = subprocess.run([sys.executable, '-m', 'canalright', 'pack-import',
                         '--db', str(db), '--file', str(pack_file)], capture_output=True, text=True)
    assert r1.returncode == 0, r1.stderr
    assert json.loads(r1.stdout)['provisional_count'] == 3
    r2 = subprocess.run([sys.executable, '-m', 'canalright', 'net-run', '--db', str(db),
                         '--canal', 'C1', '--start', START, '--end', END], capture_output=True, text=True)
    assert r2.returncode == 0, r2.stderr
    out = json.loads(r2.stdout)
    assert (out['lower'], out['upper']) == (0, 1)
