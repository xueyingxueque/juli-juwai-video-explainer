"""质量门 v2.0 —— 硬门/软门两档制。

v1.1 的致命设计：五道门全部零容忍（max_cer=0.0），好稿被模型能力不足判死。
v2.0 的做法：
  硬门（HARD）—— 客观可测、与模型能力无关。技术层、时长、音轨、字幕边界、版权连续片段。
  软门（SOFT）—— 依赖 ASR/VLM 判断力。记录 + 扣分，不单独阻断。
  交付分档    —— 由软门得分决定 A/B/C 档，而不是"过/不过"二元。

这样即使 ASR 很弱，技术层依然能保证成片可交付，同时如实报告语义风险。
"""
import json
import math
import os
import re
import subprocess
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / 'scripts'))
from policy import MIN_SEC, MAX_SEC, check_measured  # noqa: E402  时长规范唯一真相源


def hard_technical(probe: str, cfg: dict, expect_sec: float | None,
                   strict_duration: bool = False) -> dict:
    """L1 技术层：ffprobe 客观测量。"""
    out = cfg['output']
    prof = cfg['platform_profiles'][cfg.get('active_profile', 'generic')]
    fails, warns = [], []

    try:
        r = subprocess.run([probe, '-v', 'error', '-show_streams', '-show_format',
                            '-of', 'json', str(cfg['_video'])], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=120)
    except Exception as e:
        return {'gate': 'hard/technical', 'passed': False,
                'fails': [f'ffprobe_exception:{type(e).__name__}:{e}'], 'warns': []}
    if r.returncode != 0:
        return {'gate': 'hard/technical', 'passed': False, 'fails': ['ffprobe_failed'], 'warns': []}
    info = json.loads(r.stdout)
    v = next((s for s in info['streams'] if s['codec_type'] == 'video'), None)
    a = next((s for s in info['streams'] if s['codec_type'] == 'audio'), None)
    dur = float(info['format'].get('duration', 0))
    audio_dur = float(a.get('duration', 0)) if a and a.get('duration') else None
    v_width = int(v.get('width', 0)) if v else 0
    v_height = int(v.get('height', 0)) if v else 0
    container = str(info.get('format', {}).get('format_name', ''))

    if not v:
        fails.append('no_video_stream')
    else:
        if v_height < prof['min_height']:
            fails.append(f'resolution_too_low:{v_width}x{v_height}')
        expected_aspect = prof.get('aspect')
        if expected_aspect and v_width and v_height:
            try:
                ar_w, ar_h = (float(x) for x in expected_aspect.split(':', 1))
                expected_ratio = ar_w / ar_h
                actual_ratio = v_width / v_height
                if abs(actual_ratio - expected_ratio) / expected_ratio > 0.015:
                    fails.append(f'aspect_ratio_mismatch:{v_width}x{v_height} not {expected_aspect}')
            except (TypeError, ValueError, ZeroDivisionError):
                fails.append(f'invalid_profile_aspect:{expected_aspect}')
        if v.get('avg_frame_rate'):
            # ★ v3.6.0 修：非数值帧率（如 '0/0'）原来 ValueError 逃出 gate 层，
            #   整份质检报告丢失；现降级为 warns
            try:
                num, _, den = v['avg_frame_rate'].partition('/')
                fps = int(num) / int(den) if den and int(den) else 0
                if fps and abs(fps - out['fps']) > 1:
                    warns.append(f'fps_drift:{fps:.2f}')
            except (TypeError, ValueError, ZeroDivisionError):
                warns.append(f'fps_unparsable:{v["avg_frame_rate"]}')

    if not a:
        fails.append('no_audio_track')
    elif audio_dur is not None and abs(audio_dur - dur) > 0.5:
        fails.append(f'audio_video_duration_mismatch:{audio_dur:.2f}s vs {dur:.2f}s')
    if strict_duration and expect_sec and abs(dur - expect_sec) > max(1.0, expect_sec * 0.10):
        fails.append(f'duration_mismatch: {dur:.2f}s vs expect {expect_sec:.2f}s')
    # ★ 成品时长硬门（用户 2026-11-04 令：1-5 分钟）。数值正典 =
    #   engine.json duration_policy.bounds，由 policy.py 断言与 profile 一致；
    #   超限即 REJECT（warn 会被忽略，必须硬拦）。
    # ★ 2026-11-09 修（审计发现）：旧版用 <= 闭区间，边界 60/300 会放行，
    #   与 duration_policy.strict（严格开区间）和 deliver_check 的口径冲突。
    #   → 统一走 policy.check_measured，判定只存在一处。
    if dur:
        _dur_ok, _dur_msg = check_measured(dur)
        if not _dur_ok:
            fails.append(f'duration_out_of_{MIN_SEC:.0f}_{MAX_SEC:.0f}:{dur:.1f}s'
                         f'（{_dur_msg}，见 SKILL.md §3.1）')
    if not dur:
        # ★ v3.6.0 修：旧判据 `if dur` 在 total=0 时静默跳过全部时长门
        fails.append('duration_zero_or_unreadable')

    file_size = 0
    try:
        file_size = Path(cfg['_video']).stat().st_size
    except OSError:
        fails.append('video_file_unreadable')
    if cfg.get('active_profile') == 'douyin':
        suffix = Path(cfg['_video']).suffix.lower()
        if suffix not in ('.mp4', '.webm'):
            fails.append(f'douyin_unsupported_extension:{suffix or "(none)"}')
        if dur > MAX_SEC:
            fails.append(f'duration_over_max:{dur:.1f}s')
        if file_size > 128 * 1024 * 1024:
            fails.append(f'douyin_file_over_128mb:{file_size}')

    return {'gate': 'hard/technical', 'passed': not fails, 'fails': fails, 'warns': warns,
            'measured': {'duration': round(dur, 2), 'audio_duration': round(audio_dur, 2)
                         if audio_dur is not None else None,
                         'width': v_width, 'height': v_height,
                         'aspect_ratio': round(v_width / v_height, 5) if v_height else None,
                         'container': container, 'file_size_bytes': file_size}}


