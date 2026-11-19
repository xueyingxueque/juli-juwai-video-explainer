# Agent 对接文档 —— 剧里剧外 · 视频解说引擎

> 面向：Codex / Claude / 其他编码 Agent，任务是**优化或调用这个技能**
> 读完这份你应该能直接动手，不需要再问人。
>
> **本文件是「改代码」的唯一硬约束正典。**
> 概念与流程看 [SKILL.md](../SKILL.md)；踩坑查 [references/00-pitfalls.md](../references/00-pitfalls.md)。

---

## 0. 一分钟现状

**技能路径**：`C:\Users\Administrator\.workbuddy\skills\剧里剧外-视频解说引擎`

**三层入口（主次分明）**：

| 入口 | 用途 | 使用频率 |
|---|---|---|
| `scripts/produce.py --pilot <清单> --prefix <前缀>` | **唯一生产总入口**：10 步幂等状态机 | ★★★ 日常生产 |
| `scripts/build.py <plan.json>` | plan → 成片（下游装配层，被 produce.py 调用） | ★★ 调试 |
| `scripts/pipeline.py run` | 单集 S1–S6 端到端（验证场景） | ★ 验证 |

**已稳定能力**：配音 → ASR 定位原声 → 自动定时长 → 切画面 → 混音（duck）→ ASS 字幕 → **自动校验不过不交付**

**验证状态**：主力产线已在多部剧上出片交付；`eval_skill.py` **28 条断言全过**
是当前健康基线（详见 `evals/eval_report.json`），学习报告扫描 **51 集**
（详见 `evals/learning_report.json`）。
单集 `pipeline.py` 仅在《山藏万象》EP01 通过一次（成片 29.43s，**低于现行 60s 下限**），
属历史验证记录，**单样本通过 ≠ 跨素材稳定**。

---

## 1. 代码地图（改哪里）

| 文件 | 行数 | 职责 | 状态 |
|---|---|---|---|
| `scripts/build.py` | — | **plan→成片唯一入口**（六步装配 + 校验 + 贴纸层 + gen 段） | ✅ 活跃 |
| `scripts/pipeline.py` | — | 单集 S1-S6 编排入口（run/status/doctor/batch/qc） | ✅ 单样本 E2E 通过 |
| `scripts/quality_gate.py` | — | 硬门/软门两档质检 + A/B/C 分档 | ✅ |
| `scripts/s3_script.py` | — | S3 解说成稿 + POV/一致性/可读性门 | ✅ |
| `scripts/s2_story.py` | — | S2 剧情解构 + 证据绑定核验门 | ✅ |
| `scripts/series_decompose.py` | — | 全剧分解 → events.json（多集） | ⚠️ 独立运行，未入编排 |
| `scripts/series_plan.py` | — | 故事线重排 → plan（多集） | ⚠️ 独立运行，未入编排 |
| `scripts/preflight.py` | — | 零阶段能力预检门 | ✅ pipeline 首位 |
| `scripts/material_precheck.py` | — | 素材预检（拒绝"解说成片"类素材） | ✅ |
| `scripts/s1_ingest.py` | — | S1 抽帧、帧时间索引、统一 ASR | ✅ |
| `scripts/script_to_plan.py` | — | S3 逐句稿 → 剪辑 plan 映射 | ✅ |
| `scripts/plan_story.py` | — | plan 校验（有 events.json 时核对集数/时间） | ✅ |
| `scripts/plan_est.py` | ~30 | **时长预估唯一正典**（assemble/bgm_select 共享） | ✅ |
| `scripts/new_task.py` | — | 任务脚手架（_plan 三脚本从 `_task_template/` 拷贝） | ✅ |
| `scripts/bgm_select.py` | — | BGM 自动选曲（回写 plan.bgm） | ✅ |
| `scripts/make_cover.py` | — | 封面 make/burn（海报优先 > 视频帧） | ✅ |
| `scripts/extract_voice_ref.py` | — | 主角音色零样本克隆参考提取 | ✅ |
| `scripts/deliver.sh` | — | 单文件一键交付 | ✅ |
| `scripts/recontinue.py` | — | 续跑（dataset 从 build-plan.json 读，防串剧） | ✅ |
| `scripts/asr.py` / `vlm.py` / `asr_bench.py` / `fetch_asr.py` | — | ASR 三层 / VLM 封装 / 基准 / 模型续传 | 底层库 |
| `scripts/ensure_vlm.ps1` | — | VLM 守护脚本 | 工具 |
| `scripts/_archive/*` | — | 历史补丁 + 构建垃圾（`.bak_*`/`__pycache__` 已收拢） | ❌ 勿读勿改 |

