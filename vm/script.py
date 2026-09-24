"""
script.py —— 剧本层：把小说章节转成**可读、可编辑、且无损**的结构化剧本。

## 为什么要有这一层
现在拆完镜只有 52 个技术性 JSON，人看不懂"这一章讲了什么、有哪些场、谁说了什么"。
剧本层提供一个**人的视图**：场次 → 节拍（beat）→ 人物 / 对白 / 舞台指示。

## ★ 铁律：无损（这一条决定了整个设计）
调研的负面清单里明确写着：
  ⛔「storyforge 的摘要式链路」是**架构级错误**：原文第 1 步就丢，对白必丢。
所以本模块**不能替代小说作为对白来源**，只做**结构化视图**：

  · 剧本里的每一句对白都必须带 `src_span = [start, end]`，指向**小说原文的字符区间**
  · `verify()` 做**确定性校验** `chapter_text[a:b] == quote`（纯字符串比对，零成本）
  · 校验不过的行不会被悄悄接受 → 记成 `span_failures` 并**影响 gate**

拆镜仍然直接从小说来（B5 那条已被 66 条单测覆盖的路径不变）。
剧本层是**旁路产物**：丢了它，流水线照跑；有它，人能在拆镜前看懂并修改。

## 为什么让 LLM 输出字符偏移而不是让它自己切分
让 LLM "把正文切成场次"会引入改写风险（它可能顺手润色对白）。
让它只输出**偏移量**，再在本地用偏移量去原文取子串 —— 拿到的一定是原文，
改写在结构上不可能发生。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPT_DIRNAME = "scripts"
SCHEMA_VERSION = 1


# ── 数据模型 ────────────────────────────────────────────────────────────────


@dataclass
class ScriptLine:
    """一句对白。`src_span` 指向小说原文的字符区间（无损的保证）。"""

    char: str
    quote: str
    span: tuple[int, int] | None = None

    def to_dict(self) -> dict:
        return {"char": self.char, "quote": self.quote,
                "src_span": list(self.span) if self.span else None}


@dataclass
class ScriptBeat:
    """一个节拍：同一场景、同一小段时间里的连续动作与对白。"""

    id: str
    scene_id: str
    characters: list[str] = field(default_factory=list)
    action: str = ""
    lines: list[ScriptLine] = field(default_factory=list)
    span: tuple[int, int] | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "scene_id": self.scene_id,
            "characters": self.characters, "action": self.action,
            "lines": [x.to_dict() for x in self.lines],
            "src_span": list(self.span) if self.span else None,
        }


@dataclass
class SceneScript:
    scene_id: str
    name: str
    beats: list[ScriptBeat] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"scene_id": self.scene_id, "name": self.name,
                "beats": [b.to_dict() for b in self.beats]}


@dataclass
class ChapterScript:
    chapter: int
    title: str
    chapter_chars: int
    scenes: list[SceneScript] = field(default_factory=list)
    # 校验结果（跑 verify 后填充）
    span_checks: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    generated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "chapter": self.chapter, "title": self.title,
            "chapter_chars": self.chapter_chars,
            "scenes": [s.to_dict() for s in self.scenes],
            "span_checks": self.span_checks,
            "warnings": self.warnings,
            "generated_at": self.generated_at or time.time(),
        }

    @property
    def n_lines(self) -> int:
        return sum(len(b.lines) for s in self.scenes for b in s.beats)

    @property
    def n_beats(self) -> int:
        return sum(len(s.beats) for s in self.scenes)


# ── 校验（无损的判定） ──────────────────────────────────────────────────────

_PUNCT = "，。！？；：、「」『』（）《》…—·,.!?;:\"'()<>[] \n\r\t"


def _norm(s: str) -> str:
    """去掉标点与空白后的规范形。用于"只差标点"的容忍比对。"""
    return "".join(ch for ch in (s or "") if ch not in _PUNCT)


def match_merged_quotes(text_quotes: list[str], quote: str) -> bool:
    """
    判断 `quote` 是否等于正文里**连续若干条引文的拼接**（中间的叙述被去掉了）。

    这是"合并引文"的**精确判据** —— 不要再靠猜切点。
    我踩过的坑：先按句末标点（。！？）切分 quote 再逐片找，
    但合并边界完全可能落在**逗号**上：
        正文引文：①「下一站，」 ②「归墟。」
        镜头对白：「下一站，归墟。」
    按句末切 → 切不开 → 误判成"凭空生成"。

    所以反过来做：拿**正文的引文清单**（`extract_source_quotes`，权威且确定）去拼，
    看能不能拼出 quote。全拼接、去空白后比较。
    """
    def nz(x: str) -> str:
        return re.sub(r"[\s「」“”‘’\"']", "", x or "")

    q = nz(quote)
    if not q:
        return False
    src = [nz(x) for x in (text_quotes or []) if nz(x)]
    for i in range(len(src)):
        acc = ""
        for k in range(i, min(i + 8, len(src))):
            acc += src[k]
            if acc == q:
                return True
            if len(acc) >= len(q):
                break
    return False


def verify(chapter_text: str, script: ChapterScript) -> dict:
    """
    确定性校验：剧本里每句对白都必须能在原文里**逐字**找到。

    返回 {checked, exact_ok, normalized_ok, failed, failures[], coverage_pct}。
    这是本模块的**核心价值** —— 它把"无损"从口号变成可判定的断言。
    """
    # 正文的权威引文清单 —— 合并引文的判据要用它（见 match_merged_quotes）
    _src_quotes = extract_source_quotes(chapter_text)
    checked = exact = norm_ok = drift = merged = 0
    failures: list[dict] = []
    drifts: list[dict] = []
    covered: list[tuple[int, int]] = []

    for sc in script.scenes:
        for b in sc.beats:
            for ln in b.lines:
                checked += 1
                q = ln.quote or ""
                if not q:
                    continue
                # ── 三级判定（2026-09-24 修）─────────────────────────────
                # 实测：LLM 数偏移时会**把 markdown 标题那一行漏掉**，
                # 于是全篇 span 整体偏移 11 个字符 —— 台词**一个字都没错**，
                # 但第一版 verify() 因为"span 指向的原文与 quote 不一致"判了 14/14 失败。
                #
                # 「偏移量数错」和「改写台词」是**两件完全不同的事**：
                #   ① span 对上                → exact      ✅
                #   ② span 只差标点            → normalized ✅（容忍）
                #   ③ span 不对，但 quote 能在原文里逐字找到 → **span 漂移**，就地修正 ⚠️ 不算失败
                #   ④ quote 在原文里根本找不到 → 改写/幻觉    ❌ 失败
                # 只有 ④ 才该让门禁红。
                sp = ln.span
                if sp and 0 <= sp[0] < sp[1] <= len(chapter_text):
                    seg = chapter_text[sp[0]:sp[1]]
                    if seg == q:
                        exact += 1
                        covered.append((sp[0], sp[1]))
                        continue
                    if _norm(seg) == _norm(q):
                        norm_ok += 1
                        covered.append((sp[0], sp[1]))
                        continue
                idx = chapter_text.find(q)
                if idx >= 0:
                    # ③ span 漂移（LLM 数错）→ 用真实位置修正，不算失败
                    if sp and (sp[0] != idx):
                        drift += 1
                        drifts.append({"beat": b.id, "quote": q[:30],
                                       "given": list(sp), "fixed": [idx, idx + len(q)]})
                    else:
                        norm_ok += 1
                    ln.span = (idx, idx + len(q))
                    covered.append(ln.span)
                else:
                    # ⑤ **合并引文**（2026-09-24 新增判定）──────────────────────
                    # 拆镜时会把正文里**相邻的多段引文合并**进一个镜头，中间的叙述被去掉：
                    #     正文：  "听见什么。"男人没抬头，"这趟车早就该到终点站了。"
                    #     镜头：  听见什么。这趟车早就该到终点站了。
                    # 逐字搜整串必然失败，但台词**一个字都没改**。
                    # 判据：把 quote 按句切成片段，全部按顺序能在原文找到 → 通过（记 merged）。
                    mspan = _find_span(chapter_text, q)
                    if mspan or match_merged_quotes(_src_quotes, q):
                        merged += 1
                        ln.span = mspan
                        if mspan:
                            covered.append(mspan)
                    else:
                        # ④ 原文里找不到 → 这才是真问题
                        failures.append({"beat": b.id, "quote": q[:40],
                                         "span": list(sp) if sp else None,
                                         "actual": chapter_text[sp[0]:sp[1]][:40] if sp else "",
                                         "why": "quote 在原文里逐字找不到（被改写或凭空生成）"})

    # 覆盖率：对白区间覆盖了原文多少"对白字符"
    src_quotes = _src_quotes
    src_chars = sum(len(q) for q in src_quotes)
    got = sum(e - s for s, e in _merge_spans(covered))
    coverage = round(min(100.0, got / src_chars * 100), 1) if src_chars else 0.0

    return {
        "checked": checked,
        "exact_ok": exact,
        "normalized_ok": norm_ok,
        "merged_ok": merged,   # 相邻引文被合并（台词未改），不算失败
        "span_drift": drift,
        "drifts": drifts[:20],
        "failed": len(failures),
        "failures": failures[:50],
        "source_quotes": len(src_quotes),
        "source_chars": src_chars,
        "covered_chars": got,
        "coverage_pct": coverage,
        # ★ 门禁只看 `failed`（原文里找不到）。span 漂移属于"定位不准"，已就地修正，
        #   不该让门禁红 —— 否则一个 markdown 标题就能让整章"无损校验失败"。
        "gate_passed": len(failures) == 0,
    }


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    spans = sorted(spans)
    out = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(a, b) for a, b in out]


_QUOTE_RE = re.compile(r"[“\"『「]([^”\"』」]{2,80})[”\"』」]")


def extract_source_quotes(text: str) -> list[str]:
    """从正文里抽出所有引号包裹的对白（覆盖率的分母）。容忍中英文引号。"""
    out: list[str] = []
    for m in _QUOTE_RE.finditer(text or ""):
        q = m.group(1).strip()
        if q:
            out.append(q)
    return out



def _find_span(text: str, quote: str) -> tuple[int, int] | None:
    """
    在正文里定位这句台词的**逐字**位置。

    ⚠️ 不能只做一次 `text.find(quote)` —— 实测第 1 章有 5 句"找不到"，其实是
    **镜头把正文里相邻的多段引文合并了**，中间的叙述被去掉：

        正文：  "听见什么。"男人没抬头，"这趟车早就该到终点站了。"
        镜头：  听见什么。这趟车早就该到终点站了。

    所以定位策略是三层：
      ① 整串逐字找（最常见）
      ② 去掉引号再找
      ③ **按句切成片段逐个找**；全部按顺序找到 → 返回"首片段起点 → 末片段终点"的跨度。
         这正是"合并了相邻引文"的正确语义，不是找不到。

    返回 None 才是真失败（台词被改写或凭空生成）——那会进 verify 的失败清单。
    """
    q = (quote or "").strip()
    if not q:
        return None
    # ① 整串
    i = text.find(q)
    if i >= 0:
        return (i, i + len(q))
    # ② 去引号
    bare = q.strip("「」“”‘’\"'")
    if bare and bare != q:
        i = text.find(bare)
        if i >= 0:
            return (i, i + len(bare))
    # ③ 分片依次定位（合并引文的语义）
    frags = [x.strip() for x in re.split(r"(?<=[。！？!?…])", q) if x.strip()]
    if len(frags) < 2:
        return None
    pos = 0
    first = last = None
    for fr in frags:
        f = fr.strip("「」“”‘’\"'")
        if not f:
            continue
        j = text.find(f, pos)
        if j < 0:
            return None                      # 有一片找不到 → 整体算失败
        if first is None:
            first = j
        last = j + len(f)
        pos = last
    return (first, last) if first is not None else None


def build_from_shots(proj, chapter_no: int, shots, chapter_text: str,
                     log=lambda m: None) -> ChapterScript:
    """
    **从镜头表推导剧本视图 —— 不调 LLM。**

    为什么改成这样（2026-09-24）：
      原来这里自己调一次 LLM 从小说重新抽结构，产出一个**没有任何消费者**的旁路产物，
      UI 那个 tab 于是永远显示"还没有剧本"。而 `shots/chapterNN.json` 里**已经有**：
        · `scene_id` → 场次（按它分组）
        · `action`   → 舞台指示
        · `dialogue` → 对白
        · `chars`    → 人物
      直接取字段就能组出同一份视图，**省掉每次几万 token**，而且它从"旁路产物"
      变成"`plan` 结果的可读审计视图"—— 这才是它该有的位置。

    无损保证仍然成立：每句台词都要在**章节正文里逐字找到**（`_find_span`），
    找不到就进 verify 的失败清单。这一点比原来更强 —— 原来的 LLM 可能给出错误偏移，
    现在是从实际使用的镜头表反查正文。
    """
    from vm import plan as P

    scenes_meta = {}
    try:
        scenes_meta = P.load_scenes(proj)
    except Exception:                                          # noqa: BLE001
        scenes_meta = {}

    # 按 scene_id 分组，保持镜头顺序
    order: list[str] = []
    groups: dict[str, list] = {}
    for sh in shots:
        sid = str(getattr(sh, "scene_id", "") or "")
        if sid not in groups:
            groups[sid] = []
            order.append(sid)
        groups[sid].append(sh)

    scenes: list[SceneScript] = []
    n_lines = 0
    for sid in order:
        meta = scenes_meta.get(sid)
        name = (getattr(meta, "name", "") if meta else "") or (sid or "（未标场次）")
        beats: list[ScriptBeat] = []
        for sh in groups[sid]:
            chars = list(getattr(sh, "chars", None) or [])
            dlg = str(getattr(sh, "dialogue", "") or "").strip()
            nar = str(getattr(sh, "narration", "") or "").strip()
            lines: list[ScriptLine] = []
            if dlg:
                # 说话人：单角色镜直接就是那个人；多角色镜取第一个登场角色
                # （拆镜规范要求"说话人尽量在 chars 里"，所以这个推断是可靠的）
                who = chars[0] if chars else ""
                lines.append(ScriptLine(char=who, quote=dlg, span=_find_span(chapter_text, dlg)))
                n_lines += 1
            if nar:
                lines.append(ScriptLine(char="旁白", quote=nar, span=_find_span(chapter_text, nar)))
                n_lines += 1
            beats.append(ScriptBeat(
                id=str(getattr(sh, "id", "")),
                scene_id=sid,
                characters=chars,
                action=str(getattr(sh, "action", "") or "").strip(),
                lines=lines,
                span=_find_span(chapter_text, str(getattr(sh, "action", "") or "")[:20]),
            ))
        scenes.append(SceneScript(scene_id=sid, name=name, beats=beats))

    sc = ChapterScript(
        chapter=chapter_no,
        title=f"第{chapter_no}章",
        chapter_chars=len(chapter_text or ""),
        scenes=scenes,
        generated_at=time.time(),
    )
    v = verify(chapter_text or "", sc)
    sc.span_checks = v
    if v.get("failures"):
        for f in v["failures"][:5]:
            log(f"  ⚠ 剧本校验：{f}")
    log(f"  📜 剧本视图（由镜头表推导，未调 LLM）：{len(scenes)} 场次 / "
        f"{sum(len(x.beats) for x in scenes)} 节拍 / {n_lines} 句对白")
    return sc


def script_path(proj, chapter_no: int) -> Path:
    return Path(proj.root) / SCRIPT_DIRNAME / f"chapter{chapter_no:02d}.json"


def save_script(proj, script: ChapterScript) -> Path:
    p = script_path(proj, script.chapter)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(script.to_dict(), ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, p)
    return p


def load_script(proj, chapter_no: int) -> ChapterScript | None:
    p = script_path(proj, chapter_no)
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    scenes: list[SceneScript] = []
    for sc in d.get("scenes") or []:
        beats: list[ScriptBeat] = []
        for b in sc.get("beats") or []:
            lines = [
                ScriptLine(char=str(x.get("char") or ""), quote=str(x.get("quote") or ""),
                           span=tuple(x["src_span"]) if x.get("src_span") else None)
                for x in (b.get("lines") or []) if isinstance(x, dict)
            ]
            beats.append(ScriptBeat(
                id=str(b.get("id") or ""), scene_id=str(b.get("scene_id") or sc.get("scene_id") or ""),
                characters=[str(x) for x in (b.get("characters") or [])],
                action=str(b.get("action") or ""), lines=lines,
                span=tuple(b["src_span"]) if b.get("src_span") else None,
            ))
        scenes.append(SceneScript(scene_id=str(sc.get("scene_id") or ""),
                                  name=str(sc.get("name") or ""), beats=beats))
    return ChapterScript(
        chapter=int(d.get("chapter") or chapter_no), title=str(d.get("title") or ""),
        chapter_chars=int(d.get("chapter_chars") or 0), scenes=scenes,
        span_checks=d.get("span_checks") or {}, warnings=d.get("warnings") or [],
        generated_at=float(d.get("generated_at") or 0),
    )


# ── 渲染成人类可读的剧本 ────────────────────────────────────────────────────


def render(script: ChapterScript) -> str:
    out: list[str] = [f"《{script.title}》  第 {script.chapter} 章  剧本视图", "=" * 60]
    ck = script.span_checks or {}
    out.append(
        f"节拍 {script.n_beats} / 对白 {script.n_lines} / "
        f"逐字校验 {ck.get('exact_ok', 0)} 通过"
        + (f" + {ck.get('normalized_ok', 0)} 仅差标点" if ck.get("normalized_ok") else "")
        + (f" / span 漂移已修正 {ck.get('span_drift', 0)}" if ck.get("span_drift") else "")
        + f" / 失败 {ck.get('failed', 0)}"
        + f" / 覆盖率 {ck.get('coverage_pct', 0)}%"
        + ("  ✅ 门禁通过" if ck.get("gate_passed") else "  ❌ 门禁不通过")
    )
    out.append("")
    for sc in script.scenes:
        out.append(f"── {sc.scene_id}  {sc.name} ──")
        for b in sc.beats:
            who = "、".join(b.characters) if b.characters else "—"
            out.append(f"  [{b.id}] ({who})")
            if b.action:
                out.append(f"      旁白/动作：{b.action}")
            for ln in b.lines:
                span = f"  ⟨{ln.span[0]}-{ln.span[1]}⟩" if ln.span else "  ⟨无定位⟩"
                out.append(f"      {ln.char or '?'}：{ln.quote}{span}")
        out.append("")
    if script.warnings:
        out.append("提醒：")
        for w in script.warnings:
            out.append(f"  · {w}")
    return "\n".join(out)


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m vm.script",
        description="剧本层（无损结构化视图）：小说章节 → 场次/节拍/对白，每句带 src_span 可逐字校验。",
    )
    ap.add_argument("project", help="项目名或目录")
    ap.add_argument("--chapter", type=int, default=None, help="只处理第 N 章（默认全部）")
    ap.add_argument("--json", action="store_true", help="打印 JSON 而不是可读剧本")
    ap.add_argument("--no-write", action="store_true", help="只打印，不落盘")
    a = ap.parse_args(argv)

    from vm.state import Project
    from vm.taskctl import resolve_project

    pdir = resolve_project(a.project)
    proj = Project(pdir)
    cfg = json.loads((proj.root / "project.json").read_text(encoding="utf-8"))
    novels = [p for p in sorted(proj.novel_dir.glob("*.md")) + sorted(proj.novel_dir.glob("*.txt"))
              if not p.name.startswith(".")]
    if a.chapter:
        novels = [novels[a.chapter - 1]] if 0 < a.chapter <= len(novels) else []
    if not novels:
        print(f"novel/ 下没有章节文件：{proj.novel_dir}", file=sys.stderr)
        return 2

    rc = 0
    for n in novels:
        from vm import plan as P
        ch = P.chapter_number(n, cfg)
        print(f"\n抽剧本：{n.name} → chapter{ch:02d}.json")
        # 从镜头表推导（**不调 LLM**）—— 与 plan 阶段同一条路径，
        # 保证 CLI 与流水线产出的视图一致；旧的 LLM 版本已删除。
        from vm.shots import load_shots_dir
        allsh = load_shots_dir(proj.shots_dir)
        ch_no = n
        try:
            from vm import plan as _P
            ch_no = _P.chapter_number(n, cfg)
        except Exception:                                      # noqa: BLE001
            m = re.search(r"(\d+)", Path(n).stem)
            ch_no = int(m.group(1)) if m else 1
        text = Path(n).read_text(encoding="utf-8")
        shots = [x for x in allsh if str(x.id).startswith(f"{ch_no}-")]
        if not shots:
            print(f"  跳过 {Path(n).name}：镜头表里没有第 {ch_no} 章的镜头（先跑 plan）")
            continue
        s = build_from_shots(proj, ch_no, shots, text, log=lambda m: print("  " + str(m)))
        if a.json:
            print(json.dumps(s.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(render(s))
        if not a.no_write:
            print(f"已写入 {save_script(proj, s)}")
        if not (s.span_checks or {}).get("gate_passed"):
            rc = 1
            print("❌ 逐字校验未通过 —— 有对白与原文不一致（见上面 failures）", file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
