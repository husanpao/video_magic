#!/usr/bin/env python3
"""
pipeline.py —— 命令行入口，同时也是任务 worker 的执行体。

两条路，一套控制层（vm/taskctl.py）：

    python3 pipeline.py --list                          # 列项目
    python3 pipeline.py 西游记 --stage plan|chars|render|qc|assemble|all
                             [--only 1-1-01,1-1-02] [--force] [--dry-run]
    python3 pipeline.py 西游记 --serve [--port 8801]     # 极简 Web UI

    内部：pipeline.py <项目> --stage X --_worker
        —— 由 taskctl 派生，真正跑阶段代码；CLI 前台只是等它并转发信号。
        —— 之所以让 CLI 也走子进程：Web 上点「停止」才能停掉 CLI 起的任务，
           而且两边记录的是同一个 pid、同一份 state/task.json。

阶段函数全部**延迟导入**：vm/shots.py、vm/gen.py 等并行开发中的模块即使还不存在，
服务照样起得来、UI 照样打得开，只在该阶段被调用时给出人话错误。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vm import taskctl  # noqa: E402
from vm.state import Project  # noqa: E402

STAGES = taskctl.STAGES


# ---------------------------------------------------------------- 日志


def log(msg: str) -> None:
    """统一日志格式：worker 的 stdout 被 taskctl 收进 state/run.log，UI 直接显示。"""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _hr(title: str) -> None:
    log(f"──── {title} ────")


def _need(mod, modname: str, *attrs: str):
    """
    入口函数是否都在。并行开发期"文件先提交、函数后写"很常见
    （例如 chars.py 只有 0 字节），这里给人话错误，而不是 AttributeError 堆栈。
    """
    miss = [a for a in attrs if not hasattr(mod, a)]
    if miss:
        raise taskctl.ModuleNotReady(
            f"vm/{modname}.py 里还没有 {', '.join(a + '()' for a in miss)}（很可能还在写），该阶段暂不可运行"
        )
    return mod


# ---------------------------------------------------------------- 各阶段


def stage_plan(proj: Project, params: dict, *, only: list[str], force: bool, dry: bool,
               count: int = 0, on_tick=None, should_stop=None,
               chapter_sel: str | None = None) -> dict:
    plan_mod = taskctl.import_module("plan")
    _need(plan_mod, "plan", "plan_chapter", "write_char_prompts")
    # ── 多集/多章（2026-09-24）──────────────────────────────────────────────
    # 原来这里写死 `chapter = chapters[0]`，并且提示"其余章节请分项目或手改镜头表" ——
    # 也就是**多集从来不支持**。现在改成：遍历 novel/ 下全部章节，**每章一个镜头表文件**
    # （`shots/chapter01.json` / `chapter02.json` …），`load_shots_dir` 会按文件名顺序合并，
    # 所以逐镜渲染 / 质检 / 合成不需要任何改动就能跑多集。
    #
    # 角色卡是**跨章共享**的：第 2 章自动复用第 1 章已建的角色（`plan_chapter` 的 locked 机制），
    # 只对新出现的角色建卡 —— 这是"一集里角色形象统一"的前提。
    chapters = sorted(proj.novel_dir.glob("*.md")) + sorted(proj.novel_dir.glob("*.txt"))
    chapters = [c for c in chapters if not c.name.startswith(".")]
    if not chapters:
        raise taskctl.TaskError(f"novel/ 下没有章节文件（.md/.txt）：{proj.novel_dir}")
    # `--chapter N` 只拆第 N 章（按文件名排序的第 N 个，1-based）
    if chapter_sel:
        try:
            idx = int(chapter_sel) - 1
        except (TypeError, ValueError):
            raise taskctl.TaskError(f"--chapter 要给数字（第几章），收到 {chapter_sel!r}") from None
        if idx < 0 or idx >= len(chapters):
            raise taskctl.TaskError(
                f"--chapter {chapter_sel} 超出范围：novel/ 下有 {len(chapters)} 个章节"
            )
        todo = [chapters[idx]]
    else:
        todo = chapters

    if dry:
        log(f"[dry-run] 会拆镜 {len(todo)} 章：" +
            "、".join(c.name for c in todo) + f" → {proj.shots_dir}/ + {proj.prompts_dir}/char_*.txt")
        return {"dry": True, "chapters": [str(c) for c in todo]}

    all_shots = 0
    all_chars: dict = {}
    last_stats: dict = {}
    for i, chapter in enumerate(todo):
        ch_no = plan_mod.chapter_number(chapter, params)
        _hr(f"拆镜（{i + 1}/{len(todo)}）：{chapter.name} → chapter{ch_no:02d}.json")
        cfg = dict(params)
        cfg["llm"] = params.get("llm") or {}
        pcfg = dict(cfg.get("plan") or {})
        pcfg["chapter"] = ch_no          # 让 plan_chapter 写到对应章号的文件
        cfg["plan"] = pcfg
        shots, chars, stats = plan_mod.plan_chapter(proj, chapter, cfg, log)
        all_shots += len(shots)
        all_chars.update({c.name: c for c in chars})
        last_stats = stats or {}
        if stats:
            log(f"  LLM 统计：{stats}")
    paths = plan_mod.write_char_prompts(proj, list(all_chars.values()))
    log(f"✓ 拆镜完成：{len(todo)} 章 / {all_shots} 个镜头 → {proj.shots_dir}/；"
        f"角色卡 {len(all_chars)} 个 → {paths}")
    return {"shots": all_shots, "chars": len(all_chars), "chapters": len(todo), "stats": last_stats}


def stage_chars(proj: Project, params: dict, *, only: list[str], force: bool, dry: bool,
                count: int = 0, on_tick=None, should_stop=None) -> dict:
    chars_mod = taskctl.import_module("chars")
    _need(chars_mod, "chars", "gen_all_chars")
    prompts = sorted(proj.prompts_dir.glob("char_*.txt"))
    if not prompts:
        raise taskctl.TaskError(f"prompts/ 下没有角色提示词 char_*.txt：{proj.prompts_dir}")
    if only:
        log(f"只处理指定角色：{', '.join(only)}")
    if dry:
        log(f"[dry-run] 会生成 {len(only) or len(prompts)} 个角色定妆照 → {proj.refs_dir}/")
        return {"dry": True, "chars": len(only) or len(prompts)}
    _hr("角色定妆照")
    out = chars_mod.gen_all_chars(proj, params, force=force, only=only or None, log=log,
                                  on_tick=on_tick, should_stop=should_stop)
    log(f"✓ 定妆完成 {len(out)} 个：{sorted(out)}")
    return {"refs": len(out)}


def stage_gacha(proj: Project, params: dict, *, only: list[str], force: bool, dry: bool,
                count: int = 0, on_tick=None, should_stop=None) -> dict:
    """
    抽卡：同一角色用不同 seed 出 N 张候选 → refs/_gacha/<角色>/seed*.png。

    与 chars(单张) 的区别：这个阶段是**交互式挑图**的前半段，只出候选不动 refs/；
    用户后来在控制台里点「采纳」才覆盖 refs/char_<角色>.png（R2）。
    """
    chars_mod = taskctl.import_module("chars")
    _need(chars_mod, "chars", "gen_candidates")
    n = int(count or taskctl.DEFAULT_GACHA_COUNT)
    if n < 1 or n > 24:
        raise taskctl.TaskError(f"抽卡张数要在 1~24 之间，收到 {n}")
    names = [f.stem[len("char_"):] for f in sorted(proj.prompts_dir.glob("char_*.txt"))]
    if only:
        names = [x for x in names if x in set(only)]
    if not names:
        raise taskctl.TaskError(f"没有匹配的角色（prompts/ 下的 char_*.txt：{proj.prompts_dir}）")
    if dry:
        log(f"[dry-run] 会对 {len(names)} 个角色各抽 {n} 张 → {proj.refs_dir}/_gacha/")
        return {"dry": True, "names": names, "count": n}
    _hr(f"抽卡：{len(names)} 个角色 × {n} 张")
    done: dict[str, int] = {}
    failed: dict[str, str] = {}
    for i, name in enumerate(names, 1):
        if should_stop and should_stop():
            log(f"⊘ 收到停止请求，放弃剩余角色：{names[i-1:]}")
            break
        log(f"[{i}/{len(names)}] {name}")
        try:
            out = chars_mod.gen_candidates(proj, name, params, n, log,
                                           on_tick=on_tick, should_stop=should_stop)
            done[name] = len(out)
        except Exception as e:
            log(f"  ❌ {name} 抽卡失败：{e}")
            failed[name] = str(e)
    log(f"✓ 抽卡结束：成功 {len(done)} 个角色" + (f"，失败 {len(failed)}：{failed}" if failed else ""))
    return {"gacha": done, "failed": failed, "count": n}


def stage_render(proj: Project, params: dict, *, only: list[str], force: bool, dry: bool,
                 count: int = 0, on_tick=None, should_stop=None) -> dict:
    gen_mod = taskctl.import_module("gen")
    _need(gen_mod, "gen", "render_all")
    taskctl.import_module("shots")  # 明确前置：镜头表读写是渲染的输入
    _hr("逐镜生成")
    # ★ 必须把 on_tick / should_stop 传下去（2026-09-24 修）。
    # 原来这里是 `render_all(proj, params, only=…, force=…, dry=…, log=log)` ——
    # 两个回调都没传。后果不只是"没有进度"：
    #   `comfy.wait(should_stop=None)` 会跳过中止检查，而 render_all 的循环也拿不到
    #   should_stop → **渲染期间点「停止」根本不生效**，一直要等整批渲完。
    # （chars / gacha 一直是对的，只有 render 漏了。）
    res = gen_mod.render_all(
        proj,
        params,
        only=only or None,
        force=force,
        dry=dry,
        log=log,
        on_tick=on_tick,
        should_stop=should_stop,
    )
    res = res or {}
    for k in ("rendered", "reclaimed", "skipped", "failed"):
        v = res.get(k) or []
        log(f"  {k}: {len(v)}" + (f"  {v}" if k == "failed" and v else ""))
    if res.get("failed"):
        raise taskctl.TaskError(f"有 {len(res['failed'])} 个镜头渲染失败：{res['failed']}")
    return res


def stage_qc(proj: Project, params: dict, *, only: list[str], force: bool, dry: bool,
             count: int = 0, on_tick=None, should_stop=None) -> dict:
    qc_mod = taskctl.import_module("qc")
    _need(qc_mod, "qc", "check_all")
    if dry:
        log(f"[dry-run] 会质检 {len(list(proj.clips_dir.glob('*.mp4')))} 个片段")
        return {"dry": True}
    _hr("质检")
    results = qc_mod.check_all(proj, log=log) or {}
    bad = []
    for sid, r in results.items():
        ok = getattr(r, "ok", None)
        if ok is None and isinstance(r, dict):
            ok = r.get("ok")
        if not ok:
            bad.append(sid)
    log(f"✓ 质检完成：{len(results)} 个片段，不合格 {len(bad)} 个" + (f"：{bad}" if bad else ""))
    return {"qc": len(results), "failed": bad}


def stage_assemble(proj: Project, params: dict, *, only: list[str], force: bool, dry: bool,
                   count: int = 0, on_tick=None, should_stop=None) -> dict:
    asm_mod = taskctl.import_module("assemble")
    _need(asm_mod, "assemble", "assemble")
    clips = sorted(proj.clips_dir.glob("*.mp4"))
    if not clips:
        raise taskctl.TaskError(f"clips/ 下没有片段可合成：{proj.clips_dir}")

    # ── 一集一章（2026-09-24）────────────────────────────────────────────────
    # 原来这里写死 `episode="EP01"`，而 `assemble()` 读**全部**镜头表 ——
    # 于是 2 章的项目会被**静默连成一部 EP01**（规划是多集的、合成不是）。
    # 现在按 `shots/chapterNN.json` 逐章出一集：chapter01 → EP01.mp4、chapter02 → EP02.mp4。
    # 单章项目（西游记）行为不变，仍是 EP01。
    # `--only` 给了镜头号时只合成那些镜头所在的章。
    chs: list[int] = []
    for f in sorted(proj.shots_dir.glob("chapter*.json")):
        m = re.match(r"chapter(\d+)", f.stem)
        if m:
            chs.append(int(m.group(1)))
    if not chs:                      # 镜头表命名不规范时退回单集
        chs = [0]
    if only:
        want = {str(x).split("-")[0] for x in only}
        chs = [c for c in chs if str(c) in want] or chs

    if dry:
        outs = "、".join(f"{proj.final_dir}/EP{c:02d}.mp4" for c in chs) if chs != [0] else f"{proj.final_dir}/EP01.mp4"
        log(f"[dry-run] 会合成 {len(clips)} 个片段 / {len(chs)} 集 → {outs}")
        return {"dry": True, "clips": len(clips), "episodes": len(chs)}
    _hr(f"合成（{len(chs)} 集）" if len(chs) > 1 else "合成")
    finals: list[str] = []
    for c in chs:
        ep = f"EP{c:02d}" if c else "EP01"
        out = asm_mod.assemble(
            proj, episode=ep, log=log,
            chapters=None if c == 0 else [c],
            # 质检硬故障镜头默认不进成片；项目可用
            # `project.json: include_qc_fail=true` 强行包含
            include_qc_fail=bool(params.get("include_qc_fail")),
        )
        finals.append(str(out))
        log(f"✓ 成片：{out}")
    return {"final": finals[0] if len(finals) == 1 else "", "finals": finals,
            "episodes": len(finals)}


def stage_queue(proj: Project, params: dict, *, only: list[str], force: bool, dry: bool,
                count: int = 0, on_tick=None, should_stop=None, chapter_sel=None) -> dict:
    """
    抽干作业队列（2026-09-24）。

    为什么单独一个阶段：抽卡类操作（场景/道具概念图、角色定妆）单次几十秒，
    但用户想**连着点十几个**并且**不想盯着**。所以点击 = 入队立即返回，
    由这个阶段顺序抽干队列。同项目仍然只有一个 GPU 作业在跑（互斥由任务锁保证）。
    """
    _hr("作业队列")
    from vm import queue as Q
    from vm.state import Project as _P
    if dry:
        st = Q.stats(proj)
        log(f"[dry-run] 队列：待处理 {st['pending']} / 已完成 {st['done']} / 失败 {st['failed']}")
        return {"dry": True, **st}
    res = Q.drain(proj, log=log, should_stop=should_stop)
    log(f"✓ 队列处理完毕：成功 {res['done']} / 失败 {res['failed']}")
    return res


DISPATCH = {
    "plan": stage_plan,
    "chars": stage_chars,
    "gacha": stage_gacha,
    "render": stage_render,
    "qc": stage_qc,
    "assemble": stage_assemble,
    "queue": stage_queue,
}


def run_one_stage(proj: Project, stage: str, params: dict, *, only, force, dry, count=0,
                  on_tick=None, should_stop=None, chapter_sel=None) -> dict:
    fn = DISPATCH.get(stage)
    if fn is None:
        raise taskctl.TaskError(f"未知阶段：{stage}")
    kw: dict = dict(only=list(only or []), force=force, dry=dry, count=count,
                    on_tick=on_tick, should_stop=should_stop)
    # 只有 plan 阶段认 chapter_sel（其它阶段按镜头表跑，章号由镜头表文件名体现）
    if stage == "plan":
        kw["chapter_sel"] = chapter_sel
    return fn(proj, params, **kw) or {}


def run_stages(proj: Project, stage: str, params: dict, *, only, force, dry, count=0,
               on_tick=None, should_stop=None, chapter_sel=None) -> dict:
    """顺序执行；all = plan → chars → render → qc → assemble（gacha 是交互式支线，不在 all 里）。"""
    seq = ["plan", "chars", "render", "qc", "assemble"] if stage == "all" else [stage]
    summary: dict[str, dict] = {}
    for st in seq:
        if should_stop and should_stop():
            log(f"⊘ 收到停止请求，跳过剩余阶段：{seq[seq.index(st):]}")
            break
        summary[st] = run_one_stage(proj, st, params, only=only, force=force, dry=dry,
                                    count=count, on_tick=on_tick, should_stop=should_stop,
                                    chapter_sel=chapter_sel)
    return summary


# ---------------------------------------------------------------- worker


def _fake_sleep_stage(seconds: float, should_stop) -> None:
    """
    自测替身：只睡觉，不碰 GPU、不碰 ComfyUI。
    用来证明「并发保护」与「停止真的停掉」。分段 sleep 是为了及时响应 SIGTERM。
    """
    log(f"[self-test] 替身任务启动，将运行 {seconds:.0f}s（不执行任何真实渲染）")
    t0 = time.time()
    while time.time() - t0 < seconds:
        if should_stop():
            log("[self-test] 收到停止，替身任务退出")
            return
        time.sleep(0.2)
    log("[self-test] 替身任务自然结束")


def worker_main(args) -> int:
    """
    worker 进程体：跑阶段 + 定期把进度写 state/progress.json。
    进度文件只有 worker 写，task.json 只有父进程（CLI/Web）写，各一份，不打架。
    """
    import signal as _signal

    proj = Project(taskctl.resolve_project(args.project)).ensure()
    params = taskctl.load_params(proj.root)
    stop_flag = threading.Event()
    finished = threading.Event()
    forced = {"rc": 143}

    def _watchdog() -> None:
        """阶段函数若卡在阻塞调用（例如等 ffmpeg），8s 后强退。
        这样「停止」是确定性的；渲染检查点已在提交时落盘，强退不会重烧 GPU。"""
        if finished.wait(taskctl.WORKER_KILL_GRACE):
            return
        log(f"⊘ 停止信号已 {taskctl.WORKER_KILL_GRACE:.0f}s，当前阶段仍未返回，强制退出（检查点已落盘，不丢进度）")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(143)

    def _on_signal(signum, frame):  # noqa: ARG001
        if stop_flag.is_set():
            log("⊘ 再次收到停止信号，立即退出")
            sys.stdout.flush()
            os._exit(143)
        stop_flag.set()
        log(f"⊘ 收到信号 {signum}，准备停止（当前阶段/看门狗到期后退出）")
        threading.Thread(target=_watchdog, daemon=True).start()

    _signal.signal(_signal.SIGTERM, _on_signal)
    _signal.signal(_signal.SIGINT, _on_signal)

    def _progress_loop() -> None:
        stage_now = args.stage
        while not finished.is_set():
            try:
                p = taskctl.derive_progress(proj.root, stage_now)
                p["pid"] = os.getpid()
                p["phase"] = "fake-sleep" if args._fake_sleep else stage_now
                taskctl.write_progress(proj.root, p)
            except Exception:
                pass
            finished.wait(2.0)

    threading.Thread(target=_progress_loop, name="vm-progress", daemon=True).start()

    def _orphan_rebind() -> None:
        """
        监督者（CLI/Web）被强杀时，worker 的 stdout 管道就没人读了 ——
        写满管道缓冲（64KB）后 print 会永久阻塞，任务看起来"卡死"。
        检测到被 init 收养（ppid==1）就把 stdout/stderr 重新接到 run.log，
        这样服务重启后任务照样能继续跑、日志照样在增长。
        """
        while not finished.is_set():
            if os.getppid() == 1:
                try:
                    f = open(taskctl.log_path(proj.root), "a", encoding="utf-8", buffering=1)
                    os.dup2(f.fileno(), 1)
                    os.dup2(f.fileno(), 2)
                    sys.stdout, sys.stderr = f, f
                    log("（监督进程已退出，日志改写到 run.log，任务继续）")
                except OSError:
                    pass
                return
            finished.wait(2.0)

    threading.Thread(target=_orphan_rebind, name="vm-orphan", daemon=True).start()

    rc = 0

    def _on_tick(elapsed: float, running: int, pending: int, detail: str = "") -> None:
        """
        ComfyUI 轮询回调：每 ~5s 打一行进度（节流，免得把日志刷爆）。

        `detail` 是 **vm.wsclient** 从 ComfyUI `/ws` 读到的**步级**进度
        （`12/20 步 60%`）。在此之前这里只有 `running=1 pending=0` ——
        一长串一模一样的行，看着根本不像进度条（是用户指出来的）。
        连不上 WS 时 detail 为空，自动退回旧格式。
        """
        last = tick_state[0].get("last")
        # 有步级进度时刷得勤一点（5s），否则维持 10s —— 让进度条真的在动
        interval = 5 if detail else 10
        if last is not None and elapsed - last < interval and detail == tick_state[0].get("last_detail"):
            return
        tick_state[0]["last"] = elapsed
        tick_state[0]["last_detail"] = detail
        if detail:
            log(f"  ⏳ ComfyUI {int(elapsed)}s：{detail}  (running={running} pending={pending})")
        else:
            log(f"  ⏳ ComfyUI {int(elapsed)}s：running={running} pending={pending}")

    tick_state: list[dict] = [{}]
    should_stop = stop_flag.is_set

    try:
        log(f"worker 启动：项目={proj.root.name} stage={args.stage} pid={os.getpid()} "
            f"分辨率={params.get('width')}x{params.get('height')}@{params.get('fps')}fps"
            + (f" count={args.count}" if args.count else ""))
        if args.only and args.stage not in taskctl.ONLY_STAGES:
            log(f"注意：--only ({','.join(args.only)}) 只对 {'/'.join(taskctl.ONLY_STAGES)} 阶段生效，本阶段将处理全部产物")
        if args._fake_sleep:
            _fake_sleep_stage(float(args._fake_sleep), stop_flag.is_set)
            rc = 143 if stop_flag.is_set() else 0
        else:
            run_stages(
                proj,
                args.stage,
                params,
                only=args.only,
                force=args.force,
                dry=args.dry_run,
                count=args.count,
                on_tick=_on_tick,
                should_stop=stop_flag.is_set,
                chapter_sel=getattr(args, "chapter", None),
            )
            rc = 143 if stop_flag.is_set() else 0
            if rc == 0:
                log("✓ 全部阶段完成")
    except taskctl.ModuleNotReady as e:
        log(f"✗ 模块未就绪：{e}")
        rc = 3
    except taskctl.TaskError as e:
        log(f"✗ 任务失败：{e}")
        rc = 1
    except ValueError as e:
        # 阶段模块用 ValueError 报"输入不对"（例如 shots/ 里没有镜头）：
        # 这是用户自己能修的问题，给人话，不刷整页 traceback
        log(f"✗ 任务失败（输入问题）：{e}")
        rc = 1
    except KeyboardInterrupt:
        log("✗ 被中断")
        rc = 130
    except Exception as e:
        log(f"✗ 未预期异常：{type(e).__name__}: {e}")
        traceback.print_exc()
        rc = 1
    finally:
        finished.set()
    log(f"===== 任务结束 rc={rc} =====")
    # 监督者可能已经不在了（服务被重启/强杀），worker 自己把退出码补进 task.json
    taskctl.record_worker_exit(proj.root, os.getpid(), rc)
    return rc


# ---------------------------------------------------------------- CLI 输出


def cmd_list() -> int:
    names = taskctl.discover_projects()
    if not names:
        print(f"projects/ 下没有项目：{taskctl.PROJECTS_DIR}")
        return 0
    print(f"{'项目':<16}{'章节':>6}{'镜头文件':>10}{'角色提示':>10}{'参考图':>8}{'片段':>8}  {'成片':<10}任务")
    for n in names:
        s = taskctl.project_summary(n)
        fin = s["final"]["name"] if s["final"] else "-"
        task = f"运行中 {s['task_stage']}" if s["running"] else "空闲"
        print(f"{n:<16}{s['novel_chapters']:>6}{s['shot_files']:>10}{s['char_prompts']:>10}"
              f"{s['refs']:>8}{s['clips']:>8}  {fin:<10}{task}")
    print(f"\n项目根目录：{taskctl.PROJECTS_DIR}")
    return 0


def cmd_serve(project: str | None, port: int) -> int:
    from vm import web

    root = taskctl.PROJECTS_DIR
    if project:
        pdir = taskctl.resolve_project(project)
        if not pdir.is_dir():
            print(f"✗ 项目不存在：{pdir}", file=sys.stderr)
            return 2
        print(f"[pipeline] 注意：Web UI 管理 projects/ 下的全部项目，默认选中「{pdir.name}」")
    web.serve(root, port, default_project=(Path(project).name if project else None))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pipeline.py",
        description="自建漫剧流水线：小说章节 → 成片 MP4",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python3 pipeline.py --list\n"
               "  python3 pipeline.py 西游记 --stage render --only 1-1-01\n"
               "  python3 pipeline.py 西游记 --serve --port 8801\n",
    )
    p.add_argument("project", nargs="?", help="项目名（projects/ 下的目录名）")
    p.add_argument("--stage", choices=STAGES, help="要执行的阶段")
    p.add_argument("--only", default="", help="只处理这些镜头/角色，逗号分隔（对 render/chars/gacha 生效）")
    p.add_argument("--chapter", dest="chapter", default=None,
                   help="只拆第 N 章（按 novel/ 文件名排序，1-based）。默认遍历全部章节，每章一个镜头表文件。")
    p.add_argument("--count", type=int, default=0,
                   help=f"抽卡张数（仅 gacha 阶段；默认 {taskctl.DEFAULT_GACHA_COUNT}）")
    p.add_argument("--force", action="store_true", help="忽略指纹，强制重跑")
    p.add_argument("--dry-run", action="store_true", help="只说要做什么，不真跑")
    p.add_argument("--list", action="store_true", help="列出所有项目")
    p.add_argument("--serve", action="store_true", help="启动极简 Web UI")
    p.add_argument("--port", type=int, default=8801, help="Web UI 端口（默认 8801）")
    # 内部参数：由 taskctl 派生 worker 时使用；正常用户不需要碰
    p.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--_fake-sleep", dest="_fake_sleep", type=float, default=0.0, help=argparse.SUPPRESS)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.only = [s.strip() for s in (args.only or "").split(",") if s.strip()]

    if args.list:
        return cmd_list()

    if args._worker:
        if not args.project:
            print("✗ worker 模式必须给项目名", file=sys.stderr)
            return 2
        if not args.stage and not args._fake_sleep:
            print("✗ worker 模式必须给 --stage（或自测替身）", file=sys.stderr)
            return 2
        args.stage = args.stage or "render"
        return worker_main(args)

    if args.serve:
        return cmd_serve(args.project, args.port)

    if not args.project:
        build_parser().print_help()
        print("\n✗ 需要项目名，或用 --list / --serve", file=sys.stderr)
        return 2
    if not args.stage:
        print(f"✗ 需要 --stage {'|'.join(STAGES)}（或 --serve）", file=sys.stderr)
        return 2

    pdir = taskctl.resolve_project(args.project)
    if not pdir.is_dir():
        print(f"✗ 项目不存在：{pdir}", file=sys.stderr)
        return 2
    try:
        rc = taskctl.run_foreground(
            args.project,
            args.stage,
            only=args.only,
            force=args.force,
            dry=args.dry_run,
            fake_sleep=args._fake_sleep,
            count=args.count,
        )
    except taskctl.TaskBusy as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    except taskctl.ModuleNotReady as e:
        print(f"✗ {e}", file=sys.stderr)
        return 3
    return rc


if __name__ == "__main__":
    sys.exit(main())
