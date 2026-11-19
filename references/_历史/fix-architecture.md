# 治根源：从 5 个脚本合成一条命令（2026-08-12 00:30）

## 用户批评（说得对）

> 「一直打补丁没有从源头解决问题, 后面的视频彻底没有音了」

---

## 一、先诊断：「后面没音」的真正根因

先用 `silencedetect` 实测：

```bash
ffmpeg -i 成片.mp4 -af silencedetect=noise=-50dB:d=0.6 -f null -
```

结果：**无输出 = 实际没有静音段**。

> ⚠️ 注意：`astats` 的 RMS_level 通过 `ametadata=print` 读 stdout 在本机不稳，
> **会把 -18dB 误读成 -84dB**。判断静音必须用 `silencedetect`（走 stderr）。

**但用户听到的「后面没音」是真的** —— 根因不在成片文件，在**流程架构**。

---

## 二、★★ 架构级根因（这才是该治的）

之前是 **5 个脚本靠人工按顺序跑**，中间产物靠**文件名约定**传递：

```
series_tts.py   → _tts/raw01.wav        （1-based）
   ↓ 人工执行
临时脚本         → _work/audio/seg00.wav（0-based）← ★ 编号体系不一致
   ↓ 人工执行
mix_v6.py       → 读 _work/audio/seg*.wav + _work/r*.wav
```

**换一次音色 → 所有时长全变 → 但下游不会自动重算。**

文件名对不上时，代码走的是「**跳过**」而不是「**报错**」——
于是后面的段**静默地没有音**，而我只看到「成片文件生成了」就以为成功。

**这就是「一直打补丁」的病根：架构上允许静默失败。**

---

## 三、正解：`scripts/run_pipeline.py`（一条命令）

```bash
python run_pipeline.py <plan.json> <素材集目录>
```

**9 步全自动，48 秒完成**：

| 步 | 做什么 | 关键设计 |
|---|---|---|
| 1 | 清理解说音轨 | ★ **每次全清**，杜绝新旧音色混用 |
| 2 | 配音 | 谭湘文徐浩伦，实时打印语速并标 ⚠ |
| 3 | 原声定位切片 | **ASR 定位真实台词时间点**（字幕时间 ≠ 语音时间） |
| 4 | 按实际音频重定时长 | 解说 = 配音 + 0.5s；原声 = ASR 实测时长 |
| 5 | 切画面拼接 | 解说段居中截取；原声段从台词点起 |
| 6 | 混音 | 解说/原声互斥 + BGM duck |
| 7 | 烧字幕 | 三约束（见 §四） |
| 8 | **自动校验** | `silencedetect` 检静音 + `ebur128` 读响度 |
| 9 | 完成 | 打印规格与结构 |

**两条硬规则**：
1. **任何一步失败立即停并报明确错误，绝不留半成品**
2. **第 8 步自动校验** —— 静音段会直接报出来，**不用等用户听出来**

### 实测输出

```
[2/9] 配音（音色：谭湘文徐浩伦）
    raw01.wav   2.00s  语速 6.0 字/秒
    raw05.wav   2.00s  语速 7.5 字/秒  ⚠语速 7.5 字/秒
[3/9] 原声定位与切片（ASR 定位真实台词时间点）
    第 2段 r02.wav  4.50s  起点ASR 3.66-4.94
    第 4段 r04.wav  4.50s  起点ASR 9.70-11.30
[8/9] 校验
    时长 20.53s｜响度 -16.2 LUFS｜峰值 -1.5 dBTP
    无静音段 ✓
```

---

## 四、★ Windows 烧字幕的三个硬约束（缺一不可）

| # | 约束 | 不做的后果 |
|---|---|---|
| 1 | srt 复制到**纯 ASCII** 临时路径 | 中文路径 + force_style 必崩 |
| 2 | 路径用**正斜杠** | 反斜杠被当转义符吃掉 → `C:Users...` 找不到文件 |
| 3 | 盘符冒号**转义** | 冒号被当滤镜参数分隔符 |

```python
# ✓ 正确写法
sp = str(tmp).replace(chr(92), '/').replace(':', chr(92) + ':')
vf = "subtitles=filename='" + sp + "':force_style='" + style + "'"
```

### 附带的坑：docstring 里的 Windows 路径要用 raw string

```python
def burn_subs(...):
    r"""... C:\Users\... ..."""     # ✓ 必须 r"""，否则 \U 被当 Unicode 转义
    """... C:\Users\..."""           # ✗ truncated \UXXXXXXXX escape
```

> **我写这份文档时自己又踩了一次这个坑** —— 它很隐蔽，只在含 `\U` `\N` 等
> 大写字母反斜杠时触发。

---

## 五、★ 顺带消灭了三个同类 bug（一次改一类）

Python 的 `%` 格式化**从左到右只作用一次**，导致方法调用抢先在元组上执行：