def hard_subtitle(srt: str, cfg: dict) -> dict:
    """L2 字幕硬门：校验 cue 时间、平台单行字数和每 cue 最大行数。"""
    prof = cfg['platform_profiles'][cfg.get('active_profile', 'generic')]
    p = Path(srt) if srt else None
    if not p or not p.is_file():
        return {'gate': 'hard/subtitle', 'passed': False, 'fails': ['srt_missing'], 'warns': []}
    txt = p.read_text(encoding='utf-8', errors='replace')
    fails, warns = [], []

    style_path = p.with_name('subs.style.json')
    if not style_path.is_file():
        fails.append('subtitle_style_manifest_missing')
    else:
        try:
            style = json.loads(style_path.read_text(encoding='utf-8'))
            playres = style.get('playres') or {}
            caption_box = style.get('caption_box') or {}
            safe_box = style.get('safe_box') or {}
            canvas_h = int(playres.get('height', 0))
            if not canvas_h:
                fails.append('subtitle_style_canvas_missing')
            else:
                safe_top_px = math.ceil(canvas_h * float(prof.get('safe_top', 0)))
                safe_bottom_px = math.floor(canvas_h * (1.0 - float(prof.get('safe_bottom', 0))))
                margin_v = int(style.get('margin_v', -1))
                font_size = int(style.get('font_size', 0))
                line_count = int(style.get('max_lines', 0))
                expected_bottom = canvas_h - margin_v
                expected_top = expected_bottom - math.ceil(font_size * 1.25) * line_count
                if int(safe_box.get('top_px', -1)) != safe_top_px or int(safe_box.get('bottom_px', -1)) != safe_bottom_px:
                    fails.append('subtitle_style_safe_box_mismatch')
                if int(caption_box.get('bottom_px', -1)) != expected_bottom or int(caption_box.get('top_px', -1)) != expected_top:
                    fails.append('subtitle_style_box_geometry_mismatch')
                if int(caption_box.get('top_px', -1)) < safe_top_px:
                    fails.append('subtitle_above_or_outside_top_safe_area')
                if int(caption_box.get('bottom_px', canvas_h + 1)) > safe_bottom_px:
                    fails.append('subtitle_outside_bottom_safe_area')
            if style.get('platform') != cfg.get('active_profile', 'generic'):
                fails.append('subtitle_style_platform_mismatch')
            if int(style.get('max_lines', 0)) != int(prof['subtitle_max_lines']):
                fails.append('subtitle_style_line_limit_mismatch')
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
            fails.append(f'subtitle_style_manifest_invalid:{type(e).__name__}')

    time_lines = [line.strip() for line in txt.splitlines() if '-->' in line]
    if not time_lines:
        fails.append('no_valid_subtitle_cues')
    prev_end = -1
    pattern = re.compile(r'^(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})$')
    for line in time_lines:
        match = pattern.fullmatch(line)
        if not match:
            fails.append(f'invalid_timestamp_line:{line[:80]}')
            continue
        t = match.groups()
        s = int(t[0]) * 3600 + int(t[1]) * 60 + int(t[2]) + int(t[3]) / 1000
        e = int(t[4]) * 3600 + int(t[5]) * 60 + int(t[6]) + int(t[7]) / 1000
        if e <= s:
            fails.append(f'non_increasing_timestamp:{t}')
        if s < prev_end:
            warns.append('overlap')
        prev_end = e

    max_lines = int(prof['subtitle_max_lines'])
    max_chars = int(prof['chars_per_line'])
    for cue_no, block in enumerate(re.split(r'\n\s*\n', txt.strip()), 1):
        cue_lines = [line.strip() for line in block.splitlines() if line.strip()]
        text_lines = [line for line in cue_lines if not line.isdigit() and '-->' not in line]
        if not text_lines:
            continue
        if len(text_lines) > max_lines:
            fails.append(f'cue_too_many_lines:{cue_no}:{len(text_lines)}>{max_lines}')
        for line_no, line in enumerate(text_lines, 1):
            if len(line) > max_chars:
                fails.append(f'line_too_long:{cue_no}:{line_no}:{len(line)}>{max_chars}')
    return {'gate': 'hard/subtitle', 'passed': not fails, 'fails': fails, 'warns': warns}


