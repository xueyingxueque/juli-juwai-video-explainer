---
name: 本地配音工坊
description: 本地多引擎 AI 配音技能，统一封装 IndexTTS2 / dots.tts / PilotTTS /
  Chatterbox-V3 四套绿色整合包 TTS（EmotionTTS 为编排层不直连），支持自选引擎、自选音色、
  情感/副语言/节奏控制，全部跑本机 GPU 离线合成；另含三轨混音（口播+BGM+SFX）与分镜对齐。
  适用：文字转语音、视频配音、有声内容、角色音色克隆、本地离线配音、用 XXX 的声音说这段话。
  是 Agent导演引擎【幕四·后期】的配音/混音执行技能（消费 voice_jobs.json 批量配音 + 回写实测时长）。配音优先选「常用」音色。
  触发词：配音、AI配音、本地配音、TTS、文字转语音、语音合成、生成配音、给这段话配音、
  用 XXX 的声音说、IndexTTS2、dots.tts、PilotTTS、Chatterbox、voice_jobs、三轨混音、BGM闪避。
version: 0.9.4
agent_created: true
---

# 本地配音工坊

把本机 `D:\AITools\TTS` 下已装好的 TTS 绿色整合包封装成统一配音入口：**开箱即用、自选引擎、自选音色、本地离线合成**。各引擎自带 Python 与权重，本技能只写薄 worker 调起，不重装依赖、不下载模型、数据不出本机。另带 `mix_audio.py`（三轨混音）与 `dub_align.py`（按镜长对齐）两个后期脚本，覆盖幕四「配音→对齐→混音」整条链。

> **路径约定**：下文所有 `python dub.py ...` 都在**本技能根目录**（即本 SKILL.md 所在目录）执行，不写死任何绝对路径；换机器/换挂载位置都能跑。

---

## §0 前置 Agent 30 秒速用（首看这一段就够）

### 0.1 六条命令覆盖 90% 场景

```bash
# ① 体检（首次必跑）：引擎梯队/26 音色/常驻服务是否就绪
python dub.py --health

# ② 挑音色：⭐「常用」组置顶，优先从这里选
python dub.py --list-voices

# ③ 单条合成（不传 --engine 就用默认 breeze + 常用音色谭湘文徐浩伦）
python dub.py --voice 谭湘文徐浩伦 --text "大家好，今天说个好消息。" --out outputs/a.wav

# ④ 长文本走文件；批量走导演引擎 voice_jobs.json（--json 时 stdout 只有 JSON，日志在 stderr）
python dub.py --voice 谭湘文徐浩伦 --text-file 稿子.txt --out outputs/long.wav
python dub.py --batch voice_jobs.json --json --fast > result.json

# ⑤ 高保真成品：显式切第二梯队 dotstts（48kHz）
python dub.py --engine dotstts --voice 谭湘文徐浩伦 --text "..." --out outputs/hi.wav

# ⑥ 高频/批量：先起常驻服务（模型只加载一次，之后秒级合成；服务异常会自动回退一次性进程）
python dub.py --serve

# ⑦ 三轨混音：口播+BGM+SFX，自动侧链闪避 + 响度归一（短视频用 --platform shortvideo）
python mix_audio.py --videos S01.mp4,S02.mp4 --voice 配音_对齐.wav \
  --final-video 成片_无音.mp4 --durations 5.2,4.8 --silent S02 \
  --sfx "1.0,SFX/盖章.wav,0.8" --platform shortvideo -o 成片_混音.mp4

# ⑧ 跑正式任务前清场：停掉所有常驻服务，把显存让出来（不做可能慢 4 倍，见 §8.0）
python dub.py --stop-all-serve
```

### 0.2 引擎梯队（2026-09-04 用户试听拍板 · Agent 必须遵守）

```
①默认  breeze     —— 不传 --engine 就是它。听感最好，综合最快（RTF 1.89）
②备选  dotstts    —— 48kHz 高保真，成品/发行场景显式切换（代价 RTF 4.81）
停用    indextts2 / pilottts / chatterbox / emotiontts
        —— 代码与指纹保留（想恢复改 registry 的 tier 即可），但 Agent 不许主动选
```

**规则**：能不传 `--engine` 就不传；只有「要 48k 成品」才显式写 `--engine dotstts`；**停用引擎不要选**，真有需要（如方言、副语言标签）先跟用户确认。

（历史决策树保留在 §5，仅供理解能力差异，不作选型依据。）

### 0.3 六条硬约束（违反必返工）

1. **默认引擎只有 breeze**：不传 `--engine` 就走它；只有「要 48k 成品」才显式 `--engine dotstts`；**停用的四个引擎不许主动选**（真需要方言/副语言标签，先跟用户确认）。
2. **配音优先「常用」音色**（`--list-voices` 里 ⭐ 置顶那 7 个），别从全量里乱挑。
3. **参考音频 5~15s 干净人声**：<3s 不稳、>15s 建议裁剪；先保证参考音质量，再谈调参。
4. **一次只跑一个引擎**（4070 12G）；Breeze 服务常驻 ~7.7GiB 且**单并发（并发会 HTTP 409）**，批量必须串行。要连发就先 `--serve`。
5. **跑正式任务前先清场**：`--stop-all-serve` + 确认空闲显存 > 8GB。闲置服务（哪怕是属于已停用引擎的）会一直占显存，实测能让同任务慢 **4.4 倍**且完全不报错（§8.0）。
6. **长稿别关自动分段**：`--no-chunk` 会让 Breeze 静默丢尾巴（上限 544 字，超过就不念了）。默认行为已安全，别动它。

---

## 一、架构与文件

| 文件 | 职责 |
|---|---|
| `dub.py` | **统一入口**：选引擎+音色 → 优先复用常驻服务（失败自动回退一次性进程）→ 校验 WAV → 回写实测时长 |
| `registry.py` | 扫描 `D:\AITools\TTS` 按指纹识别引擎；扫描 `音色包`（含 `常用/` 分组）建音色索引；ffprobe 路径按 `emotionTTS*` 通配，不写死版本号 |
| `scan.py` | 生成 `engines.json` / `voices.json` / `音色场景表.md`（加引擎/加音色后跑一次） |
| `workers/worker_*.py` | 五个底层引擎的薄封装（indextts2 / dotstts / pilottts / chatterbox / breeze），引擎各自带独立 Python |
| `lexicon.py` + `brands.json` | 品牌名词典（**有效 167 条**：169 键减 2 个 `_` 开头注释键；DeepSeek→深度求索 等），进引擎前替换读法 |
| `serve.py` | 持久化服务（模型只加载一次）；模型缓存 LRU 上限 2，挤出旧模型时自动清显存 |
| `dub_align.py` | 按各镜时长对齐分段配音（原速+定格余量 / atempo 微调，重跑幂等） |
| `mix_audio.py` | **三轨混音**：口播/BGM/SFX，侧链闪避 + 无台词镜 BGM 自动发力 + loudnorm 响度归一 |
| `examples/分段配音模板.py` | 逐镜合成+对齐的**参数化模板**（新项目复制改参数即可；情绪参数按引擎自动选对） |
| `tests/test_smoke.py` | 不加载模型的秒级冒烟（`python tests/test_smoke.py`；加 `--e2e` 才真合成） |
| `音色说明.md` / `音色场景表.md` | 前者人工维护（常用音色明细）；后者 scan 自动生成，勿手改 |
| `references/` | error-handling（错误分类修复）+ indextts2/dotstts 最佳实践 |

