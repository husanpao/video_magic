"""
`vm.audit` 的单测包。

跑法（仓库根目录，零第三方依赖）：

```bash
python3 -m unittest discover -s vm/audit/tests -t . -v
```

设计原则（对齐 `NEXT-ACTIONS.md` #81「锁写法不锁行为，等于没锁」）：
每条测试断言的是**给定输入 → 判定结果**，不是"某个函数被调用过"。
"""

from __future__ import annotations

from typing import Any

DEFAULT_PROMPT = (
    "subject_definitions: <Subject 1> = a person.\n"
    "summary: [reference generation] single shot.\n"
    "detailed_description: Cinematic realism with natural film lighting. "
    "Medium shot on a mountain path at dusk, <Subject 1> stands and looks ahead, "
    "the wind moving the cloth of the robe."
)


def make_shot(
    i: int,
    *,
    sec: Any = 5,
    chars: list[str] | None = None,
    size: str = "中景",
    dialogue: Any = "",
    narration: str = "",
    prompt: str = DEFAULT_PROMPT,
    **extra: Any,
) -> dict:
    """造一镜（形状与 `vm.shots.load_shots` 的原始 JSON 一致：dict）。"""
    shot: dict = {
        "id": f"1-{i}-01",
        "sec": sec,
        "chars": list(chars if chars is not None else ["唐僧"]),
        "seed": 3000 + i,
        "prompt": prompt,
        "shot_size": size,
    }
    if dialogue:
        shot["dialogue"] = dialogue
    if narration:
        shot["narration"] = narration
    shot.update(extra)
    return shot
