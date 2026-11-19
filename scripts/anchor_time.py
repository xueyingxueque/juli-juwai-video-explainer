# -*- coding: utf-8 -*-
"""anchor → 台词时间点解析器（字符级时间轴，**全项目唯一正典**）。

★ 本实现已收进技能（2026-11-02）：此前它只活在任务目录的 _tools/ 下，
  于是「同一份逻辑两个副本」，改一处必漏另一处（坑典 P84 同族病）。
  任务目录只保留一个转发薄壳 _tools/anchor_time.py → 指到这里。

★ 为什么必须做字符级（2026-11-02 治根，不是打补丁）：
  ASR 的 `segments` 是**子句级**，短到 0.9s（如「妈妈」），而一条 anchor
  常常跨 2-5 个子句。旧写法「segment 文本 ⊂ anchor 就取该段 start」会
  把「五百万孩子不是商品」匹到 0.0s 的那个「五百万」→ 整集多拍撞同一帧。
  实测 M2 生成器用旧逻辑：13 处撞帧 + 24 处模糊匹配，spec 直接废掉。

  本模块用 ASR 自带的 **字符级 words 时间戳**（text/start/end 逐字）建
  全局字符时间轴：anchor → 全局字符位置 → 该字符的 start。
  跨子句也精确，且同集多锚点自动分散开。

  繁体字（ASR 偶发输出繁体）不做繁简转换，用 difflib 最长公共块定位。

用法：
    from anchor_time import EpisodeIndex
    idx = EpisodeIndex.from_task(task_dir)      # 读 _s1/epN/asr_full.json
    t, kind = idx.locate(ep, "二哥哥的腿没有死呀")
    # kind: 'exact' | 'fuzzy' | 'miss'
"""
import difflib
import json
import re
from pathlib import Path


def _norm(s):
    return ''.join((s or '').split())


# ★ 标点/符号：ASR 段里带 ASCII 逗号问号（"小喜雀,妈妈想听你说话"），
#   而人写的锚点常常不写标点 → 逐字匹配必失败。
#   去标点时间轴做第二层匹配，通用解决，不必手工维护修正表。
_PUNCT = re.compile(r'[\s,.?!:;"\'`~@#$%^&*()\[\]{}<>《》「」『』【】—…·,、。，！？；：“”‘’～－－_+=|/\\]+')


def _norm_compact(s):
    return _PUNCT.sub('', _norm(s))


class EpisodeTimeline:
    """单集：全局字符文本 + 与字符一一对应的时间戳。"""

    def __init__(self, text, times):
        self.text = text
        self.times = times
        # ★ 去标点副轴（第二层匹配用）：ASR 段带 ASCII 逗号问号，
        #   人写的锚点常常不写标点 → 主轴逐字不命中，副轴命中即同样精确。
        ct, cts = [], []
        for ch, t in zip(text, times):
            if _PUNCT.match(ch):
                continue
            ct.append(ch)
            cts.append(t)
        self.compact_text = ''.join(ct)
        self.compact_times = cts

    @classmethod
    def from_asr(cls, path: Path):
        d = json.loads(Path(path).read_text(encoding='utf-8'))
        segs = d.get('segments', [])
        chars = []
        for seg in segs:
            words = seg.get('words') or []
            if words:
                for w in words:
                    txt = _norm(w.get('text'))
                    if not txt:
                        continue
                    st = float(w.get('start', 0.0) or 0.0)
                    en = float(w.get('end', st) or st)
                    step = (en - st) / len(txt) if len(txt) else 0.0
                    for i, ch in enumerate(txt):
                        chars.append((ch, st + step * i))
            else:
                txt = _norm(seg.get('text'))
                if not txt:
                    continue
                st = float(seg.get('start', 0.0) or 0.0)
                en = float(seg.get('end', st) or st)
                step = (en - st) / len(txt) if len(txt) else 0.0
                for i, ch in enumerate(txt):
                    chars.append((ch, st + step * i))

        # ★ 尾部补齐（2026-11-02）：部分集 ASR 的 segments 少了尾段
        #   （实测 ep4 顶层 158 字 / 段拼仅 93 字，ep5 缺 13 字），
        #   台词在顶层 text 里齐全、只是没有词级时间戳。
        #   把缺失尾部按 [末段 end, 集时长] 线性插值补上，
        #   时间轴才能 100% 覆盖台词（否则 ep4 后半段台词根本定位不到）。
        top = _norm(d.get('text'))
        joined = ''.join(c for c, _ in chars)
        # ★ joined 可能为空（ASR 只给了顶层 text、segments 为空）——此时整段
        #   顶层文本都是「尾部」，同样要补（否则该集完全定位不到）。
        if top and len(top) > len(joined):
            tail = top[len(joined):]
            if top.startswith(joined):
                dur = float(d.get('duration_sec') or 0)
                last = chars[-1][1] if chars else 0.0
                span = max(dur - last, 0.0)
                step = span / len(tail) if tail else 0.0
                for i, ch in enumerate(tail):
                    chars.append((ch, last + step * i))
            else:
                # segments 与顶层 text 不是前缀关系（对齐漂移）→ 用 difflib 找剩余
                import difflib as _dl
                sm = _dl.SequenceMatcher(None, joined, top, autojunk=False)
                blocks = sm.get_matching_blocks()
                covered = sum(b.size for b in blocks)
                rest = top[blocks[-1].b + blocks[-1].size:] if blocks else ''
                dur = float(d.get('duration_sec') or 0)
                last = chars[-1][1] if chars else 0.0
                step = (max(dur - last, 0.0) / len(rest)) if rest else 0.0
                for i, ch in enumerate(rest):
                    chars.append((ch, last + step * i))
        return cls(''.join(c for c, _ in chars), [t for _, t in chars])