def hard_copyright(shotmap: str, cfg: dict) -> dict:
    """L3 版权硬门：原片连续片段时长上限 + 解说占比。

    ★ v3.7.1：冷开场钩子（is_cold_open）连续原画面上限放宽到
      compliance.cold_open.max_sec（默认 5.0s，普通段仍是 2.5s），且强制
      全片唯一、位于第 1 段——既给「前5秒放高潮原片」留出合规空间，
      又杜绝滥用（普通原声段仍严格 ≤2.5s）。
    """
    p = Path(shotmap) if shotmap else None
    if not p or not p.is_file():
        return {'gate': 'hard/copyright', 'passed': False, 'fails': ['shotmap_missing'], 'warns': []}
    sm = json.loads(p.read_text(encoding='utf-8'))
    comp = cfg['compliance']
    limit = comp['max_continuous_original_sec']
    co = comp.get('cold_open') or {}
    co_enabled = bool(co.get('enabled', False))
    co_max = float(co.get('max_sec', 5.0)) if co_enabled else limit
    fails, warns = [], []
    segments = sm.get('segments', [])
    if not segments:
        return {'gate': 'hard/copyright', 'passed': False,
                'fails': ['shotmap_has_no_segments'], 'warns': []}

    def amount(s, key, fallback=0.0):
        value = s.get(key, fallback)
        try:
            out = float(value)
            return out if math.isfinite(out) and out >= 0 else None
        except (TypeError, ValueError):
            return None

    measured_map = any('commentary_sec' in s or 'original_footage_sec' in s
                       or 'original_audio_sec' in s for s in segments)
    orig = orig_audio = commentary = total = 0.0
    cold_seen = 0
    for s in segments:
        seg_id = s.get('seg_id')
        is_cold = bool(s.get('is_cold_open', False))
        if is_cold:
            cold_seen += 1
            if co_enabled and co.get('require_follow_narration') and seg_id != 1:
                fails.append(f'cold_open_not_first:seg_{seg_id}')
        duration = amount(s, 'duration_sec')
        if duration is None:
            fails.append(f'seg_{seg_id}_invalid_duration')
            continue
        total += duration
        footage = amount(s, 'original_footage_sec',
                         duration if s.get('source') == 'original_footage' else 0.0)
        audio = amount(s, 'original_audio_sec',
                       duration if s.get('source') == 'original_audio' else 0.0)
        speech = amount(s, 'commentary_sec', duration - footage if s.get('source') != 'original_audio' else 0.0)
        if footage is None or audio is None or speech is None:
            fails.append(f'seg_{seg_id}_invalid_source_duration')
            continue
        # ★ 冷开场用放宽的连续原画面上限；普通段用严格的 2.5s
        seg_limit = co_max if is_cold else limit
        if footage > seg_limit:
            fails.append(f'seg_{seg_id}_continuous_{footage:.2f}s>{seg_limit}s'
                         + ('(cold_open)' if is_cold else ''))
        if audio > comp['max_original_audio_sec']:
            fails.append(f'seg_{seg_id}_original_audio_{audio:.2f}s>{comp["max_original_audio_sec"]}s')
        orig += footage
        orig_audio += audio
        commentary += speech
    if co_enabled and cold_seen > 1:
        fails.append(f'multiple_cold_open:{cold_seen} (最多1个)')
    commentary_ratio = commentary / total if measured_map and total else (1 - orig / total if total else 1.0)
    if total and commentary_ratio < comp['commentary_ratio_min']:
        fails.append(f'commentary_ratio_too_low:{commentary_ratio:.2f}<{comp["commentary_ratio_min"]}')
    if total and orig_audio / total > comp['max_original_audio_ratio']:
        fails.append(f'original_audio_ratio_too_high:{orig_audio/total:.2f}>{comp["max_original_audio_ratio"]}')
    return {'gate': 'hard/copyright', 'passed': not fails, 'fails': fails, 'warns': warns}


