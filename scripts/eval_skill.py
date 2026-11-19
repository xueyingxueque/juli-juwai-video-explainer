# -*- coding: utf-8 -*-
r"""eval_skill.py —— 技能离线评估（自我迭代闭环的「测」）。

★ 为什么新增（2026-11-09 深度进化，对标 skill-creator 的 eval 机制 + 项目准入标准
  「内置自我迭代/学习机制」）：
  此前技能只有「产物校验」（deliver_check 查这一集齐没齐），**没有「技能自己好不好」的度量**。
  于是改了代码只能靠人肉回归，坑典条目增长后却没人验证编号是否连续、引用是否失效。
  → 本脚本把「技能 invariants」变成 28 条客观可验证断言，全离线、不跑 GPU、不碰生产素材。
  （v6.0.0 新增 23–28：AIGC 判据收口 / media_qc 防呆 / label 唯一化 /
    publish_kit 法规落位 / stamp 幂等接链 / 调研决策卡与存疑清单。）

每轮输出：
  evals/eval_report.json   —— pass_rate / 每条断言的 passed + evidence + 耗时
  evals/eval_history.jsonl —— 追加一行，含与上一轮的 delta（迭代趋势）

用法：
    python scripts/eval_skill.py                 # 跑全部，打印摘要
    python scripts/eval_skill.py --id 5         # 只跑一条（排障用）
    python scripts/eval_skill.py --json         # 输出完整 JSON
退出码：hard 断言全过 = 0；任一 hard 失败 = 1（soft 失败只标注）。

纪律：断言必须**双向验证**（既验「正常通过」，也验「异常被拦」），只验一侧等于没验。
"""
import argparse
import ast
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / 'scripts'
EVALS = ROOT / 'evals'
sys.path.insert(0, str(SCRIPTS))
import policy                      # noqa: E402
import pipeline_state as ps        # noqa: E402
import episode_dedupe              # noqa: E402

CFG = json.loads((ROOT / 'config' / 'engine.json').read_text(encoding='utf-8'))
FFPROBE = CFG['runtime']['ffprobe']


def _tmp_task(specs, gencfg=None):
    """造一个临时任务目录（不碰生产）。specs: {集号: shots}。"""
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    (root / '_plan').mkdir()
    for n, shots in specs.items():
        (root / '_plan' / f'spec-{n}.json').write_text(
            json.dumps({'name': f'eval-{n}', 'shots': shots}, ensure_ascii=False),
            encoding='utf-8')
    if gencfg is not None:
        (root / '_plan' / 'gencfg.json').write_text(
            json.dumps(gencfg, ensure_ascii=False), encoding='utf-8')
    return td, root


def _shot(ep, t, text='解说词'):
    return {'ep': ep, 't': t, 'text': text, 'raw': False}


# ── 断言实现 ──────────────────────────────────────────────────
def e01():
    b = policy.duration_bounds()
    return b == (60.0, 300.0), f'bounds={b}'


def e02():
    cases = [(59, False), (60, False), (60.1, True), (97.1, True),
             (299.9, True), (300, False), (301, False)]
    got = [(v, policy.check_measured(v)[0]) for v, _ in cases]
    return all(a == b for (v, a), (_, b) in zip(got, cases)), str(got)


def e03():
    a = policy.check_estimate(200.0)[0]
    b = policy.check_estimate(295.0)[0]
    # 越界只返回 False（预警），不抛异常、不改硬门
    return a is True and b is False, f'200s→{a}, 295s→{b}（均为 warn，非 reject）'


def e04():
    b = list(policy.duration_bounds())
    bad = {k: v['duration'] for k, v in CFG['platform_profiles'].items()
           if isinstance(v, dict) and 'duration' in v and list(v['duration']) != b}
    # 双向：正常配置能 import；故意漂移时 policy 应抛错。
    # ★ 2026-11-09 安全修复（审计勘误 P0）：只在**临时副本**上做漂移注入，
    #   绝不改写 config/engine.json 正典 —— 旧版若进程被杀会留下 [60,900] 残值，
    #   下次全链 import policy 直接失败。评估脚本必须对一个字都不写正典。
    import os as _os
    with tempfile.TemporaryDirectory() as td:
        cfg = json.loads((ROOT / 'config' / 'engine.json').read_text(encoding='utf-8'))
        cfg['platform_profiles']['douyin']['duration'] = [60, 900]
        fake = Path(td) / 'engine.json'
        fake.write_text(json.dumps(cfg, ensure_ascii=False), encoding='utf-8')
        env = dict(_os.environ, POLICY_CONFIG=str(fake))
        cmd = f"import sys;sys.path.insert(0,r'{SCRIPTS}');import policy"
        try:
            r = subprocess.run([sys.executable, '-c', cmd],
                               capture_output=True, text=True, env=env, timeout=30)
            drift_ok = r.returncode != 0 and '漂移' in r.stderr
        except Exception:
            drift_ok = False
    return not bad and drift_ok, \
        f'漂移平台={bad}；临时副本注入时断言抛错={drift_ok}（正典未改动）'