### ⚠️ 两个入口的分工（不要搞混）

```
pipeline.py run  = 单集端到端编排（上游）
   素材预检 → 能力预检 → S1 → S2 → S3 → plan 适配
   → ★ S4 调 build.py 装配 → S5 quality_gate → S6 交付登记

build.py         = plan→成片（下游，可独立运行）
   配音 → 原声 ASR 定位 → 定时长 → 切画面 → 混音 → 烧字幕 → 校验
```

- `build.py` **不调用** pipeline，也不做素材预检/写稿/质检
- `pipeline.py` **不实现**混音/字幕逻辑，全部委托给 build.py
- **`produce.py` 是唯一生产总入口**；build.py 是下游装配层、pipeline.py 是验证路径，不要再新建第四个
- `pipeline.py` **不调用** `series_decompose.py` / `series_plan.py`

---

## 2. build.py 内部结构

```python
# 工具
class BuildError(RuntimeError)   # ★ 任何失败都抛这个，绝不留半成品
sh()             # 执行命令，失败抛异常
sh_tree()        # ★ 连根杀进程树（TTS 会 fork 孙进程，见 C15/P67）
quiet()          # 执行命令，返回 bool
probe_dur()      # 媒体时长
probe_streams()  # 各 stream 类型+时长（★ 校验音轨完整性，C2）
rms_windows()    # 逐窗 RMS（Python 直算，ffmpeg 的 astats 在本机不准，C10）
find_src()       # 找源文件，兼容 `N.mp4` / `第N集.mp4`

# 六步
narrate()      # [1] 配音 → speech/seg{k}.wav
cut_raw()      # [2] 原声 ASR 定位切片 → raw{k}.wav
retime()       # [3] 定时长（唯一数据源 = segments）
cut_clips()    # [4] 切画面 → c{k}.mp4
concat()       #      拼接 → silent.mp4
mix()          # [5] 混音 → mixed.mp4
write_srt()    # [6] 字幕 → subs.srt
burn()         #      烧 ASS → 成片
verify()       #      校验（不过不交付）
```

### ★ 关键常量真值表（2026-10-29 从代码核对，与代码冲突时以代码为准）

```python
W, H, FPS = 1080, 1920, 30    # 竖屏规格
SPEECH_LUFS = -13             # ★ 段级主声源（故意比成片目标高 1 LU，给母带留下调空间）
BREATH = 0.35                 # 每段结尾呼吸留白（2026-11-08 由 0.45 校准）
MAX_TTS_ATTEMPTS = 8          # TTS 段级重试上限（P70：8 次全废概率 ≈ 2.6e-6）
TTS_TIMEOUT = 300             # 单次 TTS 超时（秒）
TAKE = 0.8                    # 引文句子的锚定取样长度（禁止扩展，防漂到下一张字幕卡）
CENTER_BACK = 0.15            # ★ 普通句子从目标帧往前留的余量（贴帧起取连续画面）
TAKE_MAX = 2.4                # = max(0.4, 版权门 max_continuous_original_sec(2.5) - 0.1)
RAW_LEAD = 0.2                # 原声切片在台词起点前留的前导
```

**★ `DEFAULT_VOICE` 已废除**。无 `--voice` 且 plan 无 `voice` 字段 → **直接报错退出**
（用户令：禁无脑默认音色）。不要再引入默认值。

### 响度分层（配置声明 + build.py 实际生效）

