# 本地配音工坊 · 错误处理手册（对接 Agent导演引擎 幕四·后期 · 配音/混音）

> 定位：失败分类 + 修复命令的权威手册。对接导演引擎 `12-engine-onboarding.md` §3.3 统一错误枚举；
> dub.py 已按本表实现 `_classify_error()`（`--json` 输出 `error{type,reason,fix}`）。
> 纪律：新踩坑第 2 次出现必须补进本表。

## 1. 统一错误枚举（导演引擎契约）

| type | 含义 | fix 方向 | dub.py 实际识别信号（小写匹配，任一命中） |
|---|---|---|---|
| `oom` | 显存/内存不足 | `--fast` 降档 / 缩短文本 / 换小模型 / 关其它占显存进程（bf16 默认已开） | out of memory / oom / torch.cuda.oom |
| `invalid_param` | 参数非法（含枚举不匹配、**参考音格式不支持**） | 对照 SKILL §8.2 参数速查修正；m4a/mp3 报格式错时确认本机有 ffmpeg（worker 会自动转 wav） | assert / invalid / valueerror / typeerror / keyerror / unrecognized / unknown argument / not implemented / **format not recognised** |
| `model_missing` | 缺模型/权重/参考音频 | `--health` 体检；确认 `D:\AITools\TTS` 下权重与参考音存在 | filenotfound / no such file / cannot find / not found / is not a file / does not exist / 找不到 / 不存在 |
| `service_down` | GPU 驱动/CUDA 环境异常、引擎进程起不来 | 检查显卡驱动/CUDA；或 `--device cpu`；`--health` | cuda driver / driver version / no cuda / cudnn / libcuda / runtimeerror: cuda |
| `timeout` | 合成超时或服务不通 | 缩短文本 / `--fast`；检查常驻服务 | timeout / timed out / connection refused / connect failed |
| `unknown` | 其他 | 记录引擎输出尾部 2000 字现场，人工排查 | 以上都不中时兜底 |

导演引擎收到 error → 降级链：L1 本地换引擎重试 → L2 edge-tts → L3 静默+字幕；同 job 连续失败 3 次 → capability-matrix 标 `degraded`。

> v0.6 行为变化：**常驻服务返回失败不再直接判死**，dub.py 会打 warning 自动回退一次性子进程；只有子进程也失败才产出本表 error。因此「服务偶发抽风」不再上报导演引擎。

## 2. 各引擎典型失败模式与修复

### IndexTTS2（22050Hz，中文最稳）
| 现象 | 分类 | 修复 |
|---|---|---|
| CUDA out of memory（长文本/多角色） | oom | `--fast`（beam 3→2）；`--max-mel-tokens 1500` 调低；多角色减角色数（≤20） |
| 首段 FileNotFoundError | model_missing | `--health` 看 ready；确认 `nj-win-index-tts2\app\python\checkpoints` 权重齐 |
| 复读/结巴 | invalid_param | `--repetition-penalty` 调高（10→12+）；或 `--use-random` 关 beam |
| 多角色报「剧本角色未配置」 | invalid_param | `--roles` 必须覆盖剧本全部 `[角色名]`；剧本每行格式 `[角色名]: 内容`，角色名去空格 |
| 情绪没生效 | invalid_param | indextts2 用 `--emo-text "悲伤地念"`，**不是 `--emotion`**（后者只 pilottts 认，传错会被静默过滤） |

### dots.tts（48000Hz，多语种高保真）
| 现象 | 分类 | 修复 |
|---|---|---|
| language 不识别（写中文名） | invalid_param | 用 ISO 代码（zh/en/ja/ko），dub.py 对常见中文名有自动归一 |
| 音色不像/太亮 | invalid_param | `--speaker-scale` 1.5→1.8 提相似度；`--guidance-scale` >2 更亮但易炸 |
| 速度慢 | timeout | `--num-steps 10`→8 / `--fast`；**更有效：`--model dots-tts-mf`（MeanFlow NFE=4，约 2×）** |
| 找不到 dots-tts-soar | model_missing | `--model dots-tts-mf` 换权重，或 `--health` 查模型清单 |
| continuation 克隆不像 | invalid_param | soar/mf 克隆建议补 `--prompt-text`（参考音频转写） |

### PilotTTS（24000Hz，高德 AMAPVOICE，方言/副语言）
| 现象 | 分类 | 修复 |
|---|---|---|
| emotion 标签不识别 | invalid_param | worker 实际接受 13 个标签：happy/sad/angry/surprise/fear/disgust/serious/concern/blue/disdain/neutral/psychology/unknown（可逗号叠加） |
| 副语言标签原样读出 | invalid_param | 文本内嵌 `<|LAUGH|>`/`<|CRY|>`/`<|SIGH|>`，注意是半角竖线大写 |
| 方言输出不准 | invalid_param | `--language` 填方言码（如 zh-tianjin、zh-sichuan 等 14 方言），不是只填 zh |
| 切换 emotion 后首次慢 | 正常 | emotion 决定加载 base 还是 instruct 权重（加载期参数），常驻服务会缓存，LRU=2 |