def e05():
    P = policy.dedupe_policy()
    td, root = _tmp_task({
        1: [_shot(1, 10.0, '第一集开场她被赶出家门'), _shot(1, 20.0, '第一集她攥着工牌没哭'),
            _shot(2, 30.0, '第一集夜里她租下破院子')],
        2: [_shot(5, 50.0, '第二集灶火第一次旺起来'), _shot(6, 60.0, '第二集进账栏还是空的'),
            _shot(7, 70.0, '第二集她拒用注水肉')],
    })
    with td:
        ok_clean, rep_clean = episode_dedupe.check(root, 2, {}, [])
        # 制造 1 个重复镜头 → 必须被拒且报出 ep@秒桶
        specs = json.loads((root / '_plan' / 'spec-2.json').read_text(encoding='utf-8'))
        specs['shots'].append(_shot(1, 10.0, '第二集补了一个完全不同的画面说明'))
        (root / '_plan' / 'spec-2.json').write_text(
            json.dumps(specs, ensure_ascii=False), encoding='utf-8')
        ok_dup, rep_dup = episode_dedupe.check(root, 2, {}, [])
        detail = ' '.join(c['detail'] for c in rep_dup['checks'])
        return (ok_clean and not ok_dup and '1@10' in detail and
                P['max_shot_overlap_ratio'] == 0), \
            f'干净={ok_clean} 重复={ok_dup} 报出具体镜头={"1@10" in detail}'


def e06():
    # 长集 60 镜里只抄短集 3 镜 → 按较小集分母 = 3/3，必须仍被抓
    td, root = _tmp_task({
        1: [_shot(1, 10.0), _shot(1, 20.0), _shot(1, 30.0)],
        2: [_shot(1, 10.0), _shot(1, 20.0), _shot(1, 30.0)] +
           [_shot(9, float(i)) for i in range(100, 160)],
    })
    with td:
        ok, rep = episode_dedupe.check(root, 2, {}, [])
        r = next((c['detail'] for c in rep['checks'] if '镜头重叠' in c['name']), '')
        return not ok and '3/3' in r, f'被拦={not ok} 比例={r[:70]}'


def e07():
    td, root = _tmp_task({1: [_shot(1, 10.0)]})
    with td:
        ok, rep = episode_dedupe.check(root, 1, {}, [])
        return ok and not rep['checks'], f'首集放行={ok} 检查项={len(rep["checks"])}'


def e08():
    # 读不到（损坏文件）→ 保守拒绝；确实没有 → 放行
    td, root = _tmp_task({2: [_shot(2, 10.0)]})
    with td:
        (root / '_plan' / 'spec-1.json').write_text('{坏 JSON', encoding='utf-8')
        ok_bad, rep_bad = episode_dedupe.check(root, 2, {}, [])
        return (not ok_bad and bool(rep_bad['fatal']) and
                '读不到' in rep_bad['fatal']), f'损坏时拒绝={not ok_bad} fatal={bool(rep_bad["fatal"])}'


def e09():
    s = (ROOT / 'references' / '00-pitfalls.md').read_text(encoding='utf-8')
    nums = sorted({int(x) for x in re.findall(r'^### P(\d{2}) ', s, re.M)})
    idx = re.findall(r'^\| P(\d{2}) \|', s, re.M)
    return nums == list(range(1, len(nums) + 1)) and len(idx) == len(nums), \
        f'正文 {nums[0]}–P{nums[-1]:02d} 共{len(nums)}条 / 索引{len(idx)}行'


