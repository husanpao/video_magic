"""
register.py —— 整本小说级的**风格确定**（P2.1 / P2.2）。

## 为什么要有这一步

收敛前，风格句是**每章推一次**：`_derive_style(cfg, chapter_text, …)` 的调用点在
`plan_chapter()` 内部（原 `plan.py:2283`）。两个后果：

1. **跨章会漂**。同一本书的第 1 章可能被判成"冷青调都市悬疑"、第 2 章被判成
   "暖褐调年代戏"，而风格句是**逐字烘进每一镜提示词**的 —— 于是成片自己打架。
2. **改风格 = 全部重渲，但没有任何东西告诉你这件事**。风格句进了 `prompt` 文本，
   而 `prompt` 进指纹；换句话说改风格会让**所有镜头变 stale**。
   可 UI 上既没有"整本级风格"这个概念，也没有影响提示 ——
   用户只能等到 36 分钟 GPU 跑完才发现风格不对（或压根不知道为什么全变黄了）。

所以风格必须是**项目级、一次确定、用户可否决**的东西，而拆镜只是**读**它。

## 三条优先级（`resolve()` 的口径）

    用户显式设过的（project.json: style / style_sentence，且 confirmed）
      > 本模块推荐并已落盘的（LLM 读过整本）
      > 按预设查表的句子
      > DEFAULT_STYLE

## 为什么不强制"先跑 register 才能拆镜"

计划 §3B 把风格说成"拆镜的前置门禁"，但**做成必须多点一个按钮的门禁是负收益**：
用户的心智模型是"我点拆镜"，不是"我先登记再拆镜"。
所以这里让 `plan` 在**风格还没确定时自动跑一次 register**（整本一次，不是每章一次），
把结果落盘并打印出来 —— 门禁变成了"第一次拆镜时顺手把风格定了并告诉你"，
既保住"整本一致"这个实质，又不增加操作步骤。要改，去 W2 风格屏改。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from vm import chapters as CH
from vm import fsutil
from vm import style as _st

#: 整本通读的采样上限（字符）。一本 50 章的小说远大于此，取头 + 中段 + 尾段，
#: 因为**题材与世界观在开头，基调变化在中后段**，只取开头会漏掉"后半本换成战场"。
SAMPLE_CHARS = 12000
STYLE_FILE = "style.json"

_RECO_SYSTEM = """你是漫剧制片总监。读一部小说的**抽样正文**，为**整本**定一个画面风格基准。

【要判断的两件事】
1. `preset` —— 只能是这四个之一：
   - `realistic` 写实电影感（真人质感、胶片颗粒、自然光）
   - `cg` 半写实 3D 建模（游戏 CG 质感、皮肤次表面散射但仍是"做的"）
   - `anime` 日式二维动画（描边、赛璐璐上色、平面光）
   - `auto` 只在正文真的看不出门道时才用
   判断依据是**题材与目标受众**，不是文笔。悬疑/刑侦/职场 → 多为 realistic；
   仙侠/玄幻/修真 → 多为 cg 或 anime；校园/日常/热血 → 多为 anime。
2. `sentence` —— **一句英文**画面风格基准，会被逐字拼进每一镜的提示词。
   ★ 必须**具体到可执行**，禁止写 "beautiful" / "high quality" / "cinematic" 这类空话。
   要交代：色调倾向（哪几种颜色、饱和度高/低）、光源性质、颗粒/锐度质感、动态范围。
   举例（好）：`"muted cyan-grey palette, low saturation, wet reflections, fine 35mm grain"`
   举例（坏）：`"cinematic, beautiful lighting, high detail"`

【硬约束】
- 只依据正文里出现的东西。正文没写雪，不要为了"氛围"写 cold blue snow light。
- **不要写人物、服装、地点** —— 那些由角色卡/场景卡负责，写进来会和它们冲突。
- 不要写画幅/帧率/分辨率。

【输出】严格 JSON：
{"preset": "realistic", "sentence": "…一句英文…", "reason": "为什么（中文，一句话）",
 "confidence": 0.0~1.0}
