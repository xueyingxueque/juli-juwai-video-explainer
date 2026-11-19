"""ASR 统一调用层 v2.0 —— 多引擎适配，主入口。

实测结论（2026-10-08，真实短剧素材抽样）：
  SenseVoice-small (funasr) 已接入主引擎；量化基线见 config/engine.json 与本次预检报告。
  whisper-tiny 中文质量不足以作为生产主引擎；不同样本的 CER 记录不一致，不在此处固化单一数值。

统一接口：transcribe(audio_path) -> {text, engine, rtf, segments}
上层（preflight / quality_gate / pipeline）只认这个接口，不关心底层引擎。
"""
import json
import math
import re
import subprocess
import time
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_TAG = re.compile(r'<\|[^|]*\|>')
# 繁 -> 简（whisper 系输出繁体；SenseVoice 简体，但统一处理更安全）
_T2S = str.maketrans({
    '財': '财', '調': '调', '寫': '写', '電': '电', '動': '动', '給': '给', '們': '们',
    '個': '个', '沒': '没', '現': '现', '謝': '谢', '來': '来', '說': '说', '對': '对',
    '時': '时', '國': '国', '學': '学', '這': '这', '實': '实', '發': '发', '種': '种',
    '樣': '样', '點': '点', '會': '会', '經': '经', '過': '过', '還': '还', '讓': '让',
    '後': '后', '從': '从', '頭': '头', '兒': '儿', '願': '愿', '傷': '伤', '轉': '转',
    '為': '为', '務': '务', '聲': '声', '聽': '听', '變': '变', '邊': '边', '遠': '远',
    '銀': '银', '長': '长', '車': '车', '馬': '马', '鳥': '鸟', '魚': '鱼', '龍': '龙',
    '飛': '飞', '劇': '剧', '場': '场', '學': '学', '師': '师', '級': '级', '線': '线',
})


def load_cfg() -> dict:
    return json.loads((ROOT / 'config' / 'engine.json').read_text(encoding='utf-8'))


def normalize(s: str, cfg: dict) -> str:
    """中文字符比对的统一口径。preflight 与 quality_gate 共用，避免两套标准。"""
    n = cfg['asr']['normalization']
    out = s or ''
    if n.get('strip_model_tags', True):
        out = _TAG.sub('', out)
    if n.get('traditional_to_simplified', True):
        out = out.translate(_T2S)
    if n.get('fullwidth_to_halfwidth', True):
        out = re.sub(r'[\uff01-\uff5e]', lambda m: chr(ord(m.group()) - 0xfee0), out)
    if n.get('casefold', True):
        out = out.lower()
    return re.sub(r'[\s\W_]+', '', out)


def cer(ref: str, hyp: str) -> float:
    """字符错误率（Levenshtein）。空参考按 0/1 处理。"""
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, a in enumerate(ref, 1):
        cur = [i]
        for j, b in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a != b)))
        prev = cur
    return prev[len(hyp)] / max(len(ref), 1)


# ---------- SenseVoice（funasr）----------

_SV_MODEL = None


def _sensevoice(audio: str, cfg: dict) -> dict:
    global _SV_MODEL
    p = cfg['asr']['primary']
    py = cfg['runtime']['tts_python']   # funasr 在 Dots 的 python 里
    if _SV_MODEL is None:
        _SV_MODEL = p['model']
    script = f'''
import json, time
from funasr import AutoModel
t0=time.time()
m = AutoModel(model={_SV_MODEL!r}, device={p["device"]!r}, disable_update=True)
t1=time.time()
r = m.generate(input={audio!r}, cache={{}}, language="auto", use_itn=True,
               batch_size_s=60, merge_vad=True, merge_length_s=15)
dt=time.time()-t1
txt = r[0]["text"] if r else ""
print(json.dumps({{"text":txt,"infer_sec":dt,"load_sec":t1-t0}}, ensure_ascii=False))
'''
    r = subprocess.run([py, '-X', 'utf8', '-c', script], capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=3600)
    line = [x for x in r.stdout.strip().splitlines() if x.startswith('{')]
    if not line:
        return {'text': '', 'engine': 'sensevoice', 'error': (r.stderr or r.stdout)[-300:]}
    d = json.loads(line[-1])
    return {'text': d['text'], 'engine': 'sensevoice', 'model': p['model'],
            'infer_sec': d['infer_sec'], 'device': p['device']}