def e10():
    """引用完整性：扫**所有活跃文档**（含坑典），归档区 _历史/ 不扫。

    归档文档里的悬空引用是历史追溯记录（P86 已标注），不算当前能力失效。
    """
    PAT = (r'`((?:scripts|references|docs|config|evals|agents|assets|examples)'
           r'/[\w./\-]+\.(?:py|md|json|sh|ps1))`')
    files = [ROOT / 'SKILL.md', ROOT / 'references' / '00-index.md',
             ROOT / 'references' / '00-pitfalls.md']
    files += sorted((ROOT / 'docs').glob('*.md'))
    files += [x for x in sorted((ROOT / 'references').glob('*.md'))
              if x.name not in ('00-pitfalls.md', '00-index.md')]
    refs = set()
    # 明确标注「不存在 / 规划未落地 / 已废弃」的行是历史记录，不是当前引用
    LEGACY = re.compile(r'不存在|未落地|已作废|已废弃|从未存在|规划|拟建|建议新建')
    for f in files:
        if f.is_file():
            for line in f.read_text(encoding='utf-8').splitlines():
                if LEGACY.search(line):
                    continue
                refs |= set(re.findall(PAT, line))
    missing = [r for r in refs if '<' not in r and not (ROOT / r).is_file()]
    return not missing, f'{len(files)} 份活跃文档 / {len(refs)} 个引用，缺失: {missing[:4]}'


def e11():
    n = len((ROOT / 'SKILL.md').read_text(encoding='utf-8').splitlines())
    return n < 500, f'SKILL.md {n} 行（上限 500）'


def e12():
    bad = []
    for f in (ROOT / 'scripts').glob('*.py'):
        s = f.read_text(encoding='utf-8')
        if re.search(r'^TIER_RANGE\s*=', s, re.M):
            bad.append(f'{f.name}:TIER_RANGE')
        if re.search(r"'--tier',\s*self\.a\.tier", s):
            bad.append(f'{f.name}:传 --tier')
    return not bad, f'残留: {bad or "无"}'


def e13():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / '_s1').mkdir()
        for i in range(1, 11):
            (root / '_s1' / f'ep{i}').mkdir()
        a = ps.ingest_ready(root, 10)
        b = ps.ingest_ready(root, 11)
        # 布尔标志不存在时仍应正确
        return a and not b, f'10集→{a} 11集→{b}（无 bootstrapped 字段）'


def e14():
    s = (ROOT / 'scripts' / 'produce.py').read_text(encoding='utf-8')
    m = re.search(r'^STEPS\s*=\s*\[(.*?)\]', s, re.M | re.S)
    if not m:
        return False, 'STEPS 未找到'
    steps = re.findall(r"'(\w+)'", m.group(1))
    return steps and steps[0] == 'precheck' and steps[-1] == 'deliver' and len(steps) == 10, \
        f'{len(steps)} 步: {steps}'


def e15():
    td, root = _tmp_task({
        1: [{'ep': 1, 't': 10.0, 'text': '这句含破折号——会被拦', 'raw': False}]})
    with td:
        (root / '_plan' / 'gencfg.json').write_text('{}', encoding='utf-8')
        r = subprocess.run([sys.executable, str(SCRIPTS / 'spec_precheck.py'),
                            str(root), '1', str(root / '_plan' / 'gencfg.json')],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=60)
        out = (r.stdout or '') + (r.stderr or '')
        return r.returncode != 0 and '敌对字符' in out, f'rc={r.returncode} 命中={("敌对字符" in out)}'


def e16():
    r = subprocess.run([sys.executable, str(SCRIPTS / 'auto_pilot.py'), '--dry-run'],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=180)
    try:
        out = r.stdout or ''
        start = out.find('{')
        d, _ = json.JSONDecoder().raw_decode(out[start:]) if start >= 0 else ({}, 0)
    except Exception as ex:
        return False, f'输出不可解析: {type(ex).__name__}: {(r.stdout or "")[-120:]}'
    return d.get('ok') is True and d.get('actions') == [], \
        f'ok={d.get("ok")} actions={len(d.get("actions", []))}'


def _delivered():
    p = Path(CFG['paths']['task_root']) / '_tools' / 'episodes.json'
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return []


