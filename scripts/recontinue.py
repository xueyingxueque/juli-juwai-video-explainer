# -*- coding: utf-8 -*-
r"""recontinue.py —— 修复续跑：从 [4/6] 切画面重跑（speech/raw 中间产物复用，
不重跑 TTS/ASR）。数据集从 build-plan.json 的 dataset 字段读（v3.6.1 修：
原硬编码单剧数据集，换剧续跑会串剧——A 剧画面剪进 B 剧，哨兵兜底拦截）。
用法：python recontinue.py <out/Xn-v37 目录>"""
import json
import sys
from pathlib import Path

SKILL_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_SCRIPTS))
import build  # noqa: E402


def main() -> int:
    out_dir = Path(sys.argv[1])
    plan = json.loads((out_dir / 'build-plan.json').read_text(encoding='utf-8'))
    segments = plan['segments']
    work = out_dir / '_work'
    total = float(plan['total_sec'])
    name = plan['name']
    # ★ dataset 判定（v3.6.1 修）：Path('') 的 is_dir() 是 True（指向 CWD），
    #   旧判断会把空字段当合法目录 → 拿 CWD 当数据集 → 串剧/源文件不存在
    ds_field = str(plan.get('dataset') or '').strip()
    dataset = Path(ds_field) if ds_field else None
    if dataset is None or not dataset.is_dir():
        if len(sys.argv) > 2 and Path(sys.argv[2]).is_dir():
            dataset = Path(sys.argv[2])
            print(f'使用命令行指定数据集: {dataset}')
        else:
            print(f'[FAIL] 无法确定数据集（build-plan.json dataset={ds_field!r}）'
                  f'——第二参数传素材目录，或用新代码重跑 build')
            return 2
    print(f'数据集: {dataset}')

    print('[4/6] 切画面并拼接')
    clips = build.cut_clips(segments, dataset, work, None)
    silent = build.concat(clips, work)
    silent_dur = build.probe_dur(silent)
    print(f'    {len(clips)} 段 → {silent_dur:.2f}s（音频时间轴 {total:.2f}s）')
    if abs(silent_dur - total) > 1.0:
        print(f'[FAIL] 音画不对齐 {abs(silent_dur - total):.2f}s')
        return 2

    print('[5/6] 混音')
    raw_files = {k: {'file': str(work / f'raw{k:02d}.wav')}
                 for k, s in enumerate(segments) if not (s.get('text') or '').strip()}
    for k, r in raw_files.items():
        if not Path(r['file']).is_file():
            print(f'[FAIL] 原声音轨缺失: {r["file"]}')
            return 2
    bgm = None
    if plan.get('bgm'):
        for c in (Path(plan['bgm']), Path(plan['bgm']).with_suffix('.wav')):
            if c.exists():
                bgm = c
                break
    mixed = work / 'mixed.mp4'
    build.mix(silent, segments, work / 'speech', raw_files, bgm, mixed, total)
    print(f'    BGM: {bgm.name if bgm else "无"}')

    # shotmap（质检版权门度量源；v3.6.1 起抽为共享函数）
    build.write_shotmap(segments, dataset, raw_files, None, out_dir, total)

    print('[6/6] 烧字幕 + 表情包 + 校验')
    platform = 'douyin'
    profile = build.CFG['platform_profiles'][platform]
    srt = work / 'subs.srt'
    build.write_srt(segments, srt, profile)
    final = out_dir / f'{name}.mp4'
    burned = out_dir / f'.{name}.mp4.burned.mp4'
    has_stickers = any(s.get('stickers') and (s.get('text') or '').strip()
                       for s in segments)
    if has_stickers:
        try:
            build.burn(mixed, srt, burned, profile, platform, total)
            build.overlay_stickers(burned, segments, final, work, total, platform)
        finally:
            burned.unlink(missing_ok=True)
    else:
        build.burn(mixed, srt, final, profile, platform, total)

    rep = build.verify(final, work)
    print(f'视频 {rep["video_dur"]}s / 音轨 {rep["audio_dur"]}s')
    print(f'响度 {rep["lufs"]} LUFS / 峰值 {rep["peak"]} dBTP')
    print(f'最长连续静音 {rep["longest_silent_run"]}s')
    if rep['errors']:
        print('✗ 校验未通过:')
        for e in rep['errors']:
            print('  ·', e)
        return 3
    print('✓ 全部校验通过')
    print(f'成片: {final}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
