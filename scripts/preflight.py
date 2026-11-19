"""能力预检门 v2.0 —— 生产前的第一道关，也是旧技能 v1.1 缺失的那一环。

设计要点（全部来自 v1.1 真实失败复盘）：
1. 先实测 ASR 真实 CER，再决定阈值；绝不像 v1.1 那样先写死 max_cer=0.0 再撞墙。
2. 模型完整性校验：缺 model.bin 一律判不可用（v1.1 的 medium 就栽在这）。
3. 不达标时明确区分 refuse（拒生产）与 degrade（降级放行），不把好稿判死。

退出码：0 = 可生产；2 = 有能力缺口（按 gate_policy 处理）；1 = 环境异常。
"""
import json
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / 'config' / 'engine.json'
VERSION_ARGS = {
    'ffmpeg': ['-version'],
    'ffprobe': ['-version'],
    'asr_python': ['-V'],
    'tts_python': ['-V'],
    'llama_server': ['--version'],
}

# 繁 -> 简 常用映射（tiny 实测输出繁体，不规范化 CER 会虚高）
_T2S = str.maketrans({
    '財': '财', '調': '调', '寫': '写', '電': '电', '動': '动', '給': '给', '們': '们',
    '個': '个', '沒': '没', '現': '现', '謝': '谢', '來': '来', '說': '说', '對': '对',
    '時': '时', '國': '国', '學': '学', '這': '这', '實': '实', '發': '发', '種': '种',
    '樣': '样', '點': '点', '會': '会', '經': '经', '過': '过', '還': '还', '讓': '让',
    '後': '后', '從': '从', '頭': '头', '媽': '妈', '兒': '儿', '願': '愿', '傷': '伤',
})


def normalize(s: str, cfg: dict) -> str:
    """中文字符比对的必要前置：繁简、全半角、标点、大小写。"""
    n = cfg['asr']['normalization']
    out = s
    if n.get('traditional_to_simplified', True):
        out = out.translate(_T2S)
    if n.get('fullwidth_to_halfwidth', True):
        out = re.sub(r'[\uff01-\uff5e]', lambda m: chr(ord(m.group()) - 0xfee0), out)
    if n.get('casefold', True):
        out = out.lower()
    out = re.sub(r'[\s\W_]+', '', out)
    return out


def cer(ref: str, hyp: str) -> float:
    """字符错误率（Levenshtein）。"""
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, a in enumerate(ref, 1):
        cur = [i]
        for j, b in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a != b)))
        prev = cur
    return prev[len(hyp)] / max(len(ref), 1)


def check_binary(path: str, version_args: list[str]) -> dict:
    p = Path(path)
    if not p.exists():
        return {'ok': False, 'path': path, 'version_args': version_args,
                'reason': 'not_found'}
    try:
        r = subprocess.run([str(p), *version_args], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=20)
        output = (r.stdout or r.stderr or '').splitlines()
        result = {'ok': r.returncode == 0, 'path': path,
                  'version_args': version_args,
                  'version': output[0][:200] if output else None}
        if r.returncode != 0:
            result['reason'] = f'version_check_exit_{r.returncode}'
        return result
    except Exception as e:
        return {'ok': False, 'path': path, 'version_args': version_args,
                'reason': f'{type(e).__name__}: {e}'}


def check_models(cfg: dict) -> dict:
    """模型完整性：whisper 系必须齐 required_file；SenseVoice 走 funasr 自动下载。"""
    out = {}
    p = cfg['asr'].get('primary')
    if p:
        runtime = cfg['runtime'].get(p.get('runtime'), '')
        runtime_ok = bool(runtime) and Path(runtime).exists()
        out[p['model']] = {'engine': 'sensevoice',
                           'ok': p.get('status') == 'verified' and runtime_ok,
                           'baseline_status': p.get('status'),
                           'runtime_ok': runtime_ok,
                           'runtime': p.get('runtime'), 'device': p.get('device'),
                           'measured_avg_cer': p.get('measured_avg_cer'),
                           'measured_rtf': p.get('measured_rtf_gpu'),
                           'note': 'CER 来自配置基线；本次未执行模型推理' if runtime_ok else 'ASR runtime 不可用'}
    for c in cfg['asr'].get('fallback', []):
        d = Path(c['model_dir'])
        req = d / c['required_file']
        info = {'engine': c['engine'], 'dir': c['model_dir'], 'declared_status': c.get('status'),
                'measured_avg_cer': c.get('measured_avg_cer')}
        if not d.exists():
            info.update(ok=False, reason='dir_missing')
        elif not req.exists():
            partial = list(d.glob('*.part')) + list(d.glob('*.incomplete')) + list(d.glob('quality-download-parts'))
            info.update(ok=False, reason='required_file_missing',
                        partial_artifacts=[x.name for x in partial][:5],
                        hint='下载未完成；需换镜像源重下，不可将不完整下载视为就绪')
        else:
            info.update(ok=True, size_mb=round(req.stat().st_size / 1024 / 1024, 1))
        out[c['model']] = info
    return out


