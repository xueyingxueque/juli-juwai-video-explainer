# references/ 文档索引

> **用途**：按「什么时候该读」分流，覆盖技能内 16 份 references 与 2 份 docs。
> 技能主入口是 [`../SKILL.md`](../SKILL.md) —— **先读 §0 Agent 快速上手，只读那一节就能开工**。

---

## 🎯 我现在该读哪篇？

| 你的处境 | 读这个 |
|---|---|
| **第一次用 / 要出一集解说** | [`../SKILL.md`](../SKILL.md) **§0**（决策表 + 一条命令 + 清单格式） |
| 要确认参数口径（时长/去重/音色） | [`../SKILL.md`](../SKILL.md) §3 ＋ [`../config/engine.json`](../config/engine.json)（数值正典） |
| 要写解说稿 / 选画面 | `content-first.md` ★★ ＋ `directing.md` ＋ `narrative-craft.md` ＋ `real-samples.md` |
| 解说听起来「乱」 | `content-first.md` ★★★（**先怀疑内容层**）＋ 坑典 **P18**，技术层再查 **P77** |
| 第 2 集和上一集太像 | [`../SKILL.md`](../SKILL.md) F13 / §2.4 ＋ 坑典 **P85/P96** ＋ `episode_dedupe.py` / `visual_dedupe.py` |
| 改代码 / 调参数 | [`../docs/AGENT对接.md`](../docs/AGENT对接.md)（C1–C16 ＋ 常量真值表） |
| 想评估技能是否健康 | `evals/evals.json` + `scripts/eval_skill.py`（28 条离线断言） |
| 想查内容决策/合规/选镜 | [`01-content-craft.md`](01-content-craft.md) / [`02-compliance.md`](02-compliance.md) / [`04-shot-selection.md`](04-shot-selection.md) |
| 查 AI 标识 / 发布物料 / 法规依据 | [`02-compliance.md`](02-compliance.md) ＋ [`06-research-2026-11.md`](06-research-2026-11.md)（决策卡与存疑清单） |
| 想看完整文件树 | [`05-file-map.md`](05-file-map.md) |
| 踩坑了不知道为啥 | `00-pitfalls.md`（**97 条，P01–P97 连续编号**，带顶部速查索引与分类导航） |
| 出片了没声音 / 串画面 | 坑典 **P01**（音轨截断）/ **P08**（串画面）/ **P77**（混音乱）/ **P78**（峰值误杀） |
| 查环境、平台参数 | [`../config/engine.json`](../config/engine.json)（唯一正典）＋ `platform.md` ＋ `runtime.md` |
| 对外调用这个技能 | `agent-integration.md`（调用契约）／`engine-integration.md`（引擎边界） |
| 跑批守护/自愈 | [`../SKILL.md`](../SKILL.md) §0.4 ＋ 坑典 **P83/P84** |
| 交付前客观复核 / 发布物料 | `scripts/media_qc.py` / `scripts/visual_dedupe.py` / `scripts/publish_kit.py` ＋ [`06-research-2026-11.md`](06-research-2026-11.md) |

---

## 📄 活跃文档（按用途分层）

### 方法论（长期有效）

| 文件 | 用途 |
|---|---|
| `content-first.md` | ★★★ 内容优先三铁律：先读全剧字幕；时间点来自 VLM 数据；出片前抽帧验证 |
| `directing.md` | 解说方法论正典：事件链、段落任务层、镜头重排、语义去重门 |
| `narrative-craft.md` | 爆款写法正典：钩子框架、POV、衔接、加减法 |
| `real-samples.md` | 真实爆款稿样本，样本六的段落任务链是核心资产 |
| `platform.md` | 平台参数**解释**（⚠️ 数值以 engine.json 为准，本页只解释为什么这么定） |
| `runtime.md` | 本机运行环境、模型路径、状态/退出码/错误码 |

### 工程与契约

| 文件 | 用途 |
|---|---|
| `00-pitfalls.md` | ★★★ **97 条真坑**（P01–P97）：现象 → 真因 → 修法 → 判据 |
| `01-content-craft.md` | 内容决策层（成功标准/失败信号/节奏预算/Kill List） |
| `02-compliance.md` | 合规与 AI 标识、溯源、已知限制 |
| `03-evaluation.md` | 评估契约、delta 口径、学习闭环 |
| `04-shot-selection.md` | 选镜决策层与三信号 |
| `05-file-map.md` | 完整文件树 |
| `06-research-2026-11.md` | ★ v6.0.0 全网调研正典：法规 / 去重 / 擦除 / QC / 内容结构 ＋ **决策卡总表 ＋ 存疑清单** |
| `agent-integration.md` | 其他 Agent 调用本引擎的约定（不扩大用户任务授权） |
| `engine-integration.md` | 三层引擎边界：上游配音 / 下游包装 / 旁路 ffmpeg 审查 |
| `../docs/AGENT对接.md` | ★ 改代码必读：C1–C16 硬约束 ＋ 常量真值表 |
| `../docs/说明文档.md` | 给人看的分步教程 |

### 历史区（**不代表当前能力**）

`references/_历史/` 收录 9 份历史复盘与规划文档：
`fix-architecture.md` / `rewrite-from-scratch.md` / `audit-v2.5.md` /
`material-audit.md` / `series-production.md` / `original-audio-design.md` /
`evolution-roadmap.md` / `material-precheck-report.json` / `preflight-report.json`。

> 它们的价值是**追溯当时为什么这么改**；真判据已沉淀进 `00-pitfalls.md` 与 SKILL.md。
> 归档文档只作历史追溯，**不代表当前能力**；
> 已落地的是跨成片复用层 `episode_dedupe.py`（F13）与可选帧变换 `anti_dup.py`，
> v6.0.0 另补成片画面实证层 `visual_dedupe.py`（P96）。

---


---

## 🔑 四条最该记住的教训

### 1. 「乱」先怀疑内容层
用户说解说乱、剪辑乱、画面乱 —— 先查剧情对不对、画面选对不对，不是先查音轨。技术层修再多遍，救不了内容层的错误。

### 2. 同一类 bug 第 3 次出现 = 停下来重写
判据：**补丁 =「这次好了」，重写 =「下次不可能再坏」**。
多个文件里出现同一份逻辑副本 → 改一处必漏另一处 → 抽成单一真相源。
本技能的 `policy.py` / `pipeline_state.py` / `plan_est.py` 都是这么来的。

### 3. 校验通过 ≠ 画面正确
`build.py` 校验只管音频；画面必须抽帧肉眼确认。
> ⚠️ 抽帧时 `-ss` 必须放在 `-i` **之前**，否则跳帧拿到错误画面。
> v6.0.0 补上 `media_qc.py`（黑帧/冻结/静音）与 `visual_dedupe.py`（成片画面实证），
> 但**客观检查仍不等于人工审片**，见 06 号文档决策卡。

### 4. 「这件事还需要被做吗」是闸门的第二半
告警/校验的成立条件 = 状态异常 **×** 仍有待办。
清单全出片 = 正常待命，不是停滞；永久误报会把真停滞淹没。
另外：**读不到（`None`）≠ 没有（`[]`）** —— 读不到走保守路径。
> 推论（P92）：**能力存在 ≠ 链上有**。stamp 能力早就有，主链零调用 = 纸面合规；
> 任何新增能力都要有断言守着「确实被调用了」。
