# 本地配音工坊（人读说明 · v0.6.0）

> 本文是给人看的入门 + 参数手册。**Agent 调用一律以 `SKILL.md` 为准**（快速上手/决策树/对接协议都在那）；本文保留一份比 SKILL 更细的「五引擎完整参数表」（§5），默认值已与 worker 源码逐行对齐。零基础先看 `功能说明书_大白话版.md`。

本地多引擎 AI 配音 · Windows 绿色整合包路线。把已下载好的 **IndexTTS2 / dots.tts / PilotTTS / Chatterbox-V3** 四套引擎（EmotionTTS 为编排层不直连）统一封装，自选引擎+音色合成 WAV，另带分镜对齐 `dub_align.py` 与三轨混音 `mix_audio.py`。全程本机 GPU（RTX 4070 12G）离线跑，数据不出本机，各引擎自带 Python 与权重，不重装依赖。

---

## 一、设计思路

每个引擎都是自带 Python + 权重的绿色整合包。本技能不重装依赖、不下载模型，只写一层薄 worker 调起引擎自带 Python：
- `registry.py`：扫描 `D:\AITools\TTS`，按指纹识别引擎、扫 `音色包`（含 `常用/`）建索引；ffprobe 按 `emotionTTS*` 通配，不写死版本号。
- `workers/worker_*.py`：一个引擎一个薄封装，复用官方调用路径。
- `dub.py`：统一入口，优先复用常驻服务（失败自动回退一次性进程）→ 校验 WAV → 回写实测时长。
- `serve.py`：常驻服务，模型缓存 LRU=2 防显存堆积。
- `scan.py`：丢新引擎/新音色后跑一次重新挂载。

## 二、已挂载引擎（扫描自 D:\AITools\TTS）

| 引擎 | 来源 | 采样率 | 首选场景 |
|---|---|---|---|
| **IndexTTS2** | B 站 IndexTTS-2 | 22050 | 中文综合最稳、情感/音色解耦、时长可控、多角色 |
| **dots.tts** | 小红书 HiLab（soar/mf） | 48000 | 48k 高保真、多语种 SOTA；求快 `--model dots-tts-mf`（NFE=4） |
| **PilotTTS** | 高德 AMAPVOICE | 24000 | SIM 0.862、11 类情绪、笑哭叹气副语言、14 方言 |
| **Chatterbox-V3** | Resemble AI | 24000 | 23 语种 + zh-cmn 中文精调、零样本、可无参考音（PerTh 水印） |
| EmotionTTS | 编排层 | — | LLM 自动选情绪，底层=IndexTTS2，用其 WebUI，dub.py 不直连 |

## 三、快速开始

> 命令都在**本技能根目录**执行（不写死绝对路径）。

```bash
python scan.py                                   # 首次：生成 engines.json/voices.json
python dub.py --health                           # 体检
python dub.py --list-voices                      # 看音色（⭐常用置顶）

# 单条合成（默认 breeze + 常用音色）
python dub.py --voice 谭湘文徐浩伦 --text "今天天气真好。" --out outputs/demo.wav
python dub.py --engine dotstts   --voice 小棠 --text "你好，这是测试。" --language zh --out outputs/d.wav
python dub.py --engine pilottts  --voice 谭湘文徐浩伦 --text "我真的好难过。" --emotion sad --out outputs/p.wav
python dub.py --engine chatterbox --model zh-cmn --text "这本书讲勇气与成长。" --out outputs/c.wav   # 内置音色，可不给参考音

# 多角色（[角色名]: 内容 每行一条，仅 indextts2，≤20 角色）
python dub.py --engine indextts2 --multi-role 剧本.txt --roles "子言=子言,小棠=小棠" --out outputs/对话.wav
```
多角色剧本格式：
```text
[子言]: 你好，请问你是哪位？
[小棠]: 我是小棠，一起出去走走吧。
```
每个出现的角色都要在 `--roles` 里配置；等号右边可写音色别名或任意 wav 路径。首次加载 30~90s 正常，连发先 `--serve`（§7）。

## 四、命令参考（dub.py）