### Chatterbox-V3（24000Hz，23 语种/中英混说）
| 现象 | 分类 | 修复 |
|---|---|---|
| 夸张度过高抢拍 | invalid_param | 夸张度↑ 时同步降 `--cfg-weight`（0.5→0.4） |
| 中英混说语言乱 | invalid_param | 中文精调 `--model chatterbox-zh-cmn`；通用多语 `--model chatterbox-v3` |
| 无参考音报错 | invalid_param | chatterbox 可省 ref/voice（内置 conds 音色），确认没误传空 `--ref` |
| 异常时输出 NameError 而非真错误 | 已修复 | v0.6 补了 worker 缺失的 `import json`，异常分支现在能正常吐出 JSON 错误 |

### Breeze TTS 2（24000Hz，Voice Design / Direction，v0.7 新增）
| 现象 | 分类 | 修复 |
|---|---|---|
| `Error opening 'xx.m4a': Format not recognised` | invalid_param | 引擎用 soundfile 读参考音，只认 wav/flac/ogg；worker 已内置 ffmpeg 自动转 wav（前提：本机有 ffmpeg） |
| `ref_audio and ref_text must be provided together`（HTTP 400） | invalid_param | clone 模式参考音与转写必须成对：补 `--ref-text-file`，或加 `--auto-transcribe` 自动转写 |
| `An inference request is already running`（HTTP 409） | service_down | 服务单并发；串行调用，或等前一条完成再发 |
| 首次调用 2-3 分钟才出第一声 | 正常 | 权重 7.3GB 首次加载；worker 自动拉起常驻服务，之后秒接 |
| 显存被占满导致其它引擎 OOM | oom | breeze 常驻 ~7.7GiB，与其它引擎互斥；不用时停 7860 端口进程释放显存 |
| 想用于商单 | 授权 | 权重受 BreezeBlue 研究非商用许可约束，商用需 RESONIA 书面授权，否则换其它引擎 |

> **与 PilotTTS 同源坑**：两个引擎都用 soundfile 读参考音，**m4a/mp3 必须先转 wav**。两个 worker 均已内置自动转换（缓存 `.cache/`），所以常用音色里的 m4a 可直接使用。

## 3. 混音/对齐脚本常见问题（mix_audio.py / dub_align.py）

| 现象 | 原因 | 修复 |
|---|---|---|
| `视频数 N != 镜长数 M` | --videos 与 --durations 数量不一致 | 逐镜对齐，一镜一个时长 |
| filtergraph `Invalid argument` | 多为表达式逗号未转义（脚本内部已处理）或 SFX 路径含特殊字符 | 升级到 v0.6；SFX 路径避免特殊符号 |
| 某个 SFX 缺失导致整条混音崩 | v0.5 旧 bug（缺失项排在有效项前会让输入下标错位） | v0.6 已修为跳过缺失且索引重排，只打 WARN |
| dub_align 重跑报「段数 N != 镜长数」 | v0.5 旧 bug：把自己生成的 `*_tempo.wav` 又扫进去 | v0.6 已排除临时文件，可任意重跑 |
| 无台词镜 BGM 还是被压低 | v0.5 的 `--silent` 收了但没进滤镜（死参数） | v0.6 起按时间区间真正抬 BGM（`--silent-bgm-vol`，默认 1.0） |
| 响度想贴平台 | 单遍默认 | 短视频 `--platform shortvideo`（-14 LUFS）；播客默认 -16；广播 -23；精确达标加 `--two-pass` |

## 4. 对接失败回写示例（导演引擎视角）

```json
{"ok": false, "out": "D:/.../shot_003.wav",
 "error": {"type": "oom", "reason": "CUDA out of memory",
           "fix": "--fast 降档或缩短文本/换小模型，仍失败换 L2 edge-tts"}}
```
→ 导演引擎按 `type` 走降级链；`oom`/`timeout` 先降档重试，`model_missing`/`service_down` 直接换引擎。

## 5. 排障四板斧
1. `python dub.py --health`：引擎 ready / 25 音色可解析 / 常驻服务状态 / 版本号（版本以这里为准）。
2. `python dub.py --dry-run ...`（或 `--batch jobs.json --dry-run`）：只构造 worker 命令不合成，确认参数与路径。
3. `python tests/test_smoke.py`：秒级自检（不加载模型），确认技能文件完整。
4. 看引擎原生输出：dub.py 打印引擎 stdout/stderr 尾部 2000 字符；`--json` 时过程日志在 stderr，不污染结果。