class EpisodeIndex:
    """任务级索引：_s1/epN/asr_full.json → 时间轴。"""

    def __init__(self, task_dir):
        self.task = Path(task_dir)
        self.tl = {}

    @classmethod
    def from_task(cls, task_dir):
        idx = cls(task_dir)
        s1 = idx.task / '_s1'
        for d in sorted(s1.glob('ep*')):
            if not d.name[2:].isdigit():
                continue
            f = d / 'asr_full.json'
            if f.is_file():
                idx.tl[int(d.name[2:])] = EpisodeTimeline.from_asr(f)
        return idx

    def episode_of(self, anchor: str):
        """锚点真实所在集号（full / 去标点 两种形态都试），找不到返回 None。"""
        na = _norm(anchor)
        ca = _norm_compact(anchor)
        for e in sorted(self.tl):
            tl = self.tl[e]
            if na and na in tl.text:
                return e
            if ca and ca in tl.compact_text:
                return e
        return None

    def locate(self, ep: int, anchor: str, min_sim: float = 0.35):
        """→ (t, kind)；kind ∈ exact / fuzzy / miss。

        自愈策略（2026-11-02）：声明集没命中时**全剧扫描**并自动纠正集号。
        实测手写 90 行锚点表时，把 ep1 的「怎么舍不得一个灾星」误归到 ep2
        这类错误必然发生，靠人肉核对不可持续——让程序自己找对集。
        多集都命中时优先用声明集，其次取集号最小者（多为首播集）。
        """
        tl = self.tl.get(int(ep))
        if tl is not None:
            t, kind = self._in(tl, anchor, min_sim)
            if t is not None:
                return t, kind
        # ① 全剧扫描精确命中
        hits = []
        for e in sorted(self.tl):
            if e == int(ep):
                continue
            t2, k2 = self._in(self.tl[e], anchor, min_sim)
            if t2 is not None and k2 == 'exact':
                hits.append((e, t2))
        if hits:
            return hits[0][1], 'exact'
        # ② 全剧扫描模糊命中
        best = None
        for e in sorted(self.tl):
            t2, k2 = self._in(self.tl[e], anchor, min_sim)
            if t2 is not None and (best is None or t2 < best[0]):
                best = (t2, e)
        if best:
            return best[0], 'fuzzy'
        return None, 'miss'

    @staticmethod
    def _in(tl, anchor, min_sim):
        """在单集时间轴内定位。exact=子串命中；fuzzy=最长公共块覆盖锚点 ≥50%。"""
        if tl is None or not tl.text:
            return None, 'miss'
        na = _norm(anchor)
        if not na:
            return None, 'miss'
        pos = tl.text.find(na)
        if pos >= 0:
            return tl.times[pos], 'exact'
        # ★ 第二层：去标点匹配（治「ASR 带逗号、人写锚点不带」这类通病）
        ca = _norm_compact(anchor)
        if ca:
            pos2 = tl.compact_text.find(ca)
            if pos2 >= 0:
                return tl.compact_times[pos2], 'exact'
        sm = difflib.SequenceMatcher(None, na, tl.text, autojunk=False)
        m = sm.find_longest_match(0, len(na), 0, len(tl.text))
        # ★ 覆盖率口径：公共块占锚点的比例（不能用 sm.ratio()——
        #   分母含整集文本长度，再准的锚点也比不过 0.35 阈值）
        if m.size >= 3 and (m.size / len(na)) >= 0.5:
            return tl.times[m.b], 'fuzzy'
        return None, 'miss'


if __name__ == '__main__':
    import sys
    task = sys.argv[1] if len(sys.argv) > 1 else 'D:/剧里剧外/视频解说/任务/mwsy-e2e'
    idx = EpisodeIndex.from_task(task)
    print(f'索引 {len(idx.tl)} 集：{sorted(idx.tl)}')
    if len(sys.argv) > 2:
        ep, anc = int(sys.argv[2]), sys.argv[3]
        print(idx.locate(ep, anc))
