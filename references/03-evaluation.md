# 03 · 评估与自我迭代

> **用途**：让技能能回答「自己现在健康吗」「比上一轮好了还是坏了」。
> 对标：`speclip-skills/skill-creator`（evals → grading → benchmark → feedback 循环）。

## 目录
- [1. 为什么需要](#1-为什么需要)
- [2. 产物契约](#2-产物契约)
- [3. 28 条断言](#3-28-条断言)
- [4. delta 口径（防造假信号）](#4-delta-口径防造假信号)
- [5. 学习闭环](#5-学习闭环)
- [6. 与 skill-creator 的差异](#6-与-skill-creator-的差异)

---

## 1. 为什么需要

进化前只有**产物校验**（`deliver_check` 查这一集齐没齐），
没有**技能自身校验**：改了代码靠人肉回归，坑典写到 86 条却没人验证编号连续、
引用是否失效、配置与代码是否漂移。项目准入标准要求「内置自我迭代/学习机制」，
本层就是它的落地。

## 2. 产物契约

| 文件 | 内容 | 谁写 |
|---|---|---|
| `evals/evals.json` | 用例声明（id / name / assertion / severity），**唯一声明源** | 人/Agent |
| `scripts/eval_skill.py` 的 `CHECKS` | 断言实现，**必须与 evals.json 同步** | 人/Agent |
| `evals/eval_report.json` | 本轮 pass_rate / 每条 evidence / 耗时 / delta_scope | eval_skill.py |
| `evals/eval_history.jsonl` | 每轮一行，含 `by_id` 映射（同口径对比用） | eval_skill.py |
| `evals/learning_report.json` | 已出片质检的反复风险、坑典候选、rework 建议、eval 趋势 | learn_from_runs.py |

对标 skill-creator 的 `eval_metadata.json / grading.json / benchmark.json`；
本技能全部断言客观可验证，因此**不需要 grader 子代理**，直接脚本判定。

## 3. 28 条断言

| 组 | id | 覆盖 |
|---|---|---|
| 时长数值 | 1–4 | bounds、严格区间、预估只预警、四平台一致（含故意漂移注入） |
| 跨集去重 | 5–8 | 零重叠、对称分母、首集放行、读不到≠没有 |
| 文档一致 | 9–11 | 坑典编号、活跃文档引用完整性、SKILL.md <500 行 |
| 架构 | 12–14 | 旧口径清零、状态按实际产物、十步状态机 |
| 闸门 | 15–16 | TTS 敌对字符前移、推进器干跑无副作用 |
| 产物 | 17–18 | 单集时长合规、AI 隐式标识（`comment`/`description` 标准键，判据已收口 `has_aigc_tag()`） |
| 进化 | 19–20 | 配置契约审计、学习闭环产物 |
| 结构 | 21–22 | 对外契约一致、坑典结构可解析 |
| **v6 成片实证** | **23–28** | AIGC 判据收口 + 主链接入、`media_qc` 防呆、label 唯一化、法规落位、stamp 幂等、决策卡/存疑清单 |

`severity=hard` 全过才算 PASS；soft 失败只标注。
运行：`python scripts/eval_skill.py`（全跑）/ `--id 5`（单条排障）/ `--json`。

**纪律**：新增/删减断言必须**同时**改 `evals.json` 与 `CHECKS`，
否则脚本直接报错退出 —— 不允许静默漏跑一条。

## 4. delta 口径（防造假信号）

新增断言会改变 `total`，直接比较两轮 `pass_rate` 会凭空显示「进步」（P89）。
因此：

- `delta_vs_prev` 只在**两轮都存在的 id 集合**上计算；
- 显式输出 `added_ids / removed_ids / total_changed`；
- `newly_passed_ids` / `newly_failed_ids` 单列，**newly_failed 非空 = 本轮引入回归，优先修**；
- 漂移测试只允许在 `POLICY_CONFIG` 指向的**临时副本**上做，`config/engine.json` 正典零写入。

## 5. 学习闭环

```text
eval_skill.py 测 invariants
      ↓
learn_from_runs.py 扫全部 qc.json / 生产日志
      ↓  recurring_risks（≥2 次）
pitfall_candidates（带证据文件 + 建议判据）
      ↓  人/Agent 复核
写入 references/00-pitfalls.md → 下一轮新增断言覆盖它
```

首次运行实测：扫描 **51 集**，39 集命中「soft/semantic 未打分」，
39 集命中「soft/speech 未打分」，12 集命中「必须人工回听」——
这些风险长期只写在 `qc.json.risks`，没有机制推动，正是本层存在的理由。

`rework` 配置段（`max_rounds` / `strategies`）由此活化：
`learning_report.json.rework_suggestions` 把策略映射到具体集。
**只报告，不自动写坑典**，避免噪声污染正典。

**v6.0.0 往前一步**：`publish_kit.py` 的 `review_queue` 扫 `out/**/*.qc.json`，
把未打分软门拆成「可自动补测」（soft/semantic）与「必须人工回听」（soft/speech，
`narration_qc` 仍为 provisional / `allow_auto_pass=false`）两类行动队列。
**只推动复核，绝不把 provisional 校准伪装成自动放行**（SKILL.md §2.5）。

## 6. 与 skill-creator 的差异

| 维度 | skill-creator | 本技能 |
|---|---|---|
| 基线 | with_skill vs without_skill 双跑 | 无（改的是确定性脚本，用断言而非对照生成） |
| 评分 | grader 子代理 + 人工 viewer | 全部客观断言，脚本判定 |
| 反馈 | feedback.json 人工评论 | learning_report 的真实质检数据 |
| 环境 | 需子代理/浏览器 | 完全本地离线，不跑 GPU、不碰生产素材 |

不变的是核心思想：**先建评估再写长文档、断言客观可验证、
跨用例重复劳动沉淀成脚本、每轮留可对比的历史**。

> v6.0.0 新增断言先在**离线合成测试片**上做双向验证（真故障被抓 + 正常不误报），
> 再写进断言集 —— 只验「不再误报」可能把真告警一起关掉（P84 附注）。
