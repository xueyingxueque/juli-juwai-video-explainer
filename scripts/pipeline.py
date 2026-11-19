"""剧里剧外 · 视频解说引擎 v2.0 —— 六阶段主控。

设计原则（相对 v1.1 的三处根本修正）：
1. 零阶段先跑能力预检，不合格直接拒绝，不浪费生产与返修预算。
2. 硬门/软门两档质检，软门不阻断，交付分 A/B/C 档。
3. 所有阈值从 config/engine.json 读，正典唯一，禁止散落硬编码。

状态机：pending → running → done / failed / blocked
回环仅限 S5 → S3（软门需局部改写），且只修失败句。
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG_PATH = ROOT / 'config' / 'engine.json'


def _read_skill_version() -> str:
    """★ 版本号唯一源 = SKILL.md frontmatter 的 metadata.skill_version。

    历史上版本号在三个地方各写一份（SKILL.md 标题 / pipeline.py VERSION /
    engine.json engine_version），结果三处互相漂移——SKILL.md 到 3.6.6 而
    pipeline.py 还停在 2.0.0，配置一致性校验天天发假警告。

    现在只认 SKILL.md 一处：改版本只改 SKILL.md frontmatter，这里自动跟上。
    engine.json 的 engine_version 保留作**配置 schema 快照**，由 load_cfg() 的
    warn 提醒同步——它是数据不是版本源。
    """
    try:
        txt = (ROOT / 'SKILL.md').read_text(encoding='utf-8')
        m = re.search(r'^\s*skill_version:\s*["\']?([\d][\d.]*)', txt, re.M)
        if m:
            return m.group(1)
    except Exception:
        pass
    return '0.0.0'


VERSION = _read_skill_version()

STAGES = ['material_precheck', 'preflight', 's1_ingest', 's2_story', 's3_script',
          's4_assemble', 's5_qc', 's6_deliver']


def load_cfg() -> dict:
    cfg = json.loads(CFG_PATH.read_text(encoding='utf-8'))
    if cfg.get('engine_version') != VERSION:
        print(f'[warn] 配置版本 {cfg.get("engine_version")} 与引擎 {VERSION} 不一致', file=sys.stderr)
    return cfg


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


class State:
    """任务状态。落盘即恢复依据，不依赖内存。"""

    def __init__(self, out: Path, req: dict):
        self.out = out
        self.data = {
            'engine_version': VERSION,
            'created_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'request': req,
            'status': 'initialized',
            'active_stage': None,
            'stages': {s: {'status': 'pending', 'error': None, 'started_at': None, 'ended_at': None} for s in STAGES},
            'error': None,
            'rework_rounds': 0,
            'qc': None,
            'artifacts': {},
            'risks': [],
        }
        self.out.mkdir(parents=True, exist_ok=True)
        self.flush()

    def flush(self):
        write_json(self.out / 'state.json', self.data)

    def begin(self, stage: str):
        self.data['active_stage'] = stage
        self.data['stages'][stage].update(status='running', started_at=time.strftime('%H:%M:%S'))
        self.data['status'] = 'running'
        self.flush()

    def end(self, stage: str, ok=True, error=None):
        self.data['stages'][stage].update(status='done' if ok else 'failed', error=error,
                                           ended_at=time.strftime('%H:%M:%S'))
        self.data['active_stage'] = None
        if not ok:
            self.data['error'] = {'stage': stage, 'code': 'STAGE_FAILED', 'detail': error}
        self.flush()

    def artifact(self, name: str, path: Path):
        if Path(path).exists():
            self.data['artifacts'][name] = {'path': str(path), 'sha256': sha256(Path(path))}
            self.flush()


def probe_duration(cfg: dict, video: Path) -> float:
    r = subprocess.run([cfg['runtime']['ffprobe'], '-v', 'error', '-show_entries',
                        'format=duration', '-of', 'csv=p=0', str(video)],
                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def run_preflight(cfg: dict, st: State) -> dict:
    """零阶段：写入任务专属报告，绝不复用上一次任务的共享报告。"""
    script = Path(__file__).parent / 'preflight.py'
    report = st.out / 'preflight.json'
    report.unlink(missing_ok=True)
    try:
        result = subprocess.run([sys.executable, '-X', 'utf8', str(script), '--out', str(report)],
                                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3600)
    except Exception as e:
        decision = {'verdict': 'refuse',
                    'reasons': [f'预检无法启动: {type(e).__name__}: {e}'],
                    'remedy': '检查当前 Python 与本地运行时路径'}
        write_json(report, {'decision': decision, 'execution_error': 'preflight_process_failed'})
        return decision
    try:
        rep = json.loads(report.read_text(encoding='utf-8'))
    except Exception as e:
        detail = f'预检报告不可读: {type(e).__name__}: {e}; exit={result.returncode}'
        if result.stderr:
            detail += f'; stderr={(result.stderr or "")[-500:]}'
        decision = {'verdict': 'refuse', 'reasons': [detail],
                    'remedy': '直接运行 scripts/preflight.py 查看完整输出'}
        write_json(report, {'decision': decision, 'execution_error': 'preflight_report_unreadable'})
        return decision
    dec = rep.get('decision', {})
    if result.returncode not in (0, 2):
        return {'verdict': 'refuse', 'reasons': [f'预检异常退出: {result.returncode}'],
                'remedy': '检查任务目录中的预检报告与本地依赖'}
    reasons = dec.get('reasons') or []
    if dec.get('verdict') == 'ready':
        return dec
    if dec.get('verdict') == 'degraded':
        dec['risks'] = ['ASR 能力偏弱，soft/speech 门降为记分不阻断，成片语义一致性未充分验证']
        return dec
    if not reasons:
        reasons = ['预检判定为 refuse 但未给出具体原因，请查任务目录中的 preflight.json']
    return {'verdict': 'refuse', 'reasons': reasons,
            'remedy': '跑 scripts/fetch_asr.py 重下完整 ASR 模型，或换中文 ASR（Parformer/SenseVoice）'}


def run_logged(stage: str, command: list[str], out: Path,
               expected: list[Path], timeout: int = 3600,
               accepted_codes: tuple[int, ...] = (0,)) -> tuple[bool, str]:
    """Run one real stage, keep verbose output on disk, and verify its outputs."""
    try:
        r = subprocess.run([str(x) for x in command], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=timeout, cwd=str(ROOT))
        log_dir = out / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f'{stage}.log').write_text(
            (r.stdout or '') + ('\n[stderr]\n' + r.stderr if r.stderr else ''), encoding='utf-8')
        if r.returncode not in accepted_codes:
            return False, f'{stage} exited {r.returncode}; see logs/{stage}.log'
        missing = [str(p) for p in expected if not p.exists()]
        if missing:
            return False, f'{stage} returned success but required outputs are missing: {missing}'
        return True, ''
    except Exception as e:
        return False, f'{stage} failed to start: {type(e).__name__}: {e}'


def fail_stage(st: State, stage: str, detail: str, code: str = 'STAGE_FAILED') -> int:
    st.end(stage, ok=False, error=code)
    st.data['status'] = 'failed'
    st.data['error'] = {'stage': stage, 'code': code, 'detail': detail}
    st.flush()
    print(json.dumps({'status': 'failed', 'stage': stage, 'code': code,
                      'detail': detail}, ensure_ascii=False, indent=2))
    return 1


def cmd_run(args) -> int:
    cfg = load_cfg()
    req = json.loads(Path(args.request).read_text(encoding='utf-8'))
    src = Path(req['source'])
    if not src.is_file():
        print(f'[ERR] 素材文件不存在或不是文件: {src}', file=sys.stderr)
        return 1
    platform = req.get('platform', 'generic')
    if platform not in cfg['platform_profiles']:
        print(f'[ERR] 未知平台配置: {platform}', file=sys.stderr)
        return 1
    out = Path(args.out)
    if (out / 'state.json').exists():
        print(json.dumps({'status': 'failed', 'code': 'OUTPUT_TASK_EXISTS',
                          'detail': f'任务目录已存在状态文件，请使用新目录: {out}'},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    st = State(out, req)
    # S3 consumes the same request contract for platform-specific caption
    # limits. Keep it beside the task artifacts instead of silently falling
    # back to the generic profile when pipeline launches the script stage.
    write_json(out / 'request.json', req)

    # ---- 素材性质预检（源头上拦截，不合格不进入生产）----
    st.begin('material_precheck')
    sys.path.insert(0, str(Path(__file__).parent))
    from material_precheck import classify as mat_check
    try:
        mp = mat_check(src)
    except Exception as e:
        mp = {'verdict': '检测失败', 'error': f'{type(e).__name__}: {e}'}
    st.data['material_precheck'] = mp
    write_json(out / 'material_precheck.json', mp)
    if mp.get('verdict') == '解说成片':
        st.end('material_precheck', ok=False, error='MATERIAL_IS_NARRATED_VIDEO')
        st.data['status'] = 'failed'
        st.data['error'] = {
            'stage': 'material_precheck',
            'code': 'MATERIAL_IS_NARRATED_VIDEO',
            'detail': f"素材实为解说成片（{mp.get('hit_count')} 项命中）："
                      + '；'.join(s['detail'] for s in mp.get('signals', []) if s['hit']),
            'remedy': '解说成片只能当爆款参考库（学写法），不能提取原片事实或角色原声。'
                      '要生产成片请换真正的原始短剧素材。',
            'usable_as': mp.get('usable_as'),
        }
        st.flush()
        print(json.dumps({'status': 'failed', 'code': 'MATERIAL_IS_NARRATED_VIDEO',
                          'signals': [s for s in mp.get('signals', []) if s['hit']],
                          'usable_as': mp.get('usable_as')},
                         ensure_ascii=False, indent=2))
        return 2
    if mp.get('verdict') != '疑似原片':
        st.data['status'] = 'blocked'
        st.data['blocked_reason'] = '素材性质预检未能确认输入为原始素材'
        st.data['stages']['material_precheck'].update(status='blocked', ended_at=time.strftime('%H:%M:%S'))
        st.data['error'] = {'stage': 'material_precheck', 'code': 'MATERIAL_PRECHECK_UNVERIFIED',
                            'detail': mp, 'remedy': '检查本任务目录中的 material_precheck.json 后再处理'}
        st.data['stages']['s1_ingest'].update(status='blocked', ended_at=time.strftime('%H:%M:%S'))
        st.flush()
        print(json.dumps({'status': 'blocked', 'code': 'MATERIAL_PRECHECK_UNVERIFIED',
                          'report': str(out / 'material_precheck.json')}, ensure_ascii=False, indent=2))
        return 1
    st.end('material_precheck')

    # ---- 零阶段 ----
    st.begin('preflight')
    pf = run_preflight(cfg, st)
    st.data['preflight'] = pf
    # run_preflight owns preflight.json and preserves the full diagnostic
    # snapshot. Keep the compact decision in state.json; never overwrite the
    # detailed report with this summary.
    if pf['verdict'] == 'refuse':
        st.data['status'] = 'failed'
        st.end('preflight', ok=False, error='PREFLIGHT_REFUSED')
        st.data['error'] = {'stage': 'preflight', 'code': 'PREFLIGHT_REFUSED',
                            'detail': pf.get('reasons'), 'remedy': pf.get('remedy')}
        st.flush()
        print(json.dumps({'status': 'failed', 'code': 'PREFLIGHT_REFUSED',
                          'reasons': pf.get('reasons'), 'remedy': pf.get('remedy')},
                         ensure_ascii=False, indent=2))
        return 2
    st.end('preflight')
    if pf.get('risks'):
        st.data['risks'].extend(pf['risks'])

    cfg['active_profile'] = platform
    scripts = Path(__file__).parent
    target_seconds = int(req.get('target_seconds', 60))
    # 注：3–600 是单集验证路径的历史请求范围；主力产线硬门由 policy.check_measured(60–300s) 判定
    if not 3 <= target_seconds <= 600:
        return fail_stage(st, 'preflight', 'target_seconds must be between 3 and 600', 'INVALID_REQUEST')
    episode = req.get('episode')
    if episode is None:
        m = re.search(r'(\d+)', src.stem)
        episode = int(m.group(1)) if m else None
    if isinstance(episode, bool) or not isinstance(episode, int) or episode < 1:
        return fail_stage(st, 's1_ingest', '请在 request.json 提供正整数 episode；文件名无集数时不会猜', 'EPISODE_REQUIRED')
    st.data['request']['episode'] = episode
    st.flush()

    # S1: extract one source episode, timestamp every sampled frame, and prepare ASR text.
    st.begin('s1_ingest')
    s1_expected = [out / 'source.json', out / 'asr_full.json', out / 'frame_times.json', out / 'frames']
    ok, detail = run_logged('s1_ingest', [sys.executable, '-X', 'utf8', str(scripts / 's1_ingest.py'),
                             str(src), '--episode', str(episode), '--out', str(out)], out, s1_expected, 7200)
    if not ok:
        return fail_stage(st, 's1_ingest', detail)
    for n in ('source.json', 'asr_full.json', 'frame_times.json'):
        st.artifact(n, out / n)
    st.end('s1_ingest')

    # S2/S3: start the configured local VLM if it is not already healthy, then analyze and write.
    st.begin('s2_story')
    ensure = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                             '-File', str(scripts / 'ensure_vlm.ps1'), '-Quiet'],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=240)
    if ensure.returncode:
        (out / 'logs').mkdir(parents=True, exist_ok=True)
        (out / 'logs' / 's2_story.log').write_text(
            (ensure.stdout or '') + '\n[stderr]\n' + (ensure.stderr or ''), encoding='utf-8')
        return fail_stage(st, 's2_story', 'local VLM could not be made healthy; see logs/s2_story.log', 'VLM_UNAVAILABLE')
    ok, detail = run_logged('s2_story', [sys.executable, '-X', 'utf8', str(scripts / 's2_story.py'), str(out)],
                            out, [out / 'evidence.json', out / 'story.json'], 14400)
    if not ok:
        return fail_stage(st, 's2_story', detail)
    st.artifact('evidence', out / 'evidence.json')
    st.artifact('story', out / 'story.json')
    st.end('s2_story')

    st.begin('s3_script')
    ok, detail = run_logged('s3_script', [sys.executable, '-X', 'utf8', str(scripts / 's3_script.py'),
                            str(out), str(target_seconds), str(req.get('soul', 'auto')),
                            str(req.get('direction', ''))], out,
                            [out / 'script.json', out / 'narration.txt'], 14400)
    if not ok:
        return fail_stage(st, 's3_script', detail)
    st.artifact('script', out / 'script.json')
    st.artifact('narration', out / 'narration.txt')
    st.end('s3_script')

    # S4: map every spoken line to its selected, timestamped primary evidence; validate before TTS.
    st.begin('s4_assemble')
    ok, detail = run_logged('script_to_plan', [sys.executable, '-X', 'utf8', str(scripts / 'script_to_plan.py'),
                                  str(out), '--out', str(out / 'plan.json'),
                                  '--events-out', str(out / 'plan-events.json')], out,
                            [out / 'plan.json', out / 'plan-events.json'], 300)
    if not ok:
        return fail_stage(st, 's4_assemble', detail, 'SCRIPT_TO_PLAN_FAILED')
    delivery = out / 'delivery'
    build_cmd = [sys.executable, '-X', 'utf8', str(scripts / 'build.py'), str(out / 'plan.json'),
                 '--dataset', str(src.parent), '--source', str(src), '--events', str(out / 'plan-events.json'),
                 '--preflight-report', str(out / 'preflight.json'), '--out-dir', str(delivery),
                 '--platform', platform,
                 '--voice', str(req.get('voice', '谭湘文徐浩伦'))]
    if req.get('bgm'):
        build_cmd.extend(['--bgm', str(req['bgm'])])
    ok, detail = run_logged('s4_assemble', build_cmd, out,
                            [delivery / 'shotmap.json', delivery / 'build-plan.json',
                             delivery / '_work' / 'subs.srt'], 14400)
    if not ok:
        return fail_stage(st, 's4_assemble', detail)
    plan_data = json.loads((delivery / 'build-plan.json').read_text(encoding='utf-8'))
    video = delivery / (plan_data['name'] if plan_data['name'].lower().endswith('.mp4')
                        else plan_data['name'] + '.mp4')
    if not video.is_file():
        return fail_stage(st, 's4_assemble', f'build returned but video is missing: {video}')
    st.artifact('plan', out / 'plan.json')
    st.artifact('build_plan', delivery / 'build-plan.json')
    st.artifact('shotmap', delivery / 'shotmap.json')
    st.artifact('video', video)
    st.end('s4_assemble')

    # S5: the subtitle and source-usage map are mandatory hard-gate inputs.
    st.begin('s5_qc')
    qc_path = out / 'qc.json'
    script_data = json.loads((out / 'script.json').read_text(encoding='utf-8'))
    script_lint = script_data.get('lint') or {}
    grounding = script_lint.get('grounding') or {}
    inference = script_lint.get('inference_labels') or {}
    semantic_path = delivery / '_work' / 'semantic.json'
    write_json(semantic_path, {
        'schema_version': 1,
        'source': 'S3 per-line evidence grounding and inference-label checks',
        'unsupported_claims': grounding.get('issues', []),
        'unmarked_inference': inference.get('issues', []),
        'grounding_verdict': grounding.get('verdict', 'MISSING'),
        'inference_verdict': inference.get('verdict', 'MISSING'),
    })
    # The preflight threshold measures ASR on raw drama audio. The narration
    # gate loads its separate TTS calibration profile from engine.json.
    threshold = 'none'
    qc_cmd = [sys.executable, '-X', 'utf8', str(scripts / 'quality_gate.py'), str(video),
              str(plan_data.get('total_sec', target_seconds)), str(delivery / '_work' / 'subs.srt'),
              str(delivery / 'shotmap.json'), threshold,
              '--platform', platform, '--semantic', str(semantic_path),
              '--narration', str(out / 'narration.txt'), '--out', str(qc_path)]
    if req.get('strict_duration') is True:
        qc_cmd.append('--strict-duration')
    ok, detail = run_logged('s5_qc', qc_cmd, out, [qc_path], 3600, accepted_codes=(0, 3))
    if not ok:
        return fail_stage(st, 's5_qc', detail)
    qc = json.loads(qc_path.read_text(encoding='utf-8'))
    st.data['qc'] = {'verdict': qc.get('verdict'), 'hard_passed': qc.get('hard_passed'),
                     'soft_grade': qc.get('soft_grade'), 'risks': qc.get('risks', [])}
    st.data['risks'].extend(qc.get('risks', []))
    st.artifact('qc', qc_path)
    if not qc.get('hard_passed'):
        st.end('s5_qc')
        st.data['status'] = 'quarantined'
        st.data['error'] = {'stage': 's5_qc', 'code': 'QUALITY_GATE_REJECT',
                            'detail': qc.get('hard_failures', [])}
        st.data['stages']['s6_deliver'].update(status='blocked', ended_at=time.strftime('%H:%M:%S'))
        st.flush()
        print(json.dumps({'status': 'quarantined', 'qc': st.data['qc'],
                          'video': str(video), 'report': str(qc_path)}, ensure_ascii=False, indent=2))
        return 2
    st.end('s5_qc')

    st.begin('s6_deliver')
    manifest = {'status': 'delivered', 'video': str(video), 'plan': str(out / 'plan.json'),
                'qc': str(qc_path), 'verdict': qc.get('verdict'), 'soft_grade': qc.get('soft_grade'),
                'risks': qc.get('risks', []), 'human_review_required': True}
    write_json(out / 'delivery.json', manifest)
    st.artifact('delivery_manifest', out / 'delivery.json')
    st.end('s6_deliver')
    st.data['status'] = 'complete'
    st.flush()
    print(json.dumps({'status': 'complete', 'episode': episode,
                      'video': str(video), 'qc': qc.get('verdict'),
                      'grade': qc.get('soft_grade'), 'risks': qc.get('risks', []),
                      'review': str(out / 'delivery.json')}, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args) -> int:
    p = Path(args.out) / 'state.json'
    if not p.exists():
        print(json.dumps({'status': 'no_state', 'note': '可能在输入或环境检查前失败；读进程返回码'},
                         ensure_ascii=False))
        return 1
    d = json.loads(p.read_text(encoding='utf-8'))
    print(json.dumps({
        'status': d.get('status'), 'active_stage': d.get('active_stage'),
        'error': d.get('error'), 'rework_rounds': d.get('rework_rounds'),
        'risks': d.get('risks'), 'qc': d.get('qc'),
        'blocked_reason': d.get('blocked_reason'),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_doctor(args) -> int:
    cfg = load_cfg()
    sys.path.insert(0, str(Path(__file__).parent))
    from preflight import VERSION_ARGS, check_binary

    ok = True
    checks = {}
    binary_details = {}
    for k in ('ffmpeg', 'ffprobe', 'asr_python', 'tts_python', 'llama_server'):
        detail = check_binary(cfg['runtime'][k], VERSION_ARGS[k])
        binary_details[k] = detail
        checks[k] = detail['ok']
        ok &= detail['ok']
    for k, v in (('tts_model', cfg['tts']['model']),
                 ('tts_reference', cfg['tts']['default_reference']),
                 ('tts_dub_script', cfg['runtime']['tts_dub_script']),
                 ('vlm_model', cfg['runtime']['vlm_model']),
                 ('mmproj', cfg['runtime']['mmproj'])):
        checks[k] = Path(v).exists()
        ok &= checks[k]
    checks['material_root'] = Path(cfg['paths']['material_root']).exists()
    ok &= checks['material_root']
    print(json.dumps({'engine_version': VERSION, 'ok': bool(ok), 'checks': checks,
                      'binary_details': binary_details},
                     ensure_ascii=False, indent=2))
    return 0 if ok else 2


def cmd_batch(args) -> int:
    batch = json.loads(Path(args.request).read_text(encoding='utf-8'))
    reqs = batch.get('requests', [])
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log = []
    for i, rp in enumerate(reqs, 1):
        dest = out / f'task-{i:04d}'
        r = subprocess.run([sys.executable, '-X', 'utf8', __file__, 'run',
                            '--request', str(rp), '--out', str(dest)],
                           capture_output=True, text=True, encoding='utf-8', errors='replace')
        log.append({'index': i, 'request': str(rp), 'rc': r.returncode})
        write_json(out / 'batch.json', {'total': len(reqs), 'log': log})
    print(json.dumps({'total': len(reqs), 'log': log}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run'); r.add_argument('--request', required=True); r.add_argument('--out', required=True)
    s = sub.add_parser('status'); s.add_argument('--out', required=True)
    sub.add_parser('doctor')
    b = sub.add_parser('batch'); b.add_argument('--request', required=True); b.add_argument('--out', required=True)
    q = sub.add_parser('qc'); q.add_argument('--video', required=True); q.add_argument('--seconds', required=True)
    q.add_argument('--srt', default=''); q.add_argument('--shotmap', default=''); q.add_argument('--threshold', default='none')

    a = ap.parse_args()
    if a.cmd == 'run':
        return cmd_run(a)
    if a.cmd == 'status':
        return cmd_status(a)
    if a.cmd == 'doctor':
        return cmd_doctor(a)
    if a.cmd == 'batch':
        return cmd_batch(a)
    if a.cmd == 'qc':
        return subprocess.run([sys.executable, '-X', 'utf8', str(Path(__file__).parent / 'quality_gate.py'),
                               a.video, a.seconds, a.srt, a.shotmap, a.threshold]).returncode
    return 1


if __name__ == '__main__':
    sys.exit(main())