| 层 | 目标 | 实现 |
|---|---|---|
| 段级主声源 | `-13 LUFS` / TP `-3.0` | `loudnorm=I=-13:TP=-3.0:LRA=11` |
| **成片（母带）** | **`-14 LUFS`** / TP `-1.5` | alimiter 限幅 → 测 input_i → `volume` 补增益 |
| BGM | 解说段 0.20 / 原声段 0.06 | `volume=enable='between(t,...)'` 逐段 duck |
| 环境音 | **不混** | 原片底噪是噪音源 |

成片编码声明（`config/engine.json → output`）：`1080×1920 / fps 30 / libx264 crf 20 / aac 192k / -14 LUFS ±2 / TP -1.5`。⚠️ 其中响度类常量当前由 `build.py` 顶部实际生效，`contracts.py --audit` 守护两者不漂移（P88）。

### 段级滤镜链（P77 修复后，顺序不可改）

```
highpass=f=90, afftdn=nf=-34,
adelay={ms}:all=1,                                  # ★必须在 loudnorm 之前
agate=range=0.03:threshold=0.015:attack=5:release=300,   # 门掉 adelay 的前导静音
atrim=0:{段结束+0.5},                                # 裁掉尾静音（省一半混音计算）
loudnorm=I=-13:TP=-3.0:LRA=11                        # ★必须最后
```
> `loudnorm`（单遍 dynamic 模式）放在 `adelay` 之前会让 adelay **彻底失效** → 段间响度差 24dB。
> 详见坑典 **P77**。

---

## 3. 不可违背的约束（改代码前必读）

这些不是风格偏好，是**实测踩出来的**。破坏它们会复现已修好的 bug。
（每条都对应 `references/00-pitfalls.md` 里的一个编号。）

### C1. `amix` 必须 `duration=longest`　→ P01

```python
# ✗ duration=first 取最短输入，会把整条音轨截断
amix=inputs=N:duration=first:normalize=0
# ✓
amix=inputs=N:duration=longest:normalize=0,apad=whole_dur={total},atrim=0:{total}[mix]
```
**实测**：20.5s 音轨被截成 2.02s，成片后 18 秒完全没声音，且 `silencedetect` 检不出来。

### C2. 音轨长度必须校验　→ P01

```python
st = probe_streams(video); v, a = st['video'], st['audio']
if a < v - 0.5: raise BuildError(...)
```
**这是唯一能发现 C1 的手段**。文件本身就是坏的时，`silencedetect`/`astats`/`volumedetect` 都查不出来。

### C3. 解说与原声结构互斥

段落有 text → 只铺 seg 音轨；无 text → 只铺 raw 音轨。**原声段 text 必须留空**，否则同一句话说两遍。

### C4. 段长由实际音频决定

`duration = 配音实测时长 + BREATH`（解说段）/ `ASR 切片实测时长`（原声段）。
禁止「先定段长再让配音去凑」——那会强行变速。

### C5. 画面以目标帧为中心取样

```python
# 普通句：往前 CENTER_BACK=0.15s 起取，长度 min(d+0.3, TAKE_MAX)
ss = t - 0.15; take = min(d + 0.3, 2.4)
# 引文句：ss = t，取 TAKE=0.8s
```
- ❌ `ss = t` 往后取（普通句）→ 串到下一镜头
- ❌ `ss = t - d/2`（d 大于镜头长度）→ 同样串

### C6. 字幕必须走 ASS　→ P10/P11/P12

- SRT 必须 utf-8 **无 BOM** —— 带 BOM 时 libass **静默跳过整个文件**，ffmpeg 零报错
- `BorderStyle=3`（底栏）在 SRT 下**不渲染** —— SRT 没有 PlayResX/Y
- 正解：SRT → ASS，显式声明 `PlayResX:1080 / PlayResY:1920`，用 `ass=filename=` 滤镜
- 路径三约束：纯 ASCII + 正斜杠 + 转义盘符冒号

### C7. BGM duck 用逗号串联，不能用 `|`

```python
# ✗ | 被当滤镜分隔符 → Invalid argument
volume="between(t,0,2):volume=0.2|between(t,2,7):volume=0.06"
# ✓ 逗号串联 + between() 单引号包裹
volume=enable='between(t,0,2)':volume=0.2,volume=enable='between(t,2,7)':volume=0.06
```