| 参数 | 说明 |
|---|---|
| `--list-engines` / `--list-voices` | 列引擎 / 列音色（都支持 `--json`） |
| `--health` | 体检（引擎/音色/服务/版本号） |
| `--engine <id>` | indextts2 / dotstts / pilottts / chatterbox（emotiontts 会被拦并提示走 WebUI） |
| `--voice <别名>` / `--ref <wav>` | 用音色别名，或直接给参考音路径 |
| `--text` / `--text-file` | 要合成的文本 / 长文本文件 |
| `--multi-role` + `--roles` | 多角色剧本 + 角色→音色映射（仅 indextts2） |
| `--out` | 输出路径，默认 `outputs/dub.wav` |
| `--batch jobs.json --json` | 批量消费 voice_jobs，逐镜合成+回写时长；**过程日志走 stderr，stdout 只有 JSON**；shot_id 允许字符串 |
| `--dry-run` | 只构造 worker 命令不合成 |
| `--serve` / `--stop-serve` / `--no-serve` | 起常驻服务 / 停（taskkill 连进程树）/ 强制一次性 |
| `--fast` | beam 3→2（indextts2）、steps 10→8（dotstts），只在未手动传参时套 |
| `--brand-mode zh/en/off` | 品牌词读法，默认 zh |
| `--sync-glossary` | 把 brands.json 合并进 IndexTTS2 原生 glossary（需 PyYAML，缺了有提示） |

**情感控制按引擎选（选错会被静默过滤，别混）**
- IndexTTS2：`--emo-text "悲伤地念"`（**不是 --emotion**）/ `--emo-audio` / `--emo-vector` / `--emo-alpha`
- dots.tts：`--prompt-text`（参考音转写，continuation 克隆必填才像）
- PilotTTS：`--emotion`（13 标签可逗号叠加）+ 文本内嵌 `<|LAUGH|>`/`<|CRY|>`/`<|SIGH|>`
- Chatterbox：`--exaggeration` / `--cfg-weight`，可省参考音

> dots.tts 语言：引擎原生只认 ISO 代码，dub.py 已把 `chinese` 等中文名自动归一为 `zh`。

## 五、完整参数参考（专业调参 · 4 引擎 + 编排层）

> 以下默认值与各 worker 源码逐行对齐（2026-08-31）；按引擎 id 自动过滤，不会误传到别的引擎。

### 5.1 IndexTTS2（输出 22050Hz）
| 参数 | 含义 | 默认 | 调参建议 |
|---|---|---|---|
| `--device`/`--fp16` | 设备/半精度 | cuda 自动 / 关 | 显存紧再开 fp16（默认 bf16 已够省） |
| `--emo-text` | 自然语言情感 | 无 | 最省事；社区配 `--emo-alpha 0.6` |
| `--emo-audio` | 情感参考音 | 无 | 复刻某段情绪 |
| `--emo-vector` | 8 维向量 | 无 | `[高兴,愤怒,悲伤,恐惧,厌恶,忧郁,惊讶,平静]` |
| `--emo-alpha` | 情感强度 | **1.0** | worker 不衰减；社区手感音频 0.65~0.8 / 文本 0.6 |
| `--max-tokens` | 每段 token 上限 | 120 | 长句调大 |
| `--top-p`/`--top-k`/`--temperature` | 采样 | 0.8 / 30 / 0.8 | 想更稳降 temp、升 top_k |
| `--num-beams` | beam 数 | 3 | 配 `--use-random` 关 beam 走纯随机 |
| `--repetition-penalty` | 重复惩罚 | 10.0 | 复读调高 |
| `--max-mel-tokens` | 单段 mel 上限 | 1500 | 一般不动 |
| `--interval-silence` | 句间静音 ms | 200 | 调大换气更明显 |
| `--more-segment-before` | 前瞻段数 | 0 | 长句连贯 |
| `--use-random` | 随机开关 | 关 | 开=弃 beam，更自然但易飘 |

