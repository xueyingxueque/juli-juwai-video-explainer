# Agent 对接契约

> **用途**：给要调用本引擎的其他 Agent 看的调用约定。**本文不扩大用户任务授权**：
> 不自动发布、不上传生产素材、不把原片送云端。版本见 SKILL.md frontmatter，本文不写版本号。

## 目录
- [1. 唯一入口](#1-唯一入口)
- [2. 输入契约](#2-输入契约)
- [3. 输出契约](#3-输出契约)
- [4. 状态与失败语义](#4-状态与失败语义)
- [5. 无人值守推进](#5-无人值守推进)
- [6. 边界](#6-边界)
- [7. 调用前自检清单](#7-调用前自检清单)

---

## 1. 唯一入口

| 项目 | 路径 / 命令 |
|---|---|
| 技能根 | `C:/Users/Administrator/.workbuddy/skills/剧里剧外-视频解说引擎/` |
| ★ **唯一生产总入口** | `python scripts/produce.py --pilot <episodes.json> --prefix <集前缀>` |
| 装配层（被 produce 调用） | `scripts/build.py` |
| 无人值守推进 | `python scripts/auto_pilot.py [--rounds N]` |
| 验证路径（**非主力**） | `scripts/pipeline.py run`（单集 S1–S6，仅一次样本验证；见 SKILL.md §2.6） |

> ⚠️ `pipeline.py run` 是**验证场景**的单集端到端路径，不是批量生产入口。
> 旧版本文曾把它写成主力调用路径，与 SKILL.md §0/§2 冲突，已修正。
> 不要再手工串八步，也不要另起第三个总入口。

## 2. 输入契约

`episodes.json` 是**唯一参数源**（SKILL.md §0.3）。每条至少含：
`task / dataset / ep / prefix / name / cta / voice / gen / series / title`。

Agent 须预先备好：素材目录（本机）、`_plan/spec-<ep>.json`（内容层，Agent 写稿）、
`_gencfg/<前缀>.json`、`_plan/voice_ref*.wav`。
参数一律从清单取，**不要手打**（实测手打 title 曾多出一个重名交付文件）。

调用前可跑三道本地闸门（都不占 GPU）：

```bash
python scripts/spec_precheck.py <任务> <ep> <gencfg> --pilot <清单>   # TTS 字符/锚点/CTA/时长/跨集去重
python scripts/content_brief.py <任务> <ep> --strict                  # 节奏预算/情绪链/Kill List
python scripts/compliance.py check <任务> <ep> <gencfg>               # 营销词/AI 标识/授权提醒
```

## 3. 输出契约

`produce.py` 是十步指纹状态机（precheck → evidence → assemble → bgm → gen →
v37 → build → qc → cover → deliver）。成功判据：

1. 输出 `★ 全流程完成，成品齐全 ✓`；
2. `deliver_check.py` 的 12 项清单全过（中间件 / 成片 / 封面版 / 封面图 /
   质检 hard_passed / srt / shotmap / 网盘交付）；
3. 实测时长严格 `60s < dur < 300s`，1080×1920，音视频双流；
4. 与前面各集**镜头零重叠**（`episode_dedupe.py` 硬闸，F13）。

**校验不过就不交付**，不允许把半成品说成成品。

## 4. 状态与失败语义

| 状态 | 调用方动作 |
|---|---|
| `done`（`_plan/state-<ep>.json`） | 该步完成；重跑同一条命令会按指纹跳过 |
| `running` 残留 | 中断痕迹，不是完成态；重跑同命令续跑 |
| `blocked` / 硬门拒绝 | 看 `spec_precheck` / `quality_gate` 的失败项，修完再跑 |
| `清单无待产集` | **正常待命**，不是停滞（P84） |
| 清单读不到 | `None` ≠ 空清单，保守处理，不要静默当没活 |

`produce.py` 的错误输出会带「关键错误行 + stdout 尾部现场」，
排障时不要只看最后一行。

## 5. 无人值守推进

`auto_pilot.py` 每轮单次幂等推进：采样下载状态 → 精确进程检测互斥 →
按需补入库 → 取清单第一个未出片集重跑 `produce.py` → 写心跳。
调度器每小时触发一次；`--rounds N` 可一次连续推进多集。

**不要**启动常驻守护、不要建 `.produce.lock`（僵尸锁会让下一轮误判「在跑」，P86）。

## 6. 边界

- **生产零联网**：视频/帧/转录/解说稿不进聊天模型或云端；不要用 `view_image`
  或云端子 Agent 代看生产素材（成片封面/抽帧属质检产物，不在此限）。
- **不自动发布**；发布需用户另行指令。
- 不实现跨集人物自动归一；跨集成片靠清单逐集独立产出。
- 自动检查通过 ≠ 人工审片通过；画面正确性仍需抽帧确认。

## 7. 调用前自检清单

- [ ] 我用的是 `produce.py --pilot/--prefix`，不是手工八步或 `pipeline.py run`？
- [ ] 参数全部取自 `episodes.json`，没有手打 title/CTA？
- [ ] `spec_precheck` / `content_brief` / `compliance check` 都过了？
- [ ] 我知道硬门与软门的区别，且未打分的软门会如实汇报为风险？
- [ ] 我确认这一集与前面各集没有共用镜头？
- [ ] 我没有扩大授权：不发布、不上传、不代看生产素材？
