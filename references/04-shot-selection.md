# 04 · 选镜决策层

> **用途**：在 Agent 人工定稿之前，用本地客观信号先召回一批候选镜头，缩小搜索范围。
> 对标：Mixpeek「分解 → 多模态打分 → 区间合并 → 排序」、clipforge 的 RMS 能量选段、
> ZOOZ 的 CLIP + 经典 CV 信号（本技能**不引云端/新依赖**）。

## 目录
- [1. 四阶段与本技能的取舍](#1-四阶段与本技能的取舍)
- [2. 三个本地信号](#2-三个本地信号)
- [3. shot_rank.py 用法](#3-shot_rankpy-用法)
- [4. 输出契约](#4-输出契约)
- [5. 边界与已知不足](#5-边界与已知不足)

---

## 1. 四阶段与本技能的取舍

业界高光检测是：把视频分解成片段 → 每个片段用视觉/音频/文本多模态打分 →
相邻高分片段合并成 moment → 按片排序。关键洞见是
**没有单个模型「看完整片」**，智能来自「分段信号合成区间决策」。

本技能的取舍：

| 阶段 | 业界 | 本技能（本地） |
|---|---|---|
| 分解 | TransNet V2 / PySceneDetect | ffmpeg `silencedetect` 非静音区间 |
| 视觉打分 | CLIP / 帧嵌入 | ffmpeg `scene` 变化点（可选 `--scene`） |
| 文本打分 | transcript embedding | 已入库 ASR 台词覆盖计数 |
| 区间合并 | 可配置 tolerance | 固定窗口步长，不合并 |
| 排序 | 学习式 interestingness | 三信号加权（启发式、可解释） |

不引 PySceneDetect、CLIP、OpenCV 等新依赖，符合「低外部依赖、优先本机能力」的准入标准。

## 2. 三个本地信号

1. **非静音窗口**：`ffmpeg -af silencedetect=noise=-35dB:d=0.5` 解析
   `silence_start/end`，取补集。对白、冲突、争执往往落在连续有声区。
   ⚠️ 判静音用 `silencedetect`，**不用 `astats`**（本机实测 astats 把 -18dB 误读成 -84dB）。
2. **镜头切换点**：`-vf "select='gt(scene,0.35)',showinfo"` 抓 `pts_time`，
   候选点附近 3 秒内有切镜加分（动作密度代理）。默认关闭，加 `--scene` 才跑。
3. **台词密度**：若 `_s1/epN/asr_full.json` 已入库，统计覆盖该时间点的台词段数。

分数 = `0.35×离静音边界的稳定度 + 0.30×(附近有切镜) + min(0.35, 0.12×台词数)`。

## 3. shot_rank.py 用法

```bash
python scripts/shot_rank.py <素材目录> --ep 1 --top 20
python scripts/shot_rank.py <素材目录> --ep 1 --scene --out <候选.json>
```

集号发现复用技能正典 `plan_story.find_ep_file`
（兼容 `1.mp4` / `第N集.mp4` / `001_xxx-第N集.mp4`），不自写匹配规则。

## 4. 输出契约

`shot_candidates-ep<N>.json`：`source / ep / duration_sec / candidate_count /
method / caveat / candidates[]`。每个候选含 `ep / t / score / signals`
（非静音窗口、附近是否有切镜、覆盖台词数）。

可直接喂给 `anchor_time.py` 做时间点解析，或作为 Agent 写 spec 时的候选池。
**候选不是入选镜头**，最终仍须回 transcript 校验台词与情绪。

## 5. 边界与已知不足

- 全部是**代理指标**：能召回「对白密集 / 切镜频繁」的片段，
  但**不能判断表演张力、羞辱感、反击快感**——那是 `01-content-craft.md` 的人工判断。
- 音乐/无台词高能场面可能被漏掉；纯对白慢热段可能被高估。
- 未做区间合并，相邻候选可能指向同一场戏；定稿时应去重。
- 本层是**可选辅助**，不在 `produce.py` 十步主链内，不改变已验证的渲染流程。

## 与跨集零重叠的关系

本层负责「这一集里哪些镜头更值得看」；`episode_dedupe.py` 负责
「这一集不能复用前面集的镜头」。两者互补，不互相替代。
写多集时应先按源集号分段分配（事前），再在段内用本层召回候选（事中），
最后由去重闸门兜底（事后）—— 三层都不缺才不会出现 P85/P86 那类事故。