```python
# ✗ 元组没有 replace
'%02d:%02d:%06.3f' % (h, m, s).replace('.', ',')

# ✗ 参数个数对不上
'[%d:a]' % idx + CH + 'atrim=0:%.2f' % (idx, total)

# ✓ 全部改 f-string，这类错误整类消失
f'{h:02d}:{m:02d}:{s:06.3f}'.replace('.', ',')
f'[{idx}:a]' + CH + f'atrim=0:{total:.2f}[bgm]'
```

**一次改一类，不是改一处。** 这三个 bug 分别在 mix_v6.py（2 处）和 run_pipeline.py（1 处）。

---

## 六、★ 这才是「从源头解决」

| 做法 | 性质 | 后果 |
|---|---|---|
| 逐个修 bug（v3→v8） | **补丁** | 修一处漏一处，问题永远在 |
| 混音再调一次音量 | **补丁** | 下个片子照样错 |
| 换一次音色手动重跑下游 | **补丁** | 漏一步就静默出错 |
| **合成一条命令 + 自动校验** | **治根** | 改参数下游必重算；静音必被发现 |

**判断标准**：
- 补丁 = 「这次好了」
- 治根 = 「**下次不可能再坏**」

---

## 七、固化到日志的规则

| 规则 | 出处 |
|---|---|
| 涉及音频对齐必须先跑 ASR 定位 | §十四 |
| 选音色要实测语速，不看用途标签 | §十七 |
| 段长由实际音频决定，不由文案预算决定 | §十七 |
| 改判据类提示词必须留成对回归测试 | v2.7 |
| **流程必须一条命令，禁止人工按序跑多个脚本** | 本文 |
| **禁止静默失败：文件缺失要报错，不要跳过** | 本文 |

---

## 七、★★★ 最致命的一个 bug：amix duration=first 把音轨截断

### 症状

用户说「杂音又出现了」/「后面彻底没音」。
`silencedetect` 查：**没有静音段**（当时我以为是自己误判了）。

### 真正的定位方法（关键）

直接把音轨导出成 wav，用 Python 算 RMS：

```python
import wave, math, array
w = wave.open(p); n=w.getnframes(); sr=w.getframerate()
a = array.array('h', w.readframes(n))
win = int(sr*0.5)          # 0.5 秒一窗
for i in range(0, len(a)//win*win, win):
    seg = a[i:i+win]
    db = 20*math.log10(max((sum(x*x for x in seg)/len(seg))**0.5/32768, 1e-8))
```

**结果：音频总长只有 2.02 秒，而成片是 20.53 秒。**

再用 ffprobe 确认：
```
stream=video  duration=20.533333
stream=audio  duration=2.020000   ← ★ 后面 18 秒完全没声音
```

### 真凶

```python
amix=inputs=N:duration=first:normalize=0
```

**`duration=first` = 取所有输入里最短的那一路。**
第一路是解说 `seg00.wav`（约 2.0 秒），于是**整个混音被截到 2.02 秒**。

**为什么所有检测都查不出来**：
- `silencedetect` 检不到 —— 因为截断后输出文件本身就是 2 秒，没东西可检
- `astats` 读不准 —— 本机读 stdout 会把 -18dB 误报成 -84dB
- `volumedetect` 大部分位置返回空
- **只有 `ffprobe` 比对两条 stream 的 duration 能发现**

### 修法

```python
amix=inputs=N:duration=longest:dropout_transition=0:normalize=0,
    apad=whole_dur={total},atrim=0:{total}[amix]
```

| | 修复前 | 修复后 |
|---|---|---|
| 音轨时长 | **2.02s** | **20.52s** |
| 成片后 18 秒 | 完全无声 | 正常有声 |
| 响度 | -16.2 LUFS（假的，因为只有前 2 秒参与统计） | -15.4 LUFS（真实） |

### ★★ 已加进自动校验（根治）

`run_pipeline.py` 第 8 步现在会显式比对两条轨的长度：

```
[8/9] 校验
    时长 20.53s｜视频轨 20.533333s｜音轨 20.52s
    响度 -15.4 LUFS｜峰值 -3.8 dBTP
    ✓ 音轨完整、无静音段、响度达标
```

若再犯，直接报：
```
✗✗✗ 音轨被截断！视频 20.53s 但音轨只有 2.02s
   → 检查 amix 的 duration 参数（必须 longest，不能是 first）
```

### 教训

> **混音输出后必须核对「音轨长度 == 视频长度」。**
> `duration=first` / `-shortest` 这类「取最短」的参数在**分段铺轨**的场景下
> 是致命的 —— 因为每路音轨长度都不同，短的会把结果整个截断。

**多路 amix 的默认姿势**：
- `duration=longest`（取最长）
- `+ apad=whole_dur=总长`（补齐）
- `+ atrim=0:总长`（截齐）

---

## 八、重写后的最终状态

**旧补丁已归档到 `scripts/_old_patches/`**，不再参与运行。

**现在唯一入口**：`scripts/build.py`
（详细说明见 [rewrite-from-scratch.md](rewrite-from-scratch.md)）

```bash
python build.py <plan.json> --dataset <素材目录> --voice 谭湘文徐浩伦 --bgm <曲路径>
```

旧文档中的 `run_pipeline.py` / `mix_v6.py` / `series_assemble.py` 相关内容**已作废**。
