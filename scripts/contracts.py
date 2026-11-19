# -*- coding: utf-8 -*-
r"""contracts.py —— **配置契约门面**（技能所有数值的唯一入口 + 来源可追溯）。

★ 为什么新增（2026-11-09 审计 + 对标 bilibili-short-drama-cutter/config_merger.py）：
  审计发现 `config/engine.json` 里 **12 个字段写了却没有任何脚本读**（output 的
  width/height/fps/crf/preset/audio_codec/audio_bitrate/target_lufs/loudness_tolerance/
  true_peak、runtime_policy 整段、rework 整段、script_quality 主链未用），
  真正生效的数值却硬编码在 build.py / plan_est.py 顶部（W,H,FPS=1080,1920,30、
  BREATH=0.35、COLD_OPEN_MAX 五处各读一次）。
  → 配置与代码是**两份真相**，改配置不会生效，改代码不会同步配置（P88 同族病）。

  本模块不重写渲染链（零破坏），而是做三件事：
  ① 三层合并：engine.json（默认）→ overrides.json（用户，可选）→ detected.json（本机检测，可选）；
     每个值带 `<key>_source`，排障时知道「这个值从哪来」（借鉴 config_merger 的 source 标注）。
  ② **契约断言**：把 build.py / plan_est.py 顶部常量与配置逐一比对，漂移立刻报出。
     死配置因此变成「有守护的配置」，而不是纸面字段。
  ③ 供新模块（compliance / content_brief / shot_rank）统一取值，杜绝新一批硬编码。

用法：
    python scripts/contracts.py                # 打印全部契约值与来源
    python scripts/contracts.py --audit        # 比对代码常量，漂移则退出码 1
    python scripts/contracts.py --json
"""
import argparse
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / 'scripts'
CONFIG = ROOT / 'config'
UNSET = object()


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return {}


def load():
    """三层合并，返回 (values, sources)。缺关键段直接报错（读不到 ≠ 用旧值）。"""
    base = _read(CONFIG / 'engine.json')
    if not base or 'duration_policy' not in base:
        raise RuntimeError('[contracts] engine.json 读不到 —— 拒绝静默回退默认值')
    over = _read(CONFIG / 'overrides.json')       # 可选：用户显式覆盖
    detect = _read(CONFIG / 'detected.json')      # 可选：本机能力检测结果
    values, sources = {}, {}
    for layer, src in ((base, 'engine'), (detect, 'detected'), (over, 'user')):
        for k, v in layer.items():
            values[k] = v
            sources[k] = src
    return values, sources


def contract_map(v):
    """把 engine.json 的嵌套段摊平成「契约键 → (值, 来源路径)」。"""
    out = {}
    def put(key, value, path):
        out[key] = (value, path)
    # 输出规格
    o = v.get('output', {})
    for k in ('width', 'height', 'fps', 'video_codec', 'crf', 'preset',
              'audio_codec', 'audio_bitrate', 'target_lufs',
              'loudness_tolerance', 'true_peak'):
        put(f'output.{k}', o.get(k, UNSET), f'output.{k}')
    # 合规 / 叙事 / 策略
    comp = v.get('compliance', {})
    for k in ('max_continuous_original_sec', 'max_original_audio_sec',
              'max_original_audio_ratio', 'commentary_ratio_min',
              'forbid_inference_without_marker', 'inference_marker'):
        put(f'compliance.{k}', comp.get(k, UNSET), f'compliance.{k}')
    put('compliance.cold_open.max_sec',
        (comp.get('cold_open') or {}).get('max_sec', UNSET),
        'compliance.cold_open.max_sec')
    for k in ('target_chars_per_tts_segment', 'short_story_line_max_chars',
              'max_short_story_line_ratio'):
        put(f'script_quality.{k}', v.get('script_quality', {}).get(k, UNSET),
            f'script_quality.{k}')
    dp = v.get('duration_policy', {})
    put('duration.bounds', dp.get('bounds', UNSET), 'duration_policy.bounds')
    put('duration.estimate_margin', dp.get('estimate_margin', UNSET),
        'duration_policy.estimate_margin')
    ddp = v.get('dedupe_policy', {})
    for k in ('max_shot_overlap_ratio', 'max_text_overlap_ratio', 'ngram',
              'shot_time_bucket'):
        put(f'dedupe.{k}', ddp.get(k, UNSET), f'dedupe_policy.{k}')
    rp = v.get('runtime_policy', {})
    for k in ('offline_production', 'gpu_serialization', 'allow_hf_download'):
        put(f'runtime.{k}', rp.get(k, UNSET), f'runtime_policy.{k}')
    put('rework.max_rounds', v.get('rework', {}).get('max_rounds', UNSET),
        'rework.max_rounds')
    # ★ v6.0.0（2026-11-13）：第六层「成片实证」的两个新配置段纳入契约，
    #   否则又会出现「配置写了却没人守护」的死字段（P88 同族）。
    mq = v.get('media_qc_policy', {})
    for k in ('black_min_sec', 'freeze_min_sec', 'silence_min_sec',
              'max_av_drift_sec', 'require_aigc_tag',
              'cover_still_sec', 'still_tolerance_sec'):
        put(f'media_qc.{k}', mq.get(k, UNSET), f'media_qc_policy.{k}')
    vd = v.get('visual_dedupe_policy', {})
    for k in ('sample_interval_sec', 'hash_size', 'max_hamming_distance',
              'max_duplicate_ratio', 'max_frames_per_video'):
        put(f'visual_dedupe.{k}', vd.get(k, UNSET), f'visual_dedupe_policy.{k}')
    return out


