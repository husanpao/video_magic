"""
imgfp.py —— **图像产物的指纹**（P2.4）：让"改了风格"这件事对图生效。

## 为什么镜头有指纹、图没有

镜头侧早就有 `state.shot_fingerprint()`：改了任何影响画面的输入，只有相关镜头变 stale。
但**定妆照与概念图没有这套机制**，实锤的两个症状：

1. `chars.gen_char()` 的跳过条件是 `if ref.exists() and not force` ——
   **只看文件在不在**，不看它是按什么参数出的。于是换风格后重跑「定妆」，
   它一个字都没重出，日志还写着「⏭ 跳过（已存在）」。
2. `gen_candidates()` / `assets.gen_candidates()` 按 **seed** 跳过已有候选 ——
   比 (1) 更隐蔽：换风格后那些"旧风格的候选"仍然躺在目录里当"已抽好的图"，
   用户点采纳就把旧风格采纳成了新风格。

叠上 README 的实测局限「画风由参考图决定、文字控不住」，
结论是：**换风格唯一有效的通路就是重出这些图**，
而它当时既不自动、也不提示 —— 静默新旧混存。

## 设计：把"影响这张图长什么样的输入"哈希起来

指纹存在图旁边的 `*.fp.json` 里（不存进 png 元数据：png 由 ComfyUI 产出，
写元数据等于改产物，而契约禁止动 ComfyUI output 的东西）。

**故意不算 GPU 成本**：指纹不匹配时**不自动重出**，只标 `stale`。
因为"重出 12 个角色的定妆照"是真金白银的 GPU 时间，
必须让用户点一下（复用配置中心已有的「危险项影响清单」交互）。
`gen_char`（单张、用户显式点的阶段）例外：它本来就是用户主动要出的，
指纹不匹配就直接重出并说明原因。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

FP_SUFFIX = ".fp.json"


def fingerprint(kind: str, prompt: str, negative: str, render: dict, seed: int,
                extra: dict | None = None) -> str:
    """
    一张图的指纹。

    输入项与 `state.shot_fingerprint` 同思路：**凡是会改变画面输出的都要进来**。
    `prompt` 必须是**已经拼好风格后缀的最终文本** —— 所以调用方一律传
    `style.apply_image_suffix(...)` 的结果，后缀自然就进了指纹。
    """
    parts = [
        "v1",
        f"kind={kind}",
        "prompt=" + (prompt or ""),
        "negative=" + (negative or ""),
        f"seed={seed}",
    ]
    for k in sorted(render or {}):
        parts.append(f"{k}={render[k]}")
    for k, v in sorted((extra or {}).items()):
        parts.append(f"x:{k}={v}")
    return hashlib.md5("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def fp_path(img: Path) -> Path:
    return Path(str(img) + FP_SUFFIX)


def write(img: Path, fp: str, **meta: Any) -> Path:
    """记录某张图的指纹。写在图旁边，**不改图本身的字节**。"""
    p = fp_path(img)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"fp": fp, "image": Path(img).name}
    payload.update({k: v for k, v in meta.items() if v is not None})
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def read(img: Path) -> dict:
    p = fp_path(img)
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return d if isinstance(d, dict) else {}


def current(img: Path) -> str:
    return str(read(img).get("fp") or "")


def is_stale(img: Path, fp: str) -> bool:
    """
    没有指纹记录的图**不算 stale**。

    理由：老项目的产物、以及用户上传的自有图（`import_external_image`）都没有指纹，
    把它们判成 stale 会让"改风格"变成"全部重抽"，
    而用户上传的图**根本不该被系统重抽**。
    判 stale 只对"系统自己出的、且记了指纹的"图生效。
    """
    have = current(img)
    return bool(have) and have != fp


def drop(img: Path) -> None:
    """图被删/被覆盖时连带清掉指纹，避免留下指向不存在文件的孤儿记录。"""
    try:
        fp_path(img).unlink()
    except OSError:
        pass