def create_readback(video: str, readback: str, cfg: dict) -> str | None:
    """Read the final render with the TTS-specific ASR profile, entirely locally."""
    target = Path(readback)
    source = Path(video) if video else None
    if not source or not source.is_file():
        return 'final_video_missing'
    target.parent.mkdir(parents=True, exist_ok=True)
    wav = target.with_name(f'.{target.stem}.{os.getpid()}.wav')
    try:
        r = subprocess.run(
            [cfg['runtime']['ffmpeg'], '-v', 'error', '-y', '-i', str(source),
             '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(wav)],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600)
        if r.returncode or not wav.is_file():
            return f'audio_extract_failed:{(r.stderr or "")[-240:]}'
        sys.path.insert(0, str(ROOT / 'scripts'))
        import asr
        result = asr.transcribe_for_narration_qc(str(wav), cfg)
        text = (result.get('text') or '').strip()
        if not text:
            return f'asr_empty:{result.get("error", result.get("engine", "unknown"))}'
        target.write_text(json.dumps({
            'schema_version': 2, 'purpose': 'tts_narration_qc',
            'text': text, 'engine': result.get('engine'), 'model': result.get('model'),
            'calibration_status': result.get('calibration_status'),
            'source_video': str(source), 'infer_sec': result.get('infer_sec'),
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        return None
    except Exception as e:
        return f'asr_readback_failed:{type(e).__name__}:{str(e)[:180]}'
    finally:
        wav.unlink(missing_ok=True)


def readback_matches_profile(readback: str, video: str, cfg: dict) -> bool:
    """Reject cached readbacks from the old raw-ASR path or another model/video."""
    try:
        data = json.loads(Path(readback).read_text(encoding='utf-8'))
    except Exception:
        return False
    profile = cfg.get('asr', {}).get('narration_qc') or {}
    return (
        data.get('purpose') == 'tts_narration_qc'
        and data.get('model') == profile.get('model')
        and data.get('source_video') == str(Path(video))
    )


def soft_speech(narration: str, readback: str, threshold: float, cfg: dict) -> dict:
    """Score synthesized narration with its own model; never borrow raw-ASR CER."""
    if not Path(narration).is_file():
        return {'gate': 'soft/speech', 'scored': False, 'reason': 'files_missing'}
    profile = cfg.get('asr', {}).get('narration_qc') or {}
    configured_threshold = profile.get('max_cer')
    calibrated = (
        profile.get('status') == 'verified'
        and profile.get('allow_auto_pass') is True
        and isinstance(configured_threshold, (int, float))
        and not isinstance(configured_threshold, bool)
        and 0 <= float(configured_threshold) <= 1
    )
    effective_threshold = float(configured_threshold) if calibrated else None
    source_video = cfg.get('_video', '')
    if (not Path(readback).is_file()
            or not source_video
            or not readback_matches_profile(readback, source_video, cfg)):
        error = create_readback(cfg.get('_video', ''), readback, cfg)
        if error:
            return {'gate': 'soft/speech', 'scored': False, 'calibrated': False,
                    'model': profile.get('model'), 'reason': error}
    sys.path.insert(0, str(Path(__file__).parent))
    from preflight import normalize, cer  # 复用同一规范化，避免两套口径

    try:
        ref = Path(narration).read_text(encoding='utf-8')
        hyp = json.loads(Path(readback).read_text(encoding='utf-8')).get('text', '')
    except Exception as e:
        return {'gate': 'soft/speech', 'scored': False,
                'reason': f'readback_invalid:{type(e).__name__}:{str(e)[:120]}'}
    if not hyp.strip():
        return {'gate': 'soft/speech', 'scored': False, 'calibrated': False,
                'reason': 'asr_empty'}
    c = cer(normalize(ref, cfg), normalize(hyp, cfg))
    return {
        'gate': 'soft/speech', 'scored': True, 'calibrated': calibrated,
        'cer': round(c, 4), 'threshold': effective_threshold,
        'passed': calibrated and c <= effective_threshold,
        'engine': json.loads(Path(readback).read_text(encoding='utf-8')).get('engine'),
        'model': profile.get('model'),
        'legacy_preflight_threshold_ignored': threshold is not None,
        'reason': None if calibrated else 'tts_readback_calibration_provisional',
        'weight': 40,
    }


def soft_semantic(report: str, cfg: dict) -> dict:
    """软门：语义一致性（VLM 自评）。同模型自评不可全信，故记分。"""
    p = Path(report)
    if not p.exists():
        return {'gate': 'soft/semantic', 'scored': False, 'reason': 'missing'}
    d = json.loads(p.read_text(encoding='utf-8'))
    unsupported = d.get('unsupported_claims', [])
    marker = cfg['compliance']['inference_marker']
    explicit_unmarked = d.get('unmarked_inference')
    unmarked = (explicit_unmarked if isinstance(explicit_unmarked, list)
                else [c for c in unsupported if marker not in str(c)])
    return {'gate': 'soft/semantic', 'scored': True, 'unsupported_count': len(unsupported),
            'unmarked_inference': len(unmarked), 'passed': len(unmarked) == 0, 'weight': 60,
            'hard_flag': bool(unmarked)}


def grade(soft_total: float, soft_max: float) -> str:
    """分档交付：取代 v1.1 的"过/不过"二元判定。"""
    if soft_max <= 0:
        return 'C'
    ratio = soft_total / soft_max
    if ratio >= 0.85:
        return 'A'
    if ratio >= 0.60:
        return 'B'
    return 'C'


def hard_mix_uniformity(video: str, srt: str, cfg: dict) -> dict:
    """L2 混音均匀度硬门（坑典 P77，2026-10-27 用户实测「好几个音轨乱糟糟」新增）。

    判据来源：loudnorm（dynamic 模式）若放在 adelay 之前，adelay 会**彻底失效**——
    各批语音全部从 0s 开始播、前段叠加后段衰减，成片段间响度差 24dB，
    用户听感就是「好几个人同时说话、乱糟糟」。

    量化方法：按字幕 cue 边界逐段测 RMS（volumedetect mean_volume），
      stdev > 4.0dB 或 极差 > 15.0dB → 拒绝（P77 实测坏值 6.7 / 24.4）
    参考正常值：未受影响的 T1 = 1.9 / 7.0；修复后的 T2 = 1.7 / 7.2。

    这个门把「听感乱」变成可量化硬指标——同类时轴/电平 bug下次直接拦在交付前。
    """
    p = Path(srt) if srt else None
    if not p or not p.is_file():
        return {'gate': 'hard/mix_uniformity', 'passed': False,
                'fails': ['mix_uniformity_srt_missing'], 'warns': []}
    txt = p.read_text(encoding='utf-8', errors='replace')
    pattern = re.compile(r'^(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})$')
    cues = []
    for line in txt.splitlines():
        m = pattern.fullmatch(line.strip())
        if not m:
            continue
        t = m.groups()
        s = int(t[0]) * 3600 + int(t[1]) * 60 + int(t[2]) + int(t[3]) / 1000
        e = int(t[4]) * 3600 + int(t[5]) * 60 + int(t[6]) + int(t[7]) / 1000
        if e > s + 0.2:
            cues.append((s, e))
    if len(cues) < 5:
        return {'gate': 'hard/mix_uniformity', 'passed': False,
                'fails': [f'mix_uniformity_too_few_cues:{len(cues)}'], 'warns': []}

    ff = cfg['runtime']['ffmpeg']
    levels = []
    for s, e in cues:
        r = subprocess.run([ff, '-v', 'info', '-ss', f'{s:.2f}', '-t', f'{e - s:.2f}',
                            '-i', video, '-af', 'volumedetect', '-f', 'null', '-'],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=120)
        m = re.search(r'mean_volume: (-[\d.]+) dB', r.stderr or '')
        if m:
            levels.append(float(m.group(1)))
    if len(levels) < 5:
        return {'gate': 'hard/mix_uniformity', 'passed': False,
                'fails': [f'mix_uniformity_no_levels:{len(levels)}'], 'warns': []}

    n = len(levels)
    mean = sum(levels) / n
    stdev = (sum((x - mean) ** 2 for x in levels) / n) ** 0.5
    span = max(levels) - min(levels)
    fails, warns = [], []
    if stdev > 4.0:
        fails.append(f'mix_level_stdev:{stdev:.1f}dB>4.0')
    if span > 15.0:
        fails.append(f'mix_level_span:{span:.1f}dB>15.0')
    if stdev > 3.0:
        warns.append(f'mix_level_stdev:{stdev:.1f}dB（临界，建议复查）')
    return {'gate': 'hard/mix_uniformity', 'passed': not fails, 'fails': fails,
            'warns': warns,
            'mix_uniformity': {'segments': n, 'mean_db': round(mean, 1),
                               'stdev_db': round(stdev, 2), 'span_db': round(span, 1)}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='影视解说成片硬门/软门质检')
    ap.add_argument('video')
    ap.add_argument('seconds', nargs='?', type=float,
                    help='可选目标时长；只有启用 --strict-duration 时才作为硬门')
    ap.add_argument('srt', nargs='?', default='')
    ap.add_argument('shotmap', nargs='?', default='')
    ap.add_argument('threshold', nargs='?', default='none',
                    help='兼容旧命令的原片 ASR 阈值；合成解说门不再复用此值')
    ap.add_argument('--platform', choices=('douyin', 'kuaishou', 'wechat_channels', 'generic'),
                    default=None)
    ap.add_argument('--strict-duration', action='store_true')
    ap.add_argument('--semantic', default=None, help='语义质检 JSON；默认读取字幕同目录 semantic.json')
    ap.add_argument('--narration', default=None)
    ap.add_argument('--readback', default=None)
    ap.add_argument('--out', default=None, help='报告 JSON 路径；默认 <video>.qc.json')
    args = ap.parse_args(argv)

    cfg = json.loads((ROOT / 'config' / 'engine.json').read_text(encoding='utf-8'))
    cfg['_video'] = args.video
    cfg['active_profile'] = args.platform or cfg.get('active_profile', 'generic')
    if cfg['active_profile'] not in cfg['platform_profiles']:
        raise SystemExit(f'未知平台配置: {cfg["active_profile"]}')
    srt, shotmap = args.srt, args.shotmap
    threshold = float(args.threshold) if args.threshold != 'none' else None
    base = Path(srt).parent if srt else Path(args.video).parent
    semantic = args.semantic or str(base / 'semantic.json')
    narration = args.narration or str(base / 'narration.txt')
    readback = args.readback or str(base / 'readback.json')

    results = [
        hard_technical(cfg['runtime']['ffprobe'], cfg, args.seconds, args.strict_duration),
        hard_subtitle(srt, cfg),
        hard_mix_uniformity(args.video, srt, cfg),
        hard_copyright(shotmap, cfg),
        soft_semantic(semantic, cfg),
        soft_speech(narration, readback, threshold, cfg),
    ]

    hard = [r for r in results if r['gate'].startswith('hard')]
    soft = [r for r in results if r['gate'].startswith('soft')]
    hard_failed = [r for r in hard if not r['passed']]
    soft_total = sum(r.get('weight', 0) for r in soft if r.get('scored') and r.get('passed'))
    soft_max = sum(r.get('weight', 0) for r in soft if r.get('scored'))
    hard_flag = any(r.get('hard_flag') for r in soft)
    soft_unscored = [r for r in soft if not r.get('scored')]
    soft_grade = grade(soft_total, soft_max)
    # A 档表示所有软门均已验证且得分达标；缺失软门不能被其余门的高分抵消。
    if soft_unscored and soft_grade == 'A':
        soft_grade = 'B'
    semantic_hard_failures = [
        f"semantic_unmarked_inference:{r.get('unmarked_inference')}"
        for r in soft if r.get('hard_flag')
    ]
    verdict = 'REJECT' if hard_failed or hard_flag else soft_grade
    risks = [f"{r['gate']} 未打分：{r.get('reason', '原因未提供')}" for r in soft_unscored]
    for r in soft:
        if r.get('gate') != 'soft/speech' or not r.get('scored') or r.get('passed'):
            continue
        if not r.get('calibrated'):
            risks.append(f"soft/speech 尚未完成合成解说校准（CER {r.get('cer')}）；必须人工回听")
        else:
            risks.append(f"soft/speech 未通过：CER {r.get('cer')} > {r.get('threshold')}")
    if hard_flag:
        risks.append('语义检查发现未标注推断，已按硬性事实边界拒绝')
    out = {
        'engine_version': cfg['engine_version'],
        'verdict': verdict,
        'hard_passed': not hard_failed and not hard_flag,
        'hard_failures': [f for r in hard_failed for f in r.get('fails', [])] + semantic_hard_failures,
        'soft_score': f'{soft_total}/{soft_max}',
        'soft_grade': soft_grade,
        'soft_scored_gates': sum(1 for r in soft if r.get('scored')),
        'soft_total_gates': len(soft),
        'soft_unscored': [r['gate'] for r in soft_unscored],
        'risks': risks,
        'hard_flag': hard_flag,
        'gates': results,
        'caveat': '自动检查通过不等于人工审片通过；软门未打分项为已知风险',
    }
    out_path = Path(args.out) if args.out else Path(args.video).with_suffix('.qc.json')
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f'[out] {out_path}')
    return 0 if verdict != 'REJECT' else 3


if __name__ == '__main__':
    sys.exit(main())