def e17():
    items = []
    for e in _delivered():
        f = Path(e['task']) / 'out' / e['prefix'] / f"{e['name']}-封面版.mp4"
        if not f.is_file():
            continue
        r = subprocess.run([FFPROBE, '-v', 'error', '-show_entries',
                            'format=duration', '-of', 'csv=p=0', str(f)],
                           capture_output=True, text=True, timeout=60)
        try:
            items.append((e['prefix'], float(r.stdout.strip())))
        except Exception:
            pass
    # 收口到 policy.check_measured，避免第 5 份时长口径（审计勘误 §2.2）
    bad = [(p, d) for p, d in items if not policy.check_measured(d)[0]]
    return not bad, f'检查 {len(items)} 集，越界: {bad}' if items else '暂无成品，跳过'


def e18():
    """AI 隐式标识元数据 —— ★ v6.0.0 判据收口到 compliance.has_aigc_tag（P92）。

    旧版在这里自己拼了一遍 tags 判断，与 stamp 写后复核形成第二份口径；
    两处漂移就会出现「以为加了标识其实没加」却全链通过（P91 同族）。
    """
    import compliance
    items = []
    for e in _delivered():
        f = Path(e['task']) / 'out' / e['prefix'] / f"{e['name']}-封面版.mp4"
        if not f.is_file():
            continue
        marked, why = compliance.has_aigc_tag(f)
        if why.startswith('unknown:'):
            marked = False          # 读不到 ≠ 已标识（保守）
        items.append((e['prefix'], marked))
    ok = all(v for _, v in items)
    return ok, (f'{len(items)} 集中 {sum(1 for _, v in items if v)} 集含 AIGC 隐式标识'
                '（判据=compliance.has_aigc_tag）' if items else '暂无成品，跳过')


def e19():
    """配置契约审计：配置与代码常量不得漂移，且关键脚本必须从配置取值。"""
    r = subprocess.run([sys.executable, str(SCRIPTS / 'contracts.py'), '--audit'],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=60)
    first = (r.stdout or '').strip().splitlines()
    return r.returncode == 0, (first[0] if first else '无输出')


def e20():
    """学习闭环产物存在：质检结论已被抽取成可复盘的学习报告。"""
    p = EVALS / 'learning_report.json'
    if not p.is_file():
        return False, 'learning_report.json 未生成（先跑 learn_from_runs.py）'
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
    except Exception as ex:
        return False, f'学习报告不可解析: {type(ex).__name__}'
    n = d.get('episodes_scanned', 0)
    return isinstance(n, int) and n >= 1, \
        f'扫描 {n} 集 / 风险簇 {len(d.get("recurring_risks", []))} 个'


def e21():
    """对外契约一致：两份面向人/Agent 的文档不能给出相反的主入口。"""
    ai = (ROOT / 'references' / 'agent-integration.md').read_text(encoding='utf-8')
    doc = (ROOT / 'docs' / '说明文档.md').read_text(encoding='utf-8')
    bad = []
    if re.search(r'^##\s+.*当前支持的调用方式', ai, re.M):
        bad.append('agent-integration 仍有「当前支持的调用方式」章节（旧主入口）')
    if re.search(r'^##\s+.*准备单集请求', ai, re.M):
        bad.append('agent-integration 仍以单集请求为主流程')
    for label, t in (('agent-integration', ai), ('说明文档', doc)):
        if 'produce.py --pilot' not in t:
            bad.append(f'{label} 未出现 produce.py --pilot')
    if re.search(r'^## .*步 ?[0-9].*建档', doc, re.M):
        bad.append('说明文档仍以手工八步为主教程')
    return not bad, f'主入口一致，问题: {bad or "无"}'


def e22():
    """坑典结构：取代标注 / 补充段落层级 / 分类导航。"""
    t = (ROOT / 'references' / '00-pitfalls.md').read_text(encoding='utf-8')
    bad = []
    for pid in ('P80', 'P83', 'P84'):
        m = re.search(rf'^### {pid} ', t, re.M)
        if not m or '状态：' not in t[m.end():m.end() + 400]:
            bad.append(f'{pid} 缺取代标注')
    if '## 分类导航' not in t:
        bad.append('缺分类导航')
    if re.search(r'^\*\*★ (适用范围扩展|2026-11-03 补充|同一轮里连踩)', t, re.M):
        bad.append('仍有补充段落以加粗挂在正文后')
    if re.search(r'^## .*速查：出片前必过', t, re.M):
        bad.append('速查章节仍与大类同级')
    return not bad, f'结构检查，问题: {bad or "无"}'


