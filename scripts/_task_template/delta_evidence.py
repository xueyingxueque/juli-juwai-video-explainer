"""增量证据合并 —— spec 改了几枪，就别把整本 evidence 重跑一遍。

背景：spec-1/spec-2 定稿后做过归因/时间点修订（见会话记录），
全量重跑 shot_evidence 要把没变的镜头再 VLM 读一遍（D2 约 25 分钟）。
本脚本只对「(ep, t) 发生变化」的镜头调 shot_evidence.resolve()，
其余镜头直接复用旧 ev-N.json 的结果（同一 shot 的 desc/subtitle 不会变）。

用法：
  python delta_evidence.py <spec-N.json> <旧ev-N.json> <新ev-N.json>
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import shot_evidence as SE  # noqa: E402


def key(s: dict):
    return (int(s['ep']), round(float(s['t']), 2))


def main() -> int:
    spec = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    old = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
    old_by_key = {key(s): s for s in old}

    todo, reused = [], []
    for s in spec['shots']:
        k = key(s)
        if k in old_by_key:
            reused.append(old_by_key[k])
        else:
            todo.append(s)

    print(f'共 {len(spec["shots"])} 枪：复用 {len(reused)}，新解析 {len(todo)}')
    if todo:
        for s in todo:
            print(f"  新解析 ep{s['ep']} t={s['t']}")
        fresh = SE.resolve(todo)
    else:
        fresh = []

    merged = reused + fresh
    # 按 spec 顺序重排，保证 ev 与 spec 逐枪对应
    order = {key(s): i for i, s in enumerate(spec['shots'])}
    merged.sort(key=lambda s: order[key(s)])
    # ★ 文案以 spec 为准（resolve 时拷进 ev 的 text 可能已过时：
    #   例如 ep2 36.0/39.5 两枪只换了文案没换时间点，复用旧条目会带回旧文案）
    for s, m in zip(spec['shots'], merged):
        m['text'] = s.get('text', '')
        m['raw'] = bool(s.get('raw'))

    out = Path(sys.argv[3])
    out.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'→ {out}（{len(merged)} 枪）')
    for r in merged:
        tag = '新' if key(r) not in old_by_key else '复'
        print(f"  [{tag}] ep{r['ep']:>3} t={r['frame_t']:6.2f} [{r['id']}] {r['note']}")
        if r.get('dialogue'):
            print(f"        sub={r['dialogue'][:40]}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