### C8. Python 格式化一律用 f-string

```python
# ✗ % 从左到右只作用一次，方法调用会抢先在元组上执行
'%02d:%02d:%06.3f' % (h, m, s).replace('.', ',')   # tuple 没有 replace
# ✓
f'{h:02d}:{m:02d}:{s:06.3f}'.replace('.', ',')
```

### C9. 含 Windows 路径的 docstring 用 raw string

```python
def foo():
    r"""... C:\Users\..."""     # ✓ 否则 \U 被当 Unicode 转义
```

### C10. 判断静音用 `silencedetect`，不用 `astats`

```bash
ffmpeg -i x.mp4 -af silencedetect=noise=-50dB:d=0.6 -f null -
```
`astats` 的 RMS_level 经 `ametadata=print` 读 stdout 在本机不稳，**实测把 -18dB 误读成 -84dB**。
需要逐窗数值时用 `rms_windows()`（Python 直算 wav）。

### C11. shotmap 的 `original_footage_sec` 必须与 `cut_clips` 实切规则一致

否则合规数据失真（旧版恒 0.8s，与实切 `min(d+0.3, TAKE_MAX)` 不符）。
`TAKE_MAX` 与版权门阈值联动：`max(0.4, max_continuous_original_sec - 0.1)`。

### C12. 每段严格 `trim=d`；concat 后 `|silent-total| > 1.0s` 直接 BuildError（代码真值 1.0s）

音画对齐的最后一道哨兵。哨兵阈值放宽到 1.0s（31 段 × 1/30fps ≈ 0.6s 的帧量化累计会误杀）。

### C13. overlay / 合成类 ffmpeg 必须显式 `-map 0:a?`

否则音轨丢（合成输出默认只取视频流）。

### C14. GIF 不直连 overlay，先预转明确时长 mp4（fps=15）

直连会按 1fps 处理 → 贴纸几乎不动。

### C15. 杀进程用 `sh_tree()`，不要 `subprocess.run(timeout=)`　→ P67

`dub.py` 会 fork 常驻 `worker_dotstts.py` 孙进程（单个 5–11GB）。
`timeout=` 超时只杀直接子进程，worker 变孤儿 → **每次超时泄漏 5–11GB**。
用 `CREATE_NEW_PROCESS_GROUP` + `taskkill /F /T /PID` 连根杀。
**排查铁律：配音连续超时时，先看「可用内存」和 `worker_dotstts.py` 进程数（正常 ≤1），再谈超时阈值。**

### C16. Windows 上 subprocess 调外部命令**一律用绝对路径**　→ P79

```python
GIT_BASH   = r'C:/Program Files/Git/bin/bash.exe'
POWERSHELL = r'C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
```
- 裸 `bash` → 解析成 `C:\Windows\System32\bash.exe`（**WSL 启动器**），打印 `wsl.exe --install` 并 exit 1
- 裸 `powershell` → **本机 PATH 里根本没有 WindowsPowerShell 目录**，`FileNotFoundError: [WinError 2]`

---

## 4. 数据契约

### 输入：plan.json