def e23():
    """AIGC 判据收口（P92）：双向验证标准键 / 自定义键 / 读不到 / 主链接入。"""
    import compliance
    a = compliance.aigc_values({'comment': 'AIGC: x', 'encoder': 'Lavf'})
    b = compliance.aigc_values({'comment': 'hello', 'AIGC_WorkId': 'AIGC'})
    ok_std = a == {'comment': 'AIGC: x'}
    ok_custom_rejected = b == {}          # P91：自定义键不算
    ok_unknown, why = compliance.has_aigc_tag(ROOT / '__no_such_video__.mp4')
    ok_unknown = (not ok_unknown) and why.startswith('unknown:')
    prod = (SCRIPTS / 'produce.py').read_text(encoding='utf-8')
    ok_wired = '_ensure_aigc_stamp' in prod and 'compliance.py' in prod
    return all((ok_std, ok_custom_rejected, ok_unknown, ok_wired)), \
        (f'标准键={ok_std} 自定义键拒={ok_custom_rejected} '
         f'读不到=unknown:{ok_unknown} 主链接入={ok_wired}')


def e24():
    """media_qc 防呆（P95）+ 预期静止分类双向验证（P97）。"""
    import media_qc
    src = (SCRIPTS / 'media_qc.py').read_text(encoding='utf-8')
    ok_keys = all(k in media_qc.P for k in media_qc._REQUIRED)
    ok_regex = media_qc._RE_FREEZE_START.search(
        'lavfi.freezedetect.freeze_start: 0') is not None
    ok_fallback = 'total_duration' in src and 'freeze_start' in src
    _log, err = media_qc._ff([str(ROOT / '__no_such_binary__.exe')])
    ok_unknown = err is not None
    # 双向：预期区间内的归 expected，区间外的仍判异常（真告警没被吞掉）
    iv = [{'start': 0.2, 'end': 1.4}, {'start': 8.2, 'end': 10.8},
          {'start': 30.0, 'end': 33.0}]
    bad, hit = media_qc.classify_freezes(iv, [[0.0, 2.0], [5.0, 11.0]], 0.35)
    ok_split = len(hit) == 2 and len(bad) == 1 and bad[0]['start'] == 30.0
    # 不给上下文 → 全部判异常（读不到 ≠ 没有，保守）
    bad2, hit2 = media_qc.classify_freezes(iv, [], 0.35)
    ok_conservative = len(bad2) == 3 and not hit2
    ok_points = "'start': round(s, 2)" in src and 'expected_intervals' in src
    return all((ok_keys, ok_regex, ok_fallback, ok_unknown,
                ok_split, ok_conservative, ok_points)), \
        (f'配置={ok_keys} 正则={ok_regex} 片尾推算={ok_fallback} '
         f'工具失败=unknown:{ok_unknown} 分类={ok_split} 保守={ok_conservative} '
         f'时间点={ok_points}')


def e25():
    """label 唯一化（P94 回归）：同目录多片 label 全撞车时不得互相覆盖。"""
    import visual_dedupe as vd
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        paths = [root / f'clip{i}.mp4' for i in range(3)]
        for p in paths:
            p.write_bytes(b'x')
        vids = [(p.parent.name, p) for p in paths]   # 三个 label 完全相同
        labels = [lb for lb, _ in vd._unique_labels(vids)]
        uniq = len(set(labels)) == len(labels)
        clean = [('A', paths[0]), ('B', paths[1]), ('C', paths[2])]
        preserved = [lb for lb, _ in vd._unique_labels(clean)] == ['A', 'B', 'C']
        return uniq and preserved, \
            f'撞车→唯一={uniq} 唯一→保持={preserved} labels={labels}'


def e26():
    """发布物料法规落位（v6.0.0 勘误）+ 读不到 ≠ 没有。"""
    import publish_kit as pk
    kit = pk.build(ROOT / '__dummy_task__', 1, ROOT / '__dummy__.mp4')
    pos = kit['explicit_label']['video_positions']
    ok_pos = '起始画面' in pos and '播放周边' in pos
    ok_old_gone = '片头前3秒' not in json.dumps(
        CFG['compliance_policy']['ai_label_positions'], ensure_ascii=False)
    ok_label = kit['publish_labels']['recommended'] == '含有AI生成内容'
    ok_side = len(kit['explicit_label']['publish_side_positions']) == 3
    ok_unknown = kit['implicit_label']['status'] == 'unknown'
    ok_manual = len(kit['manual_checklist']) >= 5
    return all((ok_pos, ok_old_gone, ok_label, ok_side, ok_unknown, ok_manual)), \
        (f'落位={ok_pos} 旧口径清零={ok_old_gone} 标签={ok_label} '
         f'发布侧={ok_side} 读不到=unknown:{ok_unknown}')


