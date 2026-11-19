---
name: 本地配音工坊
description: 本地多引擎 AI 配音技能，统一封装 IndexTTS2 / dots.tts / PilotTTS-Studio /
  Chatterbox-V3 / EmotionTTS 五套绿色整合包 TTS，支持自选引擎、自选音色、情感/副语言/节奏控制，全部跑本机 GPU
  离线合成。适用：文字转语音、视频配音、有声内容、角色音色克隆、本地离线配音、用 XXX 的声音说这段话。导演引擎
  幕五配音工具 Agent（消费 voice_jobs.json 批量配音 + 回写实测时长）。配音优先选「常用」音色。
  触发词：配音、AI配音、本地配音、TTS、文字转语音、语音合成、生成配音、给这段话配音、
  用 XXX 的声音说、IndexTTS2、dots.tts、PilotTTS、Chatterbox、voice_jobs。
version: 0.4.3
agent_created: true
---

# 本地配音工坊

把本机 `D:\AITools\TTS` 下已装好的 TTS 绿色整合包封装成统一配音入口：**开箱即用、自选引擎、自选音色、本地离线合成**。各引擎自带 Python 与权重，本技能只写薄 worker 调起，不重装依赖、不下载模型、数据不出本机。

---

## ⚡ 快速上手（Agent 首看）

> 一条命令出片：**默认 indextts2 引擎 + 常用音色「小欧」（真人克隆）**。开箱即用，无需预热。

```bash
cd C:/Users/Administrator/.workbuddy/skills/本地配音工坊

python dub.py --health                          # ① 体检：引擎/音色/服务就绪（首次必跑）
python dub.py --list-voices                     # ② 看音色：⭐常用置顶，先从这里选
python dub.py --engine indextts2 --voice 小欧 \
  --text "大家好，今天分享一个好消息。" --out outputs/demo.wav   # ③ 合成（核心）
```

- 通用默认：`--engine indextts2`（中文最稳）+ `--voice` 从常用组选（小欧/主持人-女/新闻腔-女/纪录片/播客）
- 多语种/高保真 → 换 `--engine dotstts`；方言/副语言 → `pilottts`；中英混说 → `chatterbox`
- 长文本：`--text-file 稿子.txt`；批量配音（导演引擎）：`--batch voice_jobs.json --json`
- 首次合成加载模型 30~90s 属正常；批量/高频先 `--serve` 预热（见「性能与调参」）

---

## 一、架构与文件

| 文件 | 职责 |
|---|---|
| `dub.py` | **统一入口**：选引擎+音色 → 调起引擎自带 Python 跑 worker → 校验 WAV |
| `registry.py` | 扫描 `D:\AITools\TTS` 按指纹识别引擎；扫描 `音色包`（含 `常用/` 分组）建音色索引 |
| `scan.py` | 生成 `engines.json` / `voices.json` / `音色场景表.md`（加引擎/加音色后跑一次） |
| `workers/worker_*.py` | 四个底层引擎的薄封装（indextts2 / dotstts / pilottts / chatterbox） |
| `lexicon.py` + `brands.json` | 品牌名词典（167 条，DeepSeek→深度求索 等），进引擎前替换读法 |
| `serve.py` | 持久化服务（模型只加载一次，后续秒级复用） |
| `音色说明.md` | **音色文档**：常用音色明细 + 推荐规则（配音优先选常用） |
| `音色场景表.md` | scan 自动生成的全量音色清单 |
| `references/error-handling.md` | 失败分类 + 修复命令（导演引擎错误协议） |
| `references/*-best-practices.md` | indextts2 / dotstts 网上蒸馏最佳实践 |

