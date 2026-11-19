# -*- coding: utf-8 -*-
r"""compliance.py —— **合规预检 + AI 标识 + 生成链路溯源**（本地、零联网）。

★ 为什么新增（2026-11-09 全网调研 + 对标 bilibili_compliance_checker.py）：
  2025-09-01《人工智能生成合成内容标识办法》第四条要求视频在
  「起始画面和播放周边」显式标识，第五 / 六条要求文件元数据隐式标识
  （生成属性 / 服务提供者名称或编码 / 内容编号）；
  广电总局《微短剧发展管理办法》2026-09-01 施行，第 34 条要求明显位置提示。
  ★ v6.0.0 勘误（2026-11-13 二次全网调研）：旧版把「片头前 3 秒 / 封面 / 简介」
  写成法规要求，实为二手平台运营文口径、无权威来源，且被「显式持续 ≥2 秒」
  的实现范例证伪。已改为法规原文落位（起始画面 + 播放周边）+ 发布侧三处，
  并在 references/06-research-2026-11.md 标注存疑清单。
  实测本技能 6 支成片的 format_tags **只有 encoder，零 AIGC 标识** —— 是硬缺口。
  对方项目只做「检测」，不写入；本模块补上**写入**能力。
  ★ v6.0.0 另一处治根：AIGC 判定收口到 has_aigc_tag()，stamp 加幂等，
  并由 produce.py 的 deliver 步强制调用（此前能力存在但主链零调用，见 P92）。

三个子命令：
    python scripts/compliance.py check <task> <ep> <gencfg.json>
        预检：营销词/绝对化用语/导流词/敏感词、封面主标长度、AI 标识清单、
              音色克隆授权提醒、原片双层字幕提示（P87）。不改任何文件。
    python scripts/compliance.py provenance <task> <ep>
        生成 _plan/provenance-<ep>.json：各中间件 sha256 + 参数快照 + 模型版本。
    python scripts/compliance.py stamp <video.mp4> --task <task> --ep <ep> [--apply]
        用 ffmpeg -c copy 写 AIGC 隐式元数据（默认 dry-run，必须 --apply 才写）。
        写临时文件成功后原子替换，失败不留半成品（F10）。

判据数值全部来自 config/engine.json → compliance_policy，**不在本文件写第二份**。
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
SCRIPTS = ROOT / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import contracts                     # noqa: E402  配置契约唯一入口

CFG, _SRC = contracts.load()
POL = CFG.get('compliance_policy')
if not POL:
    raise RuntimeError('[compliance] compliance_policy 段缺失 —— 读不到 ≠ 用内置默认值')
FF = CFG['runtime']['ffmpeg']
FFPROBE = CFG['runtime']['ffprobe']


# ── AIGC 隐式标识：唯一判据（v6.0.0 收口，坑典 P92）──────────────────
# ★ 为什么必须抽函数：这条判定此前散落 3 处 ——
#   stamp 写后复核 / eval_skill.e18 / 交付前核对，各拼一遍 tags 判断。
#   三份口径一旦漂移，就会出现「以为加了标识其实没加」却全链显示通过（P91 同族：
#   「实切 X」与「度量 X」必须同一真相源）。
#   → 所有调用方一律 has_aigc_tag() / read_aigc_tags()，禁止再自己拼 tags 判断。
AIGC_MARK = 'AIGC'
AIGC_TAG_KEYS = ('comment', 'description', 'title', 'AIGC', 'aigc')


def aigc_values(tags):
    """→ {key: value}：format_tags 里带 AIGC 标记的标准键。"""
    if not isinstance(tags, dict):
        return {}
    return {k: v for k, v in tags.items()
            if k in AIGC_TAG_KEYS and AIGC_MARK in str(v)}


def read_aigc_tags(video):
    """→ (tags, err)。ffprobe 读不到 = (None, err)，**未知 ≠ 没有**（P84）。"""
    try:
        r = subprocess.run([FFPROBE, '-v', 'error', '-show_entries',
                            'format_tags', '-of', 'json', str(video)],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=60)
        if r.returncode != 0:
            return None, f'ffprobe 失败 rc={r.returncode}: {(r.stderr or "")[-160:]}'
        tags = (json.loads(r.stdout or '{}').get('format') or {}).get('tags') or {}
        return tags, None
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'


def has_aigc_tag(video):
    """→ (ok: bool, detail: str)：成片是否含有效 AIGC 隐式标识（唯一判据）。

    读不到返回 (False, 'unknown:...') —— 与「确实没有」严格区分，
    上层据此走保守路径，不能把读不到当成通过。
    """
    tags, err = read_aigc_tags(video)
    if err:
        return False, f'unknown:{err}'
    hits = aigc_values(tags)
    return bool(hits), ('已标识（' + ', '.join(sorted(hits)) + '）' if hits else '未标识')


def _patterns():
    return {
        'marketing': re.compile('|'.join(POL['marketing_words'])),
        'absolute': re.compile('|'.join(POL['absolute_ad_patterns'])),
        'drain': re.compile('|'.join(POL['platform_drain_words'])),
        'sensitive': re.compile('|'.join(POL['sensitive_words'])),
    }


def check(task, ep, gencfg):
    """只读预检 → 结构化报告（对标对方 level/category/description/recommendation）。"""
    p = Path(gencfg)
    try:
        cfg = json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:
        return {'ok': False, 'findings': [{'level': 'P0', 'category': '输入',
                'description': f'gencfg 读不到：{type(e).__name__}', 'recommendation': '补齐文件'}]}
    pat = _patterns()
    text = ' '.join(str(cfg.get(k, '')) for k in
                    ('title', 'cover_main', 'cover_sub', 'cover_lead'))
    # ★ 2026-11-09 修误报：「第一集 / 第 3 季 / 第 2 部」是集数角标，不是广告法
    #   绝对化用语。实测 MW1 因「第一集」被误报 P1 —— 判据必须能区分语境。
    text = re.sub(r'第[一二三四五六七八九十百零\d]+[集季部章回天年月日次步名轮届]', '', text)
    findings = []

    def add(level, category, desc, rec):
        findings.append({'level': level, 'category': category,
                         'description': desc, 'recommendation': rec})

    for key, level, label in (('marketing', 'P1', '营销字样'),
                              ('absolute', 'P1', '广告法绝对化用语'),
                              ('drain', 'P1', '平台导流词'),
                              ('sensitive', 'P0', '内容安全敏感词')):
        hit = pat[key].findall(text)
        if hit:
            add(level, label, f'命中 {sorted(set(hit))}',
                '换成具体剧情信息，不用夸张/绝对化/导流表述')
    main = str(cfg.get('cover_main', ''))
    if len(main) > int(POL['cover_main_max_chars']):
        add('P1', '封面主标', f'{len(main)} 字 > {POL["cover_main_max_chars"]} 字，会在首帧被裁',
            '压缩到上限内')
    # AI 标识（法规落位 + 发布侧 + 隐式元数据）；口径见 06-research-2026-11.md
    labels = POL.get('publish_labels') or {}
    add('P2', 'AI 显式标识',
        '视频落位 ' + ' / '.join(POL['ai_label_positions'])
        + '；发布侧 ' + ' / '.join(POL.get('publish_side_positions', []))
        + ' 标注「' + POL['ai_label'] + '」',
        '跑 publish_kit.py 生成可直接复制的物料包（引擎不自动发布，F7；'
        '物料含强标参数提示，标注存疑）')
    add('P2', '发布页必选标签',
        '发布须在 ' + str(len(labels.get('options', []))) + ' 类标签中选一项'
        + '（推荐「' + str(labels.get('recommended', '')) + '」）',
        '平台侧勾选由人执行；6 类标签来自网信中国转述，以平台实际标签为准')
    add('P2', 'AI 隐式标识',
        '需在视频元数据标准键写入 AIGC 字段（生成主体/版本/作品编号/AI参与比例/生成时间）',
        'produce.py 的 deliver 步已强制调用 stamp --apply（幂等 + 写后复核）；'
        '单独补标跑 compliance.py stamp ... --apply')
    if POL.get('require_voice_authorization_note'):
        add('P2', '音色授权', '主角音色克隆使用原片人声，须确认素材授权范围',
            '商用前向权利方核实声音授权；无授权时改用题材预设音色')
    add('P2', '双层字幕', '原片烧录字幕不会被自动擦除，ASS 底栏只是压盖（P87）',
        '交付前抽帧检查；必要时调整字幕位置或裁切画面')
    blocking = [f for f in findings if f['level'] == 'P0']
    return {'ok': not blocking, 'task': task, 'ep': ep,
            'gencfg': str(p), 'ai_label': POL['ai_label'],
            'findings': findings,
            'summary': {'P0': len(blocking),
                        'P1': sum(f['level'] == 'P1' for f in findings),
                        'P2': sum(f['level'] == 'P2' for f in findings)}}


def provenance(task, ep):
    """生成链路留痕：中间件哈希 + 参数快照（对标平台「全流程生产过程截图」要求）。"""
    t = Path(task)
    plan = t / '_plan'
    names = [f'spec-{ep}.json', f'ev-{ep}.json', f'plan-{ep}.json',
             f'plan-{ep}-v37.json', f'state-{ep}.json', f'precheck-{ep}.json']
    files = []
    for n in names:
        f = plan / n
        if f.is_file():
            files.append({'file': n,
                          'sha256': hashlib.sha256(f.read_bytes()).hexdigest()[:16],
                          'bytes': f.stat().st_size})
    qc = list((t / 'out').glob(f'*/*.qc.json'))
    for f in qc[:1]:
        files.append({'file': f.name,
                      'sha256': hashlib.sha256(f.read_bytes()).hexdigest()[:16],
                      'bytes': f.stat().st_size})
    d = {
        'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
        'task': str(t), 'ep': str(ep),
        'engine_version': CFG.get('engine_version'),
        'skill_version': re.search(r'skill_version:\s*([\d.]+)',
                                   (ROOT / 'SKILL.md').read_text(encoding='utf-8')).group(1),
        'ai_participation': POL['ai_participation'],
        'models': {'asr': (CFG.get('asr', {}).get('primary') or {}).get('engine'),
                   'tts': CFG.get('tts', {}).get('engine'),
                   'vlm': Path(CFG['runtime']['vlm_model']).name},
        'duration_bounds': CFG['duration_policy']['bounds'],
        'dedupe_max_shot_overlap': CFG['dedupe_policy']['max_shot_overlap_ratio'],
        'files': files,
        'note': '本清单随成片留存，用于权属/生成过程追溯；不含素材内容本身',
    }
    out = plan / f'provenance-{ep}.json'
    out.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    return out, d


def stamp(video, task, ep, apply=False):
    """写入 AIGC 隐式元数据（ffmpeg -c copy，不重编码，不碰音画）。

    ★ v6.0.0（2026-11-13）两处治根：
      ① **幂等**：已含有效标识时直接返回 already_stamped，不重复写、不叠加
         `.pre-aigc` 备份。此前 produce.py 每次续跑交付都会重写一遍标识，
         备份文件逐轮累积 —— 幂等续跑的前提是「跳过时状态不变」（P84 同族）。
      ② **判据收口**：写后复核改调 `has_aigc_tag(tmp)`，不再自己拼 tags 判断，
         与 eval_skill.e18 / media_qc / publish_kit 共用同一真相源（P92）。
    """
    v = Path(video)
    if not v.is_file():
        return {'ok': False, 'reason': f'视频不存在：{v}'}
    # 幂等：先读实测状态；「读不到」不阻断写入，只是如实记录
    already, current = has_aigc_tag(v)
    if apply and already:
        return {'ok': True, 'already_stamped': True, 'video': str(v),
                'current': current, 'note': '已含有效 AIGC 隐式标识，跳过重复写入'}
    tmp = v.with_name(v.stem + '.aigc.tmp' + v.suffix)
    # ★ 2026-11-11 实测（P91）：MP4/mov muxer **只接受标准 metadata 键**，
    #   任意键（AIGC / AIGC_WorkId…）会被 ffmpeg **静默丢弃且不报错**。
    #   → 主标识写 comment，生成信息写 description；两者 ffprobe 可读。
    work_id = f'{Path(task).name}-ep{ep}'
    gen_at = time.strftime('%Y-%m-%dT%H:%M:%S')
    tags = {
        'comment': (f"AIGC: {POL['ai_label']} | 生成主体="
                    f"{CFG.get('engine', '剧里剧外-视频解说引擎')} | "
                    f"版本={CFG.get('engine_version', '')} | 作品编号={work_id} | "
                    f"AI参与={POL['ai_participation']} | 生成时间={gen_at}"),
        'description': (f"AIGC 隐式标识；{POL['ai_participation']}；"
                        f"作品编号={work_id}；生成时间={gen_at}"),
    }
    cmd = [FF, '-v', 'error', '-y', '-i', str(v), '-c', 'copy', '-map', '0']
    for k, val in tags.items():
        cmd += ['-metadata', f'{k}={val}']
    cmd += ['-movflags', '+faststart', str(tmp)]
    if not apply:
        return {'ok': True, 'dry_run': True, 'video': str(v),
                'current': current, 'tags': tags,
                'command_preview': ' '.join(cmd[:8]) + ' …'}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=600)
        if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 10000:
            tmp.unlink(missing_ok=True)
            return {'ok': False, 'reason': f'ffmpeg 失败 rc={r.returncode}',
                    'stderr': (r.stderr or '')[-400:]}
        # 写后复核走唯一判据（F10：不合格不留半成品、不碰原片）
        marked, why = has_aigc_tag(tmp)
        if not marked:
            tmp.unlink(missing_ok=True)
            return {'ok': False,
                    'reason': f'写后复核未通过（{why}），拒绝替换原片',
                    'detail': '只认 comment/description/title 标准键，见 P91'}
        backup = v.with_name(v.stem + '.pre-aigc' + v.suffix)
        v.replace(backup)
        tmp.replace(v)
        return {'ok': True, 'video': str(v), 'backup': str(backup),
                'tags': tags, 'note': '原片已留 .pre-aigc 备份；确认无误后可自行清理'}
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return {'ok': False, 'reason': f'{type(e).__name__}: {e}'}


def main():
    ap = argparse.ArgumentParser(description='合规预检 / AI 标识 / 生成溯源')
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('check'); c.add_argument('task'); c.add_argument('ep'); c.add_argument('gencfg')
    p = sub.add_parser('provenance'); p.add_argument('task'); p.add_argument('ep')
    s = sub.add_parser('stamp'); s.add_argument('video')
    s.add_argument('--task', required=True); s.add_argument('--ep', required=True)
    s.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    if a.cmd == 'check':
        d = check(a.task, a.ep, a.gencfg)
        print(json.dumps(d, ensure_ascii=False, indent=1))
        return 0 if d['ok'] else 1
    if a.cmd == 'provenance':
        out, d = provenance(a.task, a.ep)
        print(json.dumps({'written': str(out), 'files': len(d['files']),
                          'models': d['models']}, ensure_ascii=False, indent=1))
        return 0
    d = stamp(a.video, a.task, a.ep, a.apply)
    print(json.dumps(d, ensure_ascii=False, indent=1))
    return 0 if d['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