```json
{
  "name": "EP01_三岁种菜",
  "series_index": 1,
  "voice": "ref:voice_ref.wav",
  "bgm": "D:/.../xxx.mp3",
  "segments": [
    {"ep": 1, "t": 4.10, "text": "解说词", "raw": 0},
    {"ep": 2, "t": 11.72, "text": "",       "raw": 1}
  ]
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | str | ✅ | 成片文件名（无扩展名自动补 .mp4） |
| `voice` | str | ✅★ | 音色。**缺省且 `--voice` 也未给 → 报错退出** |
| `bgm` | str | | BGM 路径（`bgm_select.py` 回写） |
| `segments[].ep` | int | ✅ | 集数 |
| `segments[].t` | float | ✅ | 目标画面时间点（秒） |
| `segments[].text` | str | ✅ | **空字符串 = 纯原声段**（C3） |
| `segments[].raw` | 0/1 | | 1 = 原声段 |
| `segments[].gen` | str | | AI 二创镜头绝对路径（mp4/png/gif），该段不切原剧 |
| `segments[].stickers` | list | | 表情包 `[{meme_path, anchor, scale, show}]`，单段 ≤2，原声段不贴 |

**`duration` / `raw_start` / `clip_duration` 由脚本写入，不要手填。**

### 中间产物（`_work/`）

```
speech/seg{k}.wav   k 为 0-based，仅解说段
raw{k}.wav          k 为 1-based（★ 与 seg 不同！），仅原声段
c{k}.mp4            0-based，分段画面
silent.mp4          无音拼接
mixed.mp4           混音后（无字幕）
subs.srt            字幕（utf-8 无 BOM）
_mix.wav / _probe.wav   临时文件
_speech/_sig.json   TTS 复用签名（音色 + 逐段原文）
```

> ⚠️ **seg 是 0-based、raw 是 1-based**（历史遗留）。优化时可统一为 0-based
> （改动点：`narrate()` / `mix()` 里的 `k+1`）——见 §5 P4。

### 输出

```
<out-dir>/<name>.mp4        成片（字幕已烧）
<out-dir>/build-plan.json   派生数据（★ 不覆盖输入 plan）
<out-dir>/shotmap.json      镜头映射（合规数据）
```

---

## 5. 优化方向（按优先级）

### P0 · 主力产线上游自动化（当前最大缺口）

产线「ev → 成片」已全自动化，但**上游仍靠人工**：读全剧字幕定剧情 → 手写 spec → 挑二创点。

**待做**：把「读字幕 → 生成候选 spec」做成可复核的半自动（Agent 出草案 + 人工确认），
而不是追求全自动——内容层错误代价远高于时间成本（见 `references/content-first.md`）。

### P1 · 单集 pipeline 多样本验证

《山藏万象》EP01 只通过一次。需要不同对白密度、字幕密度、人物数量的真实集数验证预算策略。
**不要为达到时长放松 grounding 门槛**。

### P2 · plan.json 自动生成

`plan_story.py` 增加「从 events.json 自动选段」模式：按叙事弧选（起承转合）而非按关键词抓高光；
**选画面必须用 VLM `desc` 过滤**，不能只按时间点；自动避坑口播与原声重复（C3）。

### P3 · 平台适配剩余项

safe box 是几何核验，**不识别原片烧录字幕的位置**，实际冲突仍须抽帧检查。

### P4 · 编号体系统一

seg 0-based / raw 1-based → 统一 0-based（改动点：`build.py` 的 `narrate()` / `mix()` 里的 `k+1`）。

### P5 · 测试（已部分落地）

技能不变量已有 `eval_skill.py` **28 条离线断言**覆盖（时长、去重、契约、文档引用、判据收口等），
健康基线见 `evals/eval_report.json`。
仍缺**装配层单元级测试**（音轨不被截断 / 字幕真烧进去 / 段长 = 音频 + BREATH）：

```
tests/test_amix.py      断言 audio_dur ≈ video_dur（音轨不被截断）
tests/test_burn_ass.py  断言文件大小变化 > 20%（字幕真烧进去了）
tests/test_retime.py    断言段长 = 实际音频 + BREATH
```
测试素材用**短样本**（10s 级），不要跑全片。

---

## 6. 验证方法（改完必须跑）

### 单元级

```bash
cd scripts && python -c "import ast; ast.parse(open('build.py',encoding='utf-8').read())"
```

### 端到端

```bash
python build.py <plan.json> --dataset <素材目录> --voice ref:<wav> --platform douyin
```

**期望输出（必须全过）**：
```
[6/6] 烧字幕 + 校验
    视频 23.5s / 音轨 23.5s          ← 两者必须接近
    响度 -14.x LUFS / 峰值 ≤ -1.5 dBTP
    ✓ 全部校验通过