def e27():
    """stamp 幂等 + 主链强制补标（P92）+ 干跑零副作用 + 缺文件拒绝。

    ★ 2026-11-13 真实剧集干跑实测补强：旧版 `--dry-run` 会无条件 --apply，
      遇到未补标成片时预演会真的改写视频。断言必须守住这条。
    """
    import inspect
    import compliance
    s = inspect.getsource(compliance.stamp)
    prod = (SCRIPTS / 'produce.py').read_text(encoding='utf-8')
    ok_power = 'already_stamped' in s and 'has_aigc_tag' in s
    ok_wired = ('def _ensure_aigc_stamp' in prod
                and 'self._ensure_aigc_stamp(burned)' in prod)
    ok_dry_guard = ("getattr(self, 'dry', False)" in prod
                    and "cmd.append('--apply')" in prod)
    d = compliance.stamp(ROOT / '__none__.mp4', 'x', 1, apply=True)
    ok_fail = d.get('ok') is False
    # 行为验证：apply=False 必须完全不碰文件、不留临时/备份文件
    with tempfile.TemporaryDirectory() as td:
        fake = Path(td) / 'clip.mp4'
        fake.write_bytes(b'x' * 20000)
        before = (fake.stat().st_size, fake.stat().st_mtime_ns)
        d2 = compliance.stamp(fake, 'x', 1, apply=False)
        after = (fake.stat().st_size, fake.stat().st_mtime_ns)
        left = sorted(p.name for p in Path(td).iterdir())
        ok_dry_pure = (d2.get('dry_run') is True and before == after
                       and left == ['clip.mp4'])
    return all((ok_power, ok_wired, ok_fail, ok_dry_guard, ok_dry_pure)), \
        (f'幂等+唯一判据={ok_power} 主链接入={ok_wired} 缺文件拒绝={ok_fail} '
         f'干跑守卫={ok_dry_guard} 干跑零副作用={ok_dry_pure}')


def e28():
    """调研正典：决策卡 + 存疑清单 + 配置已按法规原文修正。"""
    t = (ROOT / 'references' / '06-research-2026-11.md').read_text(encoding='utf-8')
    ok_decision = '## 6. 决策卡总表' in t
    ok_doubt = '## 8. 存疑清单' in t and '禁止写进硬门' in t
    ok_errata = '起始画面' in t and '片头前 3 秒' in t
    ok_cfg = CFG['compliance_policy']['ai_label_positions'] == ['起始画面', '播放周边']
    return all((ok_decision, ok_doubt, ok_errata, ok_cfg)), \
        (f'决策卡={ok_decision} 存疑清单={ok_doubt} '
         f'勘误留痕={ok_errata} 配置修正={ok_cfg}')


CHECKS = {1: e01, 2: e02, 3: e03, 4: e04, 5: e05, 6: e06, 7: e07, 8: e08,
          9: e09, 10: e10, 11: e11, 12: e12, 13: e13, 14: e14, 15: e15,
          16: e16, 17: e17, 18: e18, 19: e19, 20: e20,
          21: e21, 22: e22, 23: e23, 24: e24, 25: e25, 26: e26,
          27: e27, 28: e28}


