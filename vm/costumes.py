"""
costumes.py —— 服装变体系统（P2）。

## 解决的问题
小说里同一个角色在不同章节/场次会换衣服，但原来的做法把服装**锁死在三处**：
  ① 定妆照把服装烤进了参考图 `refs/char_<名>.png`
  ② 每镜的 `subject_definitions` 逐字抄角色卡里的服装句
  ③ 每镜的 `retention_analysis` 明写 `clothing must match <Picture 1> exactly`
     / `do not change the clothing colour`
再加 `prompts/char_<名>.txt` 是**整项目一份**，换装会污染别的章节。

## 采用的方案：图锁身份 + 文控服装
- **参考图只负责身份**：脸、发型、年龄、性别、体型
- **服装由文字控制**：`identity`（角色不变的部分）与 `variants`（多套服装）分开存
- 依据：本项目 A/B 实测——**提示词里的文字会压过参考图**
  （男性参考图 + 女性文字 → 输出女性，像素差 34.13）。
  所以想让模型换衣服，就必须**显式声明"服装以文字为准"**，否则它会跟着参考图走。

## 数据模型（项目级 `projects/<名>/costumes.json`）
```json
{
  "version": 1,
  "characters": {
    "孙悟空": {
      "identity": "Monkey form with golden-brown short fur covering the face, ... jaw set",
      "variants": [
        {"id": "default", "label": "常服", "prompt": "He wears a red coarse cotton short tunic ..."},
        {"id": "armor",   "label": "战甲", "prompt": "He wears golden chainmail armour ..."}
      ]
    }
  }
}
```

镜头级指定（`shots/*.json`）：`"costume": {"孙悟空": "armor"}`
解析顺序：镜头级 → 默认变体。**没有指定 = 用默认变体 = 行为与从前完全一致。**

## 为什么不做"每套服装一张参考图"
用户已确认只做"图锁身份 + 文控服装"：零额外抽卡、改文字即换装。
代价是同套服装跨镜的细节一致性略弱于参考图锁定 —— 这是明确的取舍。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

VERSION = 1
DEFAULT_VARIANT = "default"

# 服装句的起始标志。角色卡里的服装描述基本都是这几类写法。
_CLOTHING_START = re.compile(
    r"(?:^|(?<=[.!?])\s+)"
    r"((?:he|she|they)\s+wears?\b|wearing\b|dressed\s+in\b|clad\s+in\b|dressed\s+as\b)",
    re.IGNORECASE,
)


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def split_identity_costume(appearance: str) -> tuple[str, str]:
    """
    把角色卡里的「外貌+服装」拆成 (identity, costume)。

    为什么要拆：换装只能改服装那部分，身份描述必须一字不动
    （身份描述是定妆图的生成原文，动它 = 跨镜脸崩）。

    找不到服装句就返回 (原文, "")，此时该角色只有一套"服装"，
    行为与从前的"图锁一切"完全一致 —— 保守而不是猜。
    """
    text = (appearance or "").strip()
    if not text:
        return "", ""
    m = _CLOTHING_START.search(text)
    if not m:
        return text, ""
    identity = text[: m.start()].strip().rstrip(".")
    costume = text[m.start() :].strip()
    if not identity or not costume:
        return text, ""
    return identity, costume


def path_of(proj) -> Path:
    return Path(proj.root) / "costumes.json"


def load(proj) -> dict:
    """读 costumes.json；不存在或损坏返回空骨架（不抛，调用方按"无服装配置"处理）。"""
    p = path_of(proj)
    if not p.is_file():
        return {"version": VERSION, "characters": {}}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": VERSION, "characters": {}}
    if not isinstance(d, dict):
        return {"version": VERSION, "characters": {}}
    chars = d.get("characters")
    if not isinstance(chars, dict):
        chars = {}
    return {"version": VERSION, "characters": chars}


def save(proj, data: dict) -> Path:
    p = path_of(proj)
    out = {"version": VERSION, "characters": data.get("characters") or {}}
    _atomic_write_json(p, out)
    return p


def variants_of(data: dict, name: str) -> list[dict]:
    ent = (data.get("characters") or {}).get(name)
    if not isinstance(ent, dict):
        return []
    vs = ent.get("variants")
    return [v for v in vs if isinstance(v, dict)] if isinstance(vs, list) else []


def identity_of(data: dict, name: str) -> str:
    ent = (data.get("characters") or {}).get(name)
    if not isinstance(ent, dict):
        return ""
    return str(ent.get("identity") or "").strip()


def resolve(data: dict, name: str, shot_costume: dict | None = None) -> dict | None:
    """
    解析某角色在某镜应穿的服装变体。

    返回 None = 该项目对这个角色没有服装配置（调用方走原来的"图锁一切"路径）。
    """
    vs = variants_of(data, name)
    if not vs:
        return None
    want = str((shot_costume or {}).get(name) or "").strip() or DEFAULT_VARIANT
    for v in vs:
        if str(v.get("id") or "") == want:
            return v
    # 指定的变体不存在 → 回落默认，并保留原因供 UI 提示
    for v in vs:
        if str(v.get("id") or "") == DEFAULT_VARIANT:
            return v
    return vs[0]


def is_default(variant: dict | None) -> bool:
    return variant is None or str(variant.get("id") or DEFAULT_VARIANT) == DEFAULT_VARIANT


def ensure_from_cards(proj, cards: dict) -> dict:
    """
    首次使用时从角色卡引导出 costumes.json（幂等，已存在就不动）。

    每张卡只产生一个 `default` 变体，且 identity/costume 按服装句拆开。
    这样"没有多套服装"的项目与从前行为完全一致，而需要换装时只需在默认变体旁再加一个。
    """
    data = load(proj)
    chars = data["characters"]
    changed = False
    for name, card in (cards or {}).items():
        if name in chars:
            continue
        appearance = getattr(card, "appearance", "") or ""
        costume = getattr(card, "costume", "") or ""
        identity, from_appearance = split_identity_costume(appearance)
        clothing = costume.strip() or from_appearance
        chars[name] = {
            "identity": identity,
            "variants": [
                {
                    "id": DEFAULT_VARIANT,
                    "label": "默认",
                    "prompt": clothing,
                }
            ],
        }
        changed = True
    if changed:
        save(proj, data)
    return data


# ---------------------------------------------------------------- 提示词重建（P2 补 R3.2）
#
# 为什么需要：`shot_fingerprint` 只含 prompt/chars/refs/渲染参数/frames/seed。
# **只改 `shot.costume` 字段不会动 prompt → 指纹不变 → 状态仍是 current → 不会重渲 →
# 用户以为换了衣服、其实永远不生效。**（shot-edit 在 P1 发现的同类静默失效）
# 所以字段变更时必须**确定性重建**提示词，不做 LLM 调用。

_SECTION_RE = re.compile(r"^([a-z_]+): (.*?)(?=\n[a-z_]+: |\Z)", re.S | re.M)


def parse_sections(prompt: str) -> dict[str, str]:
    """
    把六段式提示词拆成 {段名: 内容}。

    注意 `_assemble_prompt` 会把 CAMERA/POSITION DISCIPLINE 两行放在
    `retention_analysis` 段尾，所以它们会一起被解析进 retention_analysis —— 这正是我们要的
    （重建时 `_assemble_prompt` 会按 camera 重新生成这两行）。
    """
    return {
        m.group(1): m.group(2).strip()
        for m in _SECTION_RE.finditer(prompt or "")
    }


def rebuild_prompt(shot, cards: dict, data: dict) -> str:
    """
    按 `shot.costume` 确定性重建整条提示词（复用 plan._assemble_prompt，不调 LLM）。

    只影响 `subject_definitions` 与 `retention_analysis` 两段：
      - 服装句取自该变体的 `prompt`
      - 默认变体 → 严格措辞；非默认变体 → 放宽措辞（"CLOTHING is authoritative …"）
    其余四段（summary/detailed_description/overall_soundscape/non_diegetic_music）原样保留。
    """
    from . import plan as P  # 局部导入，避免循环依赖

    secs = parse_sections(shot.prompt)
    missing = [k for k in ("detailed_description", "overall_soundscape") if k not in secs]
    if missing:
        raise ValueError(
            f"镜头 {getattr(shot, 'id', '?')} 的提示词缺少 {missing}，无法重建（请先补全或重写提示词）"
        )
    return P._assemble_prompt(
        chars=list(shot.chars or []),
        cards=cards or {},
        camera=str(getattr(shot, "camera", "") or ""),
        detailed_description=secs.get("detailed_description", ""),
        overall_soundscape=secs.get("overall_soundscape", ""),
        non_diegetic_music=secs.get("non_diegetic_music", ""),
        costumes=data,
        shot_costume=dict(getattr(shot, "costume", None) or {}),
    )