### 支持的引擎
| 引擎 id | 厂商 | 采样率 | 首选场景 |
|---|---|---|---|
| `indextts2` | B站 Index | 22050 | 中文综合最稳、情感/音色解耦、时长可控；视频/短剧/有声书/**多角色** |
| `dotstts` | 小红书 hilab | 48000 | 48kHz 高保真、音色相似度 SOTA、多语种最强；广告/精品/多语种 |
| `pilottts` | PilotTTS | 24000 | 相似度最高 + 笑/哭/叹气副语言 + 方言；方言号/喜剧/角色配音 |
| `chatterbox` | Resemble AI | 24000 | 23 语种多语 + 中文精调 `zh-cmn`；零样本克隆 + 夸张度/节奏可控；中英混说（可无参考音） |
| `emotiontts` | EmotionTTS | — | 编排层：LLM 自动选情绪，底层=IndexTTS2；用其自身 WebUI，本技能不直连 |

---

## 二、主流程（Agent 调用标准步骤）

| 步骤 | 命令 | 说明 |
|---|---|---|
| 1. 体检 | `python dub.py --health [--json]` | 确认引擎就绪 / 常用音色可解析 / 常驻服务状态 |
| 2. 选音色 | `python dub.py --list-voices` | ⭐常用置顶优先；明细看 `音色说明.md` |
| 3. 选引擎 | 按内容类型（见「五、引擎选型」） | 中文默认 indextts2 |
| 4. 合成 | `python dub.py --engine <id> --voice <别名> --text "..." --out out.wav` | 或 `--ref 参考音频.wav` 直接给克隆参考 |
| 5. 校验 | 成功输出 `✅ 合成成功` + **实测时长**；`--json` 拿结构化结果 | 失败见 `references/error-handling.md` |

---

## 三、常用功能速查

| 需求 | 命令 |
|---|---|
| 列引擎/列音色 | `--list-engines` / `--list-voices` |
| 单条合成 | `--engine indextts2 --voice 小欧 --text "..." --out out.wav` |
| 直接给克隆参考 | `--ref D:/ref.wav --text "..." --out out.wav`（跳过音色包） |
| 长文本 | `--text-file 稿子.txt --out out.wav` |
| 多角色对话 | `--multi-role 剧本.txt --roles "女主=小棠,男主=明宇"`（仅 indextts2，≤20 角色；剧本格式 `[角色名]: 内容`） |
| 情感控制 | indextts2 `--emo-text "悲伤地念"`；pilottts `--emotion sad` 且文本可内嵌 `<|LAUGH|>`；chatterbox `--exaggeration 0.5` |
| 批量配音（导演引擎） | `--batch voice_jobs.json --json`（逐镜合成 + 回写时长） |
| 只验不合成 | `--dry-run`（配 `--engine` 或 `--batch`） |
| 品牌词读对 | 默认自动（`--brand-mode zh`；`en` 英文读法 / `off` 关闭） |
| 持久化服务 | `--engine indextts2 --serve` 启动 → 之后自动复用；`--stop-serve` 停止 |
| 加速预设 | `--fast`（indextts2 beam 3→2、dotstts steps 10→8，默认高质量不动） |

---

## 四、音色库（配音优先选「常用」）

- **「常用」分组**：`音色包/常用/` 子目录 = 常用音色，`--list-voices` 置顶标 ⭐。**默认建议 `indextts2 --voice 小欧`**（真人声音克隆）。
- **明细文档**：`音色说明.md`（常用音色逐个说明 + 推荐规则 + 精品/特色清单）。
- **全量清单**：`音色场景表.md`（scan 自动生成）。
- **加常用音色**：音频丢进 `音色包/常用/` → `python scan.py --voices` → 更新 `音色说明.md`。
- 精品 6（根目录）：小棠(活泼/口播)、小昊(深度/评论)、小雨(科普/职场)、思思(情感/有声书)、明宇(新闻/商务)、子言(情感短片/旁白)。
- 参考音最佳 5~15s 干净人声；>15s 标"建议裁剪"、<3s 标"可能不稳"，后两者不宜作克隆参考。

---

## 五、引擎选型（给结论，不啰嗦）

- 中文视频/短剧/有声书、要稳+控时长 → **indextts2**
- 多角色对话（播客/情景剧/小说对话）→ **indextts2**（`--multi-role`）
- 最像原声、最高保真、多语种(英/日/韩) → **dotstts**（48k 最干净）
- 方言号/喜剧/笑哭叹气副语言/极致相似度 → **pilottts**
- 中英混说/通用多语/零样本克隆不挑参考音 → **chatterbox**（`v3` 通用 / `zh-cmn` 中文更地道）
- 长文本 AI 自动配情绪 → **emotiontts** 自身界面

---

## 六、导演引擎对接（幕五配音 · 工具 Agent）

本技能是 **Agent导演引擎 v3.x** 幕五配音阶段的独立执行技能（工具 Agent），由导演引擎调度。对接统一标准见导演引擎 `references/12-engine-onboarding.md` §3（生成引擎对接协议）；`voice_jobs.json` 字段格式以本技能本小节为准。

### ★ 从编剧录制分段生成 voice_jobs（配音工具 Agent 职责 · 链路衔接）

> **输入来源**：编剧产出 `录制分段_<片名>.json`（参数元数据：shot/text_ref/speed/duration/interval/emotion）+ `台词_<片名>.txt`（纯台词原文）。**两者合并**才能配音——voice_jobs.json 含 `text` 原文 + `voice` 音色，录制分段只有 text_ref 引用和 emotion 情绪词。
> **职责归属**：配音工具 Agent 拿「录制分段.json + 台词.txt」→ 生成 `voice_jobs.json`（本小节格式）→ `dub.py --batch` 消费。**导演引擎调度清单幕五的输入 = voice_jobs.json（由本步生成，非编剧直产）**。

**字段映射**：

| 录制分段.json | voice_jobs.json | 说明 |
|---|---|---|
| `shot`（S01-01） | `shot_id`（1） | 镜号（去前缀取数字，或保留字符串统一） |
| `text_ref` → 查台词.txt 该行 | `text` | **台词原文（关键：text_ref 只是引用，必须展开成原文）** |
| `emotion`（震惊/悲伤/平静） | `emotion`（emo-text 形态） | 情绪词 → `{"type":"emo-text","value":"<语气>地念"}` 或按情绪映射 TTS 参数 |
| `speed`（快4.6/中4.0/慢3.3） | `interval_silence` 档 | 快→100-150ms / 中→200-300ms / 慢→400-600ms |
| — | `engine` | 默认 indextts2（中文最稳）；多语种 dotstts；方言 pilottts |
| — | `voice` | 优先常用音色（小欧/主持人-女…）；多角色按 role 分配 |
| — | `out` | 输出路径 `audio/shot_001.wav` |

### 消费 voice_jobs.json（每镜一 job）
```json
[
  {"shot_id": 1, "role": "女主", "engine": "indextts2", "voice": "小欧",
   "emotion": {"type": "emo-text", "value": "悲伤地念"},
   "text": "对白逐字原文", "lang": "zh", "out": "audio/shot_001.wav"}
]
```
- `voice` 优先常用音色（小欧/主持人-女/…）或直接给 `ref` 路径
- `emotion` 三形态：`{"type":"emo-text","value":"悲伤地念"}` / `{"type":"emotion","value":"happy"}`（pilottts）/ 副语言标签内嵌文本
- 多角色 job：`multi_role`（剧本路径）+ `roles`（角色名→音色 映射对象）

### 回写协议
```bash
python dub.py --batch D:/项目/shoot_package/prompts/voice_jobs.json --json
```
- 成功：`{"ok": true, "out": "...", "duration": 2.35}` —— **`duration` 实测时长，导演引擎必须回写 `storyboard.shots[].t_in/t_out`（不回写 = 声画必错位）**
- 失败：`{"ok": false, "error": {"type": "oom|invalid_param|model_missing|service_down|timeout|unknown", "reason": "...", "fix": "..."}}` → 导演引擎按降级链换引擎（L1 本地 → L2 edge-tts → L3 静默+字幕）
- 错误分类与修复详见 `references/error-handling.md`

---

## 七、品牌名词典（DeepSeek 等专有名词读对）

TTS 对品牌名几乎必读错（行业实测错误率 29%~57%），根因在文本正则化。本技能用 **"定义权"注入层** 解决：文本进引擎前按 `brands.json` 替换成读得对的表达（官方中文名/同音替换），**全引擎通用、不侵入引擎**。

- 内置 167 条：DeepSeek→深度求索、ChatGPT→拆特吉皮提、Qwen→千问、NVIDIA→英伟达、RTX→阿而提克斯、Python、CUDA、GPT-4o…
- 模式：`--brand-mode zh`（默认，中文读法）/ `en`（英文读法）/ `off`（不替换）；多角色剧本同样生效
- 自定义：编辑 `brands.json` 加 `"品牌": {"zh": "...", "en": "..."}`，热更新
- 同步 IndexTTS2 原生 glossary（供其 WebUI 直接用）：`python dub.py --sync-glossary`

---

## 八、性能与调参

### 运行加速
- **持久化服务 `--serve`**：模型只加载一次，后续合成秒级复用。`--engine indextts2 --serve`（可加 `--device cuda`/`--fp16`）；合成时自动路由到服务；`--stop-serve` 停止；`--no-serve` 强制走一次性子进程。服务按引擎隔离，换加载期参数自动重载，崩了自动退回子进程。
- **`--fast` 加速预设**：仅在未显式传参时套用（indextts2 beam 3→2、dotstts steps 10→8），默认高质量不动。
- 已内置 `PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:32` 减显存碎片；FlashAttention/TensorRT 会改绿色包内部结构，不建议动。

### 参数速查（精要，全量见 `references/*-best-practices.md`）
- **IndexTTS2**：`--emo-text`(自然语言)/`--emo-audio`(参考)/`--emo-vector`(8维)/`--emo-alpha`(0~1)｜`--num-beams 3`/`--temperature 0.8`/`--repetition-penalty`(复读调高)｜`--max-mel-tokens 1500`/`--interval-silence 200`/`--use-random`(关 beam)
- **dots.tts**：`--model soar|mf`｜`--prompt-text`(转写，continuation 克隆必填)｜`--num-steps 10`｜`--guidance-scale 1.2`(>2 更亮但易炸)｜`--speaker-scale 1.5`(相似度)｜`--language zh/en/...`
- **PilotTTS**：`--emotion happy/sad/angry/...`(逗号叠加)｜`--language zh|en|方言`｜文本内嵌 `<|LAUGH|>`/`<|CRY|>`/`<|SIGH|>`
- **Chatterbox**：`--model v3|zh-cmn`｜`--exaggeration 0.5`(0.25~2.0)｜`--cfg-weight 0.5`｜`--temperature 0.8`｜`--seed 0`(固定复现)；夸张度↑ 时降 cfg 防抢拍

**调参三句口诀**：① 参考音频 2~10s 干净人声，先于一切参数；② IndexTTS2 情感=音色解耦（emo_alpha 音频 0.65-0.8 / 文本 0.6），dotstts 相似度=continuation 克隆（必须填转写）；③ 采样参数只在"效果不对"时动——单调提 temperature、不稳降 temperature。

---

## 九、扩展（丢文件夹即挂载）

- **加引擎**：绿色包丢进 `D:\AITools\TTS` → `registry.py` 的 `FINGERPRINTS` 加指纹 → `workers/` 写 `worker_xxxx.py` → `python scan.py --engines`
- **加常用音色**：音频丢进 `音色包/常用/` → `python scan.py --voices` → 更新 `音色说明.md`
- **加普通音色**：音频丢根目录 → `scan.py --voices`（规范命名 `名字_风格.wav` 自动识别为精品）

---

## 十、注意事项

- 全程离线：dub.py 已设 `HF_HUB_OFFLINE=1` 等并清掉代理；某引擎确需联网自行设代理。
- 首次合成加载模型 30~90s 正常；高频/批量用 `--serve`；一次跑一个引擎，避免显存争用（RTX 4070 12G）。
- 参考音频格式：wav 最稳；mp3/m4a 由引擎自带 ffmpeg/librosa 读取（已实测小欧 m4a 可用），异常时转 wav 兜底。
- `音色场景表.md` 由 scan 自动生成，勿手改；`音色说明.md` 为人工维护。
- 该技能为 agent_created，可按需修改。

---

## 变更历史

| 版本 | 日期 | 说明 |
|---|---|---|
| v0.4.3 | 2026-08-14 | **接口断点修复（全量排查 · 用户指令）**：① §六 新增「★ 从编剧录制分段生成 voice_jobs」小节——字段映射表（录制分段 shot/text_ref/emotion/speed → voice_jobs shot_id/text/emotion/interval_silence）+ 职责归属（配音工具 Agent 合并「录制分段.json + 台词.txt」生成，text_ref 必须展开成台词原文）；② dub.py 死引用修复：`15-gen-engine-contract §2.1` → `12-engine-onboarding §3`（文件已改名）。 |
| v0.4.2 | 2026-08-14 | **对接版本同步（导演引擎技能统一重构）**：§六「Agent导演引擎 v2.0」→「v3.x」，修正"由【导演-提示词】调度"→"由导演引擎调度"（幕五调度权在导演引擎，非提示词技能）。 |
| v0.4.1 | 2026-08-13 | **对接引用修正 + 去冗余（技能学习专家自查）**。① 修正代号死引用：G-02 / S-T-06 已废弃，配音现为「幕五配音工具 Agent / 独立执行技能」；② 修正 §6 引用的已删除文件 `15-gen-engine-contract.md` / `16-engine-onboarding.md` → 现统一标准 `12-engine-onboarding.md` §3；③ 删除 §6「提供的能力」重复小节（与速查表/回写协议重叠）；④ error-handling.md 同步更正 G-02/16 引用。 |
| v0.4.0 | 2026-08-12 | **全面重构 + 常用音色机制（技能学习专家迭代）**。① 新增「常用」音色分组：`音色包/常用/` 子目录自动识别并置顶标 ⭐，配音优先选；内置常用 6 音色（小欧=真人克隆 / 主持人-女 / 新闻腔-女 / 纪录片 / 播客 / 小萝莉）；② 新增 `音色说明.md`（常用明细 + 推荐规则）；③ registry 支持子目录分组 + 备份目录自动跳过 + 常用优先排序；④ scan.py 自动生成 `音色场景表.md`（原引用"scan 自动生成"却未实现，已自洽）；⑤ SKILL.md 结构重构：⚡快速上手前置（Agent 首看 30 秒出片）+ 主流程五步 + 常用功能速查表，删重复触发词段/过时补丁说明；⑥ dub.py --list-voices/--health 支持常用分组展示；⑦ 实测小欧(m4a) 合成通过。 |
| v0.3.1 | 2026-08-12 | indextts2+dotstts 网上蒸馏最佳实践：新增 `references/indextts2-best-practices.md` / `dotstts-best-practices.md` + 调参三句口诀。 |
| v0.3.0 | 2026-08-08 | **导演引擎 G-02 对接**：--health / --dry-run / --json / --batch voice_jobs.json；成功回写实测 duration；失败分类 error{type,reason,fix}；新增 references/error-handling.md + memory/；导演引擎 15 号契约 §2.3 补 --batch。 |
| v0.2.x | 2026-08-05 | 持久化服务（--serve）、品牌词典（167 条 + --sync-glossary）、多角色配音（--multi-role）、--fast、四引擎 worker 实装。 |