def measure_asr(cfg: dict, sample_wavs: list) -> dict:
    """用带标注的真实音频实测 CER。样本缺失时返回 skipped 而非编造。"""
    if not sample_wavs:
        # 复用配置里已实测入库的基线，不重复编造
        p = cfg['asr'].get('primary', {})
        base = p.get('measured_avg_cer')
        return {
            'skipped': True,
            'reason': 'no_labelled_samples',
            'baseline_from_config': base,
            'hint': '提供 <name>.wav + <name>.txt 样本对可现场复测；否则采用配置中已实测入库的基线',
        }
    sys.path.insert(0, str(Path(__file__).parent))
    from asr import transcribe, normalize, cer

    cers, rtf = [], []
    t0 = time.monotonic()
    for wav, ref in sample_wavs:
        r = transcribe(str(wav), cfg)
        cers.append(cer(normalize(ref, cfg), normalize(r.get('text', ''), cfg)))
        if r.get('infer_sec'):
            rtf.append(r['infer_sec'] / 3.0)
    gate = cfg['asr']['quality_gate']
    avg = sum(cers) / len(cers)
    return {
        'samples': len(cers), 'avg_cer': round(avg, 4),
        'worst_cer': round(max(cers), 4), 'per_sample': [round(c, 4) for c in cers],
        'avg_rtf': round(sum(rtf) / len(rtf), 4) if rtf else None,
        'wall_sec': round(time.monotonic() - t0, 1),
        'verdict': 'usable' if avg <= gate['hard_floor_avg_cer'] else
                   ('marginal' if avg <= gate['max_avg_cer_for_production'] else 'unusable'),
    }


def load_samples(dirpath: str) -> list:
    d = Path(dirpath)
    if not d.exists():
        return []
    return [(t.with_suffix('.wav'), t.read_text(encoding='utf-8').strip())
            for t in sorted(d.glob('*.txt')) if t.with_suffix('.wav').exists()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='检查本次任务所需的本地运行能力')
    ap.add_argument('samples', nargs='?', help='可选：标注样本目录（每个 .txt 配对同名 .wav）')
    ap.add_argument('--out', default=str(ROOT / 'references' / 'preflight-report.json'),
                    help='报告输出路径；编排任务应传入任务目录内的路径')
    args = ap.parse_args(argv)

    cfg = json.loads(CFG.read_text(encoding='utf-8'))
    samples = load_samples(args.samples) if args.samples else []

    report = {'engine_version': cfg['engine_version'],
              'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
              'checks': {}, 'decision': {}}

    report['checks']['binaries'] = {
        k: check_binary(v, VERSION_ARGS[k]) for k, v in cfg['runtime'].items()
        if k in VERSION_ARGS
    }
    report['checks']['models'] = check_models(cfg)
    report['checks']['asr_measure'] = measure_asr(cfg, samples)
    report['checks']['assets'] = {
        k: {'path': v, 'ok': Path(v).exists()}
        for k, v in {
            'tts_model': cfg['tts']['model'],
            'tts_reference': cfg['tts']['default_reference'],
            'tts_dub_script': cfg['runtime']['tts_dub_script'],
            'vlm_model': cfg['runtime']['vlm_model'],
            'mmproj': cfg['runtime']['mmproj'],
        }.items()
    }

    # 决策
    m = report['checks']['asr_measure']
    gate = cfg['asr']['quality_gate']
    models = report['checks']['models']
    # Full pipeline 依赖这些运行时。fallback ASR 的解释器不是必需项，
    # 因为主 ASR 与配置中的 primary.runtime 使用同一个解释器。
    required_binaries = ('ffmpeg', 'ffprobe', 'tts_python', 'llama_server')
    missing_binaries = [k for k in required_binaries
                        if not report['checks']['binaries'].get(k, {}).get('ok')]
    missing_assets = [k for k, v in report['checks']['assets'].items() if not v['ok']]
    ready_models = [k for k, v in models.items() if v.get('ok')]
    usable = [k for k, v in models.items() if v.get('ok') and v.get('measured_avg_cer') is not None
              and v['measured_avg_cer'] <= gate['max_avg_cer_for_production']]
    strong = [k for k, v in models.items() if v.get('ok') and v.get('measured_avg_cer') is not None
              and v['measured_avg_cer'] <= gate['hard_floor_avg_cer']]

    report['decision'] = {
        'ready_asr_models': ready_models,
        'usable_asr_models': usable,
        'strong_asr_models': strong,
        'speech_gate_available': bool(strong),
        'verdict': 'ready' if strong and not missing_binaries and not missing_assets else
                   ('degraded' if usable and not missing_binaries and not missing_assets else 'refuse'),
        'production_threshold': None,
        'reasons': [],
        'required_binaries': list(required_binaries),
        'missing_binaries': missing_binaries,
        'missing_assets': missing_assets,
        'asr_baseline_is_live_measurement': not report['checks']['asr_measure'].get('skipped', False),
    }

    r = report['decision']['reasons']
    if missing_binaries:
        r.append(f'缺少本流程必需运行时: {missing_binaries}')
    if missing_assets:
        r.append(f'缺少本流程必需模型或辅助文件: {missing_assets}')

    if strong:
        best = min(strong, key=lambda k: models[k]['measured_avg_cer'])
        base = models[best]['measured_avg_cer']
        report['decision']['production_threshold'] = round(base + gate['slack'], 4)
        report['decision']['selected_asr'] = best
        # 现场复测优先于配置基线
        if not m.get('skipped') and m.get('verdict') in ('usable', 'marginal'):
            report['decision']['production_threshold'] = round(m['avg_cer'] + gate['slack'], 4)
            report['decision']['measured_today'] = True
    else:
        if not ready_models:
            r.append('无任何完整可用 ASR 模型')
        elif not usable:
            worst = {k: v.get('measured_avg_cer') for k, v in models.items() if v.get('ok')}
            r.append(f'ASR 实测 CER 超限（{gate["max_avg_cer_for_production"]}）：{worst}')
        else:
            r.append(f'ASR 仅 marginal，硬门 speech 降级为软门（记录不阻断）')
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    print(json.dumps(report['decision'], ensure_ascii=False, indent=2))
    print(f'[out] {out}')
    return 0 if report['decision']['verdict'] == 'ready' else (2 if report['decision']['verdict'] == 'degraded' else 2)


if __name__ == '__main__':
    sys.exit(main())
