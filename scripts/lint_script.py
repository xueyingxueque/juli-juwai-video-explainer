"""只跑 S3 的稿件门校验，不动写稿 —— 用于人工改稿的秒级迭代 + 改稿后重验写回。

为什么要有：S3 一次生成要 15 分钟（VLM 写稿三次），人工改一句就重跑一轮不现实。
本脚本用**同一套判据函数**（lint_lines / schedule_problems / audit_grounding）做校验，
改完立刻知道过没过，和 S3 内部的口径完全一致（不另造标准）。

用法：
  python lint_script.py <任务目录> [script.json]           # 只读校验
  python lint_script.py <任务目录> --write                 # 校验通过后写回 script.json 的 lint
  （--write 会跑 grounding 审计，需 VLM 在线，约 1-3 分钟）
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# s3_script 在**模块级**就解析 sys.argv（TARGET_SEC = int(sys.argv[2])），
# 直接 import 会拿到本脚本的参数而崩。导入期间先给个合法 argv，之后还原。
_real_argv = sys.argv[:]
sys.argv = [sys.argv[0], '1', '60']
import s3_script as S                      # noqa: E402
sys.argv = _real_argv

args = [a for a in sys.argv[1:] if not a.startswith('--')]
WRITE = '--write' in sys.argv

TASK = Path(args[0])
SCRIPT = Path(args[1]) if len(args) > 1 else TASK / 'script.json'

story = json.loads((TASK / 'story.json').read_text(encoding='utf-8'))
ev = json.loads((TASK / 'evidence.json').read_text(encoding='utf-8'))
ev = ev if isinstance(ev, list) else (ev.get('items') or ev.get('evidence') or [])
req = json.loads((TASK / 'request.json').read_text(encoding='utf-8'))
cfg = json.loads((Path(S.__file__).resolve().parent.parent / 'config' / 'engine.json')
                 .read_text(encoding='utf-8'))

TARGET = int(req.get('target_seconds', 60))
S.TARGET_SEC = TARGET
lo, hi = TARGET * 4.5, TARGET * 6.0      # 与 s3_script.py 主流程保持一致

profile = cfg.get('platform_profiles', {}).get(
    req.get('platform', 'generic'), cfg['platform_profiles']['generic'])
cap = int(profile.get('chars_per_line', 14)) * int(profile.get('subtitle_max_lines', 2))

by_id = {e['evidence_id']: e.get('desc', '') for e in ev}
evidence_by_id = {e['evidence_id']: e for e in ev}

pov = story.get('pov') or {}
mn, fn, _ = S.name_and_tone(story)

lines = json.loads(SCRIPT.read_text(encoding='utf-8')).get('lines') or []

r = S.lint_lines(lines, by_id, lo, hi,
                 pov_gender=pov.get('gender', ''), pov_name=mn or fn,
                 caption_capacity=cap, evidence_by_id=evidence_by_id,
                 short_line_max_chars=int(cfg.get('script_quality', {}).get(
                     'short_story_line_max_chars', 12)),
                 max_short_story_line_ratio=float(cfg.get('script_quality', {}).get(
                     'max_short_story_line_ratio', 0.4)))

print(f'句数 {len(lines)}  字数 {r["chars"]}（预算 {lo:.0f}-{hi:.0f}，容差 {r["budget_slack_chars"]}）')
print(f'可读性 {r["readability"]["verdict"]}  承接 {r["cohesion"]["verdict"]}  '
      f'去重 {r["semantic_dup"]["verdict"]}  段落 {r["block_structure"]["verdict"]}  '
      f'主体 {r["subject_match"]["verdict"]}  字幕 {r["subtitle_claims"]["verdict"]}  '
      f'推断 {r["inference_labels"]["verdict"]}  多样 {r["evidence_diversity"]["verdict"]}')
print()

# ── 第一人称视角 + 短视频句法规范（2026-10-21 用户令，SKILL.md §3.8）──
#   不并进 s3_script.lint_lines（避免动 S3 主链判据），这里是 lint 壳里的
#   增量检查：改稿后一并秒验。
import re as _re
CONNECTIVES = ['然后', '接着', '首先', '其次', '最后', '与此同时', '于是', '紧接着']
texts = [ln.get('text') or '' for ln in lines]
fp = sum(1 for t in texts if '我' in t)
tp = sum(1 for t in texts if _re.search(r'[他她它]', t) and '我' not in t)
pov_issues = []
fp_ratio = fp / max(len(texts), 1)
if fp_ratio < 0.3:
    pov_issues.append(f'第一人称占比过低 {fp_ratio:.0%}（目标≥30%，默认主角「我」视角；'
                      f'萌宝/宠物等非人主角可放宽，但需在 request.json 注明 pov=narrator）')
conn_hits = [t for t in texts if any(c in t for c in CONNECTIVES)]
if conn_hits:
    pov_issues.append(f'连接词命中 {len(conn_hits)} 句（流量杀手，禁）: {conn_hits[:3]}')
long_hits = [(i, len(t)) for i, t in enumerate(texts, 1) if len(t) > 20]
if long_hits:
    pov_issues.append(f'超 20 字长句 {len(long_hits)} 条（口播句 ≤20 字，抖音字幕 26 上限）: {long_hits[:3]}')
if tp / max(len(texts), 1) > 0.4:
    pov_issues.append(f'第三人称旁白占比 {tp/max(len(texts),1):.0%} >40%（除必要转述外改「我」视角）')
# ── 情绪词库（2026-10-22 补 §6 gap 13 余项；advisory 不 FAIL）──
#   六流人格 tone 不同（治愈流/温暖向本就少烈词），命中率低只提醒不拦。
EMOTION_WORDS = ['窒息', '崩塌', '反转', '爆发', '崩溃', '暴击', '炸裂', '逆袭', '翻盘',
                 '打脸', '破防', '泪目', '沸腾', '出气', '气笑', '看傻', '看直', '惊了',
                 '愣了', '抖了', '寒了', '狠', '绝了', '燃', '爽']
emo_hits = [t for t in texts if any(w in t for w in EMOTION_WORDS)]
emo_ratio = len(emo_hits) / max(len(texts), 1)
if emo_ratio < 0.25:
    print(f'--- pov_style WARN：情绪词命中率 {emo_ratio:.0%} <25%'
          f'（爆款钩子句宜带情绪词——窒息/崩塌/反转/打脸/破防；'
          f'治愈流等软 tone 可忽略本条）---')
    print()
if pov_issues:
    print('--- pov_style FAIL（SKILL.md §3.8 第一人称规范）---')
    for x in pov_issues:
        print('  ' + x)
    print()
else:
    print('--- pov_style PASS（第一人称/禁连接词/句长）---')
    print()

for key in ('readability', 'cohesion', 'semantic_dup', 'block_structure',
            'subject_match', 'subtitle_claims', 'inference_labels', 'evidence_diversity'):
    d = r.get(key) or {}
    if d.get('verdict') == 'PASS':
        continue
    print(f'--- {key} FAIL ---')
    for k, v in d.items():
        if k in ('verdict', 'total', 'checked'):
            continue
        if v in ([], {}, '', 0, None, False):
            continue
        print(f'  {k}: {json.dumps(v, ensure_ascii=False)[:600]}')
    print()

for i, ln in enumerate(lines, 1):
    t = ln.get('text') or ''
    if len(t) > cap:
        print(f'  [超长] 第{i}句 {len(t)}字 > {cap}: {t}')
if r.get('long_lines'):
    print('  long_lines:', json.dumps(r['long_lines'], ensure_ascii=False))
if r.get('no_evidence'):
    print(f'  无证据句数: {r["no_evidence"]}')
if r.get('gender_ok') is False:
    print('  gender_ok=False（POV 人称不一致）')

# ── 镜头表门（每句 1 个主镜头 / 主镜头在引用集内 / ≥6 个不同主镜头）──
story_lines = [l for l in lines
               if (l.get('block') or '').strip().lower() not in ('cta', 'ending_cta')]
probs = S.schedule_problems(lines, min(6, len(story_lines)))
r['evidence_schedule'] = {
    'verdict': 'PASS' if not probs else 'FAIL',
    'mismatches': probs,
    # script_to_plan 逐句核对「主镜头 == 预排 ID」，必须一行一个（CTA 为空）
    'expected_ids': [(l.get('primary_evidence_id') or '') for l in lines],
    'actual_line_count': len(lines),
    'distinct_primary_ids': len({l.get('primary_evidence_id') for l in lines
                                 if l.get('primary_evidence_id')}),
    'contract': 'primary_is_cited_evidence; chronology_and_diversity_are_checked_separately',
}
print(f'镜头表 {r["evidence_schedule"]["verdict"]}')
for p in probs[:5]:
    print('  ', json.dumps(p, ensure_ascii=False)[:200])

if not WRITE:
    sys.exit(0)

# ── grounding：逐句核对事实是否被所列证据支持（VLM 审计）──
print('\n跑 grounding 审计（VLM，约 1-3 分钟）...', flush=True)
g = S.audit_grounding(lines, ev, '',
                      '不预设全片主角；按各句引用证据确认人物、动作与关系')
r['grounding'] = g
print('grounding:', g.get('verdict'), (g.get('error') or '')[:120])
if (g.get('verdict') or '') != 'PASS':
    for k, v in g.items():
        if k in ('verdict', 'total', 'checked') or not v:
            continue
        print(f'  grounding.{k}:', json.dumps(v, ensure_ascii=False)[:900])

REQUIRED = ('inference_labels', 'readability', 'cohesion', 'semantic_dup',
            'block_structure', 'subject_match', 'evidence_diversity',
            'evidence_schedule', 'grounding')
failed = [k for k in REQUIRED if (r.get(k) or {}).get('verdict') != 'PASS']
ok = (r.get('in_budget') is True and r.get('no_evidence') == 0
      and not r.get('long_lines') and r.get('gender_ok') is True and not failed)
if not ok:
    print('\n[拒绝写回] 仍有门未通过：'
          + ', '.join(failed + ([] if r.get('in_budget') else ['in_budget'])))
    sys.exit(2)

data = json.loads(SCRIPT.read_text(encoding='utf-8'))
data['lint'] = r
SCRIPT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
print(f'\n[已写回] {SCRIPT.name} 的 lint 字段（九门全 PASS）')