# ---------- Whisper（faster-whisper）----------

def _whisper(audio: str, cfg: dict, spec: dict) -> dict:
    py = cfg['runtime']['asr_python']
    script = r'''
import json, sys, time
from faster_whisper import WhisperModel
t0=time.time()
m = WhisperModel(sys.argv[2], device=sys.argv[3], compute_type=sys.argv[4])
t1=time.time()
segs, info = m.transcribe(sys.argv[1], language="zh", word_timestamps=True,
                          beam_size=int(sys.argv[5]), vad_filter=True)
segs=list(segs)
txt="".join(s.text for s in segs).strip()
rows=[]
for i,s in enumerate(segs,1):
    words=[]
    for w in (s.words or []):
        words.append({'text':(w.word or '').strip(),'start':round(float(w.start),3),
                      'end':round(float(w.end),3)})
    rows.append({'segment_id':f'A{i:04d}','start':round(float(s.start),3),
                 'end':round(float(s.end),3),'text':(s.text or '').strip(),
                 'avg_logprob':float(s.avg_logprob) if s.avg_logprob is not None else None,
                 'no_speech_prob':float(s.no_speech_prob) if s.no_speech_prob is not None else None,
                 'words':words})
print(json.dumps({'text':txt,'segments':rows,'duration':float(info.duration or 0),
                  'infer_sec':time.time()-t1,'load_sec':t1-t0},ensure_ascii=False))
'''
    r = subprocess.run([py, '-X', 'utf8', '-c', script, audio, spec['model_dir'],
                        spec.get('device', 'cpu'), cfg['asr'].get('compute_type', 'int8'),
                        str(cfg['asr'].get('beam_size', 5))], capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=3600)
    line = [x for x in r.stdout.strip().splitlines() if x.startswith('{')]
    if not line:
        return {'text': '', 'engine': 'whisper', 'error': (r.stderr or r.stdout)[-300:]}
    d = json.loads(line[-1])
    return {'text': d['text'], 'segments': d.get('segments') or [],
            'engine': 'whisper', 'model': spec['model'], 'infer_sec': d['infer_sec'],
            'load_sec': d.get('load_sec'), 'device': spec.get('device', 'cpu')}


def _cjk_chars(text: str) -> int:
    return len(re.findall(r'[\u4e00-\u9fff]', text or ''))