```

### 视觉验证（必做，build.py 不含）

**校验通过 ≠ 画面正确。** 改完必须抽帧看：

```bash
ffmpeg -ss 1  -i 成片.mp4 -frames:v 1 -vf scale=270:480 q1.png   # ★ -ss 在 -i 之前
ffmpeg -ss 9  -i 成片.mp4 -frames:v 1 -vf scale=270:480 q2.png
ffmpeg -ss 16 -i 成片.mp4 -frames:v 1 -vf scale=270:480 q3.png
ffmpeg -ss 21 -i 成片.mp4 -frames:v 1 -vf scale=270:480 q4.png
```

要确认三件事：
1. 每段画面是不是 plan 指定的（`ep` + `t` 对应的画面）
2. 解说字幕与原片烧录字幕是否重叠、遮挡人物或落出安全区（ASS 底栏**不会**遮住原片字幕）
3. 原声段是否没有叠加解说字幕

> ⚠️ **抽帧陷阱**：`-ss` 必须在 `-i` **之前**，否则跳帧拿到错误画面。
> 之前多次误判「字幕没烧进去」，实际是抽帧位置不对。

---

## 7. 环境依赖

```jsonc
// config/engine.json → runtime（全部绝对路径，★ 不要依赖 PATH）
"ffmpeg":     "D:/AITools/TTS/Dots.tts/wzf/ffmpeg/bin/ffmpeg.exe",
"ffprobe":    "D:/AITools/TTS/Dots.tts/wzf/ffmpeg/bin/ffprobe.exe",
"font":       "C:/Windows/Fonts/msyh.ttc",
"tts_python": "D:/AITools/TTS/Dots.tts/wzf/python.exe",
"asr_python": "D:/AITools/TTS/Breeze-TTS-2-T8/.../resources/python/python.exe",
"tts_dub_script": "C:/Users/Administrator/.workbuddy/skills/本地配音工坊/dub.py",
"llama_server": "D:/AITools/VLM/bin/llama-server.exe",
"vlm_endpoint": "http://127.0.0.1:18088"
```

**外部技能依赖**

| 技能 | 路径 | 用途 |
|---|---|---|
| 本地配音工坊 | `~/.workbuddy/skills/本地配音工坊/dub.py` | TTS（build.py 子进程调用） |
| BGM音效引擎 | `~/.workbuddy/skills/BGM音效引擎/` | 选曲 + SFX |
| BGM 素材库 | `D:/Agent引擎/音频素材库/` | 467 首 BGM + 891 个 SFX |
| 表情包引擎 | `~/.workbuddy/skills/表情包引擎/` | 贴纸检索 + 叠加 |
| ComfyUI 工具 | `D:/AITools/comfy-agent/` | `ui_to_api2.py` + `run_api_prompt.py` |

**★ TTS 常驻服务**（性能关键，7 倍差）：
```bash
dub.py --serve --engine dotstts     # 起：热身后 5.2s/段（一次性模式 ~39s/段）
dub.py --stop-all-serve             # 停：★ 必须 --stop-all-serve
                                    #   --stop-serve 只停 breeze，dotstts 根本不停（P80）
```
前提：VLM 不能同时在显存里（否则劣化到 88–95s/段）。

---

## 8. 改动守则

1. **一次改一类，不改一处** —— 发现 `%` 格式化问题就把全部改成 f-string，不要只改报错那行
2. **改完必须端到端跑**，看校验输出，不只看语法通过
3. **必须抽帧看画面** —— 校验只管音频，画面得肉眼确认
4. **注释写「为什么」不写「什么」** —— especially 踩过坑的地方，并注明坑典编号
5. **不要在 `_archive/` 里改代码** —— 那是历史补丁，只备查
6. **改代码后同步更新** SKILL.md §5 文件地图 与 §7 变更历史
7. **新增坑典必须用下一个连续 `P<n>` 编号**，并在 `00-pitfalls.md` 顶部索引登记

---

## 9. 一句话总结

> **音频链路已经可靠**（校验不过就不交付，C1–C16 都是实测踩出来的）。
> **主力产线「ev → 成片」全自动化**，健康基线由 `eval_skill.py` 28 条断言守护。
> **上游仍靠人工**（读字幕 / 写 spec / 挑二创点）——这是内容层，不该急着自动化。
> **出片必须人工审片**：校验只管音频，画面要抽帧看，不能自动发布。