### 5.2 dots.tts（全连续自回归 · 48kHz）
| 参数 | 含义 | 默认 | 调参建议 |
|---|---|---|---|
| `--model` | 权重 | soar（最佳 SIM） | 求快换 `dots-tts-mf`（NFE=4，约 2×） |
| `--prompt-text` | 参考音转写 | 无 | continuation 克隆填了才像 |
| `--language` | 语种 | 无 | ISO：zh/en/ja/ko，中文名自动归一 |
| `--num-steps` | 扩散步数 | 10 | 质量↑步数↑但慢；--fast 降到 8 |
| `--guidance-scale` | 引导强度 | 1.2 | >2 能量越来越大，别乱拉 |
| `--speaker-scale` | 相似度 | 1.5 | 想更像调向 1.8 |
| `--precision` | 精度 | bfloat16 | 也可 float16 |
| `--max-len` | 音频 patch 上限 | 500 | 长文本调大 |
| `--normalize-text` | 文本归一 | 关 | 新闻/数据类建议开（裸开关） |
| `--ode-method` | ODE 求解器 | euler | midpoint 更稳 |
| —（不透传 seed） | 上游有 seed，worker 未接 | — | 改节奏靠断句/步数/换参考音 |

### 5.3 PilotTTS（SIM 0.862 + 副语言 + 方言，输出 24000Hz）
| 参数 | 含义 | 默认 | 调参建议 |
|---|---|---|---|
| `--emotion` | 情感标签（13 个，可逗号叠加） | 无 | happy/sad/angry/surprise/fear/disgust/serious/concern/blue/disdain/neutral/psychology/unknown；决定加载 base 还是 instruct 权重（加载期） |
| `--language` | 语种/方言 | zh | en 或方言码（如 zh-tianjin、zh-sichuan，共 14 方言） |
| `--top-p`/`--top-k`/`--temperature` | 采样 | 0.95 / base=1·instruct=20 / 1.0 | 默认即官方推荐 |

副语言写进文本：`太好笑了<|LAUGH|>`、`<|CRY|>`、`<|SIGH|>`（半角竖线大写）。

### 5.4 Chatterbox-V3（23 语种 + 中文精调 + PerTh 水印，输出 24000Hz）
| 参数 | 含义 | 默认 | 调参建议 |
|---|---|---|---|
| `--model` | 权重 | v3 | `chatterbox-v3` 通用 / `chatterbox-zh-cmn` 中文精调 |
| `--language` | ISO 语种 | zh | zh/en/ja/ko… 23 种 |
| `--exaggeration` | 夸张度 | 0.5（0.25~2.0） | 叙事 0.25-0.35 / 对话 0.4-0.6 / 故事 0.65-0.85 / 角色 0.9-1.2 |
| `--cfg-weight` | 节奏/CFG | 0.5（0.2~1.0） | 夸张度↑时降它防抢拍 |
| `--temperature` | 随机性 | 0.8（0.05~5） | — |
| `--min-p` | 最小概率阈值 | 0.05 | 滤低概率噪声 |
| `--top-p` | 核采样 | 1.0 | — |
| `--repetition-penalty` | 重复惩罚 | 1.2（1~2） | 复读调高 |
| `--seed` | 种子 | 0=随机 | 固定值可复现 |

### 5.5 EmotionTTS（编排层）
不直连，用其自带 WebUI；底层 = IndexTTS2，参数同 5.1。

## 六、品牌名词典
TTS 对 DeepSeek/ChatGPT/RTX/B站这类名字必读错。文本进引擎前由 `lexicon.py` 按 `brands.json`（**有效 167 条** = 169 键减 2 个 `_` 注释键）替换，全引擎通用，单条/批量/多角色路径都生效。
- `--brand-mode zh`（默认，DeepSeek→深度求索、GPT-4o→吉皮提四欧）/ `en`（逐字母读法）/ `off`。
- 自己加：编辑 brands.json 加 `"品牌":{"zh":"...","en":"..."}`，热更新；长词优先（先 GPT-4o 后 GPT）。
- `--sync-glossary` 仅为 IndexTTS2 原生 WebUI 补一份 glossary.yaml（需当前 Python 有 PyYAML）；统一入口合成不需要它。

