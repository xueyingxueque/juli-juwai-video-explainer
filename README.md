# 剧里剧外 · 视频解说引擎

> 本地短剧原片 → 竖屏解说成片，**一条命令跑完整集**，中断可续跑。
> 全程本机运行（SenseVoice ASR + 本地 VLM + Dots TTS + FFmpeg），**生产零联网**。
> 版本与完整契约见 [`SKILL.md`](SKILL.md)。

---

## 一、这是什么

把下载好的短剧原片（每集一个 mp4），自动做成 60–300 秒的竖屏解说成片并交付。
引擎负责：写稿预检 → 逐镜采证 → 装配 → BGM → 二创图 → TTS 配音 → 混音压字幕 →
质检 → 封面 → 网盘交付。上游写稿与人工审片仍需人参与（见 `SKILL.md §5.2` 已知缺口）。

**三条硬规范**：① 时长严格 60s < 实测 < 300s；② 每集内容不能一样（双层去重）；③ 参数从清单取，不手打。

---

## 二、首次使用：必须先跑初始化向导 ⭐

仓库**不含**你的本机配置——`config/engine.json` 被 `.gitignore` 排除，只有
`config/engine.template.json`（占位符模板）。这样做是为了让每个人用自己的路径，不互相覆盖。

```bash
# 1. 进入技能目录（Git / 下载解压后的目录）
cd 剧里剧外-视频解说引擎

# 2. 第一次使用：交互向导（探测 + 询问，生成 config/engine.json）
python scripts/setup.py

# 3. 确认健康（28 条离线断言应全过）
python scripts/eval_skill.py
```

向导会依次探测/询问：

| 项 | 必需 | 说明 |
|---|---|---|
| ffmpeg / ffprobe | ✅ 必需 | 没装先装：[ffmpeg.org](https://ffmpeg.org/download.html)；Windows 可 `winget install Gyan.FFmpeg` |
| Python 运行时 | ✅ 必需 | 用你当前 Python；需 `Pillow`（`pip install pillow`） |
| 本地配音工坊 `dub.py` | ✅ 必需 | 已随包放在 `vendors/本地配音工坊/`，向导会自动找到 |
| VLM（llama-server + gguf 模型） | 可选 | 不配置则无法自动采证据，只能跑已有 spec 的集 |
| 素材根 / 任务根 | ✅ 必需 | 原片放哪、任务状态写哪 |
| 贴纸库 / ComfyUI | 可选 | 不配置则对应原创度层不启用 |

静默模式（CI / 不想回答）：`python scripts/setup.py --yes`；查看当前配置：`--show`。
向导是**幂等**的：重跑会先备份旧配置为 `engine.json.bak` 再重建。

> ⚠️ **TTS 引擎**：`vendors/本地配音工坊` 只含代码，**不含 TTS 模型/引擎**（体积以 GB 计）。
> 配音需要你在 `TTS_ROOT` 下装好 Dots-TTS 等引擎；`registry.py` 会从环境变量 `TTS_ROOT` 读取，
> 向导写入的 `runtime.tts_root` 由主引擎通过环境变量注入给配音脚本。没有 TTS 引擎时，
> 主流程仍可跑预检、采证、装配，但配音步会明确报错（不会静默产出无声片）。

---

## 三、日常使用

```bash
# 出片（唯一入口，只需 2 个参数）
python scripts/produce.py --pilot D:/剧里剧外/视频解说/任务/_tools/episodes.json --prefix T1

# 先看会跑什么（零副作用）
python scripts/produce.py --pilot <清单> --prefix T1 --dry-run

# 中断后续跑：重跑同一条命令即可，已完成步按指纹跳过
```

`episodes.json` 是**唯一参数源**，字段格式见 [`SKILL.md §0.3`](SKILL.md)。
第一次用建议照 [`docs/说明文档.md`](docs/说明文档.md) 走一遍「从零跑通一集」。

---

## 四、仓库内容

| 目录 | 内容 |
|---|---|
| `SKILL.md` | 入口与真相源（Agent 先读 §0） |
| `scripts/` | 54 个活跃脚本（生产主链 + 六层能力 + setup 向导） |
| `config/engine.template.json` | 配置模板（占位符版，**提交**） |
| `config/engine.json` | 本机配置（向导生成，**不提交**） |
| `references/` | 16 份一层深知识文档（含 97 条坑典） |
| `docs/` | 人看的教程 + 改代码的硬约束 |
| `evals/` | 28 条断言、评估报告、学习报告 |
| `vendors/本地配音工坊` | 随包关联技能（**仅代码**，无模型/venv/缓存） |
| `agents/` | 规划期 Agent 契约（未接入主链） |

**关联技能说明**：只有 `本地配音工坊` 是生产链路的硬依赖，已随包提供代码。
BGM音效引擎 / 表情包引擎 / ffmpeg-advisor / Agent导演引擎 / 导演后期 是可选或转交对象，
不随包提供；需要时见 [`references/engine-integration.md`](references/engine-integration.md) 的依赖表。

---

## 五、验证与排障

```bash
python scripts/eval_skill.py            # 28 条离线断言（不跑 GPU、不碰生产素材）
python scripts/contracts.py --audit     # 配置与代码常量漂移审计
python scripts/policy.py                # 时长/去重口径自检
```

- 报错先查 [`SKILL.md §5.1`](SKILL.md) 症状速查 → 指向坑典编号。
- **生产零联网**：不要把原片、帧、转录、解说稿送进聊天模型或云端。
- **不自动发布**：发布页勾选、AI 标识显式标注由人执行。
- 更多运行边界见 [`references/runtime.md`](references/runtime.md)（单集验证路径）与
  [`references/agent-integration.md`](references/agent-integration.md)（对外调用契约）。

---

## 六、License

见 [`LICENSE`](LICENSE)。本技能仅供本地学习与创作使用；素材版权、音色授权、
平台规则合规由使用者自行确认。
