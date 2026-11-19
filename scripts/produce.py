# -*- coding: utf-8 -*-
"""produce.py —— **整集可续跑编排器**（Agent 操控版唯一生产入口）。

★ 为什么要有这个（2026-11-02 血泪，用户原话「怎么防止技能中断没有正常产出解说成品」）：
  技能原先的八步是**文档里的一串手敲命令**（SKILL.md §0.2）。于是：
    · 中途任何中断（会话闲置被回收 / 机器重启 / 手滑 Ctrl+C）都停在半路；
    · 没有状态文件 → 不知道停在哪、哪些产物还能复用；
    · 没有「这一集齐了没」的判定 → 半成品躺在盘上，流程看着像在跑，实际零成品
      （实测：2026-11-01 夜 build 因沙箱删除阈值卡死 11 小时，零产出、零告警）。
  → 本脚本把八步串成**一条命令**，并补三件事：
      ① 每步产物指纹 → 上游变了下游自动失效重跑（不是靠人记）
      ② 状态文件 _plan/state-<ep>.json → 中断后重跑同一条命令即续跑
      ③ 收尾强制跑 deliver_check.py → 「齐了没」变成可自动判定的结论

★ 2026-11-06 Agent 操控版（用户令「前置 Agent 调用常用功能」）：
  旧版要手打 **10 个必填参数**，且参数与清单各存一份 → 手打必错（实测曾因
  title 写错多出一个 119MB 重名交付文件）。现在**清单是唯一参数源**：

用法（★ Agent 只需两个参数）：
    python produce.py --pilot <episodes.json> --prefix <集前缀>
    python produce.py --pilot <episodes.json> --prefix <集前缀> --dry-run
    python produce.py --pilot <episodes.json> --prefix <集前缀> --from build
    python produce.py --pilot <episodes.json> --prefix <集前缀> --force cover

  清单每项含 task/dataset/ep/prefix/name/cta/voice/gen/series/title。
  显式同名 CLI 参数可覆盖清单（仅调试用；日常一律从清单取）。

  向后兼容：不给 --pilot 时仍可手工给全 9 个必填参数（旧调用不炸）。

流水线（10 步，每步指纹续跑）：
    precheck → evidence → assemble → bgm → gen → v37 → build → qc → cover → deliver
  · precheck：spec 预检 + **跨集内容去重**（用户令「每集内容不能一样」）
  · 收尾强制 deliver_check.py 判「这一集齐了没」
  · 中断后**重跑同一条命令即续跑**；改了 spec，下游自动失效重跑

退出码：0 = 成品齐全；非 0 = 有步骤失败或成品缺项（详见输出）。
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import gpu_lifecycle as GPU   # GPU 资源串行（VLM/TTS 互斥）+ 真推理探针
CFG = json.loads((SKILL / 'config' / 'engine.json').read_text(encoding='utf-8'))
PATHS = CFG.get('paths', {})
RT = CFG.get('runtime', {})
GIT_BASH = r'C:/Program Files/Git/bin/bash.exe'

STEPS = ['precheck', 'evidence', 'assemble', 'bgm', 'gen', 'v37',
         'build', 'qc', 'cover', 'deliver']


def log(msg):
    print(f'[produce {time.strftime("%H:%M:%S")}] {msg}', flush=True)


def sha(*items):
    """输入指纹：文件按内容算，字符串/列表按值算。"""
    h = hashlib.sha1()
    for it in items:
        if it is None:
            h.update(b'\x00None')
        elif isinstance(it, (list, tuple)):
            for x in it:
                h.update(str(x).encode('utf-8'))
                h.update(b'|')
        elif isinstance(it, Path) and it.is_file():
            h.update(str(it).encode('utf-8'))
            h.update(str(it.stat().st_size).encode())
            h.update(it.read_bytes())
        else:
            h.update(str(it).encode('utf-8'))
        h.update(b'\x01')
    return h.hexdigest()[:12]


def code_fp(*rel_paths):
    """引擎代码指纹：某步产物的质量也取决于**产出它的代码**。

    ★★★ 为什么必须进指纹（2026-11-03 实踩的「改了代码却永远不重跑」死锁）★★★
      旧版 build 步的指纹只有 (plan, voice, dataset) —— **不含 build.py 本身**。
      于是：修了 build.py 的体积预算 → 指纹不变 → 判定「已是最新」→ 跳过 build
      → 拿旧的 139MB 成片继续跑 qc → 还是 REJECT → 下一轮同样跳过 → **死循环**。
      实测 M3 连续两轮（09:23 / 11:43）都是「build: 跳过（已是最新）」+ 同一个报错，
      就是这个死锁的表现。

      → 把「产出这一步的代码」也算进指纹：代码一改，产物自动判过期并重建。
      这正是「改了根因，下一轮必须真的生效」的必要条件。
    """
    parts = []
    for rel in rel_paths:
        p = SKILL / rel if not str(rel).startswith(str(SKILL)) else Path(rel)
        parts.append(sha(p))
    return sha(*parts)


class Producer:
    def __init__(self, a):
        self.a = a
        self.task = Path(a.task)
        self.ep = str(a.ep)
        self.plan_dir = self.task / '_plan'
        self.outdir = self.task / 'out' / a.prefix
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.plan_dir / f'state-{self.ep}.json'
        self.state = self._load_state()
        self.dry = False
        self.force = set()      # 强制重跑的步（--force cover,build）
        self.env = dict(os.environ, E2E_DATASET=str(a.dataset), PYTHONUTF8='1')
        self.fail = None
        # ★★ 素材存在性早失败（2026-11-04 治根，M3 连挂 3 分钟的根因）★★
        #   实测（2026-11-03 19:22）：素材目录在产线跑动期间被整体删除 →
        #   build 跑到 [2/6] 原声定位才抛 FileNotFoundError「素材目录不存在」→
        #   **白跑 3 分钟**才报错（GPU 空转、用户以为是产线卡死）。
        #   → 开工前先查：素材目录在不在、本集视频在不在。
        #     缺 → 立刻报错退出（秒级失败），不浪费任何 GPU 时间。
        #   ★ 为什么放在构造器而不是 build 步：
        #     build 步之前还有 evidence/assemble/bgm/gen/v37 五步，
        #     素材缺失时它们同样会失败，只是报错点各不相同、难以定位。
        #     在编排层入口一次性查清 → 失败信息唯一且明确。
        ds = Path(a.dataset)
        if not ds.is_dir():
            raise SystemExit(
                f'[FAIL] 素材目录不存在：{ds}\n'
                f'  → 素材被删/被移走。产出前必须先恢复素材（或重下），'
                f'    否则后面每一步都会失败。\n'
                f'  → 检查：ls "{ds.parent}" | grep "{ds.name}"')
        # 本集视频：**复用技能正典的 find_ep_file**（plan_story.py），
        #   它已覆盖各剧不同命名（`1.mp4` / `第1集.mp4` / `001_xxx-第1集.mp4`）
        #   并按 ep_from_name 解析集号、同集多份取更大的那份。
        #   ★ 为什么不自己写匹配：命名格式各剧不同（实测 K2=002_xxx-第2集.mp4、
        #     T1/T2=2.mp4）→ 自写规则 = 逻辑副本 → 换剧就误判（第一版就误判了）。
        #     正典只有一份 → 复用它，改一处即覆盖全集。
        ep_i = int(self.ep)
        try:
            from plan_story import find_ep_file
            find_ep_file(ds, ep_i)          # 找不到会抛 FileNotFoundError
        except FileNotFoundError as e:
            avail = sorted({p.stem for p in ds.glob('*.mp4')})[:12]
            raise SystemExit(
                f'[FAIL] 素材里找不到第 {ep_i} 集的视频：{ds}\n'
                f'  → 目录在但本集缺失。可用集号样本：{avail}\n'
                f'  → 素材可能被部分删除，需要补齐本集。\n'
                f'  → 详情：{e}')

    # ── 状态 ─────────────────────────────────────────────
    def _load_state(self):
        st = {'ep': self.ep, 'prefix': self.a.prefix, 'steps': {}}
        if self.state_path.is_file():
            try:
                st = json.loads(self.state_path.read_text(encoding='utf-8'))
            except Exception:
                pass
        # ★ 旧格式 state 迁移（2026-11-03 实踩）：
        #   旧版只存 status/fp，没有 done_fp。加载时若某步 status=='done'，
        #   说明它**完整跑完过** → 把 fp 补成 done_fp（跑完的指纹）。
        #   必须在**加载时**做，而不是写入时——因为 run() 跑每步前会把
        #   status 改写成 'running'，那时再补已经晚了（旧格式就没了）。
        for rec in (st.get('steps') or {}).values():
            if isinstance(rec, dict) and not rec.get('done_fp') \
                    and rec.get('status') == 'done' and rec.get('fp'):
                rec['done_fp'] = rec['fp']
        return st

    def _save_state(self):
        self.state['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1),
                                  encoding='utf-8')

    def _record(self, step, status, fp, out=None, note=''):
        if getattr(self, 'dry', False):
            return          # ★ dry-run 不落状态：预演必须无副作用，
                             #   否则「预演一次」会把没真跑过的步标成 done，
                             #   真跑时被误判成「已是最新」而跳过。
        prev = self.state['steps'].get(step) or {}
        rec = {'status': status, 'fp': fp, 'out': out,
               'note': note, 'ts': time.strftime('%H:%M:%S')}
        # ★ done_fp = 「这步完整跑完时的指纹」，与 status（本轮痕迹）**分离**。
        #   status 会被 run() 改写成 running，若新鲜判定只认 status=='done'
        #   → 每次续跑都判不新鲜 → 幂等失效（2026-11-03 实踩）。
        #   只有真正跑完（status=='done'）才刷新 done_fp；
        #   中断/失败时**保留上次的 done_fp**（那代表「上次成功跑完的版本」）。
        #
        #   旧版 state 没有 done_fp 字段 → 这里做**一次性迁移**：
        #   旧记录 status=='done' 时，fp 就是「跑完的指纹」，补写进 done_fp。
        #   （否则老 state 续跑会因为写 running 覆盖 status 而丢指纹 → 又要从头重跑。）
        if status == 'done':
            rec['done_fp'] = fp
        elif prev.get('done_fp'):
            rec['done_fp'] = prev['done_fp']
        elif prev.get('status') == 'done' and prev.get('fp'):
            rec['done_fp'] = prev['fp']      # 旧格式迁移
        self.state['steps'][step] = rec
        self._save_state()

    # ── 通用执行 ─────────────────────────────────────────
    def _run(self, cmd, timeout=7200, env=None, cwd=None):
        # ★ cwd 可覆盖：默认任务根目录，但采证据必须切到 _plan/
        #   （spec-N.json 的 out 是相对路径 ev-N.json，shot_evidence.py
        #     用 Path(spec['out']).write_text() 相对 cwd 落盘 → cwd 不对就找不到 ev）
        return subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=timeout,
                              env=env or self.env, cwd=str(cwd or self.task))

    def _skip_if_fresh(self, step, fp, outputs, inputs=()):
        """返回 True = 本步**不必再跑**（已是最新 / dry-run 下的预演）。

        判定顺序：
          ① 上次跑完（done）且指纹一致且产物都在 → 真续跑，跳过
          ② state 无记录（产物是别的脚本留下的）→ 「产物比输入新」则**收编**，
             免得第一次跑就把已产出的集重做一遍（输入一变则不再满足，会重跑）
          ③ 都不满足 → 需要跑（dry-run 时在此只报不跑）

        ★ 架构修复（2026-11-03 实踩）：「上次跑完」这个事实必须与
          「本轮正在跑」这个痕迹**分开存**，不能同一个字段覆盖。
          旧实现里 run() 跑每步前把 status 改成 'running'，
          而本判据又要求 status=='done' → 判据永远为假 →
          **每次续跑都重跑全部步骤**（build 重渲染 23 分钟，幂等彻底失效）。
          现在：完成态存 `done_fp`，运行中痕迹存 `status`，
          「是否新鲜」只看 done_fp + 产物齐全。
        """
        if step in self.force:
            log(f'  {step}: 强制重跑（--force）')
            return False
        rec = self.state['steps'].get(step)
        fresh = False
        adopted = False
        if rec:
            # ★ 新鲜判定：上次跑完（done_fp 一致）+ 产物齐全。
            #   status 只表示「本轮痕迹」（running/done/失败），不参与新鲜判定。
            #   done_fp 缺失时回退：status=='done' → fp 就是跑完的指纹（旧格式）。
            done_fp = rec.get('done_fp') or (rec.get('fp') if rec.get('status') == 'done' else None)
            fresh = (done_fp == fp and all(Path(o).is_file() for o in outputs))
        else:
            outs = [Path(o) for o in outputs if Path(o).is_file()]
            ins = [Path(i) for i in inputs if Path(i).is_file()]
            if outs and ins and all(o.stat().st_mtime >= i.stat().st_mtime for o in outs
                                     for i in ins):
                self._record(step, 'done', fp, str(outs[-1]), note='首次接管（按产物新于输入收编）')
                fresh = True
                adopted = True
        if fresh:
            log(f'  {step}: 跳过（已是最新）')
            # ★ 2026-11-04 修（实测 K2 全部跑完，watchdog 却报「步=evidence」）：
            #   run() 在跑每步**之前**先把 status 写成 'running'，
            #   而「跳过（已是最新）」这条路径直接 return，**没人把 status 改回 done**
            #   → 跳过的步骤永远停在 running
            #   → watchdog 的 todo 判定（status != 'done'）永远认为这步没做完
            #   → 已完成的集被报成「还在 evidence 阶段」，永远显示停滞告警。
            #   现在：跳过即视同完成（产物齐全 + 指纹一致 = 这步就是好的），
            #   显式落 done，watchdog 与人工看 state 都准。
            #   （不影响新鲜判定：done_fp 与 status 本就是分离的两个字段。）
            if not adopted:
                outp = next((str(Path(o)) for o in outputs if Path(o).is_file()), None)
                self._record(step, 'done', fp, outp, note='跳过（已是最新）')
            return True
        if getattr(self, 'dry', False):
            log(f'  {step}: 会执行（当前不是最新）')
            return True
        return False

    # ── 各步实现 ─────────────────────────────────────────
    def step_precheck(self):
        """spec 阶段闸门：预检 + 跨集去重。**必须排在 evidence 之前**。

        ★ 为什么（2026-11-06 重构）：
          旧版 spec_precheck.py 在文档里是「预检三件套」，但编排器**根本不调它**
          → Agent 得自己记得手动跑，忘了就等 build 跑到第 16 段才炸（白烧 9 分钟）。
          同理「每集内容不能一样」原本只是人工纪律，无代码闸门。
          → 现在两者合成流水线第一步，指纹含**所有历史 spec**：
            别的集一改，本集自动重检（不会出现「新写了一集却没查重」）。
        """
        spec = self.plan_dir / f'spec-{self.ep}.json'
        gencfg = Path(self.a.gen)
        rep_path = self.plan_dir / f'precheck-{self.ep}.json'
        hist = sorted(self.plan_dir.glob('spec-*.json'))
        fp = sha(spec, gencfg, *hist,
                 SCRIPTS / 'spec_precheck.py', SCRIPTS / 'episode_dedupe.py',
                 SCRIPTS / 'policy.py')
        if self._skip_if_fresh('precheck', fp, [rep_path], inputs=[spec]):
            return
        if self.dry:
            log('  precheck: 会执行（spec 预检 + 跨集去重）')
            return
        log('precheck：spec 预检 + 跨集去重（闸门前移，秒级失败）')
        cmd = [sys.executable, str(SCRIPTS / 'spec_precheck.py'),
               str(self.task), self.ep, str(gencfg)]
        if getattr(self.a, 'pilot', ''):
            cmd += ['--pilot', str(self.a.pilot)]
        r = self._run(cmd, timeout=300)
        print(r.stdout or '')
        if r.returncode != 0:
            print(r.stderr or '')
            raise SystemExit('[FAIL] spec 预检/跨集去重未通过：\n' + self._err_tail(r))
        rep_path.write_text(json.dumps(
            {'passed': True, 'fp': fp, 'ts': time.strftime('%Y-%m-%d %H:%M:%S')},
            ensure_ascii=False, indent=1), encoding='utf-8')
        self._record('precheck', 'done', fp, str(rep_path))

    def step_evidence(self):
        spec = self.plan_dir / f'spec-{self.ep}.json'
        ev = self.plan_dir / f'ev-{self.ep}.json'
        fp = sha(spec, self.a.dataset)
        if not spec.is_file():
            raise SystemExit(f'[FAIL] 缺 spec：{spec}')
        if self._skip_if_fresh('evidence', fp, [ev], inputs=[spec]):
            return
        # ★★★ VLM 闸门收口（2026-11-04 治根，T2 连挂 2 次的根因）★★★
        #   病根不是「delta 分支忘了调 ensure_vlm」，而是**闸门写在了分支内部**：
        #   ensure_vlm() 原本只写在「首次采证据」那个 else 分支里 →
        #   任何**新走的分支**（增量合并 / 将来再加一种）都必须自己记得再调一次，
        #   漏了就静默失败（WinError 10061 连接被拒）。
        #   → 治法：**闸门收口到单一判定点**——先算出「本步要不要真调 VLM」，
        #     再决定是否 ensure_vlm。分支内部一律不再自己管闸门，
        #     将来新增分支只需保证 need_vlm 算对，闸门不会漏。
        #   ensure_vlm 内含**真推理探针**：假死的 llama-server 也会应答 /health，
        #   只有真推理才发现得了（P80 三集连挂的根因）。
        #
        #   ★ 为什么放在编排层（produce.py）而不是 shot_evidence/delta_evidence 内部：
        #     那两个脚本在 17 个任务目录里各有一份副本（_plan/delta_evidence.py），
        #     改副本 = 改一处漏十六处；而全系统 produce.py **只有这一份**（已核）
        #     → 改一处即覆盖全集。
        #   ★ need_vlm 的三条路径：
        #     ① ev 不存在            → 首次采集 → 要 VLM
        #     ② ev 在且指纹变了      → 增量合并 → **新增枪要 VLM 读帧** → 要 VLM
        #        （T2 的 14 枪全 raw=False，且 episodes/ 事件库不存在 → 全要真推理）
        #     ③ ev 在且指纹没变      → 纯复用旧 ev → **不要 VLM**
        #        （这条路径若也 ensure_vlm，会白拉一个占 ~8GB 显存的 llama-server）
        rec = self.state['steps'].get('evidence', {}) if ev.is_file() else {}
        need_vlm = (not ev.is_file()) or bool(rec.get('fp') and rec['fp'] != fp)
        if need_vlm and not GPU.ensure_vlm():
            raise SystemExit('[FAIL] VLM 未就绪（真推理探测失败）——不采证据，避免静默卡死')
        # ★ spec 改过但 ev 在 → 用 delta_evidence 增量合并（技能正典脚本），
        #   它会刷新改动的镜头（含文案 want）→ 避免「改了文案但 plan 还是旧文案」
        if ev.is_file():
            if rec.get('fp') and rec['fp'] != fp:
                log('evidence：spec 有变更 → delta_evidence 增量合并')
                r = self._run([sys.executable, str(self.plan_dir / 'delta_evidence.py'),
                               str(spec), str(ev), str(ev)])
                if r.returncode != 0:
                    raise SystemExit('[FAIL] delta_evidence 失败：\n' + self._err_tail(r))
            else:
                log('evidence：复用已有 ev')
                self._record('evidence', 'done', fp, str(ev))
                return
        else:
            log('evidence：VLM 采证据（首次）')
            r = self._run([sys.executable, str(self.plan_dir / 'shot_evidence.py'), str(spec)],
                          timeout=5400, cwd=str(self.plan_dir))
            if r.returncode != 0 or not ev.is_file():
                raise SystemExit('[FAIL] 采证据失败：\n' + self._err_tail(r))
        self._record('evidence', 'done', fp, str(ev))

    def step_assemble(self):
        ev = self.plan_dir / f'ev-{self.ep}.json'
        plan = self.plan_dir / f'plan-{self.ep}.json'
        fp = sha(ev, self.a.name, self.a.cta)
        if self._skip_if_fresh('assemble', fp, [plan], inputs=[ev]):
            return
        log('assemble：组装 plan')
        r = self._run([sys.executable, str(self.plan_dir / 'assemble_plan.py'), str(ev),
                       '--index', self.ep, '--name', self.a.name, '--cta', self.a.cta],
                      timeout=900)
        if r.returncode != 0 or not plan.is_file():
            raise SystemExit('[FAIL] assemble 失败：\n' + self._err_tail(r))
        self._record('assemble', 'done', fp, str(plan))

    def step_bgm(self):
        plan = self.plan_dir / f'plan-{self.ep}.json'
        fp = sha(plan)
        if self._skip_if_fresh('bgm', fp, [plan], inputs=[plan]):
            return
        log('bgm：选 BGM')
        r = self._run([sys.executable, str(SCRIPTS / 'bgm_select.py'), str(plan)], timeout=600)
        if r.returncode != 0:
            raise SystemExit('[FAIL] bgm_select 失败：\n' + self._err_tail(r))
        self._record('bgm', 'done', fp, str(plan))

    def step_gen(self):
        gencfg = Path(self.a.gen)
        gcfg = json.loads(gencfg.read_text(encoding='utf-8'))
        # ★ 指纹只算真正影响出图的字段（img/prompt/seed）：
        #   gencfg 里的 anchor 只影响「图插在哪一段」，不影响图本身；
        #   用整个文件当指纹 → 改个锚点就会白白重出图。
        fp = sha([(g.get('img'), g.get('prompt'), g.get('seed'))
                  for g in gcfg.get('gen', [])], self.a.prefix)
        outs = sorted((self.task / 'gen').glob(f'{self.a.prefix}_*.png'))
        if self._skip_if_fresh('gen', fp, outs, inputs=[gencfg]):
            return
        log('gen：ComfyUI 出二创图')
        # ★ 出图前先停 VLM：ComfyUI 的扩散/放大模型要 ~9GB，VLM 占 ~8GB，
        #   叠加 12.28GB 必 OOM（与 build 前停 VLM 同一个理由）。
        GPU.stop_vlm()
        r = self._run([sys.executable, str(SCRIPTS / 'gen_images.py'), str(self.task),
                       self.a.prefix, str(gencfg)], timeout=3600)
        if r.returncode != 0:
            raise SystemExit(f'[FAIL] 出图失败 rc={r.returncode}：\n' + self._err_tail(r))
        outs = sorted((self.task / 'gen').glob(f'{self.a.prefix}_*.png'))
        if not outs:
            raise SystemExit('[FAIL] 出图后仍无图')
        self._record('gen', 'done', fp, str(outs[-1]))

    def step_v37(self):
        plan = self.plan_dir / f'plan-{self.ep}.json'
        planv = self.plan_dir / f'plan-{self.ep}-v37.json'
        fp = sha(plan, Path(self.a.gen), self.a.prefix)
        if self._skip_if_fresh('v37', fp, [planv], inputs=[plan, Path(self.a.gen)]):
            return
        log('v37：插二创图与表情包')
        r = self._run([sys.executable, str(SCRIPTS / 'make_plan_v37.py'), str(self.task),
                       self.ep, str(self.a.gen), '--prefix', self.a.prefix], timeout=600)
        if r.returncode != 0 or not planv.is_file():
            raise SystemExit('[FAIL] v37 装配失败：\n' + self._err_tail(r))
        self._record('v37', 'done', fp, str(planv))

    def step_build(self):
        planv = self.plan_dir / f'plan-{self.ep}-v37.json'
        mp4 = self.outdir / f'{self.a.name}.mp4'
        # ★ 指纹含**产出本步的代码**（build.py / size_budget.py / engine.json）：
        #   代码一改 → 产物自动判过期 → 真的会重跑（否则改了根因也永远跳过，死锁）。
        fp = sha(planv, self.a.voice, self.a.dataset,
                 code_fp('scripts/build.py', 'scripts/size_budget.py',
                         'config/engine.json'))
        if self._skip_if_fresh('build', fp, [mp4], inputs=[planv]):
            return
        log('build：TTS + 渲染（首次较慢，之后按签名复用配音）')
        # ★ GPU 串行：渲染阶段先把 VLM 停掉（它占 ~8GB），再起 dotstts 常驻服务
        GPU.stop_vlm()
        GPU.ensure_tts()
        r = self._run([sys.executable, str(SCRIPTS / 'build.py'), str(planv),
                       '--dataset', str(self.a.dataset), '--voice', self.a.voice,
                       '--platform', 'douyin', '--out-dir', str(self.outdir)], timeout=14400)
        if r.returncode != 0 or not mp4.is_file():
            raise SystemExit('[FAIL] build 失败：\n' + self._err_tail(r, tail=2500))
        self._record('build', 'done', fp, str(mp4))

    def step_qc(self):
        mp4 = self.outdir / f'{self.a.name}.mp4'
        qc = self.outdir / f'{self.a.name}.qc.json'
        # ★ 指纹含质检门代码：质检规则一改 → 重新判定（否则旧 verdict 长期挂着）。
        fp = sha(mp4, code_fp('scripts/quality_gate.py', 'config/engine.json'))
        if self._skip_if_fresh('qc', fp, [qc], inputs=[mp4]):
            # 复跑路径：产物已是最新 → 仍要**重读 verdict**，
            # 否则上一轮 REJECT 的 qc.json 会被当成 done 直接放行到交付。
            self._assert_qc_passed(qc)
            return
        log('qc：质检')
        dur = self._probe_duration(mp4)
        # ★ 补软门输入（2026-11-03 实踩）：quality_gate 的 soft/semantic 默认读
        #   srt 同目录 semantic.json，soft/speech 默认读 narration.txt / readback.json。
        #   produce.py 链路（与 pipeline.py 不同）原本**从不产出**这三个文件
        #   → 两个软门恒为「未打分」→ soft_max=0 → grade() 恒返回 'C'
        #   → 产线一集都出不来（M2 实测）。
        #   这里按 pipeline.py 同源同构生成 semantic.json（用 plan 的 evidence/inference），
        #   并把解说文本写成 narration.txt，让软门**真能打分**而不是恒 0/0。
        semantic = self._write_semantic_report()
        narration = self._write_narration_text()
        cmd = [sys.executable, str(SCRIPTS / 'quality_gate.py'), str(mp4), str(dur),
               str(self.outdir / '_work/subs.srt'), str(self.outdir / 'shotmap.json'),
               '--platform', 'douyin', '--strict-duration']
        if semantic is not None:
            cmd += ['--semantic', str(semantic)]
        if narration is not None:
            cmd += ['--narration', str(narration)]
        r = self._run(cmd, timeout=1800)
        if not qc.is_file():
            raise SystemExit('[FAIL] 质检未产出 qc.json：\n' + self._err_tail(r))
        self._assert_qc_passed(qc)
        self._record('qc', 'done', fp, str(qc))

    def _write_semantic_report(self):
        """按 pipeline.py 同源同构生成 semantic.json（供 soft/semantic 打分）。

        pipeline.py 的语义门来自 S3 script.json 的 lint（grounding / inference_labels）；
        produce.py 链路没有 script.json，但 plan 的每段都带 evidence 与 inference 标记
        → 用它们合成同结构的报告，语义门的判据完全一致：
          · unsupported_claims ← 有 evidence 缺失的段
          · unmarked_inference ← inference=True 但未标注推断标记的段
        """
        plan = self.plan_dir / f'plan-{self.ep}.json'
        if not plan.is_file():
            return None
        try:
            d = json.loads(plan.read_text(encoding='utf-8'))
        except Exception:
            return None
        segs = d.get('segments') or []
        unsupported, unmarked = [], []
        for s in segs:
            sid = s.get('id') or f"ep{s.get('ep')}-t{s.get('t')}"
            if not s.get('evidence'):
                unsupported.append(f'{sid}: 缺证据支撑')
            inf = s.get('inference')
            if inf and not s.get('inference_marked'):
                unmarked.append(f'{sid}: 未标注推断')
        out = self.outdir / '_work' / 'semantic.json'
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            'schema_version': 1,
            'source': 'produce.py plan-episode grounding and inference-label checks',
            'unsupported_claims': unsupported,
            'unmarked_inference': unmarked,
            'grounding_verdict': 'MISSING' if unsupported else 'OK',
            'inference_verdict': 'MISSING' if unmarked else 'OK',
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        return out

    def _write_narration_text(self):
        """把 plan 的解说文本写成 narration.txt（供 soft/speech 回读比对）。

        soft/speech 需要 readback.json（TTS 回读 ASR）才会打分；缺失时该门
        仍记为「未打分」，但那是**如实反映**（引擎配置 narration_qc 为
        provisional / allow_auto_pass=false，未校准前本就不该自动判通过），
        不再被门闸当成「成片不合格」。
        """
        plan = self.plan_dir / f'plan-{self.ep}.json'
        if not plan.is_file():
            return None
        try:
            d = json.loads(plan.read_text(encoding='utf-8'))
        except Exception:
            return None
        texts = [(s.get('text') or '').strip() for s in (d.get('segments') or [])]
        texts = [t for t in texts if t]
        if not texts:
            return None
        out = self.outdir / '_work' / 'narration.txt'
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text('\n'.join(texts), encoding='utf-8')
        return out

    def _assert_qc_passed(self, qc_path: Path):
        """★ 质检硬门闸（2026-11-03 实踩）：qc.json 存在 ≠ 质检通过。

        实测：H3 质检 verdict=REJECT（文件超 128MB + 字幕行超长），
        但旧版 step_qc 只判 `qc.is_file()` → 照样往下跑 cover + deliver，
        **把不合格成片交付到了网盘**，最后才在 deliver_check 里报「成品不齐」，
        交付动作已经发生了、脏文件已经落地了。
        → 硬门必须在这里拦，绝不让不合格成片流到封面与交付。

        ★ 判据取值域必须与上游对齐（2026-11-03 实踩，架构级 bug）：
          quality_gate.py 的 verdict 取值域只有 {REJECT, A, B, C} ——
          `grade()` 只返回 A/B/C，**永远不会产出 PASS / WARN**。
          旧判据写 `verdict in ('PASS','WARN')` → 任何集都必然被拦死，
          产线一集都出不来（M2 实测：4 个硬门全过、verdict=C → 被误拦）。
          正典 pipeline.py 的判据是 `if not qc.get('hard_passed')` 才隔离。

        → 最终判据（与 pipeline.py 对齐）：
          ① hard_passed 为假 → 拦（REJECT：硬门失败或语义硬旗标）
          ② hard_passed 为真 → 放行；软门等级/未打分项**记入 risks 透传**，
             不再当作不合格（软门是「待人工审片」信号，不是「机器判不合格」）
        """
        try:
            d = json.loads(qc_path.read_text(encoding='utf-8'))
        except Exception as e:
            raise SystemExit(f'[FAIL] 质检报告无法解析：{qc_path}（{e}）')
        if d.get('hard_passed') is True:
            soft = d.get('soft_grade')
            unscored = d.get('soft_unscored') or []
            if soft != 'A' or unscored:
                log(f'  qc：硬门通过，软门待人工审片（soft_grade={soft}，'
                    f'未打分={unscored or "无"}）')
            return
        fails = d.get('hard_failures') or []
        detail = ('\n'.join(f'  · {x}' for x in fails)
                  if fails else '  （无 hard_failures 明细）')
        raise SystemExit(
            f"[FAIL] 质检未通过（verdict={d.get('verdict')} / "
            f"hard_passed={d.get('hard_passed')}），不进入封面与交付：\n{detail}\n"
            f'  报告：{qc_path}\n'
            '  → 修掉上述硬伤后重跑本集（produce.py 幂等，会从失败的那步续跑）')

    def step_cover(self):
        mp4 = self.outdir / f'{self.a.name}.mp4'
        cover = self.outdir / f'cover_{self.a.prefix}.jpg'
        burned = self.outdir / f'{self.a.name}-封面版.mp4'
        cfg = json.loads(Path(self.a.gen).read_text(encoding='utf-8'))
        # ★ 指纹含封面层代码（make_cover.py / size_budget.py）：
        #   封面版是**最终交付物**，它的体积硬闸代码改了必须重新烧。
        fp = sha(mp4, Path(self.a.gen),
                 code_fp('scripts/make_cover.py', 'scripts/size_budget.py',
                         'config/engine.json'))
        if self._skip_if_fresh('cover', fp, [cover, burned], inputs=[mp4]):
            return
        log('cover：做封面并烧进首帧')
        # ★ 封面必须用**带 PIL 的 python**（runtime.tts_python）——
        #   普通 python 没装 Pillow → ModuleNotFoundError: No module named 'PIL'
        #   （技能文档 §0.2 早就写明「用带 PIL 的 tts_python」，2026-11-03 实踩）
        pil_py = RT.get('tts_python') or sys.executable
        cargs = [pil_py, str(SCRIPTS / 'make_cover.py'), 'make']
        # ★ 素材文件夹里有官方海报 → 用海报做封面底（技能规则），否则用视频帧
        poster = self._find_poster(Path(self.a.dataset))
        if poster:
            cargs += ['--base-image', str(poster)]
        else:
            fr = cfg.get('cover_frame', {})
            cargs += ['--base-video', str(Path(self.a.dataset) / str(fr.get('video', ''))),
                      '--frame-t', str(fr.get('t', 0.0))]
        cargs += ['--nick', '囡囡解说', '--main', cfg['cover_main'], '--sub', cfg['cover_sub'],
                  '--lead', cfg['cover_lead'], '--badge', cfg.get('cover_badge', f'第 {self.ep} 集'),
                  '--out', str(cover)]
        r = self._run(cargs, timeout=900)
        if r.returncode != 0 or not cover.is_file():
            log(f'  封面 make 失败（不阻断）：{(r.stderr or "")[-200:]}')
            return
        r = self._run([pil_py, str(SCRIPTS / 'make_cover.py'), 'burn',
                       '--video', str(mp4), '--cover', str(cover), '--out', str(burned)],
                      timeout=1800)
        if r.returncode != 0 or not burned.is_file():
            raise SystemExit(f'[FAIL] 封面烧录失败:\n{(r.stdout or "")[-400:]}')
        self._record('cover', 'done', fp, str(burned))

    def step_deliver(self):
        burned = self.outdir / f'{self.a.name}-封面版.mp4'
        if not burned.is_file():
            burned = self.outdir / f'{self.a.name}.mp4'
        # ★ v6.0.0 治根（坑典 P92）：compliance.py stamp 的能力早已存在，
        #   但十步状态机**零调用** → 新出的片不带 AIGC 隐式标识；
        #   评估第 18 条只查 6 支手工补标的历史成片，长期显示通过，
        #   掩盖了「能力有、链没接」的纸面合规（架构允许静默失败）。
        #   → 交付前强制补标：幂等 + 写后复核，失败立即报错，绝不带病交付（F10）。
        self._ensure_aigc_stamp(burned)
        fp = sha(burned, self.a.series, self.a.title)
        dst = Path(PATHS.get('delivery_root', 'D:/BaiduNetdiskDownload')) / self.a.series \
            / '解说成品' / f'{self.a.prefix}-{self.a.title}.mp4'
        # ★ 交付幂等（2026-11-03 缺陷④修复）：
        #   旧判据只看「目标存在且 >100KB」→ 与内容指纹脱钩 → 成片更新后不再重交，
        #   网盘上留的是**旧版**（实测 D1：网盘 34498401B vs 本地新版 34860028B）。
        #   新判据：目标与源**大小一致**才视为同一版本；不一致 → 重新交付。
        if dst.is_file() and dst.stat().st_size > 100000:
            src_sz = burned.stat().st_size if burned.is_file() else 0
            if src_sz and dst.stat().st_size == src_sz:
                self._record('deliver', 'done', fp, str(dst),
                              note='交付已存在且与成片一致（幂等跳过）')
                log('  deliver: 跳过（已交付且与成片一致）')
                return
            log(f'  deliver: 网盘旧版 {dst.stat().st_size}B ≠ 成片 {src_sz}B → 重新交付')
        if self._skip_if_fresh('deliver', fp, [dst], inputs=[burned]):
            return
        log('deliver：交付到网盘')
        # ★ deliver.sh 第 3 参是**out 下的目录名**（它 glob `out/$EP/*-封面版.mp4`），
        #   传集号会找不到我们 out/<prefix>/ 下的成片（2026-11-03 实踩：交付直接失败）。
        r = subprocess.run([GIT_BASH, str(SCRIPTS / 'deliver.sh'), str(self.task),
                            self.a.series, self.a.prefix, self.a.title, self.a.prefix],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=1800)
        if not dst.is_file():
            raise SystemExit('[FAIL] 交付未落地：' + str(dst) + '\n' + (r.stdout or '')[-400:]
                             + '\n' + (r.stderr or '')[-400:])
        self._record('deliver', 'done', fp, str(dst))

    def _ensure_aigc_stamp(self, video):
        """交付前 AIGC 隐式标识闸（v6.0.0 新增，唯一判据在 compliance.py）。

        幂等由 compliance.stamp 自己保证（已标识 → already_stamped，不重复写、
        不叠加 .pre-aigc 备份）；这里只负责「失败必须看得见、绝不放行」。

        ★ 实链验证修复（2026-11-13 真实剧集 MW1 干跑）：`--dry-run` 必须零副作用。
          旧版无条件带 `--apply`，遇到**未补标**的成片时，干跑会真的改写视频
          并留 `.pre-aigc` 备份 —— 预演变成生产，正是「架构允许静默失败」。
          → 干跑只调 stamp 的 dry-run 模式（只读 ffprobe，不写任何文件）。
        """
        if not video.is_file():
            raise SystemExit(f'[FAIL] 交付前找不到成片：{video}')
        dry = getattr(self, 'dry', False)
        cmd = [sys.executable, str(SCRIPTS / 'compliance.py'), 'stamp', str(video),
               '--task', str(self.task), '--ep', str(self.ep)]
        if not dry:
            cmd.append('--apply')
        r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=900)
        if r.returncode != 0:
            raise SystemExit('[FAIL] AIGC 隐式标识补标失败，禁止交付：\n'
                             + self._err_tail(r))
        try:
            d = json.loads(r.stdout or '{}')
        except Exception as e:
            raise SystemExit(f'[FAIL] 补标结果不可解析（{type(e).__name__}）：'
                             f'{(r.stdout or "")[-300:]}')
        if d.get('ok') is not True:
            raise SystemExit('[FAIL] AIGC 隐式标识补标未成功：'
                             + str(d.get('reason', '未返回 ok=true')))
        if dry:
            log(f'  deliver: [dry-run] AIGC 标识状态={d.get("current")}'
                '（只预演，不写入、不备份）')
        elif d.get('already_stamped'):
            log('  deliver: AIGC 隐式标识已存在（幂等跳过）')
        else:
            log('  deliver: 已写入 AIGC 隐式标识（写后复核通过）')

    # ── 辅助 ─────────────────────────────────────────────
    @staticmethod
    def _err_tail(r, tail=1500):
        """从子进程输出里捞出**真正的错误行**（而不是一堆读帧日志的尾巴）。

        ★ 为什么（2026-11-03 实踩）：失败时只印 stdout 尾部 → 满屏都是
          「ep 8 t=67.43 新读帧」这种正常日志，真正的报错被埋在中间或 stderr，
          结果**失败原因完全看不到**，只能靠猜（本例真因是 CWD 落错目录）。
        """
        import re as _re
        pat = _re.compile(r'Traceback|Error|error|失败|错误|Exception|'
                          r'out of memory|CUDA|Killed|assert|SystemExit|'
                          r'not found|找不到|未命中|超时|缺', _re.I)
        lines = []
        for stream in ((r.stdout or ''), (r.stderr or '')):
            for ln in stream.splitlines():
                if pat.search(ln):
                    lines.append(ln.rstrip())
        if lines:
            return '\n'.join(lines[-25:])
        return (f"(无显式错误行) stdout 尾部:\n{(r.stdout or '')[-600:]}\n"
                f"stderr 尾部:\n{(r.stderr or '')[-600:]}")

    @staticmethod
    def _probe_duration(path):
        try:
            r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                '-of', 'csv=p=0', str(path)], capture_output=True, text=True,
                               encoding='utf-8', timeout=60)
            return float((r.stdout or '0').strip() or 0)
        except Exception:
            return 0.0

    @staticmethod
    def _find_poster(dataset):
        for p in sorted(dataset.glob('*.jpg')) + sorted(dataset.glob('*.jpeg')) \
                + sorted(dataset.glob('*.png')):
            if p.stat().st_size > 20000:      # 封面图通常几百 KB，小图是剧集截图
                return p
        return None

    # ── 主流程 ───────────────────────────────────────────
    def run(self, only_from=None, dry=False):
        self.dry = bool(dry)
        self.force = set(x for x in (self.a.force or '').split(',') if x.strip())
        todo = STEPS[STEPS.index(only_from):] if only_from else STEPS
        log(f'开始：{self.a.prefix} ep{self.ep}（{self.a.name}）'
            + ('  [DRY-RUN]' if dry else ''))
        for step in todo:
            fn = getattr(self, f'step_{step}')
            t0 = time.time()
            # ★ 先写 running 再干活：被中断（会话回收/机器重启）时，
            #   盘上留的是「这步正在跑」的痕迹，而不是「这步没跑过」——
            #   否则下次续跑无法区分「没跑」与「跑了一半」。
            #
            # ★★ 不可抹掉指纹（2026-11-03 实踩，架构级幂等 bug）：
            #   旧写法 _record(step, 'running', '', …) 把 fp 写成空串，
            #   而新鲜判定又要求 status=='done' → 判据永远为假 →
            #   每轮续跑都重跑全部步骤（build 重渲染 23 分钟，幂等彻底失效）。
            #   现在：完成态指纹存 done_fp（_record 自动维护），
            #   本轮痕迹只改 status，fp 也保留原值便于排障。
            prev = self.state['steps'].get(step) or {}
            self._record(step, 'running', prev.get('fp', '') or prev.get('done_fp', ''),
                         prev.get('out'),
                         note=f'开始于 {time.strftime("%H:%M:%S")}')
            fn()
            rec = self.state['steps'].get(step, {})
            st = rec.get('status') or ('dry' if dry else '?')
            log(f'  {step}: {st}（{time.time() - t0:.1f}s）')
        if dry:
            return 0
        # 收尾：成品完整性校验（把「齐了没」变成可判定的结论）
        log('成品完整性校验…')
        # ★ 取消 --tier（2026-11-06）：时长判定统一走 policy.py 的单档实测硬门，
        #   编排器不再向校验器传递任何分档信息（传了也不会被使用）。
        r = subprocess.run([sys.executable, str(SCRIPTS / 'deliver_check.py'), str(self.task),
                            self.ep, self.a.prefix, self.a.name, '--title', self.a.title,
                            '--series', self.a.series],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=900)
        print(r.stdout or '')
        if r.returncode != 0:
            print(r.stderr or '')
            log('★ 成品不齐（见上）')
            return 1
        log('★ 全流程完成，成品齐全 ✓')
        return 0


def main():
    ap = argparse.ArgumentParser(
        description='整集可续跑编排器（Agent 操控版：清单驱动）',
        epilog='★ 首选：--pilot <episodes.json> --prefix <集前缀>；'
               '参数一律从清单取，不要手打。')
    # 清单模式（★ Agent 首选）
    ap.add_argument('--pilot', default='',
                    help='待产清单 episodes.json —— 唯一参数源')
    ap.add_argument('--prefix', default='', help='集前缀（清单内定位用）')
    # 手工模式（向后兼容；全部默认 None，由下方校验）
    for k, h in (('task', '任务目录'), ('dataset', '素材目录'), ('ep', '集号'),
                 ('name', '成片名'), ('cta', '结尾文案'), ('voice', '音色'),
                 ('gen', 'gencfg json'), ('series', '剧名'), ('title', '爆款标题')):
        ap.add_argument(f'--{k}', default=None, help=h)
    ap.add_argument('--tier', default='', help='已废弃（取消三档），仅为兼容旧调用保留')
    ap.add_argument('--from', dest='only_from', choices=STEPS)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--force', default='',
                    help='强制重跑的步（逗号分隔，如 cover,build）——忽略指纹与产物判定')
    a = ap.parse_args()

    FIELDS = ('task', 'dataset', 'ep', 'name', 'cta', 'voice', 'gen', 'series', 'title')
    if a.pilot:
        try:
            pilot = json.loads(Path(a.pilot).read_text(encoding='utf-8'))
        except Exception as e:
            ap.error(f'清单读不到（未知 ≠ 没有，保守拒绝）：{e}')
        if not isinstance(pilot, list):
            ap.error('清单不是数组（格式错，拒绝猜测）')
        if not a.prefix:
            ap.error('清单模式必须给 --prefix（用它定位要产哪一集）')
        ent = next((e for e in pilot if str(e.get('prefix')) == a.prefix), None)
        if ent is None:
            ap.error(f'清单里没有 prefix={a.prefix}；'
                     f'可用：{[e.get("prefix") for e in pilot]}')
        # ★ 清单是唯一参数源：显式 CLI 参数仅在清单缺该字段时兜底
        for k in FIELDS:
            if not getattr(a, k):
                setattr(a, k, ent.get(k))
        missing = [k for k in FIELDS if not getattr(a, k)]
        if missing:
            ap.error(f'清单条目 {a.prefix} 缺字段：{missing}（补进清单，别手打）')
    else:
        missing = [k for k in FIELDS if not getattr(a, k)]
        if missing or not a.prefix:
            ap.error(f'缺参数：{missing + ([] if a.prefix else ["prefix"])}。'
                     f'★ 建议改用 --pilot <episodes.json> --prefix <前缀>')

    p = Producer(a)
    try:
        return p.run(only_from=a.only_from, dry=a.dry_run)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
