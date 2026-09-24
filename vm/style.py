"""
style.py —— 画面风格预设（realistic / anime / auto）。

## 为什么单独一个模块
"风格"这件事**散落在 6 个地方**，各改各的必然不一致：
  1. `_STYLE_SYSTEM`      —— 自动推导风格句（`style: "auto"` 时用）
  2. `DEFAULT_STYLE`      —— 推导失败时的兜底风格句
  3. `_CARD_SYSTEM`       —— 抽角色卡（外貌/服装描述）
  4. `_CARD_ONE_SYSTEM`   —— 给单个角色写卡
  5. `_SCENE_SYSTEM`      —— 抽场景实体
  6. `_PROP_SYSTEM`       —— 抽道具实体
  7. `DEFAULT_PORTRAIT_BACKGROUND` —— 定妆照背景

只在 1 处注入风格句是不够的：**角色卡如果仍按写实写（"皮肤毛孔/真实布料纹理"），
二次元的风格句会被卡片的写实词汇对冲掉**。所以预设必须同时给"风格句"和"写卡指导"。

## 用哪个预设
`project.json` 里：
  · `"style_preset": "anime"`     → 日式二维动画（2026-09-24 用户要求）
  · `"style_preset": "realistic"` → 写实电影感（之前的默认）
  · 不写 / `"auto"`                → 按题材自动推（仍受预设的"指导语"约束）

⚠️ 与 `_STYLE_ANCHOR_BAN` 的关系：那个黑名单只禁**否定式**锚
（`not photorealistic` / `not japanese anime` / `stylized illustration`）——
当年是为了压"廉价 3D 卡通"观感。
**肯定式**的风格句（"Japanese 2D anime style, cel-shaded…"）**不在黑名单里**，
所以二次元预设可以正常生效，不需要放宽那个守卫。
"""

from __future__ import annotations

# 否定式风格锚黑名单 —— 与 plan.py 的 _STYLE_ANCHOR_BAN 同源（保留在那里，这里只说明）
NEGATIVE_ANCHORS_NOTE = (
    "禁的是‘not photorealistic’这类**否定式**锚（实测会把画面拉成廉价 3D 卡通）；"
    "肯定式风格句不受影响。"
)