def _segment_confidence(segments: list, cfg: dict) -> tuple[list[dict], dict]:
    rules = cfg['asr'].get('narrative_transcript', {})
    min_logprob = float(rules.get('min_segment_avg_logprob', -1.2))
    max_no_speech = float(rules.get('max_segment_no_speech_prob', 0.6))
    eligible = []
    logs, no_speech = [], []
    for row in segments:
        if not isinstance(row, dict):
            continue
        lp, nsp = row.get('avg_logprob'), row.get('no_speech_prob')
        if isinstance(lp, (int, float)) and not isinstance(lp, bool):
            logs.append(float(lp))
        if isinstance(nsp, (int, float)) and not isinstance(nsp, bool):
            no_speech.append(float(nsp))
        ok = ((lp is None or float(lp) >= min_logprob)
              and (nsp is None or float(nsp) <= max_no_speech)
              and bool(str(row.get('text') or '').strip()))
        row['eligible_for_narrative'] = ok
        if ok:
            eligible.append(row)
    logs.sort()
    no_speech.sort()
    summary = {
        'segment_count': len(segments),
        'eligible_segment_count': len(eligible),
        'eligible_cjk_chars': _cjk_chars(''.join(str(x.get('text') or '') for x in eligible)),
        'median_avg_logprob': logs[len(logs) // 2] if logs else None,
        'median_no_speech_prob': no_speech[len(no_speech) // 2] if no_speech else None,
    }
    return eligible, summary


def transcribe_for_story(audio: str, cfg: dict | None = None,
                         duration_sec: float | None = None) -> dict:
    """Select a complete narrative transcript and retain timed speech evidence.

    SenseVoice remains the fast primary. A verified timestamped Whisper pass is
    compared per source so a non-empty but severely sparse primary result does
    not silently become the story's only dialogue evidence.
    """
    cfg = cfg or load_cfg()
    primary = transcribe(audio, cfg)
    duration = float(duration_sec or 0)
    if not duration:
        duration = float(primary.get('duration_sec') or 0)
    primary_text = str(primary.get('text') or '')
    primary_cjk = _cjk_chars(primary_text)
    rules = cfg['asr'].get('narrative_transcript', {})
    min_cjk_rate = float(rules.get('min_primary_cjk_per_second', 0.75))
    min_timed_chars = int(rules.get('min_timed_cjk_chars', 32))
    min_timed_segments = int(rules.get('min_timed_segments', 3))
    min_ratio = float(rules.get('richer_transcript_ratio', 1.8))
    min_agreement = float(rules.get('min_agreement_for_timing_attachment', 0.65))

    timed = primary if primary.get('engine') == 'whisper' and primary.get('segments') else None
    timed_error = None
    if timed is None:
        eligible_models = {x['model'] for x in available(cfg) if x.get('engine') == 'whisper'}
        spec = next((x for x in cfg['asr'].get('fallback', [])
                     if x.get('engine') == 'whisper' and x.get('model') in eligible_models), None)
        if spec:
            try:
                timed = _whisper(audio, cfg, spec)
            except Exception as exc:
                timed_error = type(exc).__name__
                timed = None

    segments = []
    timed_summary = {'segment_count': 0, 'eligible_segment_count': 0,
                     'eligible_cjk_chars': 0, 'median_avg_logprob': None,
                     'median_no_speech_prob': None}
    agreement = None
    timed_cjk = 0
    use_timed_text = False
    if timed and timed.get('text'):
        segments = timed.get('segments') or []
        eligible, timed_summary = _segment_confidence(segments, cfg)
        timed_text = str(timed.get('text') or '')
        timed_cjk = _cjk_chars(timed_text)
        usable_timing = (timed_summary['eligible_segment_count'] >= min_timed_segments
                         and timed_summary['eligible_cjk_chars'] >= min_timed_chars)
        norm_primary = normalize(primary_text, cfg)
        norm_timed = normalize(timed_text, cfg)
        if norm_primary and norm_timed:
            agreement = SequenceMatcher(None, norm_primary, norm_timed, autojunk=False).ratio()
        sparse_primary = bool(duration and primary_cjk / duration < min_cjk_rate)
        substantially_richer = timed_cjk >= max(min_timed_chars,
                                                  math.ceil(max(primary_cjk, 1) * min_ratio))
        conflict = agreement is not None and agreement < min_agreement
        use_timed_text = bool(usable_timing and (sparse_primary or substantially_richer or conflict))
        if use_timed_text:
            selected = timed
            selected_text = timed_text
            reason = ('primary_sparse_and_timed_candidate_usable' if sparse_primary
                      else 'timed_candidate_richer_than_primary' if substantially_richer
                      else 'primary_whisper_disagreement_timed_candidate_usable')
        else:
            selected = primary
            selected_text = primary_text
            reason = 'primary_retained_after_timed_crosscheck'
            if agreement is not None and agreement >= min_agreement and usable_timing:
                segments = eligible
            else:
                segments = []
    else:
        selected = primary
        selected_text = primary_text
        reason = 'timed_whisper_unavailable_or_unusable'
        segments = []

    if not selected_text and timed and timed.get('text'):
        selected = timed
        selected_text = str(timed.get('text') or '')
        segments = timed.get('segments') or []
        reason = 'primary_empty_timed_candidate_selected'

    primary_summary = {
        'engine': primary.get('engine'), 'text_chars': len(primary_text),
        'cjk_chars': primary_cjk,
        'cjk_chars_per_second': round(primary_cjk / duration, 3) if duration else None,
    }
    quality = {
        'status': 'timed_transcript_selected' if selected.get('engine') == 'whisper' and segments
                  else 'primary_text_selected',
        'selection_reason': reason,
        'agreement_ratio': round(agreement, 3) if agreement is not None else None,
        'primary': primary_summary,
        'timed_candidate': {**timed_summary, 'cjk_chars': timed_cjk,
                            'cjk_chars_per_second': round(timed_cjk / duration, 3) if duration else None},
        'timed_whisper_error_type': timed_error,
        'attached_segment_count': len(segments),
    }
    return {
        'text': selected_text, 'segments': segments,
        'engine': selected.get('engine', 'none'), 'model': selected.get('model'),
        'infer_sec': selected.get('infer_sec'), 'device': selected.get('device'),
        'quality': quality,
    }


def available(cfg: dict) -> list:
    """返回当前真正可用的引擎列表（按优先级）。"""
    out = []
    p = cfg['asr'].get('primary')
    if p and p.get('status') == 'verified':
        out.append({'engine': 'sensevoice', 'model': p['model'], 'tier': 'primary',
                    'measured_avg_cer': p.get('measured_avg_cer', 0.03),
                    'measured_rtf': p.get('measured_rtf_gpu')})
    for spec in cfg['asr'].get('fallback', []):
        if spec['engine'] != 'whisper':
            continue
        cer_value = spec.get('measured_avg_cer')
        max_cer = cfg['asr'].get('quality_gate', {}).get('max_avg_cer_for_production', 0.35)
        if (spec.get('status') == 'verified'
                and isinstance(cer_value, (int, float)) and not isinstance(cer_value, bool)
                and cer_value <= max_cer
                and Path(spec['model_dir'], spec['required_file']).is_file()):
            out.append({'engine': 'whisper', 'model': spec['model'], 'tier': 'fallback',
                        'measured_avg_cer': cer_value,
                        'measured_rtf': spec.get('measured_rtf_cpu'),
                        'note': spec.get('verdict', '')})
    return out


def transcribe(audio: str, cfg: dict | None = None) -> dict:
    """统一入口：按优先级选引擎，返回 {text, engine, infer_sec}。"""
    cfg = cfg or load_cfg()
    p = cfg['asr'].get('primary')
    if p and p.get('status') == 'verified':
        try:
            r = _sensevoice(audio, cfg)
            if r.get('text'):
                return r
        except Exception as e:
            print(f'[asr] SenseVoice 失败，回退 whisper: {e}', flush=True)
    eligible = {item['model'] for item in available(cfg) if item['engine'] == 'whisper'}
    for spec in cfg['asr'].get('fallback', []):
        if spec.get('model') in eligible:
            try:
                r = _whisper(audio, cfg, spec)
                if r.get('text'):
                    return r
            except Exception:
                continue
    return {'text': '', 'engine': 'none', 'error': '无可用 ASR 引擎'}


def timed_lines(audio: str, cfg: dict | None = None) -> list:
    """Return simplified [{s, e, t}] lines from a verified timed Whisper pass.

    ★ 为什么抽成函数（2026-10-21 源头优化）：
      build.py 原本自己 subprocess 起 faster-whisper（第二份实现），
      两处都要单独维护 word_timestamps / compute_type / beam_size，
      改一处漏一处 —— 上一轮正是 asr.py 侧已传 wt、build.py 侧没传，
      导致 medium 整段幻觉（时间点错位十几秒）。
    → 唯一实现在 asr.py，build.py 只调这个函数。
    ★ 空结果不抛异常，由调用方决定降级路径（build.py 会退回 plan 证据时间点）。
    """
    cfg = cfg or load_cfg()
    spec = None
    quality_limit = float(cfg.get('asr', {}).get('quality_gate', {}).get(
        'max_avg_cer_for_production', 0.35))
    for cand in cfg.get('asr', {}).get('fallback', []):
        if cand.get('engine') != 'whisper' or cand.get('status') != 'verified':
            continue
        cer = cand.get('measured_avg_cer')
        if not isinstance(cer, (int, float)) or isinstance(cer, bool) or cer > quality_limit:
            continue
        model_dir = cand.get('model_dir')
        if not model_dir or not Path(model_dir, cand.get('required_file', 'model.bin')).is_file():
            continue
        spec = cand
        break
    if spec is None:
        return []
    py = cfg['runtime'].get(spec.get('runtime', 'asr_python'))
    if not py:
        return []
    # ★ 必须传 word_timestamps（2026-10-16 实测）：同一模型同一音频，不传时
    #   medium 会整段幻觉（32.55s 起「我心里不安」每 1.00s 重复、19.37s 的「我 May」），
    #   且时间点错位十几秒（「我叫梁彦舟」被定到 13.46s，真实 29.85s）。
    wt = bool(cfg.get('asr', {}).get('word_timestamps', True))
    code = (
        "import json,sys\n"
        "from faster_whisper import WhisperModel\n"
        f"m=WhisperModel({spec['model_dir']!r},"
        f"device={spec.get('device', 'cpu')!r},"
        f"compute_type={cfg.get('asr', {}).get('compute_type', 'int8')!r})\n"
        "segs,_=m.transcribe(sys.argv[1],language='zh',vad_filter=True,"
        f"beam_size={cfg.get('asr', {}).get('beam_size', 5)!r},"
        f"word_timestamps={wt!r})\n"
        "print('@@'+json.dumps([{'s':round(x.start,2),'e':round(x.end,2),"
        "'t':x.text} for x in segs],ensure_ascii=False))\n"
    )
    r = subprocess.run([py, '-X', 'utf8', '-c', code, audio],
                       capture_output=True, text=True, encoding='utf-8', errors='replace',
                       timeout=2400)
    for line in (r.stdout or '').splitlines():
        if line.startswith('@@'):
            try:
                return json.loads(line[2:])
            except Exception:
                return []
    return []


def transcribe_for_narration_qc(audio: str, cfg: dict | None = None) -> dict:
    """Use the dedicated Chinese ASR profile for synthesized narration readback.

    Raw-drama ASR baselines and engine priority are not valid proxies for TTS
    speech. This path deliberately does not fall back to the raw-audio primary.
    """
    cfg = cfg or load_cfg()
    spec = cfg.get('asr', {}).get('narration_qc') or {}
    if not spec.get('enabled_for_scoring') or spec.get('engine') != 'whisper':
        return {'text': '', 'engine': 'none', 'error': 'narration_qc_profile_unavailable'}
    model_dir = spec.get('model_dir')
    required = spec.get('required_file', 'model.bin')
    if not model_dir or not Path(model_dir, required).is_file():
        return {'text': '', 'engine': 'none', 'error': 'narration_qc_model_missing'}
    effective_cfg = dict(cfg)
    effective_cfg['asr'] = dict(cfg.get('asr', {}))
    effective_cfg['asr']['compute_type'] = spec.get(
        'compute_type', effective_cfg['asr'].get('compute_type', 'int8'))
    effective_cfg['asr']['beam_size'] = int(spec.get(
        'beam_size', effective_cfg['asr'].get('beam_size', 5)))
    result = _whisper(audio, effective_cfg, spec)
    result['purpose'] = 'tts_narration_qc'
    result['calibration_status'] = spec.get('status', 'unconfigured')
    return result