"""


# ── 存储 ────────────────────────────────────────────────────────────────────


def path_of(proj) -> Path:
    root = Path(proj.root) if hasattr(proj, "root") and not isinstance(proj, (str, Path)) else Path(proj)
    return root / STYLE_FILE


def load(proj) -> dict:
    """读已确定的风格。缺文件 → 空 dict（表示"还没定过"）。"""
    d = fsutil.read_json(path_of(proj))
    return d if isinstance(d, dict) else {}


def save(proj, doc: dict) -> Path:
    doc = dict(doc)
    doc.setdefault("version", 1)
    doc["updated_at"] = int(time.time())
    return fsutil.write_json(path_of(proj), doc)


def resolve(proj) -> tuple[str, str, dict]:
    """
    当前生效的 `(预设, 风格句, 出处)`。

    出处 `source` 取值：`user`（人在 W2/设置里显式定的）/ `llm`（本模块推荐的）
    / `preset`（查表默认句）/ `derived`（从已有角色卡继承，仅在补拆时用）。
    """
    doc = load(proj)
    preset = _st.preset_of({"style_preset": doc.get("preset"), "style": doc.get("preset")})
    sentence = str(doc.get("sentence") or "").strip()
    source = str(doc.get("source") or "")
    if not sentence:
        sentence = _st.get(preset)["sentence"]
        source = "preset"
    return preset, sentence, {"source": source or "preset", "confirmed": bool(doc.get("confirmed")),
                              "reason": str(doc.get("reason") or ""),
                              "confidence": doc.get("confidence")}


def set_style(proj, *, preset: str | None = None, sentence: str | None = None,
             source: str = "user", log: Any = None) -> dict:
    """
    人工设定风格（W2 风格屏 / ⚙ 设置 走的都是这里）。

    ★ `source='user'` 会置 `confirmed=True` —— 之后 register 不再自动覆盖它。
    这条很重要：LLM 每次重读整本都可能给出不一样答案，
    不加这个"人已表态"的位，用户改的风格会在下次拆镜时被悄悄改回去。
    """
    doc = load(proj)
    changed: list[str] = []
    if preset:
        p = _st.resolve_preset(preset)
        if str(preset).strip().lower() not in ("", "auto") and p != str(preset).strip().lower():
            # 允许别名（"日漫"→anime），但非法值要说话，别静默变 realistic
            if log:
                log(f"  · 风格名「{preset}」按别名归一为 {p}")
        if doc.get("preset") != p:
            doc["preset"] = p
            changed.append("preset")
    if sentence is not None and str(sentence).strip() != str(doc.get("sentence") or "").strip():
        doc["sentence"] = str(sentence).strip()
        changed.append("sentence")
    if source == "user":
        doc["confirmed"] = True
    doc["source"] = source
    save(proj, doc)
    if log:
        log(f"  🎨 风格已设为 {doc.get('preset')}"
            + (f"（改了 {'、'.join(changed)}）" if changed else "（无变化）"))
    return doc


# ── 整本通读 ────────────────────────────────────────────────────────────────


def collect_sample(proj, max_chars: int = SAMPLE_CHARS) -> tuple[str, int, list[int]]:
    """
    抽一份"能代表整本"的正文。返回 `(文本, 总字数, 用到的章号)`。

    为什么不是"直接截前 N 字"：那样第 2 章之后的题材变化完全看不到。
    头 40% / 中 30% / 尾 30% 的分配是经验值 —— 开头给题材与世界观，
    中段与尾段给"基调是否变了"。
    """
    files = CH.ordered_files(proj)
    texts: list[tuple[int, str]] = []
    for i, f in enumerate(files, 1):
        try:
            texts.append((i, Path(f).read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    total = sum(len(t) for _n, t in texts)
    if not texts:
        return "", 0, []
    if total <= max_chars:
        return "\n\n".join(t for _n, t in texts), total, [n for n, _t in texts]

    budget = {"head": int(max_chars * 0.4), "mid": int(max_chars * 0.3), "tail": int(max_chars * 0.3)}
    head = texts[0][1][: budget["head"]]
    mid_i = len(texts) // 2
    mid = texts[mid_i][1][: budget["mid"]] if mid_i < len(texts) and mid_i != 0 else ""
    tail = texts[-1][1][-budget["tail"]:] if len(texts) > 1 else ""
    parts = [f"【开头 · 第{texts[0][0]}章】\n{head}"]
    if mid:
        parts.append(f"【中段 · 第{texts[mid_i][0]}章】\n{mid}")
    if tail and len(texts) > 1:
        parts.append(f"【结尾 · 第{texts[-1][0]}章】\n{tail}")
    used = sorted({texts[0][0], texts[mid_i][0] if mid else texts[0][0], texts[-1][0]})
    return "\n\n".join(parts), total, used


def recommend(proj, cfg: dict, log: Callable[[str], None] = print, *,
              force: bool = False) -> dict:
    """
    读整本抽样 → 让 LLM 推荐 `(preset, sentence)` 并落盘。

    `confirmed`（人已表态）时**默认跳过** —— 除非 `force`。
    返回里带 `skipped`，让调用方能说清"为什么这次没重推"。
    """
    doc = load(proj)
    if doc.get("confirmed") and not force:
        log("  🎨 风格已由人工确认，跳过自动推荐（要重来用 force）")
        return {**doc, "skipped": True}

    sample, total, used = collect_sample(proj)
    if not sample.strip():
        raise ValueError("项目里没有任何章节正文，无法推荐风格")

    preset_hint = _st.preset_of(cfg)
    user = (f"【抽样正文（全书约 {total} 字，取第 {'、'.join(str(n) for n in used)} 章）】\n"
            f"{sample}\n\n"
            f"【当前配置预设】{preset_hint}"
            f"{'（来自 style_preset/style 配置，若与正文题材明显不符，请以正文为准并在 reason 里说明）' if preset_hint else ''}")
    from vm import plan as _pl

    obj, meta = _pl._llm_json(cfg, _RECO_SYSTEM + "\n" + _st.guidance(preset_hint), user,
                              temperature=0.3, log=log, tag="整本风格推荐")
    preset = _st.resolve_preset((obj or {}).get("preset"))
    sentence = str((obj or {}).get("sentence") or "").strip().rstrip(".")
    if not sentence:
        raise ValueError("风格推荐没给出句子")
    doc.update({
        "preset": preset, "sentence": sentence,
        "reason": str((obj or {}).get("reason") or "").strip(),
        "confidence": (obj or {}).get("confidence"),
        "source": "llm", "confirmed": False,
        "book_chars": total, "sampled_chapters": used,
    })
    save(proj, doc)
    # 用 tokens 记一笔，让成本台账看得见这次调用（推荐只跑一次，但它是真花钱的）
    usage = meta.get("usage") if isinstance(meta, dict) else None
    log(f"  🎨 整本风格：{preset} ｜ {sentence[:70]}{'…' if len(sentence) > 70 else ''}")
    if doc.get("reason"):
        log(f"     理由：{doc['reason']}")
    return {**doc, "skipped": False, "usage": usage}


def ensure_style(proj, cfg: dict, log: Callable[[str], None] = print) -> dict:
    """
    `plan` 阶段的入口：**风格没定过就先定一次**（整本一次，不是每章一次）。

    这是把计划 §3B 的"前置门禁"落到实处的做法 —— 但做成自动的，
    不逼用户多点一个按钮（见模块头的说明）。
    """
    doc = load(proj)
    if doc.get("preset") and doc.get("sentence"):
        return doc
    try:
        return recommend(proj, cfg, log)
    except Exception as e:                       # 推荐失败不该拖垮拆镜
        log(f"  ⚠️ 整本风格推荐失败，退回预设查表：{e}")
        preset = _st.preset_of(cfg)
        doc = {"preset": preset, "sentence": _st.get(preset)["sentence"], "source": "preset"}
        save(proj, doc)
        return doc


def style_impact(proj, preset: str | None = None, sentence: str | None = None) -> dict:
    """
    「改风格会影响什么」的清单（计划 §2.4）。

    ★ 这里必须把两类影响**分开说**，因为它们代价差一个数量级：

    1. **图**（定妆照 / 场景道具概念图 / 分镜图）：
       风格后缀现在是在**生成时**拼进提示词的（P2.3），且拼完的结果进指纹（P2.4），
       所以改风格 → 这些图的指纹立刻不匹配 → 界面上能看到"哪几张是旧风格出的"。
       重出是分钟级 GPU。

    2. **镜头提示词**：
       风格句是在**拆镜那一刻**被逐字烘进每一镜的 `prompt` 文本的，
       所以**改 `style.json` 不会让任何已有镜头变 stale** ——
       旧镜头继续带旧风格句。要让新风格进到镜头，唯一的路是**重跑拆镜**，
       而那会重写全部镜头表 ⇒ 全部镜头重渲（实测 52 镜约 36 分钟 GPU）。

    把第 2 点写成"改了就好"是骗人的；写成"改了要重跑拆镜 + 全部重渲"才是可决策的信息。
    这也是计划 §3B 说"风格应当是拆镜的前置门禁"的原因 —— 事前定比事后改便宜得多。
    """
    from vm import chars as _c
    from vm import assets as _a
    from vm import imgfp as _fp
    from vm.state import Project as _P

    pj = _P(Path(proj.root) if hasattr(proj, "root") else Path(proj))
    cur_preset, cur_sentence, meta = resolve(proj)
    npreset = _st.resolve_preset(preset) if preset else cur_preset
    nsentence = (sentence if sentence is not None else cur_sentence)
    # `params` 只用来喂 `apply_image_suffix` / `negative_for_params` 的口径，
    # 所以这里造一份等价视图，不去读线上 project.json
    view = {"style_preset": npreset, "style": npreset, "style_sentence": nsentence}
    changed = (npreset != cur_preset) or (nsentence.strip() != cur_sentence.strip())

    portraits = {"total": 0, "stale": 0, "untracked": 0, "names": [], "never": 0}
    for f in sorted(pj.prompts_dir.glob("char_*.txt")):
        name = f.stem[len("char_"):]
        portraits["total"] += 1
        ref = pj.refs_dir / f"char_{name}.png"
        if not ref.is_file():
            portraits["never"] += 1
            continue                     # 还没出过图 → 不算"待重出"，是"待首次出图"
        try:
            prompt = f.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        final, neg, render = _c.portrait_inputs(prompt, view, 0)
        fp = _fp.fingerprint("portrait", final, neg, render, 0)
        if not _fp.current(ref):
            # ★ 指纹机制之前出的图 / 用户上传的图 **没有指纹记录**。
            # 把这种情况算成"不 stale"会给出**虚假的安心**（0/2 看起来像"都不用重出"），
            # 所以单独计 `untracked`，让界面能说"这 N 张无从判断，建议按新风格重出"。
            portraits["untracked"] += 1
            portraits["names"].append(name)
            continue
        if _fp.is_stale(ref, fp):
            portraits["stale"] += 1
            portraits["names"].append(name)

    assets = {"total": 0, "stale": 0, "untracked": 0, "keys": []}
    try:
        recs = _a.load_index(pj)
    except Exception:
        recs = {}
    for key, rec in (recs or {}).items():
        adopted = getattr(rec, "adopted", "") or ""
        if not adopted:
            continue
        assets["total"] += 1
        img = _a.asset_dir(pj, rec.kind, rec.id) / adopted
        if not img.is_file():
            continue
        final, neg, render = _a.asset_inputs(getattr(rec, "prompt", "") or "", view, 0,
                                             rec.kind, rec.id)
        fp = _fp.fingerprint(f"asset:{rec.kind}", final, neg, render, 0, {"id": rec.id})
        if not _fp.current(img):
            assets["untracked"] += 1
            assets["keys"].append(key)
            continue
        if _fp.is_stale(img, fp):
            assets["stale"] += 1
            assets["keys"].append(key)

    # 镜头侧：只报"有多少镜带着**旧风格句**"，并说清要改得重跑拆镜
    shots_with_old, shots_total = _count_prompts_with_style(pj, cur_sentence)
    return {
        "from": {"preset": cur_preset, "sentence": cur_sentence, "source": meta["source"]},
        "to": {"preset": npreset, "sentence": nsentence},
        "changed": changed,
        "images": {
            "portraits": portraits, "assets": assets,
            "note": "这些图的指纹会因风格变化而不匹配；界面会标出来，重出请显式点（要花 GPU）。",
        },
        "shots": {
            "total": shots_total, "with_old_style": shots_with_old,
            "auto_stale": 0,
            "requires": "replan",
            "note": ("★ 改风格**不会**让已有镜头自动变 stale —— 旧风格句已经烘在每镜的 "
                     "prompt 文本里。要让新风格进到镜头，必须重跑「拆镜」重写镜头表，"
                     "而那会让**全部镜头重渲**（这是本项目最大的一笔成本）。"
                     "所以风格应当在拆镜之前就定好。"),
        },
    }


def _count_prompts_with_style(proj, sentence: str) -> tuple[int, int]:
    """数一数有多少镜的提示词里带着给定的风格句。返回 (带旧句的镜数, 总镜数)。"""
    from vm import shots as _sh

    root = Path(proj.root) if hasattr(proj, "root") else Path(proj)
    try:
        rows = _sh.load_shots_dir(root / "shots")
    except Exception:
        return 0, 0
    needle = (sentence or "").strip().lower().rstrip(".")
    hit = 0
    for sh in rows:
        p = str(getattr(sh, "prompt", "") or "").lower()
        if needle and needle in p:
            hit += 1
    return hit, len(rows)


def summary(proj) -> dict:
    """给 W2 风格屏 / ⚙ 设置的展示结构。"""
    doc = load(proj)
    preset, sentence, meta = resolve(proj)
    presets = []
    for name in ("realistic", "cg", "anime"):
        p = _st.get(name)
        presets.append({"key": name, "label": p.get("label") or name,
                        "sentence": p.get("sentence"), "on": name == preset})
    return {
        "preset": preset, "sentence": sentence,
        "source": meta["source"], "confirmed": meta["confirmed"],
        "reason": meta.get("reason", ""), "confidence": meta.get("confidence"),
        "book_chars": doc.get("book_chars"), "sampled_chapters": doc.get("sampled_chapters") or [],
        "presets": presets,
        "note": ("画风由**参考图**决定、文字控不住（实测）。改这里的风格只影响提示词写法；"
                 "要让画面真的变风格，需要按新风格重出定妆照与场景/道具概念图。"),
    }