PRESETS: dict[str, dict[str, str]] = {
    # ── 半写实 3D CG / 游戏建模风（2026-09-24 用户给定参考图）────────────────
    # ⚠️ 和 `anime` 不是一回事，别混：
    #   anime = **二维**动画：平涂赛璐璐、清晰线稿、硬边双色阴影
    #   cg    = **三维**渲染：真实皮肤质感 + 理想化五官、PBR 材质、电影景深与辉光
    # 用户给的参考图（月下冰晶头饰女子）本身就带「千问AI生成」水印 ——
    # 说明这套词汇在**本机模型**上确实能兑现，不是照抄别家的风格名。
    "cg": {
        "sentence": (
            "Semi-realistic 3D CG render, high-end xianxia game cinematic, "
            "physically-based materials, realistic subsurface-scattered skin with "
            "idealised features, cinematic depth of field with soft bloom and rim light"
        ),
        "guidance": (
            "整部片是**半写实 3D CG（游戏过场动画质感）**：\n"
            "  · **不是二维动画**：不要平涂色块、不要粗线稿、不要赛璐璐硬边阴影。\n"
            "  · 皮肤是真实的次表面散射质感（通透、有血色、高光柔和），但**五官理想化**"
            "（大而清澈的眼睛、细腻鼻梁、饱满唇形），不是普通人脸。\n"
            "  · **材质要写足**：水晶/冰晶的通透与折射、丝绸的层叠与织纹、金属錾刻的"
            "高光、发丝的分缕与空气感。这是这个风格最贵的部分，描述里必须体现。\n"
            "  · **光**：影视级布光 —— 冷暖对撞（冷月蓝主光 + 暖色轮廓光）、体积光、"
            "柔和辉光（bloom）、浅景深虚化背景、空气中的浮尘/粒子。\n"
            "  · 构图偏**半身/特写**，突出人物精致度与服饰细节。"
        ),
        "examples": (
            "    古装神话 → \"Semi-realistic 3D CG render, high-end xianxia game cinematic, "
            "physically-based materials, realistic skin with idealised features, "
            "cool moonlight key with warm rim light, soft bloom and shallow depth of field.\"\n"
            "    现代都市悬疑 → \"Semi-realistic 3D CG render, grounded cinematic realism with "
            "stylised idealised features, wet reflective surfaces, practical neon light, "
            "volumetric haze, shallow depth of field.\"\n"
            "    科幻 → \"Semi-realistic 3D CG render, hard-surface sci-fi with PBR materials, "
            "cold blue-white key light, anamorphic flares, holographic glow, volumetric fog.\"\n"
            "    日常治愈 → \"Semi-realistic 3D CG render, soft warm daylight, gentle bloom, "
            "clean PBR materials, idealised features, shallow depth of field.\""
        ),
        "portrait_background": (
            "Background: a simple neutral grey studio backdrop with soft cinematic lighting, "
            "slight depth of field."
        ),
        # ★ 只有这个字段可以进**图像模型**的提示词。
        #   `guidance` 是写给 LLM 看的中文指导语，**绝不能拼进图像提示词** ——
        #   我犯过这个错：把「整部片是**半写实 3D CG（…）**：」塞进了英文提示词，
        #   中文 + markdown 星号只会稀释提示词，出图又糊又假。
        #   这里全是**具体到可执行**的英文：对比度、材质、皮肤、镜头、氛围。
        "image_suffix": (
            "Semi-realistic 3D CG character render, high-end game cinematic quality, "
            "physically-based rendering, dramatic chiaroscuro lighting with deep shadows "
            "and bright specular highlights, strong rim light separating the figure from the "
            "background, translucent materials with realistic refraction and faceted sparkle, "
            "fine metal filigree catching the light, layered fabric with visible weave and "
            "embroidery, silky individual hair strands with flyaways, porcelain skin with "
            "subtle subsurface scattering and defined bone structure, volumetric light beams, "
            "floating dust motes and bokeh particles, deep atmospheric background with "
            "shallow depth of field, shot on 85mm f/1.4, high detail, sharp focus on the face"
        ),
    },
    # ── 日式二维动画（用户明确要求的"二次元"）───────────────────────────────
    "anime": {
        "sentence": (
            "Japanese 2D anime style, cel-shaded flat colouring with clean dark line art, "
            "vibrant saturated palette, crisp two-tone shadows, stylised expressive faces"
        ),
        "guidance": (
            "整部片是**日式二维动画（anime）**：平涂赛璐璐上色、清晰线稿、饱和明快的色彩、"
            "硬边双色阴影、程式化的五官与发型。\n"
            "  · 写任何描述时用**动画设定稿**的语言（线稿感、色块、高光形状、发型剪影），"
            "**不要**写皮肤毛孔、真实布料纹理、胶片颗粒、镜头眩光这类写实词汇。\n"
            "  · 人物用 anime 五官比例（大眼睛、简化鼻口、清晰发块），不要真人脸。\n"
            "  · 光线用**动画式**处理（明确的明暗分界、色块化阴影），不是自然光摄影。"
        ),
        # 给 _STYLE_SYSTEM 的例子（按题材给 anime 版本）
        "examples": (
            "    古装神话 → \"Japanese 2D anime style, cel-shaded colouring with clean line art, "
            "warm earthy palette with saturated accent colours, dramatic two-tone shadows.\"\n"
            "    现代都市悬疑 → \"Gritty 2D anime style, cel-shaded with heavy line art, "
            "cold cyan-grey palette, hard-edged shadows, rain-slicked night scenes.\"\n"
            "    科幻 → \"Clean 2D anime style, cel-shaded, high-contrast blue-white lighting, "
            "crisp mechanical line art, saturated neon accents.\"\n"
            "    日常治愈 → \"Soft 2D anime style, cel-shaded pastel palette, gentle line art, "
            "warm low-contrast lighting, simple flat backgrounds.\""
        ),
        "portrait_background": (
            "Background: a simple flat neutral grey backdrop, cel-shaded, minimal detail."
        ),
        "image_suffix": (
            "Japanese 2D anime key visual, cel-shaded flat colouring with clean confident "
            "line art, crisp two-tone hard-edged shadows, vibrant saturated palette, "
            "stylised anime facial features with large expressive eyes and simplified nose "
            "and mouth, clear hair clumps with sharp silhouettes, painted background, "
            "high detail, sharp focus"
        ),
    },
    # ── 写实电影感（2026-09-24 之前的默认）──────────────────────────────────
    "realistic": {
        "sentence": (
            "Cinematic realism with natural film lighting, muted desaturated earthy tones "
            "and subtle film grain"
        ),
        "guidance": (
            "整部片是**写实电影感**：自然光、柔和层次、真实材质纹理、轻微胶片颗粒。\n"
            "  · 人物用真人比例与真实皮肤质感。\n"
            "  · **不要写风格锚**：`not photorealistic` / `not a real person` / "
            "`stylized illustration` / `not japanese anime` 这类词一律禁止 —— "
            "本机实测会把画面拉成廉价 3D 卡通。"
        ),
        "examples": (
            "    古装神话 → \"Cinematic realism with natural film lighting, muted desaturated "
            "earthy tones and subtle film grain.\"\n"
            "    现代都市悬疑 → \"Gritty cinematic realism, cool cyan-grey palette, "
            "high-contrast practical lighting, wet reflective surfaces, subtle 35mm grain.\"\n"
            "    科幻 → \"Clean hard sci-fi look, cold blue-white key light, high dynamic range, "
            "anamorphic flares, fine digital grain.\"\n"
            "    日常治愈 → \"Soft natural daylight, warm pastel palette, gentle low-contrast "
            "lighting, shallow depth of field.\""
        ),
        "portrait_background": (
            "Background: a plain neutral grey studio backdrop with soft even lighting."
        ),
        "image_suffix": (
            "Highly polished commercial photograph, natural perspective, "
            "soft directional key light with clear direction and low-to-moderate contrast, "
            "realistic skin texture with visible pores and fine detail, natural fabric weave "
            "with believable folds and drape, delicate specular highlights on metal and glass, "
            "warm natural colour grading, moderate depth of field with the subject separating "
            "cleanly from the background, high resolution, sharp focus on the eyes"
        ),
    },
}


