# -*- coding: utf-8 -*-
"""deliver_check.py —— **成品完整性校验**：这一集到底产出了没有？

★ 为什么必须有（2026-11-02 血泪）：
  技能原本只有「逐阶段自检」，没有「**这一集到底齐了没**」的判定。
  于是：八步手工串 + 中途被会话回收 → 半成品留在盘上，
  而流程显示「跑到 build 了」，人以为在产出，实际零成品（实测 11 小时零产出）。
  → 把「成品」定义成**一份可核对的清单**，任何一项缺失就明确报出来。

校验项：
  ① spec-N.json / ev-N.json / plan-N.json / plan-N-v37.json 四个中间件
  ② 成片 mp4（存在 + 实测时长合规 + 音视频双流）
  ③ 封面版 mp4（首帧已合成封面）
  ④ 封面 jpg
  ⑤ 质检 qc.json（hard_passed=true）
  ⑥ 字幕 srt + shotmap.json（质检输入，也在盘上才可复检）
  ⑦ 网盘交付文件（delivery_root/<剧名>/解说成品/<prefix>-<title>.mp4）

用法：
    python deliver_check.py <任务目录> <ep> <集前缀> <成片名> --title "<爆款标题>" --series "<剧名>"
    python deliver_check.py <任务目录> <ep> --plan-only     # 只查中间件（还没到交付）
退出码：0 = 全齐；1 = 有缺项（逐条列出）。
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_state as ps          # 产线状态唯一真相源（出片判定）
from policy import check_measured, format_range  # noqa: E402  时长规范唯一真相源

SKILL = Path(__file__).resolve().parent.parent
CFG = json.loads((SKILL / 'config' / 'engine.json').read_text(encoding='utf-8'))
DELIVERY_ROOT = Path(CFG.get('paths', {}).get('delivery_root', 'D:/BaiduNetdiskDownload'))


def probe(path):
    """→ (时长秒, 有视频流, 有音频流)"""
    try:
        r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                            'format=duration:stream=codec_type', '-of', 'json', str(path)],
                           capture_output=True, text=True, encoding='utf-8', timeout=60)
        d = json.loads(r.stdout or '{}')
        dur = float((d.get('format') or {}).get('duration') or 0)
        types = [s.get('codec_type') for s in (d.get('streams') or [])]
        return dur, ('video' in types), ('audio' in types)
    except Exception:
        return 0.0, False, False


def main():
    ap = argparse.ArgumentParser(description='成品完整性校验')
    ap.add_argument('task')
    ap.add_argument('ep')
    ap.add_argument('prefix', nargs='?', default='')
    ap.add_argument('name', nargs='?', default='')
    ap.add_argument('--title', default='')
    ap.add_argument('--series', default='')
    ap.add_argument('--tier', default='', help='已废弃（取消三档），保留仅为兼容旧调用')
    ap.add_argument('--plan-only', action='store_true')
    a = ap.parse_args()

    task = Path(a.task)
    ep = str(a.ep)
    plan_dir = task / '_plan'
    outdir = task / 'out' / a.prefix if a.prefix else None
    missing, ok = [], []

    def need(cond, label, detail=''):
        (ok if cond else missing).append(f'{label}{(" — " + detail) if detail else ""}')

    # ① 中间件
    for f, label in ((f'spec-{ep}.json', 'spec'), (f'ev-{ep}.json', '证据'),
                     (f'plan-{ep}.json', 'plan'), (f'plan-{ep}-v37.json', 'v37 plan')):
        p = plan_dir / f
        need(p.is_file(), f'{label}（{f}）')
    if a.plan_only:
        print('── 中间件 ──')
        for x in ok:
            print(f'  ✓ {x}')
        for x in missing:
            print(f'  ✗ {x}')
        print(f'\n[{"全齐" if not missing else str(len(missing)) + " 项缺失"}]')
        return 1 if missing else 0

    if not outdir:
        print('[FAIL] 缺 --prefix/成片名，无法校验成片')
        return 2
    mp4 = outdir / f'{a.name}.mp4'
    # ★ 2026-11-04 收口到 pipeline_state（单一真相源）：
    #   「封面版路径」此前在本文件与 auto_resume.py（已移除） 各写一遍
    #   → 同一规则写两遍 = 改一处必漏另一处 → 现在都调 ps.final_path()。
    cover_mp4 = ps.final_path({'task': task, 'prefix': a.prefix, 'name': a.name})
    cover_jpg = outdir / f'cover_{a.prefix}.jpg'
    qc = outdir / f'{a.name}.qc.json'

    # ② 成片
    if mp4.is_file():
        dur, has_v, has_a = probe(mp4)
        need(has_v and has_a, '成片音视频双流', f'{dur:.1f}s')
        # ★ 变量名别叫 ok：本函数里 `ok` 是「已通过项列表」（missing, ok = [], []）
        dur_ok, msg = check_measured(dur)
        need(dur_ok, f'成片实测时长合规（{format_range()}）', msg)
    else:
        need(False, '成片 mp4', str(mp4))

    need(cover_mp4.is_file(), '封面版 mp4（封面已合成进首帧）')
    need(cover_jpg.is_file(), '封面 jpg')
    need((outdir / '_work/subs.srt').is_file(), '字幕 srt')
    need((outdir / 'shotmap.json').is_file(), 'shotmap.json')

    # ⑤ 质检
    if qc.is_file():
        try:
            d = json.loads(qc.read_text(encoding='utf-8'))
            need(d.get('hard_passed') is True, '质检 hard_passed',
                 f"verdict={d.get('verdict')}")
        except Exception as e:
            need(False, '质检 qc.json 可解析', str(e)[:60])
    else:
        need(False, '质检 qc.json')

    # ⑥ 网盘交付
    if a.series and a.title:
        dst = DELIVERY_ROOT / a.series / '解说成品' / f'{a.prefix}-{a.title}.mp4'
        need(dst.is_file(), '网盘交付文件', str(dst))
    else:
        ok.append('网盘交付检查（未给 --series/--title，跳过）')

    print(f'── 成品清单 · {task.name} / {a.prefix} ──')
    for x in ok:
        print(f'  ✓ {x}')
    for x in missing:
        print(f'  ✗ {x}')
    if missing:
        print(f'\n[FAIL] {len(missing)} 项缺失 —— 这一集**没有正常产出成品**')
        return 1
    print('\n[OK] 成品齐全 ✓')
    return 0


if __name__ == '__main__':
    sys.exit(main())