def main():
    ap = argparse.ArgumentParser(description='技能离线评估（不跑 GPU / 不碰生产）')
    ap.add_argument('--id', type=int, default=0)
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()

    spec = json.loads((EVALS / 'evals.json').read_text(encoding='utf-8'))
    # ★ 两处同步（审计勘误）：新增/删减断言必须同时改 evals.json 与 CHECKS，
    #   否则会因 total 变化让 pass_rate 凭空「进步」，或静默漏跑一条断言。
    declared = {e['id'] for e in spec['evals']}
    if declared != set(CHECKS) and not a.id:
        raise SystemExit(
            f'[FAIL] evals.json 与 CHECKS 不同步：缺实现 {sorted(declared - set(CHECKS))} / '
            f'缺声明 {sorted(set(CHECKS) - declared)}。两处必须同改。')
    cases = [e for e in spec['evals'] if not a.id or e['id'] == a.id]
    results = []
    for c in cases:
        t0 = time.time()
        try:
            passed, evidence = CHECKS[c['id']]()
        except Exception as ex:
            passed, evidence = False, f'断言自身异常: {type(ex).__name__}: {ex}'
        results.append({'id': c['id'], 'name': c['name'], 'severity': c['severity'],
                        'passed': bool(passed), 'evidence': str(evidence)[:300],
                        'compliance_pending': bool(c.get('compliance_pending')),
                        'note': c.get('note', ''),
                        'ms': round((time.time() - t0) * 1000)})
    hard = [r for r in results if r['severity'] == 'hard']
    soft = [r for r in results if r['severity'] == 'soft']
    hp = sum(r['passed'] for r in hard)
    report = {
        'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
        'skill_version': re.search(r'skill_version:\s*([\d.]+)',
                                   (ROOT / 'SKILL.md').read_text(encoding='utf-8')).group(1),
        'total': len(results), 'hard_pass': hp, 'hard_total': len(hard),
        'soft_pass': sum(r['passed'] for r in soft), 'soft_total': len(soft),
        'pass_rate': round((hp + sum(r['passed'] for r in soft)) / max(1, len(results)), 3),
        'ok': all(r['passed'] for r in hard),
        'results': results,
    }
    cur_by_id = {str(r['id']): r['passed'] for r in results}
    prev = None
    hist = EVALS / 'eval_history.jsonl'
    if hist.is_file():
        lines = [l for l in hist.read_text(encoding='utf-8').splitlines() if l.strip()]
        if lines:
            try:
                prev = json.loads(lines[-1])
            except Exception:
                pass
    # ★ 审计勘误：新增断言会改变 total，直接比 pass_rate 会凭空显示「进步」。
    #   → delta 只在**两轮都存在的 id 集合**上计算，并显式标注新增/移除。
    if prev and prev.get('by_id'):
        common = sorted(set(cur_by_id) & set(prev['by_id']), key=int)
        added = sorted(set(cur_by_id) - set(prev['by_id']), key=int)
        removed = sorted(set(prev['by_id']) - set(cur_by_id), key=int)
        if common:
            cur_rate = sum(cur_by_id[k] for k in common) / len(common)
            old_rate = sum(prev['by_id'][k] for k in common) / len(common)
            report['delta_vs_prev'] = round(cur_rate - old_rate, 3)
        else:
            report['delta_vs_prev'] = None
        newly_passed = [int(k) for k in common
                        if cur_by_id[k] and not prev['by_id'][k]]
        newly_failed = [int(k) for k in common
                        if not cur_by_id[k] and prev['by_id'][k]]
        report['newly_passed_ids'] = newly_passed
        report['newly_failed_ids'] = newly_failed
        report['delta_scope'] = {
            'common_ids': common, 'added_ids': added, 'removed_ids': removed,
            'total_changed': len(results) != prev.get('total'),
            'note': ('仅同 id 集合可比；新增断言不计入 delta。'
                     'newly_failed_ids 非空 = 本轮引入回归，优先修它。'),
        }
    else:
        report['delta_vs_prev'] = None
        report['newly_passed_ids'] = []
        report['newly_failed_ids'] = []
        report['delta_scope'] = {'note': '首轮或历史缺 by_id，无同口径对比'}
    (EVALS / 'eval_report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    with hist.open('a', encoding='utf-8') as f:
        f.write(json.dumps({
            'ts': report['ts'], 'skill_version': report['skill_version'],
            'pass_rate': report['pass_rate'], 'ok': report['ok'],
            'hard_pass': report['hard_pass'], 'hard_total': report['hard_total'],
            'total': report['total'], 'delta_vs_prev': report['delta_vs_prev'],
            'by_id': cur_by_id,
        }, ensure_ascii=False) + '\n')

    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(f'── 技能评估 · {report["skill_version"]} · {report["ts"]} ──')
        for r in results:
            mark = '✓' if r['passed'] else ('✗' if r['severity'] == 'hard' else '!')
            print(f'  {mark} [{r["id"]:02d}] {r["name"]:16s} {r["ms"]:5d}ms  {r["evidence"][:82]}')
        print(f'\n  hard {hp}/{len(hard)} · soft {report["soft_pass"]}/{len(soft)} · '
              f'pass_rate={report["pass_rate"]} · delta={report["delta_vs_prev"]} · '
              f'{"✓ PASS" if report["ok"] else "✗ FAIL"}')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
