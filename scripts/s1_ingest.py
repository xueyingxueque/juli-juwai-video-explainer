"""Prepare a single source episode for S2 with auditable frame timestamps."""
import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / 'config' / 'engine.json').read_text(encoding='utf-8'))


def probe_duration(video: Path) -> float:
    r = subprocess.run(
        [CFG['runtime']['ffprobe'], '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'csv=p=0', str(video)], capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=120)
    if r.returncode:
        raise RuntimeError(f'ffprobe failed: {(r.stderr or "")[-300:]}')
    duration = float(r.stdout.strip())
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f'invalid video duration: {r.stdout!r}')
    return duration


def _discard(p: Path) -> None:
    """★ 清理一律 rename 到 _trash，**不做 unlink/rmtree**（坑典 P82）。

    沙箱对 `unlink` / `rmtree` 有**按次计数**阈值（实测 50 次/回合即触发
    `SAFE_DELETE_BULK_CONFIRM_REQUIRED`）。批量 ingest 一部剧 20+ 集，
    每集要清 staging / frames / 临时 wav，累积必撞阈值 →
    跑到第 6 集就开始整批失败，且失败点随集数漂移，极难定位。

    rename 不触发删除计数，代价只是留一点垃圾（外部统一清）。
    与 build.py「_work → _trash_<ts>_<pid> 不删除」是同一条纪律。
    """
    try:
        if not p.exists():
            return
        tr = p.parent / '_trash'
        tr.mkdir(parents=True, exist_ok=True)
        p.rename(tr / f'{int(time.time() * 1000)}_{os.getpid()}_{p.name}')
    except Exception:
        pass


def atomic_json(path: Path, data) -> None:
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(path)
    finally:
        _discard(tmp)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Extract ASR text and timestamped frames for one source episode.')
    ap.add_argument('video', help='source video file')
    ap.add_argument('--episode', required=True, type=int, help='episode number represented by this file')
    ap.add_argument('--out', required=True, help='new task directory')
    ap.add_argument('--interval', type=float, default=4.0, help='frame sampling interval in seconds')
    ap.add_argument('--force', action='store_true', help='replace only S1-owned outputs in the task directory')
    args = ap.parse_args(argv)

    video = Path(args.video).resolve()
    out = Path(args.out).resolve()
    if not video.is_file():
        print(f'[ERR] source video not found: {video}', file=sys.stderr)
        return 2
    if args.episode < 1 or not math.isfinite(args.interval) or args.interval <= 0:
        print('[ERR] episode must be positive and interval must be finite and > 0', file=sys.stderr)
        return 2
    owned = [out / 'source.json', out / 'asr_full.json', out / 'frame_times.json', out / 'frames']
    existing = [p for p in owned if p.exists()]
    if existing and not args.force:
        print('[ERR] S1 outputs already exist; use a new task directory or --force', file=sys.stderr)
        return 2

    out.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(video)
    frames_dir = out / 'frames'
    staging = out / '_s1_frames_tmp'
    if staging.exists():
        _discard(staging)
    staging.mkdir(parents=True)

    records = []
    times = []
    failed = []          # 抽帧失败的时间点（仅统计，单帧失败不致命）
    # ★ 尾部留余量（2026-10-27 修）：抽帧点原本只要 t < duration 就收，
    #   会落到距末尾几毫秒处——那里已无可解码帧，ffmpeg 静默失败 →
    #   整集 ingest 报废（实测《八零小福包》ep21 时长 42.005s，抽帧点正好 42.000s）。
    #   改为保留 TAIL_MARGIN 秒余量；余量取「半个 interval」与 1.0s 的较大值。
    tail_margin = max(args.interval / 2, 1.0)
    t = min(args.interval / 2, duration / 2)
    while t < duration - tail_margin:
        times.append(round(t, 3))
        t += args.interval
    if not times:
        times = [round(duration / 2, 3)]
    try:
        for i, sec in enumerate(times):
            # ★ 抽帧一律 png（坑典：Dots ffmpeg 的 mjpeg 编码器对非全范围 YUV 源
            #   会静默 0 帧——输出 jpg 时 ffmpeg 报 "Nothing was written" 或直接
            #   Terminating thread -22）。shot_evidence 早已改 png+threads 1，
            #   但 s1_ingest 这份副本漏改（同一坑两处副本，改一处必漏另一处），
            #   实测《天降福星》ep10 即因此整集 ingest 失败。此处同步为 png。
            name = f'f{i:04d}.png'
            dst = staging / name
            r = subprocess.run(
                [CFG['runtime']['ffmpeg'], '-v', 'error', '-y', '-ss', f'{sec:.3f}',
                 '-i', str(video), '-frames:v', '1', '-threads', '1', str(dst)],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
            if r.returncode or not dst.is_file() or dst.stat().st_size == 0:
                # ★ 单帧失败不再让整集报废（2026-10-27 修）：源片个别时间点
                #   损坏/无关键帧是常态，跳过该点即可；只有「一帧都抽不出来」
                #   才是真失败。旧实现一处 raise 就整集 ingest 失败，
                #   下游读全剧字幕直接缺一集。
                print(f'[warn] 抽帧失败跳过 {sec:.3f}s: {(r.stderr or "")[-120:]}')
                _discard(dst)
                failed.append(sec)
                continue
            records.append({'frame': name, 't': sec})

        # ★ 只有一帧都抽不出来才是真失败（源片个别点损坏不算）
        if not records:
            raise RuntimeError(f'frame extraction failed for all {len(times)} points: {failed[:5]}')
        if failed:
            print(f'[warn] {len(failed)}/{len(times)} 个抽帧点失败已跳过: {failed[:5]}')

        # Run the configured text ASR through the shared adapter; do not save audio intermediates.
        wav = out / '_s1_asr_tmp.wav'
        r = subprocess.run(
            [CFG['runtime']['ffmpeg'], '-v', 'error', '-y', '-i', str(video), '-vn',
             '-ac', '1', '-ar', '16000', str(wav)],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600)
        if r.returncode or not wav.is_file():
            raise RuntimeError(f'audio extraction failed: {(r.stderr or "")[-300:]}')
        sys.path.insert(0, str(ROOT / 'scripts'))
        import asr
        transcript = asr.transcribe_for_story(str(wav), CFG, duration_sec=duration)
        _discard(wav)
        if not transcript.get('text'):
            raise RuntimeError(f'ASR produced no usable text: {transcript.get("error", transcript.get("engine"))}')

        if frames_dir.exists():
            _discard(frames_dir)
        staging.replace(frames_dir)
        atomic_json(out / 'source.json', {
            'schema_version': 1, 'episode': args.episode, 'video': str(video),
            'duration_sec': round(duration, 3),
        })
        atomic_json(out / 'frame_times.json', {
            'schema_version': 1, 'episode': args.episode, 'video': str(video),
            'frames': records,
        })
        atomic_json(out / 'asr_full.json', {
            'schema_version': 2,
            'text': transcript['text'], 'engine': transcript.get('engine'),
            'model': transcript.get('model'), 'duration_sec': round(duration, 3),
            'segments': transcript.get('segments') or [],
            'quality': transcript.get('quality') or {},
        })
    except Exception as e:
        wav_path = out / '_s1_asr_tmp.wav'
        _discard(wav_path)
        if staging.exists():
            _discard(staging)
        print(f'[ERR] {type(e).__name__}: {e}', file=sys.stderr)
        return 1

    print(json.dumps({'status': 'ready', 'episode': args.episode,
                      'duration_sec': round(duration, 3), 'frames': len(records),
                      'asr_engine': transcript.get('engine'),
                      'timed_segments': len(transcript.get('segments') or []),
                      'transcript_selection': (transcript.get('quality') or {}).get('selection_reason'),
                      'outputs': ['source.json', 'asr_full.json', 'frame_times.json', 'frames/']},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