# build.py / plan_est.py 顶部常量的「应等于哪个契约键」（不一致即漂移）
# ★ COLD_OPEN_MAX 不列：它本来就是运行时从 engine.json 读的（正确做法），
#   审计改为检查「确实读了配置」，而不是比对字面量。
CODE_CONTRACTS = {
    'build.py': {
        'W': ('output.width', 1080), 'H': ('output.height', 1920),
        'FPS': ('output.fps', 30),
        'MAX_SILENT_RUN_SEC': (None, 0.8),
        'BREATH': (None, 0.35),
    },
    'plan_est.py': {
        'BREATH': (None, 0.35), 'TTS_CPS': (None, 5.6),
    },
}

DYNAMIC_CONFIG_READERS = ('build.py', 'plan_est.py', 'quality_gate.py',
                          'spec_precheck.py', 'auto_pilot.py', 'compliance.py',
                          'media_qc.py', 'visual_dedupe.py', 'publish_kit.py')


def _consts(path):
    """静态读取模块顶部常量（不 import，避免触发副作用）。

    支持 `W, H, FPS = 1080, 1920, 30` 元组赋值与顶层 try 块内赋值；
    只扫模块顶层，不进函数体，避免把局部变量误当契约。
    """
    try:
        tree = ast.parse(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return {}
    out = {}

    def handle(node):
        for t in node.targets:
            if isinstance(t, ast.Name):
                names = [t.id]
            elif isinstance(t, ast.Tuple):
                names = [e.id for e in t.elts if isinstance(e, ast.Name)]
            else:
                continue
            try:
                val = ast.literal_eval(node.value)
            except Exception:
                continue
            if len(names) == 1:
                out[names[0]] = val
            elif isinstance(val, (list, tuple)) and len(val) == len(names):
                for n, v in zip(names, val):
                    out[n] = v

    for node in tree.body:
        if isinstance(node, ast.Assign):
            handle(node)
        elif isinstance(node, ast.Try):
            for sub in node.body:
                if isinstance(sub, ast.Assign):
                    handle(sub)
    return out


def audit():
    """比对配置契约与代码常量。返回 (ok, findings)。"""
    v, _src = load()
    cm = contract_map(v)
    findings = []
    for fname, mapping in CODE_CONTRACTS.items():
        consts = _consts(SCRIPTS / fname)
        for name, (key, expected) in mapping.items():
            if name not in consts:
                findings.append(f'{fname}:{name} 未在顶部找到（可能改名，需复核）')
                continue
            got = consts[name]
            if key:
                want = cm.get(key, (UNSET,))[0]
                if want is UNSET:
                    findings.append(f'{fname}:{name} 对应配置 {key} 缺失')
                elif float(got) != float(want):
                    findings.append(
                        f'{fname}:{name}={got} 但配置 {key}={want} → 漂移（改配置不生效）')
            elif float(got) != float(expected):
                findings.append(f'{fname}:{name}={got}，期望 {expected}（未收口的硬编码）')
    # 配置段完整性
    for seg in ('output', 'compliance', 'duration_policy', 'dedupe_policy',
                'runtime_policy', 'rework', 'script_quality',
                'media_qc_policy', 'visual_dedupe_policy'):
        if seg not in v:
            findings.append(f'engine.json 缺段 {seg}')
    # ★ 这些脚本必须运行时读配置，不许硬编码第二份真相
    for fname in DYNAMIC_CONFIG_READERS:
        f = SCRIPTS / fname
        if not f.is_file():
            continue        # 尚未落地的新模块由 eval 用例管
        src = f.read_text(encoding='utf-8')
        if ('engine.json' not in src and 'import contracts' not in src
                and 'from contracts import' not in src
                and 'from policy import' not in src):
            findings.append(f'{fname} 未从配置取值（疑似硬编码副本）')
    return not findings, findings


def main():
    ap = argparse.ArgumentParser(description='配置契约门面')
    ap.add_argument('--audit', action='store_true', help='比对代码常量，漂移则退出码 1')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    v, src = load()
    if a.audit:
        ok, findings = audit()
        print(f'契约审计: {"✓ 一致" if ok else "✗ 发现漂移"}')
        for f in findings:
            print('  -', f)
        return 0 if ok else 1
    cm = contract_map(v)
    if a.json:
        print(json.dumps({k: {'value': val if val is not UNSET else None,
                              'path': path, 'source': src.get(path.split('.')[0], 'engine')}
                          for k, (val, path) in cm.items()}, ensure_ascii=False, indent=1))
    else:
        print(f'── 配置契约（{len(cm)} 项）──')
        for k, (val, path) in cm.items():
            print(f'  {k:38s} = {str(val):22s}  ← {src.get(path.split(".")[0], "engine")}:{path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
