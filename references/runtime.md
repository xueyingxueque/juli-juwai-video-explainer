# 运行与边界
> **⚠️ 入口声明**：本文描述的是 `pipeline.py run` **单集验证路径**的运行环境，
> **不是主力生产入口**。批量生产一律走
> `produce.py --pilot <episodes.json> --prefix <前缀>`（见 SKILL.md §0/§2）。
> 素材根以 `config/engine.json → paths.material_root` 为准，本文旧路径仅作历史说明。


> 文档状态校准｜2026-10-12｜面向有 Windows 本地文件与进程权限的 Agent

## 目录

- [1. 环境依赖（本机已核验）](#1-环境依赖本机已核验)
- [2. 标准调用](#2-标准调用)
- [3. 请求 JSON](#3-请求-json)
- [4. 批量素材](#4-批量素材)
- [5. 状态与退出码](#5-状态与退出码)
- [6. 错误码](#6-错误码)
- [7. 隔离与恢复](#7-隔离与恢复)

## 1. 环境依赖（本机已核验）

| 组件 | 路径 | 状态 |
|---|---|---|
| FFmpeg / FFprobe | 见 `config/engine.json` | 每次预检确认 |
| ASR / TTS Python | 见 `config/engine.json` | 每次预检确认 |
| VLM | 见 `config/engine.json` | 每次预检确认 |
| 字体 | `C:\Windows\Fonts\msyh.ttc` | 以目标机器检查为准 |

**全部路径正典在 `config/engine.json`**，本表仅供快速查阅。

## 2. 标准调用

```bash
cd <技能根目录>

# 单集自动流程；request 至少包含 source、episode、platform
python scripts/pipeline.py run --request <request.json> --out <新任务目录>

# 或手动分阶段复核
python scripts/s1_ingest.py <原片.mp4> --episode 1 --out <任务目录>
python scripts/s2_story.py <任务目录>
python scripts/s3_script.py <任务目录> 60 auto "冲突、选择与代价"
python scripts/script_to_plan.py <任务目录>

# 基于已有 plan.json 装配成片
python scripts/build.py <plan.json> --dataset <素材目录> --events <events.json> --bgm <曲路径> --out-dir <输出目录>

# 校验人工挑好的 plan；有逐段 event_id 时可附加 --events <events.json>
python scripts/plan_story.py <plan草稿.json> --out <plan.json>

# 运行环境诊断（会实际调用各程序的版本参数）
python scripts/pipeline.py doctor

# 独立预检，可指定报告位置
python scripts/preflight.py --out <任务目录>/preflight.json

# 独立质检；字幕与 shotmap 是硬门必需输入
python scripts/quality_gate.py <成片.mp4> <目标秒数> <字幕.srt> <shotmap.json> <CER阈值或none> --platform generic
```

### 原声定位的 ASR 边界

`build.py` 需要带时间戳的 ASR 才能把原声精确对齐到对白。目前统一 ASR 的 SenseVoice 主引擎只提供文本；
因此 build 只会使用 `config/engine.json` 中状态为 `verified`、有测量 CER 且通过生产阈值的 Whisper 定时模型。
没有合格模型时，原声片段按 plan 中人工核对的 `ep/t` 证据时间点切取，并在日志中标记降级；不会悄悄调用已标记为中文不可用的 tiny 模型。

S1 将真实来源秒数写入 `frame_times.json`；S2 将 `ep/t` 原样带入 `evidence.json`。证据 ID 只能通过这些来源元数据映射到 build 的 `ep/t`；严禁将帧序号当作秒数。

`pipeline.py run` 接通单集 S1-S6：素材分类、预检、抽帧/ASR、S2 证据与参考摘要、S3 写稿、证据映射、build 和质量门。`story.json` 是编辑参考，不具事实权威；逐句事实只以 `evidence.json` 中被引用的证据为准。多集 `series_decompose.py` / `series_plan.py` 尚未进入自动编排。VLM 不可用、S2/S3/适配器失败或硬门失败时，读 `state.json` 与 `logs/`；硬门拒绝时不会写交付登记。软门缺报告会显式标为未打分，不等于通过。

S3 每个剧情句用 `evidence_ids` 列出支持句内事实的证据集合，并从集合中选一个 `primary_evidence_id` 作为剪辑镜头锚点；程序分别校验事实集合、主镜头时间顺序和覆盖度。CTA 不引用事实证据，仅沿用前一剧情句的镜头作为 `visual_anchor_only`。`script_to_plan.py` 保留该区分并只用主镜头来源时间映射 `ep/t`。pipeline 将 `request.json` 复制到任务目录，确保 S3 使用与 build 一致的平台字幕配置。运行或保存人工 plan 时应保留输入草稿；build 的实测时长写入输出目录 `build-plan.json`。

无标注样本时，预检沿用配置中的 CER 基线，并在报告中标记不是本次实测。任务目录的 `preflight.json` 保留完整检查快照，简要裁决保存在 `state.json`。版本探测按程序区分参数：FFmpeg/FFprobe 用 `-version`，Python 用 `-V`，llama-server 用 `--version`。子进程使用参数数组，不拼接命令字符串。

## 3. 请求 JSON

```json
{
  "source": "D:/剧里剧外/素材/短剧/抖音/一池好宴/1.mp4",
  "target_seconds": 60,
  "platform": "douyin",
  "direction": "交代冲突、选择和结果；未获证实的动机不要写",
  "soul": "auto",
  "strict_duration": false,
  "reference_audio": null,
  "reference_text": null
}
```

| 字段 | 规则 |
|---|---|
| `source` | 必填，已存在的本地单个视频；mp4/mkv/mov/avi/webm/m4v |
| `target_seconds` | **60–300**；**是写作预算，不是最终时长**。实测超此区间质检 REJECT |
| `platform` | `douyin` / `kuaishou` / `wechat_channels` / `generic`，决定质量门阈值与字幕规范 |
| `direction` | 叙述风格与重点。**不是事实补全来源** |
| `soul` | 六流之一或 `auto`（由 S2 依据题材自动选型） |
| `strict_duration` | true 时启用误差 ≤ max(1秒, 10%) 的硬门 |
| `reference_audio` | 本地参考音色。**不填则用三级决策（SKILL.md §3.2 音色决策），无音色直接报错退出——禁无脑默认** |
| `reference_text` | 参考录音的准确文字；**不知道就不填，不能编造** |

**不要塞入**未实现的字段：平台发布凭证、云端端点、角色映射、多集列表。

## 4. 批量素材

素材根以 `config/engine.json → paths.material_root` 为准
（当前主力为 `D:/BaiduNetdiskDownload/<剧名>/`，每剧一个目录，内含 `1.mp4…N.mp4`）。
早期「小说/短剧 × 快手/抖音/视频号」结构已非主力；**批量生产一律走 `produce.py --pilot`**，
下述 `pipeline.py batch` 仅用于单集验证路径。

```json
{"requests": ["D:/.../001.json", "D:/.../002.json"]}
```

```bash
python scripts/pipeline.py batch --request 批次.json --out 批次目录
```

按 `task-0001`、`task-0002` 顺序运行，每项结束即更新 `batch.json`；
**隔离或失败不阻塞下一项**。它是有限批次执行器，**不是后台目录监控器或定时任务**。

> **批量前先跑 S1 体检**确认本批素材可处理数量。

## 5. 状态与退出码

| 状态 | 含义 | 调用方动作 |
|---|---|---|
| `initialized` / `running` | 执行中 | 等待，读状态摘要；不读生产内容到聊天模型 |
| `blocked` | 输入类型、依赖或阶段契约需要处理 | 按 `state.json` 的错误和 remediation 操作 |
| `complete` | 单集阶段完成且硬门通过 | 查看交付清单、风险与人工审片要求 |
| `quarantined` | 质量硬门拒绝 | 不交付，按 QC 报告返修 |
| `needs_review` | 预留状态；当前 pipeline 不自动设置 | 以真实产物和质检报告为准 |
| `failed` | 环境/数据异常 | 查日志与错误码，**不可当成质量通过** |
| 无 `state.json` | 输入或环境检查前失败 | 读进程返回码 |

返回码：成功 `0`｜质量隔离 `2`｜其他异常 `1`｜`doctor` 就绪 `0`、缺项 `2`。

> 某些 PowerShell 宿主会改写非零退出码，**以状态文件和返回 JSON 为权威**。

## 6. 错误码

| 错误码 | 处理 |
|---|---|
| `PREFLIGHT_REFUSED` | 预检判定 ASR 或必要本地依赖不可用；查任务目录 `preflight.json` 的 reasons |
| `MATERIAL_PRECHECK_UNVERIFIED` | 素材性质无法确认；检查任务目录 `material_precheck.json`，不要继续当原片处理 |
| `ASR_MODEL_INCOMPLETE` | 模型缺 `model.bin`，跑 `scripts/fetch_asr.py` 重下并校验 |
| `INPUT_CHANGED_NEW_OUTPUT_REQUIRED` | 输入/配置已变，建新任务版本，保留旧结果 |
| `REGISTERED_ARTIFACT_CHANGED` | 已登记产物被改；恢复原件或新建任务，不手改哈希 |
| `TASK_BUSY` | 任务或 GPU 锁占用；等待，不删锁、不杀其他服务 |
| `GPU_BUSY_OR_UNAVAILABLE` | 资源不足；保存状态等待，不关用户其他程序 |
| `MODEL_PORT_IN_USE` | 不接管已有服务；配新回环端口并新建任务 |
| `DURATION_MISMATCH` | 硬门：成片与音频总长超差；返修或调 target_seconds |
| `COPYRIGHT_CONTINUOUS_EXCEED` | 硬门：原片连续片段 >2.5s；压缩或改用评论式表达 |
| `SPEECH_GATE_UNSCORED` | 软门未打分（ASR 不可用）。**不等于通过**，须写入风险说明 |

## 7. 隔离与恢复

- 每轮有 `qNN-*.json` 阶段登记与 `quality/round-NN/` 产物；**旧轮不覆盖**。
- 相同请求恢复**不会重置**返修预算。
- 失败视频**不得**搬到根目录 `final.mp4`；只有硬门全过才能触发 delivery。
- 任务目录结构升级后不保证旧任务可续跑；必要时用归档重现或按当前结构新建任务。