### 支持的引擎
| 引擎 id | 来源 | 采样率 | 首选场景 |
|---|---|---|---|
| `indextts2` | B 站 IndexTTS-2 | 22050 | 中文综合最稳、情感/音色解耦、时长可控；视频/短剧/有声书/**多角色** |
| `dotstts` | 小红书 HiLab dots.tts（soar/mf 两权重） | 48000 | 48kHz 高保真、音色相似度 SOTA、多语种；广告/精品/多语种；**求快 `--model dots-tts-mf`（MeanFlow NFE=4）** |
| `pilottts` | 高德 AMAPVOICE PilotTTS | 24000 | 相似度高 + 11 类情绪 + 笑/哭/叹气副语言 + **14 种中文方言**；方言号/喜剧/角色 |
| `chatterbox` | Resemble AI Chatterbox-V3 | 24000 | 23 语种 + 中文精调 `zh-cmn`；零样本克隆 + 夸张度/节奏可控；中英混说（可无参考音）；输出带 PerTh 感知水印（特性非 bug） |
| `breeze` | BreezeBlue Breeze TTS 2 | 24000 | **开源 TTS 榜首**：Voice Design 无参考音造音色 + Voice Direction 克隆音色同时改语气 + 中英双语 + 中文 `[笑]` 事件标签；**权重仅研究非商用**（见 §十） |
| `emotiontts` | EmotionTTS | — | **编排层**：LLM 自动选情绪，底层仍是 IndexTTS2；用其自身 WebUI，dub.py 不直连（误选会被拦并提示） |

---

## 二、主流程（Agent 调用标准五步）

| 步骤 | 命令 | 说明 |
|---|---|---|
| 1. 体检 | `python dub.py --health [--json]` | 引擎就绪 / 26 音色可解析 / 常驻服务状态；版本号以此处为准 |
| 2. 选音色 | `python dub.py --list-voices` | ⭐常用置顶优先；明细看 `音色说明.md` |
| 3. 选引擎 | 按 §0.2 梯队 | 不传 = 默认 breeze；要 48k 才显式 dotstts |
| 4. 合成 | `python dub.py --engine <id> --voice <别名> --text "..." --out outputs/x.wav` | 或 `--ref 参考.wav` 直接给克隆参考 |
| 5. 校验 | 成功输出 `✅ 合成成功` + **实测时长**；`--json` 取结构化结果 | 失败按 `references/error-handling.md` 六类错误修 |

---

## 三、常用功能速查

| 需求 | 命令 |
|---|---|
| 列引擎/列音色 | `--list-engines` / `--list-voices`（都支持 `--json`） |
| 单条合成 | `--voice 谭湘文徐浩伦 --text "..." --out outputs/x.wav` |
| 直接给克隆参考 | `--ref D:/ref.wav --text "..." --out outputs/x.wav`（跳过音色包） |
| 长文本 | `--text-file 稿子.txt --out outputs/long.wav` |
| 多角色对话 | `--multi-role 剧本.txt --roles "女主=小棠,男主=明宇"`（仅 indextts2，≤20 角色；剧本每行 `[角色名]: 内容`） |
| 情感控制 | indextts2 `--emo-text "悲伤地念"`；pilottts `--emotion sad`（可逗号叠加）且文本可内嵌 `<|LAUGH|>`；chatterbox `--exaggeration 0.5` |
| Breeze 造音色（无参考音） | `--engine breeze --instruction "一位温柔自信的年轻女性，声音清晰" --text "..." --out x.wav` |
| Breeze 克隆 / 调语气 | `--engine breeze --voice 谭湘文徐浩伦 --ref-text-file 转写.txt --text "..." --out x.wav`（无转写加 `--auto-transcribe`） |
| 批量（导演引擎） | `--batch voice_jobs.json --json`（逐镜合成 + 回写时长；**shot_id 允许字符串如 S01-01**） |
| 只验不合成 | `--dry-run`（配 `--engine` 或 `--batch`，打印将执行的 worker 命令） |
| 品牌词读对 | 默认自动（`--brand-mode zh`；`en` 英文读法 / `off` 关）；单条与批量、单/多角色路径行为一致 |
| 常驻服务 | `--engine indextts2 --serve` 起 → 之后自动复用；`--stop-serve` 停（Windows 走 taskkill 连进程树）；`--no-serve` 强制一次性 |
| 加速预设 | `--fast`（indextts2 beam 3→2、dotstts steps 10→8；只在你没显式传该参数时套用） |
| 按镜对齐 | `python dub_align.py --dir 分段目录 --durations 3.5,2.5,... -o 配音_对齐.wav`（自动跳过自己产生的临时文件，可安全重跑） |
| 三轨混音 | 见 §6.5 |

---

## 四、音色库（配音优先选「常用」）

- **「常用」分组**：`音色包/常用/` = 常用音色，`--list-voices` 置顶标 ⭐（当前 6 个：小欧 / 主持人-女 / 新闻腔-女 / 纪录片 / 播客 / 小萝莉）。**默认建议：不传 `--engine`（走 breeze）+ `--voice 谭湘文徐浩伦`**。
- **精品 6（根目录）**：小棠(活泼口播)、小昊(深度评论)、小雨(科普职场)、思思(情感有声书)、明宇(新闻商务)、子言(情感短片旁白)。
- **明细**：`音色说明.md`（人工维护）；**全量**：`音色场景表.md`（scan 自动生成，勿手改）。
- **加音色**：音频丢进 `音色包/常用/`（常用）或 `音色包/` 根（普通，命名 `名字_风格.wav` 自动判精品）→ `python scan.py --voices` → 同步更新 `音色说明.md`。
- **参考音时长**：最佳 **5~15s** 干净人声；registry 会对 <3s 标「可能不稳」、>15s 标「建议裁剪」，这两类不宜做克隆参考。wav 最稳，mp3/m4a 由引擎自带 ffmpeg/librosa 读（已实测 m4a 可用），异常转 wav 兜底。
- 归档：不用的音色移到 `音色包/备份/`，scan 自动跳过，不进索引。

---

## 五、引擎梯队与选型（2026-09-04 用户试听拍板）

### 5.1 梯队（机器可读，`registry.py` 的 `tier` 字段，Agent 必须遵守）

| 梯队 | 引擎 | 定位 | 何时用 |
|---|---|---|---|
| **① 默认** | `breeze` | 听感最好 + 综合最快（RTF 1.89），Voice Design 可无参考音造音色 | **默认，不传 `--engine`** |
| **② 备选** | `dotstts` | 48kHz 高保真，音色相似度 0.908 | 成品/发行要 48k 时显式切换（RTF 4.81，慢 ~2.5 倍） |
| 停用 | `indextts2` | 音色最像（sim 0.944）但 RTF 7.66 太慢 | 不主动选；恢复改 `tier` |
| 停用 | `pilottts` | 方言 + 笑哭叹气副语言 | 不主动选；**确需方言/副语言时先问用户** |
| 停用 | `chatterbox` | 最快（RTF 1.44）但吞字最多（CER 0.128）、峰值贴顶 | 不主动选 |
| 停用 | `emotiontts` | 编排层（LLM 配情绪，底层调 IndexTTS2） | 不主动选；用其自带 WebUI |

恢复方法：把 `registry.py` 里该引擎的 `"tier"` 改回 1/2 再 `python scan.py --engines`。**不做物理删除**——留着随时能对比。

### 5.2 能力差异（仅供理解，不作选型依据）
- **IndexTTS2**：情感/音色解耦 + 时长精确可控 + `--multi-role` 多角色对话（22.05k）
- **dots.tts**：48k，`soar` 自修正对齐（默认）/ `mf` 求快（NFE=4）；多语种最强
- **PilotTTS**：高德 AMAPVOICE，11 类情绪枚举 + `<|LAUGH|>` 副语言 + 方言语言码
- **Chatterbox-V3**：23 语种 + `zh-cmn` 中文精调，可调夸张度/CFG，自带水印
- **Breeze TTS 2**：Voice Clone / **Voice Design（无参考音造音色）** / Voice Direction（克隆+指令调语气），中英双语，`[笑]` 事件标签，**权重非商用**

### 5.3 Breeze 能力全表（默认引擎，Agent 务必读完）

来源：官方模型卡 + 本机源码（`breeze_infer/`）+ 本机实测。

| 能力 | 官方支持 | 本机 | 怎么用 |
|---|---|---|---|
| **Voice Clone** | ✅ | ✅ | `--voice 谭湘文徐浩伦`（ref_text 自动转写，无需手填） |
| **Voice Design** | ✅ | ✅ | 不给 `--voice`，只给 `--instruction "一位温柔的年轻女性…"` |
| **Voice Direction** | ✅ | ✅ | `--voice 谭湘文徐浩伦` + `--instruction "说得慢一点、语气严肃"` |
| **Vocal Events** | ✅ | ✅ 实测 | 中文 `[笑]` `[叹气]` `[咳嗽]` `[清嗓子]`；英文 `(laugh)` `(sigh)` `(cough)` `(clears throat)`，**写在文本里** |
| **中英双语** | ✅ | ✅ | 同一个模型，混说也行 |
| **多角色对话** | ❌ 未文档化 | ✅ **本技能补齐** | `--multi-role 剧本.txt --roles "A=音色1,B=音色2"`（逐段合成+拼接） |
| **长文本自动分段** | ❌ 未处理 | ✅ **本技能补齐** | 默认开启，**超过 350 字才切**；不想切用 `--no-chunk`（**会截断**），改阈值用 `--chunk-chars N` |
| 方言（四川话等） | ❌ 未支持 | ⚠️ 实验性 | 只能靠 `--instruction "用四川话说"` 引导，**不保证地道**；真需求用 pilottts（需先恢复 tier） |
| 流式输出 | ✅ | 未接 | 服务端流式返回 PCM，本技能按整段落盘，暂不需要 |
| **fast 推理路径** | ✅ | ❌ 用不了 | `--fast-all` 需 14.4GiB 显存，本机 12G 装不下（eager 已占 7.7GiB） |

**Vocal Events 实测证据**（同一句话，两个标签）：
- 不带标签 4.32s ｜ 带 `[笑]`+`[叹气]` **5.60s** → 多出 1.28s 真实事件音，**不是把标签当字念**。

**多角色实测证据**（2 角色 4 句）：ECAPA 分段比对参考音，判定序列 `小欧 → 小棠 → 小欧 → 小棠`，与剧本完全一致（区分度 0.735 vs 0.368）。

**⚠️ 长文本会静默截断（本技能已自动兜住，但必须知道为什么）**

服务端硬上限 `max_new_tokens=1500` / `max_seq_len=2048`（源码常量）。超限不报错、不失败，
**直接少念一段尾巴**——产物听着是完整的，其实结尾没了。这是最危险的一类故障。

实测（650 字稿）：
| | 音频时长 | 覆盖率/长度比 | 结论 |
|---|---|---|---|
| 不分段（旧行为） | 106.56s | 91.1% | **尾部整段丢失** |
| 自动分段（现在） | 133.44s | 98.1% | 完整 |

worker 现在默认按句读切段（`[。！？!?；;]`，仍超预算再按 `[，,、]` 切）逐段合成再拼接，
段间留 200ms 静音。**多角色模式同样受益**（每段各自克隆音色）。

阈值为什么是 350 字：上限 544 字（106.56s ÷ 5.11 字/秒），留 36% 余量。
**刻意不定成 150** —— 分段不提速，切细了只会凭空插停顿；30 秒口播稿 156 字，
用 150 会被切成两段，纯属添乱。现在普通段落一律不切。

怎么自己验证：`tests/check_coverage.py`（见 §8.3）。
**别用循环重复的稿子测**——重复文本会让对齐算法失真，脚本会主动提示。

**方言实测（存疑，别当能用）**：`--instruction "用四川话说"` 后 ASR 从"今天这个事情硬是有点意思"变成"今天这事情一直有点隐私我们慢慢掰哈"——发音确实变了，但**无法确认是地道四川话还是发音劣化**。`dialect_sichuan.wav` / `dialect_baseline.wav` 在 `outputs/`，自己听。

**⚠️ 单并发（HTTP 409）与自愈**：Breeze 服务同时只处理一个请求。worker 已内置：409 → 退避重试 3 次 → 仍失败判定锁死 → 杀进程重启服务 → 再试一次。但**批量仍要串行**（`--batch` 本来就是串行的）。

> 冷知识：模型 tokenizer 里有 `[S0]`~`[S9]` 说话人 token，说明**原生多角色对话**的潜力是有的，但官方 README 没写、自带 API 把 speaker 写死成 `[S0]`。等官方文档化后再考虑走原生路径。
- **EmotionTTS**：编排层，本技能不直连

---

## 六、导演引擎对接（幕四·后期 · 配音环节）

本技能是 **Agent导演引擎 幕四·后期 Agent** 的配音/混音执行技能（后期 Agent 整合配音/混音/剪辑/封面），由导演引擎调度。对接标准见导演引擎 `references/12-engine-onboarding.md` §3（§3.1 输入字段、§3.2 回写、§3.3 错误枚举）；`voice_jobs.json` 字段格式以本节为准。

### 6.1 从「录制分段.json + 台词.txt」生成 voice_jobs

> 编剧产出 `录制分段_<片名>.json`（shot/text_ref/speed/duration/interval/emotion 元数据）+ `台词_<片名>.txt`（纯台词原文）。**两者合并**：text_ref 只是引用，必须展开成台词原文塞进 `text`。

| 录制分段.json | voice_jobs.json | 说明 |
|---|---|---|
| `shot`（S01-01） | `shot_id`（1 或 "S01-01"） | 数字补零三位；**字符串也安全**（非法字符自动替换，不会崩） |
| `text_ref`→查台词.txt | `text` | 台词逐字原文 |
| `emotion`（震惊/悲伤） | `emotion` | `{"type":"emo-text","value":"<语气>地念"}`；pilottts 用 `{"type":"emotion","value":"happy"}` |
| `speed`（快4.6/中4.0/慢3.3） | `interval_silence` | 快 100-150ms / 中 200-300ms / 慢 400-600ms |
| — | `engine` / `voice` / `out` | 默认 breeze + 常用音色；输出 `audio/shot_001.wav` |

### 6.2 消费与回写

```json
[
  {"shot_id": 1, "role": "女主", "engine": "breeze", "voice": "谭湘文徐浩伦",
   "emotion": {"type": "emo-text", "value": "悲伤地念"},
   "text": "对白逐字原文", "lang": "zh", "out": "audio/shot_001.wav"}
]
```
```bash
python dub.py --batch D:/项目/prompts/voice_jobs.json --json --fast > result.json
```
- 成功/job：`{"ok":true,"out":"...","duration":2.35,"size_kb":...}` —— **duration 是实测时长，导演引擎必须回写 `storyboard.shots[].t_in/t_out`（不回写=声画必错位）**。
- 失败/job：`{"ok":false,"error":{"type":"oom|model_missing|invalid_param|timeout|service_down|unknown","reason","fix"}}` → 导演引擎按降级链 L1 本地换引擎 → L2 edge-tts → L3 静默+字幕。
- **管道纪律**：`--json` 时 `[batch i/N]` 等过程日志全部走 **stderr**，stdout 只有一份 JSON，可直接 `json.loads` / 重定向到文件。
- 多角色 job：`multi_role`（剧本路径）+ `roles`（角色名→音色别名映射对象）；品牌词替换在批量路径同样生效。

---

## 6.5 三轨混音（幕四混音 · mix_audio.py）

> 配音完用 `dub_align.py` 按镜对齐，再用本节混「口播 + BGM + SFX」。**选曲先读编剧分镜的 B 块（音效）/C 块（配乐）——C 块写器乐就禁用人声流行歌，B/C 块太笼统就打回编剧，不脑补。**

### 6.5.1 响度规范（2026 核对 · 人声优先）

| 元素 | 目标 | 说明 |
|---|---|---|
| 口播/对白 | 主轨，清晰最优先 | 200Hz-5kHz 完整 |
| BGM 有台词段 | 线性底量 0.35 + 侧链闪避 -6dB | 给人声让路（sidechaincompress 自动压） |
| BGM 无台词镜（`--silent`） | 自动抬到 `--silent-bgm-vol`（默认 1.0 满音量发力） | v0.6 起真正按时间区间生效 |
| SFX | 单点 0.6~0.9，动作点对齐 | 缺失文件自动跳过且不影响其它音效 |
| 真峰 True Peak | ≤ -1 dBTP | loudnorm 保证 |

**平台响度预设（`--platform`）**：`podcast`=-16 LUFS（默认，播客/Apple）、`shortvideo`=-14 LUFS（抖音/B站/YouTube/Spotify/TikTok）、`broadcast`=-23 LUFS（EBU R128 广播）；`--loudness` 显式数值覆盖预设。默认单遍 loudnorm（动态模式，快）；要广播级精确达标加 `--two-pass`（先测量再线性校正）。

### 6.5.2 命令模板

```bash
# H3 音轨模式（默认，零外部依赖）：各镜 H3 视频自带音轨做 BGM
python mix_audio.py --videos S01.mp4,S02.mp4,S03.mp4 --voice 配音_对齐.wav \
  --final-video 成片_无音.mp4 --durations 5.2,4.8,3.0 \
  --silent S02 --sfx "1.0,SFX/盖章.wav,0.8;5.5,SFX/转场.wav,0.6" \
  --platform shortvideo -o 成片_混音.mp4

# 库选曲模式：BGM 用素材库（情绪更精准），无台词镜 BGM 满音量
python mix_audio.py --voice 配音_对齐.wav --final-video 成片_无音.mp4 \
  --durations 10 --bgm-track 音频素材库/BGM/温暖/the_shire.mp3 \
  --sfx "0.5,SFX/生活/森林风.mp3,0.6" --silent-bgm-vol 1.0 -o 成片_混音.mp4
```
内部：BGM 先按「有/无台词时间区间」做 volume 自动化 → 口播 asplit 侧链闪避 → 与 SFX amix（normalize=0 + alimiter 防爆音）→ loudnorm 归一 → 与无音成片合成 AAC 192k。

### 6.5.3 选曲与对位（简版，完整方法论见导演后期技能）
- **情绪四轴**：Energy（BPM）/Valence（大小调）/Tension（和声张力）/Intimacy（编曲规模），从 C 块情绪词反推。
- **选曲三查**：① 器乐还是人声（C 块说器乐就禁人声）② 情绪对位 ③ 曲长≥镜长。
- **三对位**：节拍对位（cut on beat，转场快剪）/ 情绪对位（叙事）/ 动作对位（SFX 卡动作点，动画广告）。**前 3 秒音画必须匹配**。

---

## 七、品牌名词典（专有名词读对）

TTS 对品牌名几乎必读错，根因在文本正则化。本技能在文本进引擎前按 `brands.json` 替换成读得对的表达，**全引擎通用、不侵入引擎**；单条/批量/多角色三条路径都生效。

- 内置 **有效 167 条**（文件 169 键，其中 `_说明`/`_用法` 两个注释键不参与替换）：DeepSeek→深度求索、GPT-4o→吉皮提四欧、Qwen→千问、NVIDIA→英伟达…
- 模式：`--brand-mode zh`（默认）/ `en`（英文读法，如 GPT-4o→G P T four o）/ `off`。
- 自定义：编辑 `brands.json` 加 `"品牌": {"zh":"...","en":"..."}`，热更新；长词优先（先匹配 GPT-4o 再匹配 GPT）。
- 同步到 IndexTTS2 原生 glossary（供其 WebUI 用）：`python dub.py --sync-glossary`（当前 Python 需有 PyYAML，缺了会给明确安装提示，不影响正常合成）。

---

## 八、性能与调参（4070 SUPER 12G 实测口径）

### 8.0 🔴 先清场再跑（最容易忽略、也最便宜的一次提速）

**症状**：完全相同的文案/音色/引擎，速度掉 4 倍，且不报任何错。

实测对照（同一条 30 秒文案、小欧、breeze，代码一字未改）：

| 环境 | 音频 | 耗时 | RTF |
|---|---|---|---|
| 机器干净（04:00） | 30.5s | 57.9s | **1.89** |
| 机器被占（08:36） | 30.80s | 258s | **8.38** |

音频时长几乎一致（30.5 vs 30.80），产物等价，**慢的全是环境**。两个元凶：
1. **别的 GPU 程序在跑** —— 抓到 ComfyUI 占 18GB 内存 + GPU 算力，正好压在测试全程。
2. **桌面应用常吃显存** —— WorkBuddy / Chrome / 豆包 / Quark / dwm 合计约 6GB，
   Breeze 的 7.7GiB 装不进剩下的 ~6GB → 溢出到内存 → **不报错，只是慢**。

**做法**：
```bash
python dub.py --stop-all-serve   # 清掉闲置常驻服务（含已停用引擎的，抓到过空转 9 小时）
nvidia-smi                       # 确认空闲显存 > 8GB 再开跑
```
`dub.py` 每次合成前会做显存预检，不够会**主动告警**（不阻断）。看到告警就先清场，
别硬等。

### 8.1 运行加速
- **常驻服务 `--serve`**：模型只加载一次，后续秒级；合成自动路由，**服务失败自动回退一次性子进程**（不会因服务抽风整条任务挂掉）；服务内模型缓存 LRU=2（pilottts 双权重 / dotstts 切 soar,mf 不堆显存），挤出时自动 `torch.cuda.empty_cache()`。
- **`--fast`**：只在你没显式传对应参数时套（indextts2 beam 3→2、dotstts steps 10→8）；dotstts 想更激进提速直接 `--model dots-tts-mf`（NFE=4，约 2×）。
- 已内置 `PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:32` 减碎片；**bf16 默认保留**（Ada 架构完整支持，占 FP32 的 60~70%、质量损失极小；fp16 更省但偶有溢出，不默认）。FlashAttention/TensorRT 改绿色包内部，不建议动。

### 8.2 参数速查（精要，全量看 references/*-best-practices.md）
- **IndexTTS2**：`--emo-text`(自然语言)/`--emo-audio`(参考)/`--emo-vector`(8维)/`--emo-alpha`(0~1，worker 默认 1.0，社区推荐起点：音频模式 0.65~0.8、文本模式 0.6)｜`--num-beams 3`/`--temperature 0.8`/`--repetition-penalty`(复读调高)｜`--max-mel-tokens 1500`/`--interval-silence 200`/`--use-random`(关 beam)
- **dots.tts**：`--model dots-tts-soar|dots-tts-mf`｜`--prompt-text`(continuation 克隆必填转写)｜`--num-steps 10`(mf 更快)｜`--guidance-scale 1.2`(>2 更亮易炸)｜`--speaker-scale 1.5`(相似度)｜`--language zh/en/...`
- **PilotTTS**：`--emotion happy/sad/angry/...`(11 类，逗号叠加)｜`--language zh|en|方言码`｜文本内嵌 `<|LAUGH|>`/`<|CRY|>`/`<|SIGH|>`
- **Chatterbox**：`--model chatterbox-v3|chatterbox-zh-cmn`｜`--exaggeration 0.5`(0.25~2.0)｜`--cfg-weight 0.5`｜`--temperature 0.8`｜`--seed 0`(固定复现)；夸张度↑时降 cfg 防抢拍
- **Breeze**：`--instruction`(自然语言指令：描述音色/调语气语速)｜`--cfg-scale`(指令跟随强度，**官方推荐 4**)｜`--ref-text` / `--ref-text-file`(参考音精确转写，clone 必填)｜`--auto-transcribe`(内置 faster-whisper 自动转写)｜`--seed 42`；文本内嵌事件：中文 `[笑]`/`[叹气]`/`[清嗓子]`，英文 `(laugh)`/`(sigh)`

**调参三句口诀**：① 参考音 5~15s 干净人声，先于一切参数；② IndexTTS2 情感/音色解耦，dotstts 相似度靠 continuation 克隆（必须填转写）；③ 采样参数只在「效果不对」时动——单调提 temperature、不稳降 temperature。

### 8.3 产物质量评估（客观打分 + 人耳盲测）
横评/验收时用，两个脚本配合：**机器测客观，人耳测主观，缺一不可**。

```bash
# ① 客观打分（CER / 音色相似度 / 带宽 / 基频 / 削波静音 → score.json）
set BP=D:\AITools\TTS\Breeze-TTS-2-T8\T8star-Aix-Voice-Studio-*\resources\python\python.exe
set PYTHONPATH=C:\Users\Administrator\.workbuddy\tools\ttsqa
set HF_HUB_OFFLINE=1 & set HF_HOME=C:\Users\Administrator\.cache\huggingface
%BP% tests/score_tts.py --dir outputs/bench_20260904 --ref refs/小欧_24k.wav ^
    --text 文案_30s.txt --alt-text 文案_30s.txt --pattern "*_30s.wav" ^
    --kws "深度求索,英伟达,数据中心,十分之一"

# ② 改权重后免重跑 ASR
%BP% tests/score_tts.py --dir <dir> --ref <ref> --text <txt> --rescore

# ③ 生成人耳盲测页（随机编号、隐藏引擎名、可导出 JSON）
python tests/make_blind_test.py --dir outputs/bench_20260904 --ref refs/小欧_24k.wav
```

**测什么**：CER（faster-whisper 回听算字错率，越低咬字越清楚）/ sim（ECAPA `spkrec-ecapa-voxceleb` 音色余弦）/ bw（-60dB 截止频率）/ f0σ（半音标准差，<2.5 念稿感、>7 电音感）/ 削波率、静音占比、最长停顿 / 关键词读错检查。
**测不了**：自然度、机械感、情感、好不好听 —— 这些只能盲测，所以第 ③ 步不是可选项。
**环境依赖**：复用 Breeze 内置 python（torch/librosa/soundfile/faster_whisper 现成）；speechbrain+opencc 隔离装在 `.workbuddy/tools/ttsqa`（用 `PYTHONPATH` 指过去，**不要**装进任何引擎自带环境）。ECAPA 与 whisper-small 走本机 HF 缓存离线加载。

**2026-09-04 实测（同文案 156 字 / 同参考音小欧，机器分）：**
| 排名 | 引擎 | 机器分 | CER↓ | sim↑ | bw Hz↑ | 备注 |
|---|---|---|---|---|---|---|
| 1 | pilottts | 83.0 | 0.060 | 0.928 | 11977 | 综合最均衡 |
| 2 | dotstts | 82.2 | 0.060 | 0.908 | 11850 | 唯一 48kHz，最慢（RTF 4.81） |
| 3 | breeze(clone) | 80.9 | 0.058 | 0.897 | 11600 | 同质量档里速度最快 |
| 4 | indextts2 | 78.4 | 0.053 | **0.944** | 10638 | **音色最像**，但 RTF 7.66 最慢 |
| 5 | chatterbox | 60.1 | **0.128** | 0.850 | 11488 | 最快（RTF 1.44）但吞字最多、峰值 -0.36dBFS 贴顶 |
| — | breeze(design) | 52.9 | 0.087 | 0.604 | 11517 | 分数低是**因为它本就不是克隆小欧**（sim 低属预期），非质量问题 |

⚠️ **口径纪律**：同一批产物的 ground truth 必须一致 —— 走 `dub.py` 的已被品牌词替换（深度求索/英伟达），直连引擎的没替换。`--alt-text` 给两份 GT 取 CER 较小者来兜底，但**正确做法是横评统一走 `dub.py`**。另外 whisper 对部分音色会输出繁体，`norm_text` 已做繁简归一，否则 CER 会虚高到 0.48（实测踩过）。

---

## 九、扩展（丢文件夹即挂载）

- **加引擎**：绿色包丢进 `D:\AITools\TTS` → `registry.py` 的 `FINGERPRINTS` 加指纹 → `workers/` 写 `worker_xxxx.py` → `python scan.py --engines`。
- **加音色**：丢进 `音色包/常用/` 或根目录 → `python scan.py --voices` → 更新 `音色说明.md`。
- **新批量项目**：复制 `examples/分段配音模板.py`，改 `--workdir/--engine/--voice` 即可（它会自动按引擎选对情绪参数，别再像旧脚本那样给 indextts2 传 `--emotion`——那会被静默丢弃）。
- 扫描时 `音色包` 同级的非引擎目录（如 `口播稿/`）会出现在 `engines.json.unknown_dirs`，属正常，忽略即可。
- **同步到分享目录（百度网盘等）**：`python sync_share.py`（默认目标 `D:\BaiduSyncdisk\AISkill\04_音频音乐\本地配音工坊`，改目标加 `--dst`）。它做三件事：① 按固定排除表同步（跳过 `_deprecated`(1.1G)/`outputs`/`audio`/`segments`/`音频`/`runtime`/缓存，同步后仅几 MB）；② **反向扫描「目标有、源没有」的影子文件**并列出（同步覆盖不到它们，容易变成过期副本——`功能说明书_大白话版.md` 就吃过这个亏）；③ 同步后在目标副本上跑 `--health` 自检 + 校验版本号一致。用 `robocopy /E` 且**不带 /PURGE**，非破坏性，不会删目标独有文件。

## 十、注意事项与兜底

- 全程离线：dub.py 已设 `HF_HUB_OFFLINE=1` 并清代理；某引擎确需联网再自行设。
- **参考音格式（踩过坑）**：`breeze` 与 `pilottts` 引擎内部用 soundfile 读音频，**只认 wav/flac/ogg，不认 m4a/mp3**；两个 worker 已内置 ffmpeg 自动转 24k mono wav 并缓存到 `.cache/`，所以音色包里的 m4a/mp3 照用不误。`indextts2`/`dotstts`/`chatterbox` 可直接吃 mp3/m4a。
- **breeze 是常驻服务**：权重 ~7.3GB、常驻显存 ~7.7GiB，worker 首次调用自动拉起 `breeze_infer.api`（约 2-3 分钟），之后秒接。它与其它引擎显存互斥，长时不用建议手动停 7860 端口进程释放显存。
- **Breeze 权重许可**：代码 Apache-2.0，但**权重受 BreezeBlue 研究非商用许可约束**（商用需 RESONIA 书面授权）。出商用片请改用其它引擎。
- 首次加载 30~90s 正常；高频/批量先 `--serve`；一次一个引擎。
- 六类错误（oom/model_missing/invalid_param/timeout/service_down/unknown）的判别与修复命令见 `references/error-handling.md`；`--health` 是排障第一命令。
- `音色场景表.md` scan 自动生成勿手改；`音色说明.md` 人工维护。
- 快速自检：`python tests/test_smoke.py`（秒级，不加载模型）。

## 十一、上游雷达（知道但不追新 · 2026-08 核对）

绿色整合包路线优先稳定，不追上游版本；以下仅记录事实，等整合包出新版再走「九、扩展」流程挂载：
- **IndexTTS-2.5**（2026-08-10 发布）：中/英/日/西/阿 5 语言、语义 codec 50→25Hz 更快、拼音/音素可控。本机仍为 IndexTTS-2。
- **Chatterbox Turbo 350M**：英文副语言、低显存；多语主力仍是 Multilingual v3。
- **生态位参考**：2026 中文 TTS 第一梯队还有 CosyVoice3（综合/方言/流式）、Qwen3-TTS（97ms 流式）、F5-TTS、GPT-SoVITS；它们部署链路更碎，不纳入本技能「绿色开箱」路线。

---

## 变更历史

| 版本 | 日期 | 说明 |
|---|---|---|
| v0.9.4 | 2026-09-05 | **默认音色由小欧改为谭湘文徐浩伦（仅文档约定，`--voice` 仍必填）**。① 全文档默认/推荐音色 小欧→谭湘文徐浩伦：SKILL.md（§0.1 示例/§三速查/§5.3 能力表/§四音色库/§6.2 批量 JSON）、音色说明.md（用法/默认建议/选音色规则/引擎梯队）、功能说明书_大白话版.md、README.md、tests/test_smoke.py（默认 voice）。② `dub.py` 的 `--voice` **不**加代码默认——Voice Design（无参考音造音色）依赖空 `--voice` 判定（dub.py:557 `if args.voice:`），加默认会静默破坏设计模式。③ 新增 ⚠️：谭湘文徐浩伦 参考音 70.5s，远超技能 5~15s 最佳实践，作默认克隆会**更慢且更糊**，建议裁到 5~15s 再做默认（已裁到 12s，原 70.5s 备份于 音色包/备份/）。④ 谭湘文徐浩伦 已是 `voices.json` 常用组别名，无需改索引。 |
| v0.9.3 | 2026-09-04 | **修 Breeze 长文本静默截断 + 抓出「同任务慢 4.4 倍」的环境元凶**。① **主修复**：Breeze 服务端 `max_new_tokens=1500`，超长**不报错、直接少念一段尾巴**——实测 650 字只出 106.56s（覆盖率 91.1%）。`worker_breeze.py` 新增按句读自动分段（默认**超 350 字才切**，推导见 §5.3），逐段合成再拼接（复用多角色的 `concat_wavs`）；修复后同稿 133.44s、**非重复稿实测覆盖率 100.0%**。阈值刻意不定 150：实测分段**不提速**（130 字/段与 650 字/段 RTF 6.95 vs 6.98，生成耗时对长度线性），切细只会在句中插停顿。② **新增 `tests/check_coverage.py`** 截断检测器（序列对齐算覆盖率，含**重复文本防护**——重复稿会让对齐失真，脚本会主动改看长度比）。③ **修端口分裂（真隐患）**：worker 一次性合成用 7860、`dub.py --serve` 用 8735，两种用法并存会起**两个 Breeze 实例**（各 7.7GiB，12G 卡必 OOM）。端口收进 `registry.ENGINE_PORTS` 作唯一来源。④ **新增 `dub.py --stop-all-serve`** 批量停止常驻服务；抓到 tier 3 已停用的 indextts2 服务**空转 9.5 小时**占着显存。⑤ **新增显存预检**：合成前查空闲显存，不足主动告警（不阻断）。起因是同输入 A/B 实测——相同文案/音色/引擎，RTF **1.89 → 8.38**，代码一字未改，元凶是 ComfyUI 占 18GB 内存+GPU 算力、桌面应用常吃 6GB 显存导致模型溢出到内存；文档新增 §8.0「先清场再跑」。⑥ **修错误信息被截断（妨碍排障）**：`_classify_error` 原用 `output_tail[:200]` 取**开头**，而异常堆栈永远在**末尾** → 真死因被切掉，只剩正常日志（实测一次失败只看到 `[breeze] [1/2] 327 字`，报错在 200 字外）。改为「挑报错行 + 截头保尾」，并给所有错误附 `raw_tail` 原始尾部（新增字段，不动既有 type/reason/fix 契约）；⑦ **分段参数打通到主 CLI**：`--chunk-chars/--no-chunk/--gap-ms` 原本只有 worker 认，`dub.py` 层传不进去（文档写了却用不了）。⚠️ **教训复现**：新增参数要同时改三处——`argparse` 定义、`_ENGINE_OPTS` 转发白名单、`cmd_dub`/`cmd_batch` 的 `opts` 值字典（手写 dict，不查会漏）。上次是批量路径硬编码默认漏网，这次是参数透传漏网，**同一类错误第二次犯**。⑧ **消除 `cmd_dub` 里的内联执行副本（同一类错误第三次，这次从根上修）**：它和 `_run_single` 是两份重复实现，导致「多角色必须绕开常驻服务」的修复只落在批量路径、单人路径漏网——起了 `--serve` 之后 `dub.py --multi-role ...` 会被路由到 `serve.py`（它不透传 `--multi-role`）→ **静默退化成单人声**，不报错。现在 `cmd_dub` 直接委托 `_run_single`，重复实现删除。⑨ **多角色改用 `oneshot_port()`（服务端口 +100）**：统一端口后多角色会撞上常驻服务，用独立端口逼 worker 起自己的原生服务。⑩ 批量 job 支持 `text_file`（长稿不必塞进 JSON；相对路径按 **job 文件所在目录**解析，不跟 CWD 漂移）。 |
| v0.9.2 | 2026-09-04 | **Breeze 能力深挖与补齐（用户要求：自带功能全加进来）**。① **新增多角色**：`worker_breeze.py` 实装 `--multi-role/--roles/--gap-ms`（逐段合成+拼接，与 indextts2 同思路），`dub.py` 放开引擎限制（原写死「只有 indextts2 支持」），并让多角色**跳过常驻服务路径**（`serve.py` 不透传 multi-role 参数，走服务会静默退化成单人声）；② **409 自愈**：Breeze 单并发，上一请求异常中断会导致锁永久不释放（health 仍 ok 但全量 409），worker 加退避重试 3 次 → 杀进程重启服务 → 再试；`_post_once` 强制读完流（服务端在生成器 finally 里放锁）；③ 修 `--text` 必填导致多角色模式启动失败（改 default=""）；④ 实测验证：**Vocal Events 确证有效**（同文本带 `[笑][叹气]` 5.60s vs 不带 4.32s，多出 1.28s 真实事件音）；**多角色确证有效**（ECAPA 判定序列 小欧→小棠→小欧→小棠 与剧本一致）；⑤ 方言实测**存疑**——官方未支持，`--instruction "用四川话说"` 能改发音但无法确认为地道方言，文档标为实验性；⑥ 新增 §5.3 Breeze 能力全表（官方 vs 本机、含 `[S0]`~`[S9]` speaker token 的发现与 fast 路径 12G 用不了的结论）。⚠️ 教训：worker 跑在 Breeze 的 **Python 3.10** 上，f-string 表达式不能含反斜杠（3.12+ 才放宽），用 3.13 做语法检查会漏判。 |
| v0.9.1 | 2026-09-04 | **补批量路径的梯队缺口（v0.9.0 漏网）**。① **修真 bug**：`cmd_batch` 里 `engine_id = job.get("engine") or "indextts2"` 硬编码默认 —— 导演引擎走 `--batch` 时，所有不带 engine 字段的 job 都会跑去用**已停用**的 indextts2（RTF 7.66），完全绕过梯队。改为 `resolve_engine()`（不写 engine → 默认 breeze，不可用走回退链，逐 job 打印提示）；② 显式点名停用引擎（如 `--engine pilottts`）现在会**报警但放行**，保留方言/副语言救急通道，不会悄悄用；③ 批量确认为**严格串行**（纯 for 循环、无并发），与 Breeze 单并发（HTTP 409）兼容；④ 导演引擎对接示例的 `engine` 字段改为 breeze；⑤ 新增 `sync_share.py` 一键同步到分享目录（含**影子文件反向扫描**，防「目标有源没有 → 源更新覆盖不到」的过期副本问题，见 §九）。实测：2 条不带 engine 的 job 全部自动走 breeze，2/2 成功，时长回写正常；`--dry-run` 验证停用引擎告警生效。 |
| v0.9.0 | 2026-09-04 | **引擎梯队重构：breeze 默认、dotstts 二梯队、其余停用（用户试听拍板）**。① `registry.py` 新增机器可读 `tier` 字段（1=默认 / 2=备选 / 3=停用）+ `DEFAULT_ENGINE`/`FALLBACK_CHAIN`/`TIER_LABEL` 常量 + `resolve_engine()`（不传 `--engine` 走默认，不可用自动沿回退链切换，全链挂了才保底选任意就绪引擎）；② `dub.py` 三处引擎解析改走 `resolve_engine`，修「不传 `--engine` 只打印 help 不干活」的分发 bug（`if a.engine:` → 也给 text/out 让路），`--serve` 不再强制要 `--engine`，`--list-engines` 按梯队排序并显示 ①默认/②备选/停用+「勿选」标记，补 breeze 服务端口 8735；③ `worker_breeze.py` 自动转写改为**默认开启**（新增 `--no-auto-transcribe` 关闭）+ 转写结果缓存到 `.cache/breeze_refs/<名>.txt`（同一参考音只转一次）；④ 文档：§0.1/§0.2/§0.3/§5/音色说明 全部改为梯队口径，停用引擎标注「不许主动选、恢复改 tier」；⑤ **不做物理删除**——指纹与 worker 全保留，想恢复只改 tier。实测：不传 `--engine` 走 breeze，`--voice 谭湘文徐浩伦` 无需给 ref-text，15s 出片 4.40s 音频，时长回写正常。 |
| v0.8.0 | 2026-09-04 | **新增产物质量评估工具链（技能学习专家）**。① `tests/score_tts.py` 客观打分器：CER（faster-whisper 回听字错率）/ sim（ECAPA `spkrec-ecapa-voxceleb` 余弦）/ 带宽 -60dB 截止 / pyin 基频半音标准差 / 削波与静音 / 关键词读错检查，输出 `score.json` 与加权机器分（清晰度 35 · 相似度 25 · 带宽 15 · 干净度 15 · 韵律 10），支持 `--rescore` 免重跑 ASR 重算；② `tests/make_blind_test.py` 生成人耳盲测页（随机编号、隐藏引擎名、四维 1~5 分 + 采用意愿、揭晓对照、导出 JSON）——**机器测客观、人耳测主观，两者不可互相替代**；③ 新增 §8.3 评估章节（命令/依赖/口径纪律/实测排名表）。踩坑：whisper 对部分音色输出繁体 → `norm_text` 加 opencc 繁简归一（否则 CER 虚高 0.087→0.481）；同批产物 GT 口径必须一致（`--alt-text` 兜底）。 |
| v0.7.1 | 2026-09-04 | 引擎扫描降噪：registry 新增 `NON_ENGINE_DIRS`（音色包/口播稿/outputs/输出/成品），这些工作目录不再报「未识别目录，需补指纹+worker」；同时跳过 `.` 开头的隐藏目录。 |
| v0.7.0 | 2026-09-04 | **新增 Breeze TTS 2 引擎 + 音色包全引擎通用（技能学习专家）**。① 新增 `breeze` 引擎（registry 指纹 + `workers/worker_breeze.py`）：支持 Voice Clone / **Voice Design（无参考音造音色）** / Voice Direction（克隆音色+指令调语气），中英双语、中文 `[笑]` 事件标签，24kHz 输出；② worker 内置三项兜底：自动拉起 `breeze_infer.api` 常驻服务、参考音 m4a/mp3 自动转 wav、**`--auto-transcribe` 用内置 faster-whisper 自动转写 ref_text**；③ registry 新增 `python_glob` 通配（GUI 版本号升级不失效）；④ **修 pilottts 同类 bug**：它同样用 soundfile 读参考音，常用音色的 m4a/mp3 报 `Format not recognised` → worker 加 `ensure_wav()` 自动转 wav 缓存，**音色包真正全引擎通用**；⑤ dub.py 新增 `--instruction/--cfg-scale/--ref-text/--ref-text-file/--auto-transcribe`（单条与 `--batch` 双路径）；⑥ 五引擎 30s 同文案横评（产物与数据在 `outputs/bench_20260904/`）。 |
| v0.6.0 | 2026-08-31 | **全量通读+联网核对+重构（R1-R5）**。① 修真 bug：health 版本号硬编码 0.4.0→单一 VERSION；batch 字符串 shot_id 不再 ValueError；常驻服务失败真正自动回退子进程（原文档承诺但代码直接退出）；chatterbox worker 异常分支补 import json；mix_audio 缺失 SFX 导致 filtergraph 崩（输入索引重排）；`--silent` 从死参数改为按时间区间真正抬 BGM；dub_align 重跑把自己的临时文件重复扫入（幂等修复）+ atempo 不再强制改采样率；batch 多角色补上品牌词替换；registry ffprobe 通配版本号；serve 模型缓存 LRU=2 防显存堆积；`--json` 过程日志改走 stderr；stop-serve Windows taskkill /T；多角色临时剧本改唯一文件名并在进程退出时自动清理（不再残留/并发互踩）；② 增强：mix_audio 加 `--platform`（podcast -16/shortvideo -14/broadcast -23）与 `--two-pass`；③ 结构：幕次统一为幕四·后期（删矛盾的幕五表述）、新增 §0 前置 Agent 速用/决策树/硬约束、dub_segments 一次性脚本参数化为 examples 模板、_verify 重写为 tests、清 voice_tags 10 个死键；④ 文档：PilotTTS 厂商更正为高德 AMAPVOICE（11 情绪/14 方言）、dotstts 补 mf 求快路径、参考音口径统一 5~15s、新增上游雷达。静态 63 + 逻辑 33 + batch 12 + 混音/对齐真机 + 旧版阴性对照，全量回归 27/27 绿。 |
| v0.5.1 | 2026-08-16 | mix_audio.py 实装 + 响度归一：脚本补 `--loudness` 参数（默认 -16，无台词传 -18），混音后 loudnorm（True Peak ≤ -1dBTP），宫崎骏 10s 实测跑通；命令模板改为真实参数。 |
| v0.5.0 | 2026-08-16 | 新增混音能力：编剧 B/C 块选曲正典、情绪四轴与选曲三查、双栈响度规范、三大对位法与 3 秒规则、架构表补 mix_audio.py。 |
| v0.4.3 | 2026-08-14 | 新增「从录制分段生成 voice_jobs」字段映射；死引用 15-gen-engine-contract→12-engine-onboarding §3。 |
| v0.4.2 | 2026-08-14 | 对接导演引擎 v3.x，调度权表述更正。 |
| v0.4.1 | 2026-08-13 | 去 G-02 等废弃代号与重复小节，引用统一到 12-engine-onboarding。 |
| v0.4.0 | 2026-08-12 | 全面重构 +「常用」音色分组与置顶、音色说明.md、scan 自动生成场景表、快速上手前置。 |
| v0.3.1 | 2026-08-12 | indextts2/dotstts 最佳实践蒸馏 + 调参三句口诀。 |
| v0.3.0 | 2026-08-08 | 导演引擎对接：--health/--dry-run/--json/--batch、实测时长回写、六类错误协议。 |
| v0.2.x | 2026-08-05 | 常驻服务、品牌词典、多角色、--fast、四引擎 worker 实装。 |