## 七、运行加速
**常驻服务 `--serve`**：默认每次合成冷启动要重新加载模型（约 90s）。起服务后模型常驻，第二条起秒级：
```bash
python dub.py --engine indextts2 --serve           # 首次仍加载 30~90s
python dub.py --engine indextts2 --voice 谭湘文徐浩伦 --text "第一条" --out outputs/a.wav  # 自动路由
python dub.py --engine indextts2 --stop-serve      # 用完停
```
- 端口按引擎隔离：indextts2 8731 / dotstts 8732 / pilottts 8733 / chatterbox 8734。
- 换加载期参数（device/fp16/model/precision/emotion）自动加载新模型，缓存 LRU=2，挤出旧模型自动清显存。
- **服务偶发失败会自动回退一次性子进程**（v0.6 起真正实现），任务不中断。

**`--fast`**：只在你没手动改对应参数时套（indextts2 beam 3→2、mel 1500→1200；dotstts steps 10→8），草稿快、出成品去掉。dotstts 想更激进直接换 mf 权重。

## 八、三轨混音（mix_audio.py）与对齐（dub_align.py）
- 对齐：`python dub_align.py --dir 分段目录 --durations 3.5,2.5,... -o 配音_对齐.wav`（原速+定格余量，超了才 atempo；v0.6 起重跑幂等、不再把临时文件重复扫入）。
- 混音：口播+BGM+SFX。BGM 有台词段自动侧链闪避，`--silent S02` 标注的无台词镜 BGM 自动抬到满音量；缺的 SFX 跳过不崩。
  - `--platform podcast(-16 默认)/shortvideo(-14)/broadcast(-23)`，`--loudness` 数值覆盖；`--two-pass` 广播级精确响度。
  - H3 模式 `--videos S01.mp4,...`；库选曲 `--bgm-track 曲目.mp3`；完整方法论见 SKILL §6.5。

## 九、音色
⭐常用 6 个（`音色包/常用/`，置顶）：小欧/主持人-女/新闻腔-女/纪录片/播客/小萝莉。精品 6（根目录）：小棠(活泼口播)、小昊(深度评论)、小雨(科普职场)、思思(情感有声书)、明宇(新闻商务)、子言(情感旁白)。全量看 `音色场景表.md`（scan 自动生成）。参考音 5~15s 干净人声；不用的移到 `音色包/备份/` 自动排除。

## 十、扩展
- 加引擎：绿色包丢 `D:\AITools\TTS` → registry 加指纹 → workers 写 worker → `python scan.py --engines`。
- 加音色：丢 `音色包/常用/` 或根目录 → `python scan.py --voices`。
- 新项目批量：复制 `examples/分段配音模板.py` 改参数（它会按引擎自动选对情绪参数）。
- 自检：`python tests/test_smoke.py`（秒级不加载模型）。

## 十一、目录结构（v0.6 实际）
```
本地配音工坊/
├── SKILL.md / README.md / 功能说明书_大白话版.md
├── dub.py 统一入口 · registry.py 指纹索引 · scan.py 扫描器
├── serve.py 常驻服务 · lexicon.py 品牌词 · dub_align.py 对齐 · mix_audio.py 混音
├── brands.json(167) / engines.json / voices.json(25) / voice_tags.json
├── workers/（4 个 worker + __init__）
├── references/（error-handling + 两份 best-practices）
├── examples/（分段配音模板 + 归档的旧一次性脚本）
├── tests/（test_smoke.py）
├── 音色说明.md（人工）/ 音色场景表.md（scan 生成）
└── outputs/（合成产物，自动建）
```

## 十二、故障排查
- 先 `python dub.py --health`，再 `--dry-run` 看命令构造，最后看引擎输出尾部；完整错误分类见 `references/error-handling.md`。
- 报网络/下载：已强制离线（HF_HUB_OFFLINE=1）并清代理，确需联网自行设。
- 显存不足：一次一个引擎、`--fast`、关其它占显存程序；dotstts soar/mf 各约 4GB 级。
- 参考音 <3s 不稳、>15s 建议裁剪；mp3/m4a 可读，异常转 wav。
