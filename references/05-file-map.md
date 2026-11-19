# 05 · 完整文件地图

> **用途**：查「某个脚本/文档负责什么」。SKILL.md 只留入口索引，完整清单一层深放这里。
> 渐进式披露：Agent 平时不读本文，只在排查或扩展时按需加载。

## 目录
- [1. 根目录](#1-根目录)
- [2. scripts/ 生产主链](#2-scripts-生产主链)
- [3. scripts/ v5–v6 新增能力层](#3-scripts-v5v6-新增能力层)
- [4. scripts/ 单集路径与工具](#4-scripts-单集路径与工具)
- [5. config / references / evals / docs](#5-config--references--evals--docs)
- [6. 归档区](#6-归档区)

---

## 1. 根目录

```
剧里剧外-视频解说引擎/
├── SKILL.md          ★ 唯一入口 + 版本号唯一源（<500 行）
├── config/           engine.json（数值正典）、narrative-patterns.json
├── scripts/          生产代码
├── references/       一层深知识文档 + 坑典
├── evals/           离线评估用例、报告、历史、学习报告
├── docs/             给人看的对接文档与教程
├── agents/           撰稿/质检 Agent 契约（规划期）
├── assets/comfy/     ComfyUI 基础工作流 c2a.json
└── examples/         plan 结构示意（历史短样例，不可直接跑）
```

## 2. scripts/ 生产主链

| 脚本 | 职责 |
|---|---|
| `produce.py` | ★★ 唯一生产总入口：10 步指纹状态机，清单驱动，中断续跑；**v6.0.0 起 deliver 步强制 AIGC 补标** |
| `auto_pilot.py` | ★ 调度器单次幂等推进（`--rounds N` 可连续多集），无常驻/锁/标志 |
| `pipeline_state.py` | ★ 状态唯一真相源：`is_produced` / `pending_entries` / `production_alive` / `ingest_ready` |
| `watch_downloads.py` | 下载就绪监控（7 条判据）+ 看板数据 + bootstrap |
| `policy.py` | ★ 时长/去重数值唯一读取器 + 四平台一致性断言 |
| `episode_dedupe.py` | ★ 跨成片内容去重硬闸（**spec 声明层**：镜头零重叠、文案 n-gram、冷开场/标题/二创图唯一） |
| `spec_precheck.py` | spec 阶段闸门：TTS 敌对字符 / gencfg 锚点 / CTA / 时长 / 跨集去重 |
| `deliver_check.py` | 成品完整性校验（中间件 + 成片 + 封面版 + 质检 + 网盘交付） |
| `build.py` | plan → 成片装配层（配音/原声/切画面/混音/烧字幕/校验，不过不产出） |
| `quality_gate.py` | 硬门/软门质检 + A/B/C 分档（时长判定统一走 policy） |
| `make_cover.py` | 封面 make/burn（官方海报优先 > 视频帧） |
| `deliver.sh` | 单文件一键交付（第 3 参是 **out 目录下的集目录名**，不是集号） |
| `bgm_select.py` / `gen_images.py` / `make_plan_v37.py` | BGM 选曲 / ComfyUI 出图 / 二创图与表情包精确锚点插入 |
| `extract_voice_ref.py` | 主角音色零样本克隆参考音 |
| `gpu_lifecycle.py` | GPU 串行（VLM/TTS/ComfyUI 互斥）+ 真推理探针 |
| `plan_est.py` | 时长预估唯一正典（仅预警，硬门认实测） |
| `new_task.py` / `sync_task_templates.py` | 任务脚手架 / 把正典模板同步到已建任务副本 |
| `anchor_time.py` / `size_budget.py` | 台词锚点→时间点 / 成片体积预算 |
| `anti_dup.py` | 可选帧变换后处理（防平台 pHash 查重，默认关、主链不调用）；**定位为鲁棒性自测，见 06 号文档 §2** |
| `recontinue.py` | build 局部续跑修复 |
| `watchdog.py` | 心跳看门狗（盯产出物 mtime + 有无待产集） |

## 3. scripts/ v5–v6 新增能力层（2026-11-09 / 2026-11-13）

| 脚本 | 层 | 详情 |
|---|---|---|
| `contracts.py` | ① 契约 | 三层配置合并 + `<key>_source` + `--audit` 比对代码常量 |
| `content_brief.py` | ② 内容决策 | 节奏预算 / 情绪链 / Kill List / 插片理由 / 质量清单 → `brief-<ep>.json` |
| `compliance.py` | ③ 合规溯源 | `check` 预检 / `stamp` 写 AIGC 元数据（**幂等 + 写后复核**）/ `provenance` 留痕；★ **AIGC 判据唯一真相源 `has_aigc_tag()`** |
| `eval_skill.py` | ④ 评估 | **28 条**离线断言 + benchmark + 同 id 集合 delta |
| `learn_from_runs.py` | ④ 学习 | 扫 qc.json 抽反复风险 → 坑典候选 + rework 建议 |
| `shot_rank.py` | ⑤ 选镜 | ffmpeg 非静音/切镜/台词密度三信号召回候选镜头 |
| `media_qc.py` | **⑥ 成片实证** | 黑帧 / 冻结 / 静音 / 音画时长 / 成片时长 / AIGC 实测；纯本地、默认只报告，`--strict` 才拦（P95） |
| `visual_dedupe.py` | **⑥ 成片实证** | 抽帧 dHash 跨集汉明距离，F13 的**成片级证据**（P96）；`--pilot` 清单驱动 |
| `publish_kit.py` | **⑥ 交付闭环** | 显式标识三处物料 + 6 类必选标签 + 隐式标识实测 + 软门复核队列；**不自动发布**（F7） |

> ⑥ 层的调研依据、采纳/不采纳决策与存疑清单见 `references/06-research-2026-11.md`。
> 三层去重分工：`episode_dedupe.py`（写稿声明硬闸）/ `anti_dup.py`（帧变换，默认关）/
> `visual_dedupe.py`（出片后画面实证报告），互补、不可互相替代。

## 4. scripts/ 单集路径与工具

`pipeline.py`（S1–S6 单集编排，验证场景）、`s1_ingest.py` / `s2_story.py` /
`s3_script.py` / `script_to_plan.py` / `plan_story.py`（单集上游）、
`asr.py` / `vlm.py` / `fetch_asr.py` / `asr_bench.py`（转写与视觉层）、
`preflight.py` / `material_precheck.py` / `tts_precheck.py`（预检门）、
`series_decompose.py` / `series_plan.py` / `lint_script.py` / `make_clipper.py`
（全剧分解与稿件工具）、`dialogue_map.py` / `burn_ass.py` /
`check_undefined.py` / `test_tension.py` / `ensure_vlm.ps1`（工具类）、
`_task_template/`（new_task 拷贝的三份正典模板）。

## 5. config / references / evals / docs

| 路径 | 用途 |
|---|---|
| `config/engine.json` | 数值正典；含 `compliance_policy` / `content_policy` / `duration_policy` / `dedupe_policy` / **`media_qc_policy` / `visual_dedupe_policy`** |
| `references/00-index.md` | 按场景分流的文档索引 |
| `references/00-pitfalls.md` | P01–P97 真坑速查（现象→真因→修法→判据），带顶部索引 |
| `references/01-content-craft.md` | 内容决策层（成功标准/失败信号/节奏预算/Kill List） |
| `references/02-compliance.md` | 合规与 AI 标识、溯源、已知限制 |
| `references/03-evaluation.md` | 评估契约、delta 口径、学习闭环（28 条断言） |
| `references/04-shot-selection.md` | 选镜决策层与三信号 |
| `references/05-file-map.md` | 本文件 |
| `references/06-research-2026-11.md` | ★ v6.0.0 全网调研正典：法规 / 去重 / 擦除 / QC / 内容结构 ＋ 决策卡总表 ＋ 存疑清单 |
| `evals/evals.json` / `eval_report.json` / `eval_history.jsonl` / `learning_report.json` | 评估与学习产物 |
| `docs/AGENT对接.md` | 改代码必读：C1–C16 硬约束与常量真值表 |
| `docs/说明文档.md` | 给人看的分步教程 |

## 6. 归档区

- `references/_历史/`：9 份历史复盘与规划文档，**不代表当前能力**。
- `scripts/_archive/`：历史补丁与收拢的构建垃圾（`.bak_*` / `__pycache__`）。
- 活跃区不得再出现 `__pycache__` 与 `.bak_*`（2026-11-09 已清理）。
