"""张力判定回归测试 —— 防止改提示词时又犯已修的错。

★ 为什么必须有这个文件：
我在 `tension_llm` 的提示词上迭代了 3 版，每版都会把某些案例判错：
  v1（初版）  数数判 0.70（误判为「认知冲突」）
  v2（修误判） 数数修好了，但真高潮被压到 0.60（保守规则用力过猛）
  v3（平衡）  8/8 全对 ← 当前版本

以后任何改动都必须跑这个测试：
    python test_tension.py

判据：低分案例必须真的低（防误判），高分案例必须真的高（防压制）。
"""
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'sd', Path(__file__).resolve().parent / 'series_decompose.py')
sd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sd)

# (名称, 画面, 台词, 说话人, 期望最低档)
# 期望用「最低档」而非精确分值——判定有主观性，卡档位更稳。
CASES = [
    # ---- 必须低的：日常/无冲击（这些是 v1 误判的根源）----
    ('数数报数', '小女孩举起三根手指，嘴里说着"六"，手里拿着一本破旧的书。',
     '六', '画面内女性', 'L1'),
    ('动物卖萌', '小女孩撅着嘴，旁边有一只戴护目镜的狗和一只鸭子。',
     '知道了知道了', '画面内女性', 'L1'),
    ('日常行走', '一名穿古装的女性在院中行走，画面下方有字幕显示对话内容。',
     '你爹出去大半年不着家了', '画外音', 'L1'),
    ('静态站立', '一位老人站在山门前望着远方，背影静止。',
     '', '无', 'L1'),

    # ---- 必须高的：明确事件（v2 曾把这些压低）----
    ('屠万鬼谷', '画面内男性表情震惊，字幕显示"一个人屠了整个万鬼谷"。',
     '一个人屠了整个万鬼谷', '画外音', 'L3'),
    ('老者惊恐', '一位老年男性面部特写，双眼圆睁，嘴巴大张，表情极度惊恐。',
     '', '画面内男性', 'L3'),
    ('黑熊扑来', '一只巨大的黑熊张开大嘴，露出锋利的牙齿，正向前扑来。',
     '', '无', 'L3'),
    ('持刀对峙', '一个小女孩背着装有白鹅的竹篮，双手握着一把大刀，表情严肃地盯着前方。',
     '', '画面内女性', 'L2'),
    ('握棍怒吼', '一个小女孩双手握棍，仰头怒吼，表情愤怒。',
     '恁拽啥拽', '画面内女性', 'L2'),
    ('异象发光', '一只狗仰头嚎叫，背景中有发光的狼形轮廓。',
     '', '无', 'L2'),
]


def run() -> int:
    print('=' * 56)
    print('张力判定回归测试（当前提示词版本）')
    print('=' * 56)
    fails = []
    for name, desc, dia, spk, min_lv in CASES:
        r = sd.tension_llm(desc, dia, spk)
        ok = r['level'] >= int(min_lv[1])
        mark = 'PASS' if ok else 'FAIL'
        print(f"  [{mark}] {name.ljust(12)} L{r['level']} {r['score']:.2f}  期望≥{min_lv}")
        if not ok:
            fails.append((name, r['level'], min_lv))
            print(f"         理由: {r.get('why', [''])[-1][:44]}")

    print('-' * 56)
    print(f'{len(CASES) - len(fails)}/{len(CASES)} 通过')
    if fails:
        print('\n★ 回归失败 —— 说明提示词改动破坏了已修的判定：')
        for n, got, exp in fails:
            print(f'   {n}: 得 L{got}，期望 ≥{exp}')
        print('\n修复要点：低分案例要真的低，高分案例不能被"保守规则"压低。')
        return 1
    print('全部通过。改动提示词后请重跑此测试。')
    return 0


if __name__ == '__main__':
    sys.exit(run())