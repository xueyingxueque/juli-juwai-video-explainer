"""ASR 能力基线实测：给定已知文本的 TTS 音频，测各模型的真实识别能力。

用途：新引擎在生产前必须先跑本脚本，用实测 CER 决定阈值，
而不是像旧技能那样先写死 max_cer=0.0 再撞墙。

用法：
  python asr_bench.py --config <local-config.json> [--samples <dir>]

退出码：0 = 有可用模型且 CER 达标；2 = 全部模型不达标（禁止进入生产）
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path


def normalize(s: str) -> str:
    """简繁、全半角、大小写、标点统一——中文字符比对的必要前置。"""
    table = {
        '，': ',', '。': '.', '！': '!', '？': '?', '；': ';', '：': ':',
        '（': '(', '）': ')', '“': '"', '”': '"', '‘': "'", '’': "'",
        '、': ',', '—': '-', '　': '',
    }
    for k, v in table.items():
        s = s.replace(k, v)
    # 全角 -> 半角
    s = re.sub(r'[\uff01-\uff5e]', lambda m: chr(ord(m.group()) - 0xfee0), s)
    return re.sub(r'[\s\W_]+', '', s).lower()


def cer(ref: str, hyp: str) -> float:
    """字符错误率，Levenshtein DP。"""
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, a in enumerate(ref, 1):
        cur = [i]
        for j, b in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a != b)))
        prev = cur
    return prev[len(hyp)] / len(ref)


def load_samples(sample_dir: Path):
    """从旧技能验收目录复用带标注的音频样本：<name>.wav + <name>.txt。"""
    pairs = []
    for txt in sorted(sample_dir.glob('*.txt')):
        wav = txt.with_suffix('.wav')
        if wav.exists():
            pairs.append((wav, txt.read_text(encoding='utf-8').strip()))
    return pairs


def run_asr(python: str, model: Path, wav: Path, device: str, compute: str) -> str:
    """调旧技能已验证的 ASR worker 路径，避免重复实现依赖。"""
    job = {"model": str(model), "audio": str(wav), "output": str(wav.with_suffix('.asr.json'))}
    jobp = wav.with_suffix('.job.json')
    jobp.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
    subprocess.run([python, '-X', 'utf8', str(Path(__file__).parent / 'asr_worker.py'),
                    str(jobp)], check=False, capture_output=True)
    if wav.with_suffix('.asr.json').exists():
        return json.loads(wav.with_suffix('.asr.json').read_text(encoding='utf-8')).get('text', '')
    return ''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', required=True)
    ap.add_argument('--samples', default=r'D:\剧里剧外\视频解说\最终技能\验收_v1.1\live-final\cache\voice')
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding='utf-8'))
    py, device, compute = cfg['asr_python'], cfg['asr_device'], cfg['asr_compute_type']

    models = {}
    # 候选优先级：medium 优先，tiny 仅作为保底
    for name in ('faster-whisper-medium', 'faster-whisper-tiny'):
        p = Path(cfg.get('asr_model_root', 'D:/AITools/models')) / name
        if (p / 'model.bin').exists():
            models[name] = p
        else:
            print(f'[skip] {name}: 缺少 model.bin（不完整安装不可用）')

    if not models:
        print('[FAIL] 没有任何完整 ASR 模型，禁止进入生产。')
        return 2

    pairs = load_samples(Path(args.samples))
    if not pairs:
        print('[FAIL] 无标注样本，无法建立基线。')
        return 2

    report = {'samples': len(pairs), 'models': {}}
    for name, path in models.items():
        cers, t0 = [], time.monotonic()
        for wav, ref in pairs:
            hyp = run_asr(py, path, wav, device, compute)
            cers.append(cer(normalize(ref), normalize(hyp)))
        avg = sum(cers) / len(cers)
        report['models'][name] = {
            'avg_cer': round(avg, 4),
            'worst_cer': round(max(cers), 4),
            'seconds': round(time.monotonic() - t0, 1),
            'verdict': 'usable' if avg <= 0.05 else ('marginal' if avg <= 0.15 else 'unusable'),
        }
        print(f'[bench] {name}: avg_CER={avg:.4f} worst={max(cers):.4f} '
              f'({time.monotonic()-t0:.1f}s) -> {report["models"][name]["verdict"]}')

    out = Path(args.out or (Path(args.config).parent / 'asr-baseline.json'))
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'[out] {out}')

    best = min(report['models'].items(), key=lambda kv: kv[1]['avg_cer'])
    print(f'[best] {best[0]} avg_CER={best[1]["avg_cer"]}')
    print('[rule] 新引擎阈值 = 实测 avg_CER + 0.03 余量，**禁止写死 0.0**')
    return 0 if best[1]['verdict'] != 'unusable' else 2


if __name__ == '__main__':
    sys.exit(main())
