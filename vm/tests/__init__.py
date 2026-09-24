"""
vm/tests —— 后台核心的回归门禁（v0.2 测试补位，对应计划 S5/S14）。

跑法（仓库根目录，零第三方依赖；无 GPU、无 ComfyUI 环境也能跑）：

```bash
python3 -m unittest discover -s vm/tests -q
```

设计原则（与 vm/audit/tests 一致：锁行为不锁写法）：
  · 每条断言的是「给定输入 → 判定结果」，不是"某个函数被调用过"；
  · 夹具**完全自足**：一切数据在 tempfile 里现造，绝不读 projects/ 下的真实项目
    （vm/qi_test.py 依赖 projects/西游记 的真实数据，是反面教材 ——
     换台机器/重跑一次 plan 就红，红得还没有信息量）；
  · 涉及进程/服务的用例只用 fake_sleep 替身与临时 HTTP 服务（随机端口），
    不起 GPU、不连 ComfyUI、不占用户的 8801 端口。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# 仓库根（video_magic/）：给需要跑 pipeline.py 子进程的用例定位真代码
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# 合成镜头用的六段式提示词（形状与 vm/shots.py 的 SIX_SECTIONS 一致）。
# 为什么自己造而不是用真实项目的镜头：测试的期望值必须和输入一起摆在同一屏里，
# 看测试文件就能复算，不用去翻项目数据。
DEFAULT_PROMPT = (
    "subject_definitions: <Subject 1> = a young monk.\n"
    "summary: [reference generation] single shot of <Subject 1>.\n"
    "retention_analysis: <Subject 1>: fully_preserved.\n"
    "detailed_description: A winding mountain road at dusk, the wind moving the cloth of the robe.\n"
    "overall_soundscape: wind in the pines.\n"
    "non_diegetic_music: low strings.\n"
)


def make_shot(i: int, **over: Any) -> dict:
    """造一镜（形状与 vm.shots.load_shots 读的原始 JSON 一致）。"""
    shot: dict = {
        "id": f"1-{i:02d}-01",
        "sec": 5,
        "chars": ["唐僧"],
        "seed": 3000 + i,
        "prompt": DEFAULT_PROMPT,
    }
    shot.update(over)
    return shot


def make_project(root: Path, name: str = "p1", *, shots: list[dict] | None = None,
                 budget: dict | None = None) -> Path:
    """
    在 tmp 的 projects 根下造一个最小可测项目。

    root      —— projects 根目录（相当于线上那个 projects/）
    shots     —— 镜头 dict 列表；默认 3 镜，写进 shots/chapter01.json
    budget    —— 给 project.json 的 budget 段（None = 不写 project.json，
                 即"未配置护栏"的向后兼容形态）
    返回项目目录。
    """
    pdir = Path(root) / name
    for sub in ("novel", "shots", "refs", "prompts", "clips", "final", "state"):
        (pdir / sub).mkdir(parents=True, exist_ok=True)
    if shots is None:
        shots = [make_shot(1), make_shot(2, chars=["孙悟空"]), make_shot(3)]
    (pdir / "shots" / "chapter01.json").write_text(
        json.dumps(shots, ensure_ascii=False, indent=2), encoding="utf-8")
    if budget is not None:
        (pdir / "project.json").write_text(
            json.dumps({"budget": budget}, ensure_ascii=False, indent=2), encoding="utf-8")
    return pdir