# ── 负向词（2026-09-24）────────────────────────────────────────────────────
# 用户的反馈里带了 negative prompt，而我们**完全没有这个概念** ——
# `qi.build()` 早就有 `negative` 参数，但从没被接上过。
#
# 负向词是"不要让 ai 猜"的另一半：**前向说清要什么，负向说清不要什么**。
# 下面这几条正是"塑料感"的直接来源，实测（用户手写对照）压掉它们后质感差异明显。
NEGATIVE_COMMON = (
    "plastic skin, over-smoothed skin, waxy skin, doll-like face, "
    "malformed hands, extra fingers, extra limbs, distorted feet, fused fingers, "
    "harsh flash, flat lighting, cluttered background, "
    "readable text, watermark, signature, logo, jpeg artifacts, low resolution"
)

# 各预设额外要压的
NEGATIVE_BY_PRESET = {
    "realistic": "anime, illustration, cel shading, 3d render, cartoon, painting",
    "cg": "flat cel shading, thick outline, 2d anime, cartoon, low-poly, clumpy hair",
    "anime": "photorealistic, 3d render, live action photo, realistic skin texture, cgi",
}


def negative_for(name: object) -> str:
    """该预设的负向词。项目可用 `project.json: negative_prompt` 覆盖/追加。"""
    pre = resolve_preset(name)
    extra = NEGATIVE_BY_PRESET.get(pre, "")
    return (NEGATIVE_COMMON + (", " + extra if extra else "")).strip()


DEFAULT_PRESET = "realistic"


def resolve_preset(value: object) -> str:
    """把各种写法归一到预设名。未知值退回默认（不抛错 —— 风格是软配置）。"""
    v = str(value or "").strip().lower()
    if v in ("anime", "2d", "二次元", "日式", "日漫", "动画"):
        return "anime"
    if v in ("cg", "3d", "3dcg", "建模", "半写实", "游戏", "游戏建模", "cg建模", "国风"):
        return "cg"
    if v in ("realistic", "photo", "photoreal", "写实", "电影感", "cinematic"):
        return "realistic"
    return DEFAULT_PRESET


def get(name: object) -> dict[str, str]:
    return PRESETS[resolve_preset(name)]


def image_suffix(name: object) -> str:
    """
    **可以进图像模型**的风格后缀（纯英文，具体到可执行）。

    与 `guidance()` 的区别是硬性的：`guidance` 是中文、给写提示词的 LLM 看；
    把它拼进图像提示词会**稀释**提示词（我踩过：中文 + markdown 星号进了英文提示词，出图又糊又假）。
    """
    return get(name).get("image_suffix", "")


def guidance(name: object) -> str:
    """
    给各 system 提示词追加的风格指导块。

    这是"一处定义、六处生效"的关键：抽角色卡 / 抽场景 / 抽道具 / 推风格句
    都会带上它，否则各步产物之间风格会互相打架。
    """
    p = get(name)
    return (
        f"\n\n【画面风格（整部片统一，必须严格遵守）】\n"
        f"统一风格句：{p['sentence']}\n"
        f"{p['guidance']}\n"
    )
