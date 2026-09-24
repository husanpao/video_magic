"""
plan.py —— 小说章节 → DeepSeek 拆镜 → 镜头表 + 角色卡 + 逐镜六段式 H3 提示词。

这是整条流水线唯一"有创作性"的阶段，也是质量最不稳定的一环，所以设计上把
**能确定的都交给代码，只把必须创作的部分交给 LLM**：

  阶段⓪ 角色卡（每章 1 次 LLM 调用，仅 allow_new_characters 时）
      正文 → 全章需要建卡的角色（name/appearance/costume）→ prompts/char_<名>.txt。
      独立成一步而不是搭拆镜表的便车：实测让拆镜 LLM 顺手建卡时，它会把没有卡的
      配角当成画外音/剪影糊掉（沙僧的台词被整句丢掉），拆镜质量因此被工具限制扭曲。

  阶段① 拆镜表（每章 1~N 次 LLM 调用）
      小说正文 → 镜头列表：场次/景别/运镜/登场角色/台词/旁白/时长/可渲染画面句。
      不含提示词正文，输出小、不易被 max_tokens 截断；长章节按段落分块，块间场次号累进。
      校验含"正文对白零丢失"（29 句全比对，去标点后拼串比对，支持长句拆到连续几镜）。

  阶段② 逐镜六段式正文（每镜 1 次 LLM 调用）
      单镜行 → detailed_description / overall_soundscape / non_diegetic_music。
      单镜失败只重写这一镜，不会拖垮整章。

  阶段③ 六段式装配（纯代码，无 LLM）
      subject_definitions / summary / retention_analysis 由**角色卡与代码模板**生成，
      不经过 LLM —— 这是本机 A/B 实测逼出来的硬要求：

        ★ 提示词里的文字外貌会**压过**参考图。参考图是男性、文字写女性 → 输出女性。
          所以 subject_definitions 的外貌必须严格来自角色卡（= 定妆图的生成原文），
          且同一角色在所有镜头里**逐字一致**。让 LLM 每次"复述"外貌必然漂移，
          因此这段只能由代码拼接。

      同时按 NiliX 实测把 CAMERA DISCIPLINE / POSITION DISCIPLINE 放在
      `detailed_description:` 段标题紧前面（段尾纪律服从度弱，实测无效）。

  阶段④ 校验
      JSON/schema 校验 + 台词逐字入 <d> + <Subject N> 引用完整 + 语音时长预算
      （4 字/秒，超了自动延长，clamp 15s）。不合格按其意见重写 1 轮，仍不合格报错。

Key 解析顺序（CONTRACTS.md）：① 环境变量 DEEPSEEK_API_KEY ② ~/.config/video_magic/deepseek_key
③ 都没有 → 显式报错。Key 绝不落到项目文件里。

只依赖标准库（urllib）。所有落盘原子写。
"""

from __future__ import annotations

import json
import math
import os
import re

from . import style as _style
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

try:  # 作为包导入（pipeline.py / 测试）
    from vm.shots import (
        CHARS_PER_SEC,
        FL2VA_MAIN,
        MAX_CHARS_PER_SHOT,
        SEC_MAX,
        SEC_MIN,
        TARGET_CHARS_PER_SHOT,
        Shot,
        fit_sec_to_speech,
        save_shots,
        speech_chars,
        validate_shots,
    )
    from vm import costumes as costsys
    from vm.state import Project
except ImportError:  # 直接 `python vm/plan.py` 时的回退
    from shots import (  # type: ignore
        CHARS_PER_SEC,
        FL2VA_MAIN,
        MAX_CHARS_PER_SHOT,
        SEC_MAX,
        SEC_MIN,
        TARGET_CHARS_PER_SHOT,
        Shot,
        fit_sec_to_speech,
        save_shots,
        speech_chars,
        validate_shots,
    )
    import costumes as costsys  # type: ignore
    from state import Project  # type: ignore

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
KEY_FILE = Path("~/.config/video_magic/deepseek_key")

MAX_TOKENS = 8192  # deepseek-chat 上限 8192；拆镜表分块输出，单次不会顶到
LLM_TIMEOUT = 300
MAX_ATTEMPTS = 3  # 瞬时失败（429/5xx/网络）指数退避重试，别让一次抖动打断整章
DEFAULT_TEMPERATURE = 0.3

# 单句台词上限（字）。字幕一行放不下更长的句子，且 4 字/秒下 20 字 ≈ 5 秒，
# 正好是一镜的合理语速。超过它的对白在分镜阶段就应拆到连续两镜（原文不删字）。
DIALOGUE_MAX = 20
# 单镜旁白上限（字）。旁白本来就该少（每章 ≤3 句），合并镜头时不能把旁白堆起来。
NARRATION_MAX = 15

# 全局风格句：没有角色卡可参考时的兜底。
# **不要写风格锚**（semi-realistic stylized illustration / not photorealistic /
# not Japanese anime 之类）——Lead 本机实测：这类词会把画面拉成廉价 3D 卡通。
# 写实的朴实描述 + 明确景别与光线才是本机跑得好的写法。
DEFAULT_STYLE = (
    "Cinematic realism with natural film lighting, muted desaturated earthy tones "
    "and subtle film grain"
)
# 旧素材里可能残留的风格锚：抽到也要丢掉，不能把廉价观感带进逐镜提示词
# ── 提示词详细度纪律（2026-09-24）──────────────────────────────────────────
# 用户的反馈原话：「你生成提示词啥的需要细致一点。**不要让 ai 猜**」
#
# 为什么这条是硬要求而不是风格偏好：图像模型在**没被交代的维度上会取训练集的众数**，
# 众数就是"平均脸 + 平光 + 糊背景"—— 看起来又假又丑。
# 实测对照：用户手写 2061 字符、逐项交代（门框在左/人物偏右/水滴镂空/透明鞋跟/光从左上来/
# 地板有倒影）→ 商业级精修质感；我写的 ~200 字符抽象形容词 → 塑料娃娃。
#
# 差距不在风格词，在**信息密度**。所以把"必须交代的维度"列成清单，逐项要求。
DETAIL_CHECKLIST = """
【详细度纪律 —— ★最重要的一条】
**不要让模型猜。** 凡是画面里存在的维度，都必须**明确写出来**；没写的维度模型会取
训练集的"平均值"，而平均值就是**平光、糊背景、塑料脸**。

必须逐项交代（缺一项就等于把那一项交给运气）：
  1. 姿态与动作：具体到手脚位置、身体朝向、重心（不要只写"站着"）
  2. 构图与画幅：竖幅/横幅、人物在画面中的位置（中央偏左/偏右）、切到哪（全身/七分身/胸口以上）
  3. 服装形制：领型、袖型、腰线、裙长、面料（缎面/纱/棉麻）、是否贴身、褶皱与垂坠
  4. 配饰与鞋：耳饰/项链/手镯/发饰的形制与材质；鞋的样式与鞋跟；正文没写的按角色身份补合理项
  5. 光位与光比：光从哪个方向来（左上/正前/背光）、硬光还是柔光、光比高低、投影落在哪
  6. 色调：以哪几个颜色为主（给出 3-5 个具体色名）
  7. 环境细节：背景有什么、地面有没有反射、有没有纵深/景深
  8. 材质质感：皮肤、织物、金属、玻璃各自的表面特性（写"通透折射"而不是"好看"）

**反例（要避免）**：「a beautiful woman standing in a room, soft lighting, cinematic」
—— 这句话把 8 项全交给了模型去猜，出来必然是平均脸 + 平光 + 糊背景。
**正例**：见上面的 8 项，每一项都有具体值。

**每写一个形容词，问自己：能不能换成可执行的名词？**
  · "beautiful" → 换成具体特征（oval face, defined jawline, clear almond eyes）
  · "nice dress" → 换成形制与面料（sleeveless satin dress with high neck and teardrop cutout）
  · "good lighting" → 换成方向与性质（soft warm key light from upper-left front, low-to-moderate contrast）
"""


# ── 风格锚黑名单（2026-09-24 大幅放宽）────────────────────────────────────
# 历史：这条规则是我 2026-09 加的，理由是"否定式风格锚（not photorealistic 之类）
# 会把画面拉成廉价 3D 卡通"。**后来证明是误诊。**
# 反证：用户手写的正向提示词结尾就是同一类构造 ——
#   "Clearly maintain realistic indoor fashion photography rather than anime,
#    illustration, or 3D rendering." —— 出图是商业级精修质感。
# 真正的原因是**当时的提示词太稀**（~200 字符的抽象形容词），不是风格锚。
# 我却在错的地方下了一条禁令，把正确的做法也一起封了。
#
# 现在只保留**极少数确定有害**的词（这些会让模型去"否认"一个它本来就会画的东西，
# 反而把该特征画出来 —— 即 Rebound 效应）：
_STYLE_ANCHOR_BAN = re.compile(
    r"(?i)^\s*(no|not|without)\s+(photorealistic|realistic|anime|illustration)"  # 单独的否定开场
)
DEFAULT_PORTRAIT_BACKGROUND = (
    "Background: a plain neutral grey studio backdrop with soft even lighting. "
    "Muted desaturated earthy tones, slight film grain. "
    "Camera slowly pushes in. Ambient audio: quiet room tone, no dialogue."
)

# 拆镜表分块：一次请求塞太多正文会让输出被 max_tokens 截断（表现为 JSON 半截）。
CHUNK_CHARS = 2600


class PlanError(RuntimeError):
    """拆镜阶段的显式失败：带人话原因，不静默降级。"""


# ── 日志适配 ────────────────────────────────────────────────────────────────


def _log(log: Any, msg: str) -> None:
    """
    适配未知形状的 log 参数（契约里只写了 `log`）。

    pipeline/webui 可能传函数、logging.Logger，或带 .log()/.write() 的对象。
    不支持时用 print 兜底 —— 日志宁可冗余，也不能因为形状不对就抛异常中断拆镜。
    """
    if log is None:
        return
    if callable(log):
        log(msg)
        return
    for attr in ("log", "info", "write"):
        fn = getattr(log, attr, None)
        if callable(fn):
            fn(msg)
            return


# ── Key 与 LLM 客户端 ───────────────────────────────────────────────────────


def api_key() -> str:
    """
    按 CONTRACTS.md 的解析顺序取 Key：环境变量 → 配置文件 → 显式报错。

    绝不静默降级（不返回空串、不跳过 LLM）——那会把"拆镜失败"伪装成"没有镜头"。
    """
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if key:
        return key
    path = KEY_FILE.expanduser()
    if path.is_file():
        try:
            key = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise PlanError(f"读不到 DeepSeek Key 文件 {path}：{e}") from e
    if not key:
        raise PlanError(
            "缺少 DeepSeek API Key。请设置环境变量 DEEPSEEK_API_KEY，"
            f"或把 Key 写入 {path}（600 权限）。Key 不要写进项目文件。"
        )
    return key


def _extract_json(text: str) -> dict:
    """三层容错：整体解析 → 剥 ```json 围栏 → 首 { 到末 } 截取（与 NiliX 同策略）。"""
    t = (text or "").strip()
    if not t:
        raise PlanError("LLM 返回空内容（deepseek-chat 偶发，可重试或精简 system 提示词）")
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    i = t.find("```")
    if i >= 0:
        j = t.find("```", i + 3)
        seg = (t[i + 3 : j] if j >= 0 else t[i + 3 :]).strip()
        if seg.lower().startswith("json"):
            seg = seg[4:].strip()
        try:
            obj = json.loads(seg)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    a, b = t.find("{"), t.rfind("}")
    if a >= 0 and b > a:
        try:
            obj = json.loads(t[a : b + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError as e:
            raise PlanError(f"LLM 输出不是合法 JSON：{e}（前 200 字：{t[:200]!r}）") from e
    raise PlanError(f"LLM 输出不是 JSON 对象（前 200 字：{t[:200]!r}）")


def _llm_json(
    cfg: dict,
    system: str,
    user: str,
    *,
    temperature: float = DEFAULT_TEMPERATURE,
    log: Any = None,
    tag: str = "",
) -> tuple[dict, dict]:
    """
    调 DeepSeek /chat/completions 并返回 (JSON对象, meta)。

    meta 含 model/usage/finish_reason —— plan 的 stats 要把 token 用量交给 UI，
    用户有权知道这一次拆镜花了多少 token。
    """
    llm = cfg.get("llm") if isinstance(cfg.get("llm"), dict) else {}
    base = str(llm.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    model = str(llm.get("model") or DEFAULT_MODEL)
    temperature = float(llm.get("temperature", temperature))
    max_tokens = int(llm.get("max_tokens") or MAX_TOKENS)

    body = json.dumps(
        {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "stream": False,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    url = base + "/chat/completions"
    key = api_key()

    last_err: Any = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        # Body 只能读一次：每次重试必须重建 Request（NiliX 审计 P1 踩过：第二次发空 body）
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
                raw = resp.read(8 << 20)
            parsed = json.loads(raw.decode("utf-8"))
            choice = (parsed.get("choices") or [{}])[0]
            text = (choice.get("message") or {}).get("content") or ""
            finish = choice.get("finish_reason")
            if finish == "length":
                raise PlanError(
                    f"LLM 输出被 max_tokens={max_tokens} 截断（{tag}）。"
                    "请调大 max_tokens 或减小分块大小。"
                )
            obj = _extract_json(text)
            meta = {
                "model": model,
                "usage": parsed.get("usage") or {},
                "finish_reason": finish,
                "temperature": temperature,
            }
            return obj, meta
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read(2000).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - 读错误体失败不影响主流程
                pass
            if e.code == 429 or e.code >= 500:
                last_err = f"HTTP {e.code}: {detail[:200]}"
                _log(log, f"  ⚠️ DeepSeek {last_err}，{attempt * 2}s 后重试（{attempt}/{MAX_ATTEMPTS}）")
                time.sleep(attempt * 2)
                continue
            raise PlanError(f"DeepSeek HTTP {e.code}（{tag}）：{detail[:300]}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = repr(e)
            _log(log, f"  ⚠️ DeepSeek 网络错误 {last_err}，{attempt * 2}s 后重试（{attempt}/{MAX_ATTEMPTS}）")
            time.sleep(attempt * 2)
        except json.JSONDecodeError as e:
            raise PlanError(f"DeepSeek 响应不是 JSON（{tag}）：{e}") from e
    raise PlanError(f"DeepSeek 连续 {MAX_ATTEMPTS} 次请求失败（{tag}）：{last_err}")


# ── 角色卡 ──────────────────────────────────────────────────────────────────


@dataclass
class CharCard:
    """角色卡。appearance/costume 是英文外貌/服装描述，会被逐字写进 subject_definitions。"""

    name: str  # 中文名，如 "孙悟空"
    appearance: str  # 外貌描述（照参考图写）
    costume: str  # 服装
    portrait_prompt: str  # 定妆照用的 H3 单段式提示词（正面近景）


_SENT_RE = re.compile(r"(?<=[.!?])\s+")
# 只认**美术风格**词。别把 "Dim blue-grey evening light ..., slight film grain." 这类
# 光影句当成风格句——那会把夜景光照强加给黎明镜（实测踩过）。
_STYLE_HINT_RE = re.compile(
    r"(?i)(illustration|realism|realistic|photorealistic|painterly|anime|watercolo?ur|"
    r"cel[- ]?shad|concept art|comic|3d render|render style)"
)
_SUBJECT_RE = re.compile(
    r"(?i)^(?:a|an|the)?\s*(?:cinematic\s+)?(?:medium\s+)?(?:close-?up\s+)?portrait of\s+"
    r"(.+?)(?:,\s*standing|\s*\.|$)"
)


def parse_portrait_prompt(text: str) -> tuple[str, str, str]:
    """
    从单段式定妆提示词里拆出 (主体短语, 外貌+服装句, 风格尾句)。

    **逐字保留**外貌句 —— 这是"文字与参考图一致"的唯一保证：定妆图就是照着
    这段文字生成的，拆镜时再原样引回去，文字与图就不会打架。

    解析失败不抛异常，整段当外貌用（宁可长一点，也不能丢了外貌描述）。
    """
    t = (text or "").strip()
    if FL2VA_MAIN in t:
        t = t.split(FL2VA_MAIN, 1)[1]
    t = re.sub(r"^\s*\[Shot\s*\d+\]\s*", "", t).strip()
    sents = [s.strip() for s in _SENT_RE.split(t) if s.strip()]

    style = ""
    if len(sents) >= 2:
        # 风格句不一定在最后一句（定妆卡可能以 Camera/Audio 结尾），从尾部往前找
        for k in range(len(sents) - 1, 0, -1):
            low = sents[k].lower()
            if low.startswith(("background:", "camera", "ambient audio")):
                continue
            if _STYLE_HINT_RE.search(sents[k]):
                style = sents.pop(k)
                break

    subject = ""
    if sents:
        m = _SUBJECT_RE.match(sents[0])
        if m:
            subject = m.group(1).strip().rstrip(",")
        else:
            m2 = re.match(r"(?i)^[^,]*portrait of\s+([^,.]+)", sents[0])
            if m2:
                subject = m2.group(1).strip()

    body: list[str] = []
    for i, s in enumerate(sents):
        if i == 0:
            s = re.sub(r"(?i)^.*?portrait of\s+[^,]+,\s*", "", s)
            s = re.sub(r"(?i)^standing still and facing the camera directly\.?$", "", s).strip()
            if not s:
                continue
        if s.lower().startswith("background:"):
            break
        body.append(s)

    appearance = " ".join(body).strip()
    if not appearance:
        appearance = t  # 兜底：整段当外貌（仍是原文）
    return subject, appearance, style


def load_char_cards(proj: Project) -> dict[str, CharCard]:
    """
    读取已锁定的角色卡：`prompts/char_<名>.txt`。

    只认提示词文件，不认参考图 —— 参考图没有文字描述，无法保证"文字与图一致"。
    已有卡片 = 该角色的外貌是权威，拆镜时必须逐字沿用，禁止 LLM 重新描述。
    """
    out: dict[str, CharCard] = {}
    pdir = Path(proj.prompts_dir)
    if not pdir.is_dir():
        return out
    for p in sorted(pdir.glob("char_*.txt")):
        if p.name.startswith("."):
            continue
        name = p.name[len("char_") : -len(".txt")]
        if not name:
            continue
        try:
            text = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text:
            continue
        _, appearance, _ = parse_portrait_prompt(text)
        out[name] = CharCard(name=name, appearance=appearance, costume="", portrait_prompt=text)
    return out


_STYLE_SYSTEM = """你是漫剧美术指导。读一段小说正文，为它定一句**英文视觉风格句**（10-25 词）。

【要求】
- 这句话会被拼进**每一个镜头**的提示词，是整部片的统一视觉基调。
- 只写**风格层面**：写实/动画/水墨/赛博/胶片质感、光比、色彩倾向、颗粒。
  **不要写剧情、人物、地点**（那些由每镜自己的描述负责）。
- **必须贴合这一段的题材与时代**：现代都市悬疑 ≠ 古装神话 ≠ 科幻 ≠ 日常治愈。
  例：
    古装神话 → "Cinematic realism with natural film lighting, muted desaturated earthy tones and subtle film grain."
    现代都市悬疑 → "Gritty cinematic realism, cool cyan-grey palette, high-contrast practical lighting, wet reflective surfaces, subtle 35mm grain."
    科幻 → "Clean hard sci-fi look, cold blue-white key light, high dynamic range, anamorphic flares, fine digital grain."
    日常治愈 → "Soft natural daylight, warm pastel palette, gentle low-contrast lighting, shallow depth of field."
- 不要照抄例子，按正文实际内容写。

【输出】严格 JSON：{"style": "一句英文风格句"}"""


def _preset_of(cfg: dict, pcfg: dict | None = None) -> str:
    """当前项目用哪个风格预设。`style_preset` 优先，其次看 `style` 字段里的关键词。"""
    src = dict(cfg or {})
    if pcfg:
        src.update({k: v for k, v in pcfg.items() if v is not None})
    v = src.get("style_preset")
    if v:
        return _style.resolve_preset(v)
    return _style.resolve_preset(src.get("style"))


def _style_system_for(preset: str) -> str:
    """
    `_STYLE_SYSTEM` + 该预设的**例子与约束**。

    原来 `_STYLE_SYSTEM` 里的例子全是写实的（cinematic realism / film grain…），
    那样推出来的风格句必然偏写实 —— **二次元无从谈起**。
    所以例子块和约束块都要跟着预设换。
    """
    p = _style.get(preset)
    base = _STYLE_SYSTEM
    i = base.find("  例：")
    j = base.find("- 不要照抄例子")
    if i > 0 and j > i:
        base = base[:i] + "  例：\n" + p["examples"] + "\n" + base[j:]
    return base + "\n【本片风格约束（必须严格遵守）】" + p["guidance"]


def _derive_style(cfg: dict, chapter_text: str, log: Any, stats: dict) -> str:
    """
    从正文**自动推导**整片视觉风格句。失败返回空串（调用方退回 DEFAULT_STYLE）。

    为什么需要（通用性）：`DEFAULT_STYLE` 是写死的"muted desaturated earthy tones"。
    实测换题材才发现 —— 现代都市悬疑和古装神话拿到的是**同一句风格**，
    结果新项目的定妆照和分镜全被染成"土黄写实"。项目级 `style` 配置项虽然存在
    （`plan.py` 的 pcfg/cfg 都能给），但没人会记得每次都配。
    把 `project.json` 的 `style` 设成 `"auto"` 即可让每部片自己定风格。
    """
    user = f"【正文】\n{chapter_text}\n\n按正文实际题材写风格句。"
    try:
        obj, meta = _llm_json(cfg, _style_system_for(_preset_of(cfg)), user,
                              temperature=0.3, log=log, tag="风格推导")
        stats["llm_calls"] = stats.get("llm_calls", 0) + 1
        _accumulate_usage(stats, meta)
        style = str((obj or {}).get("style") or "").strip().rstrip(".")
        if style:
            _log(log, f"  🎨 自动风格句：{style}")
        return style
    except Exception as e:
        _log(log, f"  ⚠️ 风格推导失败，退回默认：{e}")
        stats.setdefault("warnings", []).append(f"风格推导失败：{e}")
        return ""


def _card_style(cards: Iterable[CharCard]) -> str:
    """
    从角色卡里抽全局风格句：多张卡取众数（同一套定妆图风格应当一致）。

    含旧风格锚的句子直接丢掉（见 _STYLE_ANCHOR_BAN）：宁可退回默认写实句，
    也不能把"廉价 3D 卡通"的观感锚重新写回逐镜提示词。
    """
    votes: dict[str, int] = {}
    for c in cards:
        _, _, style = parse_portrait_prompt(c.portrait_prompt)
        if style and not _STYLE_ANCHOR_BAN.search(style):
            votes[style] = votes.get(style, 0) + 1
    if not votes:
        return ""
    return max(votes.items(), key=lambda kv: (kv[1], len(kv[0])))[0]


def _build_portrait_prompt(name: str, appearance: str, costume: str, style: str, background: str,
                           preset: str | None = None) -> str:
    """
    给新角色拼一张定妆提示词（与已有角色卡同一模板，保证风格统一）。

    主体短语尽量从外貌描述的第一个分句里取（"giant demon, twelve chi tall…" → "a giant demon"）——
    中文名塞进英文提示词没有信息量，模型只会画出"一个角色"。
    """
    first = (appearance.strip().split(",")[0].strip()) if appearance else ""
    if first and re.match(r"(?i)^(a|an|the)\b", first):
        who = first
    elif first and re.match(r"^[A-Za-z]", first):
        article = "an" if first[0].lower() in "aeiou" else "a"
        who = f"{article} {first[0].lower()}{first[1:]}"
    elif name and name.isascii():
        who = f"a character named {name}"
    else:
        who = "the character"
    desc = appearance.strip().rstrip(".")
    if costume.strip():
        desc = f"{desc}. {costume.strip().rstrip('.')}"
    # 兜底值随预设走 —— 否则 anime 项目里定妆卡仍会得到写实背景句
    _pre = _style.get(preset) if preset else _style.get(_style.DEFAULT_PRESET)
    style = (style or _pre["sentence"]).strip().rstrip(".")
    bg = (background or _pre["portrait_background"]).strip()
    return (
        f"{FL2VA_MAIN}: [Shot 1] Cinematic medium close-up portrait of {who}, "
        f"standing still and facing the camera directly. {desc}. {bg} {style}."
    )


# ── 新角色卡抽取（allow_new_characters）────────────────────────────────────

# ── 关键道具实体（2026-09-23）──────────────────────────────────────────────
# 和场景同理：道具外观若每镜各写各的，就会出现"金箍棒每镜长得不一样"。
# LLM 抽出关键道具 → 一段权威英文外观描述 → 同道具出现的镜头注入**同一段文字**。


@dataclass
class PropCard:
    """一个关键道具。description 是英文权威外观，被逐字注入用到它的镜头。"""

    id: str            # P1 / P2…
    name: str          # 中文名，如「金箍棒」
    owner: str         # 归属角色（可空）
    description: str   # ★ 英文权威外观描述（逐字注入）
    inferred: bool = False   # True = 正文没给外观，是保守推断的通用形象，需人工复核


_PROP_SYSTEM = """是漫剧制片。从小说章节正文里找出**需要在画面里保持一致外观的关键道具**，并写英文外观描述。

【建道具标准】
- 只收**反复出现、或在关键动作/特写里出现**的道具（武器、法器、信物、关键器物）。
- 普通背景陈设（桌椅、瓦片、杂草）不收。
- 同一个道具在正文里多次出现**只建一条**，不要重复。
- description 一律英文，**必须包含可画的视觉信息**：形状、材质、颜色、尺寸、纹样、磨损状态。
  ⚠️ **"A rake carried by Zhu Bajie." 这种同义反复是废的** —— 它没告诉渲染任何东西，
  当一致性锚点毫无价值。写完自检一遍：**画师只看这句话，能画出这个道具吗？**
  画不出来就不要建这条（宁缺勿滥）。
- **正文没有给任何外观信息的道具，不要建卡**；只有在该道具处于关键动作/特写、
  缺了会导致画面不成立时，才给一个**最保守的通用外观**，并置 `inferred: true` 提示人工复核。
- **这段文字会被逐字注入用到该道具的每一镜**，
  所以必须**与镜头无关**（不写"被举起来"这类动作，不写景别）。
- owner 填归属角色的中文名（如「孙悟空」），不明确就留空字符串。
- id 用 P1/P2/P3… 顺序编号。

【输出】严格 JSON：
{"props": [{"id": "P1", "name": "金箍棒", "owner": "孙悟空",
  "description": "A two-ended golden-banded cudgel, dark iron core with bright gold rings at both ends, worn smooth, about the length of a man's arm when shrunk.",
  "inferred": false}]}
""" + DETAIL_CHECKLIST


def _extract_props(cfg: dict, chapter_text: str, log: Any, stats: dict) -> list[PropCard]:
    """从正文抽关键道具。失败返回空列表（plan 继续，只是没有道具锚定）。"""
    user = f"【正文】\n{chapter_text}\n\n只依据正文，不要编造。"
    try:
        obj, meta = _llm_json(cfg, _PROP_SYSTEM + _style.guidance(_preset_of(cfg)), user,
                              temperature=0.2, log=log, tag="道具抽取")
        stats["llm_calls"] = stats.get("llm_calls", 0) + 1
        _accumulate_usage(stats, meta)
    except Exception as e:
        _log(log, f"  ⚠️ 道具抽取失败（继续，无道具锚定）：{e}")
        stats.setdefault("warnings", []).append(f"道具抽取失败：{e}")
        return []
    out: list[PropCard] = []
    seen: set[str] = set()
    for i, x in enumerate((obj.get("props") if isinstance(obj, dict) else None) or [], 1):
        if not isinstance(x, dict):
            continue
        name = str(x.get("name") or "").strip()
        desc = str(x.get("description") or "").strip()
        if not name or not desc:
            continue
        pid = str(x.get("id") or f"P{i}").strip() or f"P{i}"
        if pid in seen:
            pid = f"P{i}"
        seen.add(pid)
        # 反同义反复：太短的描述（如 "A rake carried by Zhu Bajie."）当锚点没价值。
        # 这里不是"卡长度"，是拒绝**不含任何视觉信息**的描述。
        if len(desc) < 40 and not x.get("inferred"):
            _log(log, f"  ⏭ 丢弃无视觉信息的道具「{name}」：{desc!r}")
            stats.setdefault("warnings", []).append(f"道具「{name}」描述无视觉信息，已丢弃")
            continue
        out.append(PropCard(
            id=pid, name=name,
            # owner 同样过占位词过滤：正文没写归属时 LLM 可能填 unknown / 无
            owner=_no_placeholder(x.get("owner")),
            description=desc, inferred=bool(x.get("inferred")),
        ))
    return out


def save_props(proj, props: list[PropCard]) -> Path:
    p = Path(proj.root) / "props.json"
    data = {"version": 1, "props": [
        {"id": c.id, "name": c.name, "owner": c.owner,
         "description": c.description, "inferred": c.inferred} for c in props
    ]}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def load_props(proj) -> dict[str, PropCard]:
    p = Path(proj.root) / "props.json"
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {
        str(x["id"]): PropCard(id=str(x["id"]), name=str(x.get("name") or ""),
                               owner=str(x.get("owner") or ""), description=str(x.get("description") or ""),
                               inferred=bool(x.get("inferred")))
        for x in (d.get("props") or []) if isinstance(x, dict) and x.get("id")
    }


# ── 场景实体（2026-09-23）────────────────────────────────────────────────
# 为什么需要：数据里的 `scene` 只是"场次号"，实测 52 镜被分成了 47 个场次
# （几乎每镜一个），**"场景"这一层从来没真正建立过**。
# 场景一致性因此没有任何锚定 —— 同一间破庙的墙、光、色调，每镜各写各的。
# 做法：LLM 从正文抽出**真正的地点**（不是场次号），给一段权威英文视觉描述，
# 同场景的每一镜注入**同一段文字** —— 用文字锚定，不占 H3 的参考图槽位
# （H3 的 <Picture N> 语义是"主体"，场景图不是主体）。


@dataclass
class SceneCard:
    """一个场景（地点）。description 是英文权威视觉描述，会被逐字注入同场景每一镜。"""

    id: str            # 稳定短 id，如 S1 / S2（不是场次号）
    name: str          # 中文场景名，如「云隐寺正殿」
    location: str      # 地点
    time_of_day: str   # 时间（黄昏/深夜/破晓…）
    lighting: str      # 光线
    atmosphere: str    # 氛围
    description: str   # ★ 英文权威视觉描述（逐字注入，保证跨镜一致）


_SCENE_SYSTEM = """是漫剧制片。从小说章节正文里找出**不同的地点/场景**，并为每个场景写一段英文视觉描述。

【建场景标准】
- 一个"场景" = **一个物理地点 + 内景/外景档次**。
- ★ **内景与外景必须分开建**（这是本条最重要的一条）：同一座庙，
  「山门/院落（室外）」与「正殿/禅房（室内）」是**两个场景**，不要合成一个。
  理由：锚定文字会被逐字注入该场景每一镜，若把室外描述（杂草、门板、天光）
  注进室内镜，会**把镜头的内容改错** —— 实测本片 16 镜的"山门"场景里有 15 镜
  其实发生在殿内。
- 同一地点**同一内/外档次**在正文里反复出现，只能算一个场景，不要按场次或镜头重复建。
- 场景切换的标志：空间移动、时间跳跃、**内外景切换**、昼夜切换。
- 正文只提到一次、且没有画面的路过地点可以不建。
- ★ **正文没写的字段就留空字符串 `""`，不要填 `unknown` / `timeless` / `N/A` / `none` 这类占位词。**
  实测踩过：有章节没交代时间，LLM 把 `time_of_day` 填成 `"unknown, timeless"`，
  一路显示到控制台的场景列里（"站台 / unknown, timeless"），看起来像坏了。
  **留空是正确表达"正文没说"的方式**；填占位词是把"不知道"伪装成"知道"。
- description 一律英文，只写**能从正文推断出的**：地点结构、材质、天气、时间、光源方向、色调、环境音。
  正文没写的不要编造。**这段文字会被逐字复制进该场景每一镜的提示词**，
  所以它必须**与镜头无关**（不要写人物动作、不要写景别运镜）。
- id 用 S1/S2/S3… 顺序编号。

【输出】严格 JSON：
{"scenes": [{"id": "S1", "name": "云隐寺山门", "location": "crumbling mountain temple gate",
  "time_of_day": "dusk", "lighting": "dim blue-grey from the left", "atmosphere": "desolate",
  "description": "A crumbling old mountain temple courtyard at dusk, weathered grey brick walls, dry grass, dark pine branches; dim blue-grey evening light from the left, muted desaturated earthy tones, slight film grain."}]}

【内/外景分开的例子】同一个「云隐寺」应该建成两条而不是一条：
  S2 = 云隐寺山门（**外景**）：门板、匾额、台阶、院里杂草、天光
  S3 = 云隐寺正殿（**内景**）：头断的佛像、香案、梁柱阴影、窗棂透进的光
两条 description 都不能提到对方的地点特征（内景那条不要写"杂草/门板"，外景那条不要写"佛像/香案"）。
""" + DETAIL_CHECKLIST


# 占位词黑名单：LLM 在"正文没说"时会拿这些词充数。它们不是内容，是伪装成内容的空值。
_PLACEHOLDER = {
    "", "-", "n/a", "na", "none", "null", "nil", "unknown", "unspecified", "not specified",
    "not mentioned", "unclear", "ambiguous", "timeless", "any", "generic", "tbd", "todo",
    "未知", "不详", "未提及", "无", "不明", "不限", "任意",
}


def _no_placeholder(v: Any) -> str:
    """占位词/空值 → 空字符串。留空是"正文没说"的正确表达。"""
    t = str(v or "").strip().strip(".,;:，。；：")
    if t.lower() in _PLACEHOLDER:
        return ""
    # 「unknown, timeless」「未知 / 无」这种组合也判为占位
    parts = [x.strip().strip(".,;:，。；：").lower() for x in t.replace("/", ",").split(",") if x.strip()]
    if parts and all(x in _PLACEHOLDER for x in parts):
        return ""
    return t


def _extract_scenes(cfg: dict, chapter_text: str, log: Any, stats: dict) -> list[SceneCard]:
    """从章节正文抽场景实体。失败返回空列表（plan 仍可继续，只是没有场景锚定）。"""
    user = f"【正文】\n{chapter_text}\n\n只依据正文，不要编造。"
    try:
        obj, meta = _llm_json(cfg, _SCENE_SYSTEM + _style.guidance(_preset_of(cfg)), user,
                              temperature=0.2, log=log, tag="场景抽取")
        stats["llm_calls"] = stats.get("llm_calls", 0) + 1
        _accumulate_usage(stats, meta)
    except Exception as e:
        _log(log, f"  ⚠️ 场景抽取失败（继续，无场景锚定）：{e}")
        stats.setdefault("warnings", []).append(f"场景抽取失败：{e}")
        return []
    out: list[SceneCard] = []
    seen: set[str] = set()
    for i, x in enumerate((obj.get("scenes") if isinstance(obj, dict) else None) or [], 1):
        if not isinstance(x, dict):
            continue
        name = str(x.get("name") or "").strip()
        desc = str(x.get("description") or "").strip()
        if not name or not desc:
            continue
        sid = str(x.get("id") or f"S{i}").strip() or f"S{i}"
        if sid in seen:
            sid = f"S{i}"
        seen.add(sid)
        # 过滤占位词：`"unknown, timeless"` 这类不该当数据显示（也不该进拆镜提示词的场景清单）
        dropped = [k for k in ("location", "time_of_day", "lighting", "atmosphere")
                   if str(x.get(k) or "").strip() and not _no_placeholder(x.get(k))]
        if dropped:
            _log(log, f"  · 场景 {name} 的 {', '.join(dropped)} 是占位词，已置空（正文没说）")
            stats.setdefault("warnings", []).append(
                f"场景「{name}」的 {', '.join(dropped)} 被填成占位词，已置空"
            )
        out.append(
            SceneCard(
                id=sid, name=name,
                location=_no_placeholder(x.get("location")),
                time_of_day=_no_placeholder(x.get("time_of_day")),
                lighting=_no_placeholder(x.get("lighting")),
                atmosphere=_no_placeholder(x.get("atmosphere")),
                description=desc,
            )
        )
    return out


def save_scenes(proj, scenes: list[SceneCard]) -> Path:
    """写 `scenes.json`（原子写）。空列表也写，便于 UI 显示"本章未抽出场景"。"""
    p = Path(proj.root) / "scenes.json"
    data = {
        "version": 1,
        "scenes": [
            {"id": c.id, "name": c.name, "location": c.location,
             "time_of_day": c.time_of_day, "lighting": c.lighting,
             "atmosphere": c.atmosphere, "description": c.description}
            for c in scenes
        ],
    }
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def load_scenes(proj) -> dict[str, SceneCard]:
    """读 `scenes.json` → {id: SceneCard}。缺失/损坏返回空 dict（不抛）。"""
    p = Path(proj.root) / "scenes.json"
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, SceneCard] = {}
    for x in (d.get("scenes") or []):
        if not isinstance(x, dict) or not x.get("id"):
            continue
        out[str(x["id"])] = SceneCard(
            id=str(x["id"]), name=str(x.get("name") or ""),
            location=str(x.get("location") or ""), time_of_day=str(x.get("time_of_day") or ""),
            lighting=str(x.get("lighting") or ""), atmosphere=str(x.get("atmosphere") or ""),
            description=str(x.get("description") or ""),
        )
    return out


_CARD_SYSTEM = """是漫剧制片。从小说章节正文里找出**需要单独建角色卡**的角色，并给出英文外貌与服装描述。

【建卡标准】
- 有台词的角色必须建卡；虽无台词但反复出镜、且正文给了明确称呼/形象的角色也要建卡。
- 纯群体（众僧、路人、士兵、围观百姓）不建卡。
- 已经有角色卡的角色不要重复列出。
- appearance / costume 一律英文，只写**能从正文推断出的**特征（年龄段、性别、体型、发型、面部特征、神态、衣着）。正文没写的不要编造。
- name 用正文里的中文称呼（2-4 字，如「沙僧」），不要另起名字。

- ★ **必须给 `evidence`：一小段（8-30 字）从正文里逐字抄出来的文字**，用来证明这个角色真的存在。
  抄出现在**称呼他**或**明确描写他**的那一句/半句原文，**一个字都不要改**（不能加标点、不能概括）。
  若你给角色起的名字是**描述性的**（正文只说"男人""老妇人""那道影子"），
  evidence 就抄那句描述他的话，name 保持描述性即可。

【输出】严格 JSON：{"characters": [{"name": "沙僧", "appearance": "English appearance", "costume": "English costume", "evidence": "从正文逐字抄出的 8-30 字"}]}
""" + DETAIL_CHECKLIST


def _card_background(cards: Iterable[CharCard], preset: str | None = None) -> str:
    """复用已有角色卡的 Background 段，让新角色的定妆照和老角色处于同一环境。"""
    for c in cards:
        m = re.search(r"Background:[^\n]*", c.portrait_prompt or "")
        if m:
            return m.group(0).strip()
    return _style.get(preset)["portrait_background"] if preset else _style.PRESETS[_style.DEFAULT_PRESET]["portrait_background"]


def _gen_new_cards(
    chapter_text: str,
    locked: dict[str, CharCard],
    cfg: dict,
    log: Any,
    stats: dict,
) -> list[dict]:
    """
    专门调一次 LLM 抽"全章登场角色"。

    为什么不搭拆镜表的便车：实测让拆镜 LLM 顺手建卡时，它会把没有卡的配角
    当成画外音/模糊剪影糊掉（沙僧的台词甚至被整句丢掉）。把建卡拆成独立一步后，
    新角色在拆镜前就有卡，拆镜 LLM 才愿意正常让他出镜。
    幻觉守卫：名字必须在正文里出现过，否则丢弃（LLM 会凭空造角色）。
    """
    known = "、".join(sorted(locked)) or "(无)"
    user = (
        f"【已有角色卡（不要重复列出）】{known}\n\n"
        f"【正文】\n{chapter_text}\n\n"
        '【输出 JSON】\n{"characters": [{"name": "中文名", "appearance": "English appearance", '
        '"costume": "English costume"}]}'
    )
    obj, meta = _llm_json(cfg, _CARD_SYSTEM + _style.guidance(_preset_of(cfg)), user,
                          temperature=0.2, log=log, tag="角色卡抽取")
    stats["llm_calls"] += 1
    _accumulate_usage(stats, meta)

    out: list[dict] = []
    for c in obj.get("characters") or []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        appearance = str(c.get("appearance") or "").strip()
        costume = str(c.get("costume") or "").strip()
        if not name or name in locked:
            continue
        alias = _alias_of_locked(name, locked)
        if alias:
            stats["warnings"].append(f"新角色「{name}」被识别为已有角色「{alias}」的别称，未重复建卡")
            continue
        # ★ 防幻觉校验（2026-09-24 修）：
        # 原来是裸的 `name not in chapter_text` 子串匹配。实测换个题材立刻误杀 ——
        # 正文写「坐在对面的男人」（「男人」出现 6 次），LLM 据"穿灰风衣"起名「灰风衣男人」，
        # 完整串不在正文里 → **整个角色被丢弃**，成片就只剩一个角色、另一个成了没有脸的路人。
        # 现在优先用 LLM 给的**逐字原文证据**（B5 同款思路），证据对得上就认；
        # 没有证据时退回子串匹配，但放宽到"名字里任一 2 字连续子串出现在正文里"。
        evidence = str(c.get("evidence") or "").strip()
        if evidence:
            if evidence not in chapter_text:
                stats["warnings"].append(
                    f"新角色「{name}」的证词无法在正文里逐字找到，已丢弃（防幻觉造角色）：{evidence[:30]!r}"
                )
                continue
        else:
            if name in chapter_text:
                pass  # 全名对得上
            elif any(name[i:i + 2] in chapter_text for i in range(len(name) - 1)):
                # 描述性名字（「灰风衣男人」）：任一 2 字子串（如「男人」）在正文里即认可
                stats["warnings"].append(
                    f"新角色「{name}」是描述性名字（正文无此完整串，但有 2 字子串命中），已建卡"
                )
            else:
                stats["warnings"].append(f"新角色「{name}」没在正文里出现过，已丢弃（防幻觉造角色）")
                continue
        if not appearance:
            stats["warnings"].append(f"新角色「{name}」没有外貌描述，跳过")
            continue
        out.append({"name": name, "appearance": appearance, "costume": costume})
        _log(log, f"  🆕 新角色卡：{name} — {appearance[:70]}")
    return out


# 单章自动建卡上限：防 LLM 把路人也建成角色，也防费用失控
MAX_NEW_CARDS = 4

_CARD_ONE_SYSTEM = """是漫剧制片。为给定的**一个**角色写定妆用角色卡。

- 只依据给定的正文写，与正文矛盾的特征一律不写。
- appearance / costume 一律英文，写年龄段、性别、体型、发型、面部特征、神态、衣着。
- **禁止写元说明**（如"正文没有描述他的面部特征"）——这类句子会被当成画面指令，
  可能把角色画成没有脸。正文没写就按身份/职业/称呼给一个**保守但具体**的形象
  （例：a sturdy middle-aged man with a square jaw, short black hair, calm steady eyes）。
- 只输出 JSON，不要解释。

【输出】严格 JSON：{"name": "角色名", "appearance": "English appearance", "costume": "English costume"}
""" + DETAIL_CHECKLIST


def _alias_of_locked(name: str, locked: Iterable[str]) -> str | None:
    """
    判断 name 是不是某个已有角色的别称。

    判据用子串包含（「老僧」⊂「白眉老僧」、「悟空」⊂「孙悟空」）。
    这条守卫是实测逼出来的：角色抽取 LLM 会为正文里的「老僧」新建一张卡，
    而它其实是已有卡「白眉老僧」，重复建卡会让同一个角色有两张参考图。
    """
    for k in locked:
        if not k or not name:
            continue
        if name == k or name in k or k in name:
            return k
    return None


def _add_cards_for(
    names: list[str],
    chapter_text: str,
    cards: dict[str, CharCard],
    cfg: dict,
    style: str,
    background: str,
    log: Any,
    stats: dict,
) -> tuple[list[str], dict[str, str]]:
    """
    按需补卡：拆镜表用了某个没卡的名字时，当场为他建一张卡。

    为什么不直接让拆镜 LLM 改成画外音：Lead 的裁决是"不要为了工具限制扭曲内容"。
    拆镜 LLM 自己判断出"这个角色要出镜"是最可靠的信号，比事前的角色抽取更准
    （实测抽取会漏掉沙僧这种"以为你已经有卡了"的角色）。

    返回 (新建的名字, 别称映射 {别称: 正式名})。
    """
    added: list[str] = []
    alias_map: dict[str, str] = {}
    for name in names:
        alias = _alias_of_locked(name, [n for n in cards if n != name])
        if alias:
            alias_map[name] = alias
            stats["warnings"].append(f"角色「{name}」按别称归到已有角色「{alias}」")
            continue
        if len(stats.get("new_characters", [])) + len(added) >= MAX_NEW_CARDS:
            stats["warnings"].append(f"自动建卡已达上限 {MAX_NEW_CARDS}，「{name}」未建卡")
            continue
        if name not in chapter_text:
            stats["warnings"].append(f"角色「{name}」没在正文里出现过，不建卡（防幻觉）")
            continue
        user = (
            f"【角色名】{name}\n\n"
            f"【正文（只依据它写外貌）】\n{chapter_text}\n\n"
            '【输出 JSON】\n{"name": "%s", "appearance": "English appearance", '
            '"costume": "English costume"}' % name
        )
        try:
            obj, meta = _llm_json(cfg, _CARD_ONE_SYSTEM + _style.guidance(_preset_of(cfg)), user,
                              temperature=0.2, log=log, tag=f"补卡 {name}")
        except PlanError as e:  # 补卡失败不该拖垮整章
            stats["warnings"].append(f"补卡「{name}」失败：{e}")
            continue
        stats["llm_calls"] += 1
        _accumulate_usage(stats, meta)
        appearance = str(obj.get("appearance") or "").strip()
        costume = str(obj.get("costume") or "").strip()
        if not appearance:
            stats["warnings"].append(f"补卡「{name}」没拿到外貌描述，跳过")
            continue
        cards[name] = CharCard(
            name=name,
            appearance=appearance,
            costume=costume,
            portrait_prompt=_build_portrait_prompt(name, appearance, costume, style, background, preset),
        )
        stats["new_characters"].append(name)
        added.append(name)
        _log(log, f"  🆕 按需补卡：{name} — {appearance[:60]}")
    return added, alias_map


def write_char_prompts(proj: Project, chars: list[CharCard]) -> dict[str, Path]:
    """
    写 `prompts/char_<名>.txt`，返回 {名: 路径}。

    内容与磁盘一致时**不写**：避免无谓地翻新 mtime（mtime 是下游参考图指纹的输入，
    虽然 refs 的 mtime 才进指纹，但少一次无谓写盘就少一次"看起来变了"的误判）。
    """
    proj.ensure()
    paths: dict[str, Path] = {}
    for c in chars:
        if not c.name or "/" in c.name or "\\" in c.name or ".." in c.name:
            raise PlanError(f"角色名非法，不能作为文件名：{c.name!r}")
        p = Path(proj.prompts_dir) / f"char_{c.name}.txt"
        text = c.portrait_prompt.rstrip() + "\n"
        old = p.read_text(encoding="utf-8") if p.is_file() else None
        if old != text:
            _atomic_write_text(p, text)
        paths[c.name] = p
    return paths


def _atomic_write_text(path: Path, text: str) -> None:
    """先写 .tmp 再 os.replace（与 state.py/shots.py 同一策略，不允许半截文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ── 拆镜提示词（可单独迭代，不动代码逻辑）──────────────────────────────────

TABLE_SYSTEM = """你是资深漫剧分镜师。把小说正文拆成 H3 视频分镜表（每镜将单独渲成一段视频）。

【拆镜硬规范】
（编号是稳定引用锚 —— 代码与复核清单按号引用，改动请**只追加、不要重排**）
1. **按段落/节拍拆镜**，覆盖全文情节、对白、动作、情绪；**约每 90 字一镜**，
   全章镜数 ≈ 正文字数 ÷ 90（允许 ±30%）。同一场景、同一角色的连续动作或对白**必须合并进一镜**，
   **绝不要把每一句对白都单独成镜**（实测那样会把 1745 字拆成 48 镜：片子碎成幻灯片、
   渲染时间翻倍、单镜信息量不足还更容易崩脸）。
   只在下列情况才新开一镜：场景切换、时间跳跃、视角/景别明显变化、或一镜确实装不下这个节拍。
2. 单镜 3-15 秒；对白镜时长按「台词+旁白总字数 ÷ 4」向上取整并留 1 秒余量；动作/空镜 4-8 秒；禁止所有镜头同一时长。
3. `scene_id` 必须从给定的【场景清单】里选，标明这一镜发生在**哪个地点**。
   同一地点的连续镜头要给同一个 scene_id —— **这是跨镜场景一致性的唯一锚点**，
   不要每镜换一个新值。
4. `prop_ids` 从给定的【道具清单】里选**画面里真的出现**的道具（可以空数组）。
   只在道具确实入镜时才写 —— **不要因为剧情提到就写**。这是外观一致性锚点。
5. `conflict` = 本镜的**戏剧张力 0-10**（**不是打斗强度**）：信息揭露、情绪升级、正面对抗、悬念压迫都算。
   平铺直叙的环境/过场给 2-3，铺垫与试探给 4-6，对峙/摊牌/爆发给 8-10。
   **必须逐镜给**——它用于渲染前的节奏审计（检测"连续多镜低张力"= 观众划走的地方），
   让系统在花钱渲染之前就能发现"全片没有高潮"或"高开低走"。
6. 台词**一句都不能丢**：正文里每一句引号对白都必须出现在某个镜头的 dialogue 里（长句可按语义拆到连续几镜，但合起来要完整）。没有角色卡的角色（例如沙僧）的台词也要保留，写成画外音：dialogue 仍只放纯台词原文，并在该镜 action 里点明是谁在画外说（如 "the line comes from Sha Wujing off-screen"）。台词逐字进 dialogue（原词原标点，不加引号、不加「旁白：」「他说」等前缀）；**单句必须 ≤20 字**——超过 20 字的对白必须在分镜阶段就按语义拆到**连续两镜**（原文一个字都不能删，两镜合起来仍是完整原句），不要留给后期。narration 只放正文中真正需要旁白的少量内容：画面能表达的不要写旁白；每章旁白 ≤3 句，每句 ≤15 字；没有就写空字符串。
7. chars 只能从给定的【可用角色】里选（写中文名），每镜登场角色 ≤3（超了拆镜）；说话人必须尽量在 chars 里。除画外音外，一个镜头不要出现没有角色卡的清晰人脸。
   画外音台词（说话人没有角色卡，例如沙僧）：把该镜交给在场角色，并在 action 里**明确写出** "the line is spoken off-screen by <名字>"，让画面角色闭嘴；不要把它写成画面角色在说。
8. 对话密集处用正反打：说话人近景 + 听者反应特写，避免双人同框说话。
9. action 用英文写「可渲染的视觉句」：谁在哪做什么 + 光线 + 色调；写清站位（谁左谁右、前景/中景/背景）与朝向（facing camera/left/right/turned away）；开头用【场景名】标注地点；结尾落在完成态动作，禁止静止定格收尾；画面里不要出现数字（年龄/数量/年份会被模型画成画面文字）。
10. shot_size 用中文（远景/全景/中景/近景/特写/大特写）；camera 写「中文（English, amplitude, speed）」，如「缓推（Push In, small amplitude, slow speed）」；每镜只安排一种主要运镜，固定镜写「固定（Static）」。
11. 群戏用背影/剪影/虚化消化，绝不写群众有清晰面孔。避免贴身缠斗、快速肢体接触（模型最易崩），改成可观察的单人动作或画外音。
12. 有角色卡的角色的外貌/服装一个字都不要写进 action —— 参考图与角色卡负责长相，你只写位置、动作、景别、光线、环境。

【详细度纪律】action 字段**不要让模型猜**：姿态要具体到手脚位置与朝向，\n  环境要写清背景有什么、光从哪个方向来；服装与外貌交给角色卡与参考图（见第 12 条）。\n  没写的维度模型会取平均值，而平均值就是平光 + 糊背景。\n\n【输出】严格 JSON，不要输出 JSON 以外的任何内容。"""

TABLE_SCHEMA = """{
 "title": "本章标题",
 "unknown_characters": ["正文里出现但没有角色卡、你已用画外音/背影处理掉的角色名"],
 "characters": [{"name": "新角色中文名", "appearance": "英文外貌描述", "costume": "英文服装描述"}],
 "shots": [{
   "scene": 1,
   "shot_size": "中景",
   "camera": "缓推（Push In, small amplitude, slow speed）",
   "chars": ["孙悟空"],
   "dialogue": "台词原文，没有则空字符串",
   "narration": "旁白原文，没有则空字符串",
   "sec": 6,
   "conflict": 5,
   "scene_id": "S1",
   "prop_ids": ["P1"],
   "action": "【场景名】English visual sentence: who, where, doing what, screen position, facing, light, colour tone..."
 }]
}"""

SHOT_SYSTEM = """你是 MiniMax H3 Ref2VA（全参考模式）提示词撰写者。为**单个镜头**写三段正文：
detailed_description / overall_soundscape / non_diegetic_music。

【硬规范】
- 全部英文（仅 <d> 标签内保留中文台词原文）。
- detailed_description 结构：先用给定的【全局风格句】开一句自然风格句 → [Shot 1] → 画面主体外观与屏幕位置（左/中/右 + 前景/中景/背景 + 朝向）→ 动作（至少一个明确的动作动词与幅度分级；结尾落在完成态动作，禁止静止定格收尾）→ 与给定 camera 一致的运镜三要素（类型 + amplitude + speed）→ 光影 → 台词。
- **不要写风格锚**：semi-realistic stylized illustration / not photorealistic / not a real person / not Japanese anime 这类词一律禁止（本机实测会把画面拉成廉价 3D 卡通）。只写朴实的写实描述 + 明确景别 + 光线来源与色调。
- 画面角色台词写：<Subject N> (S1) says: <d>[Chinese] 原文</d>；旁白写：The narrator says in an off-screen voiceover: <d>[Chinese] 原文</d> while the on-screen characters' lips remain completely closed.
- **画外音台词（说话人不是本镜 <Subject>）**：必须写成 "a gruff off-screen male voice (S2) says: <d>[Chinese] 原文</d>" 这样"稳定音色描述 + (Sx)"的形式，**禁止**写成 <Subject N> 在说；同时写清画面角色的嘴保持闭合（their lips remain closed）。判据：action 里写了 "off-screen / 画外" 的台词都属于这一类。
- 台词/旁白**逐字**使用给定原文：禁止增删改写、禁止改标点、禁止同一句出现两次、禁止加中文引号或「旁白：」前缀。
- 只有这一个镜头，只写 [Shot 1]，禁止写时间戳或 [Shot 2]。
- <Subject N> 的编号严格对应【本镜角色顺序】里的第 N 个角色；一个角色只写它的动作与位置，**禁止在正文里重新描述或改写任何外貌、发型、服装**（外貌由 <Picture N> 与 subject_definitions 负责）。
- 篇幅 **160-260 英文词**。对白镜优先保证台词念得完；其余情况**宁可细，不要省** ——\n  用户明确要求过「不要让 ai 猜」：没交代的维度模型会取训练集众数（平均脸 + 平光 + 糊背景），\n  那正是"丑"的来源。每个形容词都要能换成可执行的名词（见下方纪律）。
- overall_soundscape：英文，写环境音与物理音，不写台词、不写配乐。
- non_diegetic_music：英文，只写乐器 + 速度 + 节奏 + 动态变化，禁止情绪形容词（悲伤/史诗感…）；有对白时写「对白时压低、对白结束后回升」；没有配乐就写 none。

【输出】严格 JSON：{"detailed_description": "...", "overall_soundscape": "...", "non_diegetic_music": "..."}"""


# ── 分块与工具 ──────────────────────────────────────────────────────────────


def _chunk_text(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """
    按空行（段落）分块，块 ≤ limit 字符。单段超长时按句号硬切。
    分块是为了让单次请求的输出不会被 max_tokens 截断（截断 = 半截 JSON = 白跑）。
    """
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(p) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            sents = [s for s in re.split(r"(?<=[。！？.!?])", p) if s.strip()]
            cur = ""
            for s in sents:
                if len(cur) + len(s) > limit and cur:
                    chunks.append(cur)
                    cur = ""
                cur += s
            if cur:
                chunks.append(cur)
            continue
        if len(buf) + len(p) + 2 > limit and buf:
            chunks.append(buf)
            buf = ""
        buf = (buf + "\n\n" + p) if buf else p
    if buf:
        chunks.append(buf)
    return chunks or ([text.strip()] if text.strip() else [])


_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_number(s: str) -> int:
    """中文数字 → int（支持 十/十一/二十三；解析不出来返回 0）。"""
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    if "十" in s:
        a, _, b = s.partition("十")
        tens = _CN_DIGITS.get(a, 1) if a else 1
        ones = _CN_DIGITS.get(b, 0) if b else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(s, 0)


def chapter_number(chapter_path: Path, cfg: dict) -> int:
    """章节号：优先 cfg，其次从文件名「第X章」解析，最后 1。"""
    p = cfg.get("plan") if isinstance(cfg.get("plan"), dict) else {}
    explicit = _as_int(p.get("chapter", cfg.get("chapter")))
    if explicit and explicit > 0:
        return explicit
    m = re.search(r"第([零一二两三四五六七八九十\d]+)[章回]", Path(chapter_path).stem)
    if m:
        n = _cn_number(m.group(1))
        if n > 0:
            return n
    return 1


def _as_int(value: Any, default: int | None = None) -> int | None:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        t = value.strip()
        if re.fullmatch(r"-?\d+", t):
            return int(t)
        m = re.match(r"^(\d+)", t)
        if m:
            return int(m.group(1))
    return default


def _norm_chars(value: Any) -> list[str]:
    """规范化 chars：容忍字符串写法，去空白、去重保序。"""
    if isinstance(value, str):
        items = re.split(r"[,，、\s]+", value)
    elif isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        items = []
    out: list[str] = []
    for c in items:
        c = c.strip()
        if c and c not in out:
            out.append(c)
    return out


# ── 阶段①：拆镜表 ──────────────────────────────────────────────────────────


def _table_user_prompt(
    *,
    title: str,
    chapter_text: str,
    chunk_no: int,
    chunk_total: int,
    cards: dict[str, CharCard],
    allowed: list[str],
    style: str,
    unknown_so_far: list[str],
    allow_new: bool,
    feedback: str = "",
    scenes: list | None = None,
    props: list | None = None,
) -> str:
    card_lines = []
    for name in allowed:
        c = cards.get(name)
        if c:
            card_lines.append(f"- {name}: {c.appearance}" + (f" | 服装: {c.costume}" if c.costume else ""))
        else:
            card_lines.append(f"- {name}: （有参考图，无文字卡）")
    new_rule = (
        "本章所有需要出镜的角色都已经建好角色卡并列在上面（含新补的卡）："
        "有戏份的角色**必须**正常写进 chars，不要为了省事把他降级成画外音或模糊剪影；"
        "也不要在 characters 里自行新增角色。"
        if allow_new
        else "正文里若出现没有角色卡的角色，**不要**为他建卡：把涉及他的镜头改成以已定妆角色为主体，"
        "他的台词按画外音处理（用稳定的音色描述 + (S2) 之类，不写 <Subject>），并把他的名字列进 unknown_characters。"
    )
    part = (
        f"【任务】为《{title}》拆镜。"
        + (f"这是本章第 {chunk_no}/{chunk_total} 段正文，只拆这一段。\n" if chunk_total > 1 else "\n")
        + f"""
【可用角色（chars 只能从这里选）】
{chr(10).join(card_lines) or "(无)"}

【场景清单（scene_id 只能从这里选；同一地点的连续镜头必须给同一个 scene_id）】
{chr(10).join(f"- {c.id} {c.name}：{c.time_of_day} / {c.lighting} / {c.atmosphere}" for c in (scenes or [])) or "(本章未抽出场景，scene_id 一律留空)"}

【道具清单（prop_ids 只能从这里选，只在道具真的入镜时才写）】
{chr(10).join(f"- {c.id} {c.name}：{c.description}" for c in (props or [])) or "(本章未抽出道具，prop_ids 一律留空数组)"}

【全局美术风格句（action 里体现风格，不要照抄整句）】
{style}

【拆镜规则补充】
- {new_rule}
- 本流水线要求每镜 chars 非空（参考图注入靠它）：纯环境空镜也要挂一个最相关的已定妆角色作为画面主体或前景背影。
- 台词必须逐字来自正文；正文没有的台词不要编。

【正文（拆镜唯一依据）】
{chapter_text}

【输出 JSON（严格按此结构，不许增删字段）】
{TABLE_SCHEMA}"""
    )
    if unknown_so_far:
        if allow_new:
            part += (
                f"\n\n【注意】你上一版把这些角色当成了无卡角色：{'、'.join(unknown_so_far)}"
                "—— 他们的角色卡已经补好，请把他们正常写进 chars。"
            )
        else:
            part += f"\n\n【已知无角色卡的角色（照上面的画外音/背影规则处理）】{'、'.join(unknown_so_far)}"
    if feedback:
        part += f"\n\n【上一版不合格，必须修正】\n{feedback}"
    return part


def _clamp_conflict(v) -> float | None:
    """conflict 归一化到 [0,10]；缺失/非法返回 None（审计层据此判断该退化为代理分）。"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return max(0.0, min(10.0, f))


def _validate_table(
    obj: dict,
    allowed: set[str],
    allow_new: bool,
    chunk_text: str = "",
    chars_per_sec: float = CHARS_PER_SEC,
) -> tuple[list[dict], list[dict], list[str], list[str]]:
    """
    校验阶段①输出。返回 (shots, new_cards, unknown, issues)。

    issues 分三档（前缀决定处置方式）：
      硬问题（无前缀）→ 重写 1 轮后仍不合格就报错（chars 空/角色没卡/sec 出域/action 空）
      "__fix__" 必须先让 LLM 改一次的问题（正文对白丢失、语音超单镜上限）；
                重写后仍存在则只告警继续（前者人工看表补，后者有自动拆镜兜底）
      "__style__" 风格建议（台词 >20 字、拆得过碎）→ 只记录，不触发重写、不阻断
    
    台词长短是**风格偏好**：正文里的原句可能本来就 40 字，强行拆成两镜会把一句话
    切成两半、字幕对不上。真正会出事的是"念不完"，而那是 4 字/秒预算管的
    （fit_sec_to_speech 自动延长时长）。
    """
    issues: list[str] = []
    raw_shots = obj.get("shots")
    if not isinstance(raw_shots, list) or not raw_shots:
        return [], [], [], ["shots 为空或不是数组（必须至少拆出一个镜头）"]

    new_cards: list[dict] = []
    unknown: list[str] = []
    for i, c in enumerate(obj.get("characters") or [], 1):
        if not isinstance(c, dict):
            issues.append(f"characters 第 {i} 项不是对象")
            continue
        name = str(c.get("name") or "").strip()
        appearance = str(c.get("appearance") or "").strip()
        costume = str(c.get("costume") or "").strip()
        if not name:
            issues.append(f"characters 第 {i} 项缺少 name")
            continue
        if name in allowed:
            continue  # 已有角色卡：外貌以卡为准，LLM 的版本丢弃
        if not allow_new:
            # 不允许新建角色：这个名字不能出现在 chars 里，否则没有参考图必然脸崩
            if name not in unknown:
                unknown.append(name)
            issues.append(
                f"出现了没有角色卡/参考图的角色「{name}」：请把涉及他的镜头改成以已定妆角色为主体，"
                "他的台词按画外音处理（稳定的音色描述 + (S2)，不写 <Subject>），并把他列进 unknown_characters"
            )
            continue
        if not appearance:
            issues.append(f"新角色「{name}」缺少 appearance")
        new_cards.append({"name": name, "appearance": appearance, "costume": costume})

    known = set(allowed) | {c["name"] for c in new_cards}

    for u in obj.get("unknown_characters") or []:
        if isinstance(u, str) and u.strip():
            u = u.strip()
            if u not in known and u not in unknown:
                unknown.append(u)

    shots: list[dict] = []
    for i, s in enumerate(raw_shots, 1):
        if not isinstance(s, dict):
            issues.append(f"shots 第 {i} 项不是对象")
            continue
        chars = _norm_chars(s.get("chars"))
        bad = [c for c in chars if c not in known]
        for b in bad:
            if b not in unknown:
                unknown.append(b)
        if bad:
            issues.append(
                f"shots 第 {i} 镜的 chars 里有不在可用角色清单里的名字：{'、'.join(bad)}"
                "（没有角色卡/参考图的角色不能进 chars，请改成画外音或背影）"
            )
        if not chars:
            issues.append(f"shots 第 {i} 镜（scene={s.get('scene')}）chars 为空")
        if len(chars) > MAX_CHARS_PER_SHOT:
            issues.append(f"shots 第 {i} 镜登场角色 {len(chars)} 个超 {MAX_CHARS_PER_SHOT}（须拆镜）")
        action = str(s.get("action") or "").strip()
        if not action:
            issues.append(f"shots 第 {i} 镜缺少 action（可渲染画面句）")
        sec = _as_int(s.get("sec"))
        if sec is None or not (SEC_MIN <= sec <= SEC_MAX):
            issues.append(f"shots 第 {i} 镜 sec={s.get('sec')!r} 不在 [{SEC_MIN},{SEC_MAX}] 秒内")
            sec = min(SEC_MAX, max(SEC_MIN, sec or 5))
        dialogue = str(s.get("dialogue") or "").strip()
        narration = str(s.get("narration") or "").strip()
        if dialogue and len(dialogue) > DIALOGUE_MAX:
            # 用户要求：单句台词必须 ≤20 字。
            # 但这是**结构性**要求，LLM 一轮重写经常做不到 —— 所以用 __fix__ 档
            # （先给它一次改的机会），仍不改则由 _normalize_rows 按标点确定性拆到连续两镜，
            # 原文一个字都不删。用硬问题会整章判死，那是拿全局换局部，不值。
            issues.append(
                f"__fix__shots 第 {i} 镜台词 {len(dialogue)} 字超过 {DIALOGUE_MAX} 字/句："
                f"{dialogue[:24]}…（请把长对白按语义拆到连续两镜，原文不得删改）"
            )
        speech_n = speech_chars(dialogue, narration)
        if speech_n and speech_n / chars_per_sec > SEC_MAX:
            # 硬约束：单镜最长 15s，装不下的语音必须拆镜（改内容不行，时长也加不上去）。
            # 标成 __fix__：先让 LLM 按语义拆一次；它拆不动还有 _split_speech 兜底。
            issues.append(
                f"__fix__shots 第 {i} 镜台词+旁白 {speech_n} 字约需 {speech_n / chars_per_sec:.1f}s，"
                f"超过单镜上限 {SEC_MAX}s：必须拆成 2 镜（按语义断句），或把部分内容改成画面表达"
            )
        shots.append(
            {
                "scene": _as_int(s.get("scene"), 1) or 1,
                "shot_size": str(s.get("shot_size") or "").strip(),
                "camera": str(s.get("camera") or "").strip(),
                "chars": chars,
                "dialogue": dialogue,
                "narration": narration,
                "sec": int(sec),
                "action": action,
                # conflict：本镜戏剧张力 0-10，供 vm/audit/pacing.py 做渲染前节奏审计。
                # LLM 可能不给或给越界值 —— clamp 到 [0,10]，缺省 None（审计层会标 proxy 模式）
                "conflict": _clamp_conflict(s.get("conflict")),
                "scene_id": str(s.get("scene_id") or "").strip(),
                "prop_ids": [str(x) for x in (s.get("prop_ids") or []) if x],
            }
        )

    # 拆镜密度：过碎 → 合并同场景同角色的相邻镜（硬约束，2026-09-23 用户要求）
    # 原来是 __style__ 软建议，实测 1745 字被拆成 48 镜（平均 36 字/镜）而目标 50-130，
    # 结果片子碎、GPU 时间翻倍。现在按"每 90 字一镜"算出目标镜数，超出 1.5 倍即判不合格。
    if chunk_text and shots:
        avg = len(chunk_text) / len(shots)
        # 目标镜数与 _normalize_rows 保持一致：取"按字数的目标"与"对白句数下限"的较大者。
        # 每镜只承载一句 dialogue，所以对白句数就是镜数下限 —— 不这么算会把
        # 对白密集的章节一律误判成"拆得过碎"，白烧一轮重写。
        by_chars = max(1, round(len(chunk_text) / TARGET_CHARS_PER_SHOT))
        target = max(by_chars, len(chapter_dialogues(chunk_text)))
        if len(shots) > target * 1.5:
            # 同样是 __fix__ 档：先给 LLM 一次合并的机会；它合并不动还有
            # _normalize_rows 的确定性合并兜底，所以不该让整章判死。
            issues.append(
                f"__fix__本章拆得过碎：{len(chunk_text)} 字拆了 {len(shots)} 镜（平均 {avg:.0f} 字/镜），"
                f"目标约 {target} 镜（每镜 {int(TARGET_CHARS_PER_SHOT*0.55)}-{int(TARGET_CHARS_PER_SHOT*1.45)} 字）。"
                "请把同一场景、同一角色的连续动作或对白合并进一镜，"
                "**不要把每一句对白都单独成镜**。"
            )

    # 对白零丢失：正文明写的对白必须全部落进 dialogue（含无角色卡角色的画外音）
    missing = _missing_dialogues(chunk_text, shots) if chunk_text else []
    if missing:
        shown = "；".join(f"「{d[:20]}…」" if len(d) > 20 else f"「{d}」" for d in missing[:6])
        more = f"（共 {len(missing)} 句，仅列前 6 句）" if len(missing) > 6 else ""
        issues.append(
            f"__fix__正文对白被漏掉{more}：{shown}。"
            "每一句对白都必须进某个镜头的 dialogue；没有角色卡的角色（如沙僧）也要保留为画外音，"
            "并在该镜 action 里写明是谁在画外说话。"
        )
    return shots, new_cards, unknown, issues


# 引号单元提取：本作用直引号 "..." 写对白（实测），也兼容中文弯引号
_QUOTE_RE = re.compile(r"[“\"]([^\"”]{2,})[\"”]")
# 言语动词：本作用「道：『…』」的语序（动词在引号前），也兼容动词在后
_SPEECH_VERB_RE = re.compile(r"说|喊|问|道|嚷|叫|念|答|吼|叹|讲|嘀咕|嘟囔|应|骂|喝道|笑道|低声道|高声")
# "未说出口的台词"守卫：只在引号**紧前/紧后**出现意图词时生效。
# 早期版本用「引号后 ±14 字含 没答/没喊」判非对白，把沙僧那句真台词误杀了——
# 因为紧跟引号后的「孙悟空没答话」说的是孙悟空没回答，不是这句没被说出。
_NOT_SPEECH_HEAD_RE = re.compile(r"(?:想|没|未|没有)(?:说|喊|叫|写|念|问|答|讲)[^。！？]{0,3}$")
_NOT_SPEECH_TAIL_RE = re.compile(r"^(?:[，,、]\s*)?(?:想想|却没|但没|又没|没来得及|咽了回去|吞了回去)")
_NORM_SPEECH_RE = re.compile(
    r"[\s\u3000，。！？、；：“”‘’（）《》〈〉【】…—～·,.!?;:'\"()\[\]{}<>|/\\*#`~^&+=@$-]+"
)


def _norm_speech(s: str) -> str:
    """去掉全部标点与空白，只留字，用于对白覆盖比对（标点差异不该算丢台词）。"""
    return _NORM_SPEECH_RE.sub("", s or "")


_SENT_BOUNDARY_RE = re.compile(r"(?<=[。！？!?…])")


def _split_by_punct(text: str, limit: int) -> list[str]:
    """
    按标点把长台词切成每段 ≤limit 字的片段，**一个字都不删**。

    这是"单句 ≤20 字"的确定性兜底：LLM 一轮重写经常改不动（实测 48 镜里 11 镜超标、
    最长 53 字），靠重写只能逼近。切点优先落在标点上（语音自然停顿处），
    实在没有标点的长句才按字数硬切。
    """
    if not text or len(text) <= limit:
        return [text] if text else []
    # 按标点分组：每块 = 一段非标点文字 + 其后跟着的标点串
    parts = re.findall(r"[^，。！？；：、…\s]+[，。！？；：、…]*", text)
    if not parts:
        parts = [text]
    segs: list[str] = []
    cur = ""
    for p in parts:
        if cur and len(cur) + len(p) > limit:
            segs.append(cur)
            cur = p
        else:
            cur += p
    if cur:
        segs.append(cur)
    # 兜底：仍有超长段（无标点长句）→ 硬切
    final: list[str] = []
    for s in segs:
        while len(s) > limit:
            final.append(s[:limit])
            s = s[limit:]
        if s:
            final.append(s)
    return final


def _normalize_rows(
    rows: list[dict],
    target: int,
    log: Any = None,
    stats: dict | None = None,
) -> list[dict]:
    """
    提纲行的确定性后处理，保证两条**结构性**要求真的达标（不靠 LLM 自觉）：

      1. 单句台词 ≤ DIALOGUE_MAX：超长的按标点拆到连续两镜（原文一字不删）
      2. 镜数接近目标：过碎时合并"同场次 + 同角色 + 同景别"的相邻镜

    顺序是先拆后合，且合并时要求"合并后台词仍 ≤20 字"，
    所以不会把刚拆开的长台词又合回去 —— 两步不会互相打架。

    为什么必须在代码里做：这两条都是全局/结构性要求，LLM 一轮重写做不到
    （实测 1745 字：第一稿 48 镜、重写后 35 镜，目标 19）。只靠重写要么整章判死，
    要么永远不达标。
    """
    stats = stats if stats is not None else {}

    # ── 第 1 步：长台词按标点拆到连续两镜 ──
    split_out: list[dict] = []
    n_split = 0
    for r in rows:
        d = r.get("dialogue") or ""
        if len(d) <= DIALOGUE_MAX:
            split_out.append(r)
            continue
        segs = _split_by_punct(d, DIALOGUE_MAX)
        if len(segs) <= 1:
            split_out.append(r)
            continue
        n_split += 1
        for si, seg in enumerate(segs):
            nr = dict(r)
            nr["dialogue"] = seg
            # 时长按该段字数重算（4 字/秒），clamp 到单镜区间
            need = len(seg) / CHARS_PER_SEC
            nr["sec"] = int(min(SEC_MAX, max(SEC_MIN, round(need + 1))))
            if si > 0:
                nr["action"] = (
                    f"{r.get('action','')} Continuation of the same action: "
                    "same framing and camera, the speaker continues."
                )
            split_out.append(nr)

    # ── 第 2 步：过碎则合并相邻同场同角色镜 ──
    merged = list(split_out)
    ceiling = max(1, int(round(target * 1.25)))
    n_merged = 0
    while len(merged) > ceiling:
        pick = -1
        for i in range(len(merged) - 1):
            a, b = merged[i], merged[i + 1]
            if a.get("scene") != b.get("scene"):
                continue
            if set(a.get("chars") or []) != set(b.get("chars") or []):
                continue
            # 注意：**不要求 shot_size 相同**。真实分镜本就逐镜换景别，
            # 早先加了这条约束导致一次都合并不成（实测合并 0 次）。
            # 合并时保留前一镜的景别，视觉上仍然自洽。
            # 合并后台词/旁白都不能超限，否则会把刚拆开的长台词又合回去
            if len((a.get("dialogue") or "") + (b.get("dialogue") or "")) > DIALOGUE_MAX:
                continue
            if len((a.get("narration") or "") + (b.get("narration") or "")) > NARRATION_MAX:
                continue
            if (a.get("sec") or 0) + (b.get("sec") or 0) > SEC_MAX:
                continue
            pick = i
            break
        if pick < 0:
            break  # 找不到可合并的对（宁可保留多几镜，也不破坏场次/角色结构）
        a, b = merged[pick], merged[pick + 1]
        a["dialogue"] = (a.get("dialogue") or "") + (b.get("dialogue") or "")
        a["narration"] = (a.get("narration") or "") + (b.get("narration") or "")
        act_a = (a.get("action") or "").rstrip()
        act_b = (b.get("action") or "").strip()
        if act_a and not act_a.endswith((".", "!", "?", "。")):
            act_a += "."
        a["action"] = f"{act_a} Then: {act_b}".strip()
        a["sec"] = min(SEC_MAX, (a.get("sec") or 0) + (b.get("sec") or 0))
        # 合并后张力取较大者：两段合成一段，高潮不应因合并而被平均掉
        ca, cb = a.get("conflict"), b.get("conflict")
        if ca is not None or cb is not None:
            a["conflict"] = max(ca or 0.0, cb or 0.0)
        merged.pop(pick + 1)
        n_merged += 1

    if log and (n_split or n_merged):
        _log(
            log,
            f"  🧹 确定性规整：长台词拆 {n_split} 镜 → 合并 {n_merged} 次；"
            f"镜数 {len(rows)} → {len(merged)}（目标 {target}）",
        )
    if n_split:
        stats.setdefault("normalized", []).append(f"长台词拆镜 {n_split} 处")
    if n_merged:
        stats.setdefault("normalized", []).append(f"合并相邻镜 {n_merged} 次")
    return merged


def _split_speech(dialogue: str, narration: str, max_speech: float) -> list[tuple[str, str]]:
    """
    按句边界把一句超长台词切成若干段，每段语音量 ≤ max_speech（字数）。

    这是 LLM 没拆干净时的**兜底**：宁可多切两镜，也不能让 H3 念一半就切。
    先按句末标点切，单句仍然超长才按字数硬切（硬切点必然不完美，但不切就是丢内容）。
    """
    text = dialogue or ""
    units = [u for u in _SENT_BOUNDARY_RE.split(text) if u] or ([text] if text else [])
    max_chars = max(1, int(max_speech))
    pieces: list[str] = []
    for u in units:
        while len(u) > max_chars:
            pieces.append(u[:max_chars])
            u = u[max_chars:]
        if u:
            pieces.append(u)
    out: list[tuple[str, str]] = []
    cur = ""
    for u in pieces:
        if cur and speech_chars(cur + u) > max_speech:
            out.append((cur, ""))
            cur = u
        else:
            cur += u
    if cur:
        out.append((cur, ""))
    if out:
        d, _ = out[-1]
        out[-1] = (d, narration or "")  # 旁白挂到最后一段，保持顺序
    elif narration:
        out = [("", narration)]
    return out


def chapter_dialogues(text: str) -> list[str]:
    """
    从正文里抽出"真对白"单元。

    难点是排除非对白引号（本作里有 `门楣上"云隐寺"三个字`、`听到"丈二青面獠牙"`）。
    判据：引号内容带句末标点（。！？…）→ 是对白；否则要求前后 ±14 字内有言语动词。
    只用"言语动词"判据会漏掉本作的写法（`咧嘴一笑："…"`、`A："…"B："…"` 连排），
    实测 29 个引号单元里只能认出 14 个；加上句末标点判据后 27 个真对白全部认出、
    2 个专名提及全部排除。
    """
    out: list[str] = []
    for m in _QUOTE_RE.finditer(text):
        seg = m.group(1).strip()
        if len(re.findall(r"[\u4e00-\u9fff]", seg)) < 2:
            continue
        head = text[max(0, m.start() - 14) : m.start()]
        tail = text[m.end() : m.end() + 14]
        if _NOT_SPEECH_HEAD_RE.search(head) or _NOT_SPEECH_TAIL_RE.search(tail):
            continue
        if re.search(r"[。！？…]", seg) or _SPEECH_VERB_RE.search(head) or _SPEECH_VERB_RE.search(tail):
            out.append(seg)
    return out


def _missing_dialogues(chapter_text: str, rows: list[dict]) -> list[str]:
    """
    正文对白覆盖检查：把各镜 dialogue 按顺序拼起来（去标点）比 substring。

    长对白被拆到连续几镜时拼起来仍能命中；被漏掉、被改写、被截断的会命中失败。
    这条检查的价值在实测中体现：LLM 遇到"没有角色卡的角色"时会把他的台词**整句丢掉**
    （沙僧的「师兄，如何不对？」就这么消失过），而这种丢失在成片里完全无声无息。
    """
    covered = _norm_speech("".join(str(r.get("dialogue") or "") for r in rows))
    missing: list[str] = []
    for d in chapter_dialogues(chapter_text):
        if _norm_speech(d) and _norm_speech(d) not in covered:
            missing.append(d)
    return missing


# ── 阶段② + ③：逐镜六段式 ──────────────────────────────────────────────────


def _camera_phrase(camera: str) -> str:
    """
    从「缓推（Push In, small amplitude, slow speed）」里抽出英文运镜短语。

    抽不到就按中文关键词映射；都没有则返回空（空就不注入纪律行，
    而不是塞一句假运镜——固定镜注入"保持运镜可见"会自相矛盾）。
    """
    text = (camera or "").strip()
    for m in re.finditer(r"[（(]([^）)]*[A-Za-z][^）)]*)[）)]", text):
        phrase = re.sub(r"\s+", " ", m.group(1)).strip()
        if phrase:
            return phrase
    table = (
        ("环绕", "Arc around the subject"), ("升降", "Pedestal"), ("跟", "Tracking"),
        ("推", "Push In"), ("拉", "Pull Out"), ("摇", "Pan"), ("移", "Track"),
        ("固定", "Static"), ("静止", "Static"),
    )
    for cn, en in table:
        if cn in text:
            return en
    return ""


def _position_phrase(chars: list[str]) -> str:
    """站位纪律：通用句（不硬解析 LLM 的英文正文，避免猜错反而制造矛盾）。"""
    if not chars:
        return ""
    return (
        "keep every defined subject at the screen position stated in the description "
        "(left, centre or right, foreground, midground or background) for the whole shot; "
        "do not mirror the composition sideways and do not let a subject drift out of frame"
    )


def _subject_of(card: CharCard) -> str:
    subject, _, _ = parse_portrait_prompt(card.portrait_prompt)
    return subject or ""


def _assemble_prompt(
    *,
    chars: list[str],
    cards: dict[str, CharCard],
    camera: str,
    detailed_description: str,
    overall_soundscape: str,
    non_diegetic_music: str,
    costumes: dict | None = None,
    shot_costume: dict | None = None,
    scene: "SceneCard | None" = None,
    props: list | None = None,
) -> str:
    """
    六段式装配（纯代码）。

    subject_definitions / retention_analysis 里的外貌**逐字**取自角色卡：
    本机 A/B 实测"文字压过参考图"，让 LLM 每镜复述外貌必然逐镜漂移，
    所以这段不允许 LLM 参与，也保证同一角色跨镜逐字一致（NiliX 的刚需）。

    服装（P2，见 vm/costumes.py）：
      - 角色没有服装配置 → 走原路径，`retention_analysis` 保持"服装必须与图一致"（最稳）
      - 角色有服装配置且本镜用的是**默认变体** → 同样保持严格措辞
      - 角色有服装配置且本镜用的是**非默认变体** → 措辞放宽为
        "脸/发型/年龄/性别/体型必须与图一致；**服装以本镜文字为准**"，
        否则模型会跟着参考图走、换装不生效（A/B 实测：文字压过图，前提是要显式声明）
    """
    costumes = costumes or {}
    shot_costume = shot_costume or {}
    use_costumes = bool(costumes.get("characters"))

    defs: list[str] = []
    retention: list[str] = []
    for i, name in enumerate(chars, 1):
        card = cards.get(name)
        variant = costsys.resolve(costumes, name, shot_costume) if use_costumes else None
        identity, clothing = "", ""

        if variant is not None:
            # 有服装配置：身份取配置里的 identity（退役自角色卡拆分），服装取本镜变体
            identity = costsys.identity_of(costumes, name)
            if not identity and card is not None:
                identity = costsys.split_identity_costume(card.appearance or "")[0]
            clothing = str(variant.get("prompt") or "").strip()
            if not clothing and card is not None:
                clothing = (card.costume or "").strip()
        elif card is not None:
            identity = (card.appearance or "").strip().rstrip(".")
            clothing = (card.costume or "").strip()

        if card is None and not identity:
            # 没有卡（配置允许新角色但表里没给外貌）：最保守写法，不编造特征
            defs.append(
                f"<Subject {i}> = the character shown in <Picture {i}>. "
                f"<Picture {i}> supplies the identity and appearance of <Subject {i}>."
            )
            retention.append(
                f"<Subject {i}> ([Shot 1]): fully_preserved - keep the appearance exactly as in <Picture {i}>."
            )
            continue

        subject = _subject_of(card) if card is not None else ""
        if not subject:
            subject = f"the character shown in <Picture {i}>"

        body = identity.strip().rstrip(".")
        if clothing:
            body = f"{body}. {clothing.strip().rstrip('.')}" if body else clothing.strip().rstrip(".")
        line = f"<Subject {i}> = {subject} shown in <Picture {i}>"
        if body:
            line += f": {body}"
        line += f". <Picture {i}> supplies the identity and appearance of <Subject {i}>."
        defs.append(line)

        if variant is not None and not costsys.is_default(variant):
            # 换装镜：脸靠图锁、衣服靠文字
            retention.append(
                f"<Subject {i}> ([Shot 1]): fully_preserved - facial features, hairstyle, age, gender "
                f"and body build must match <Picture {i}> exactly; the CLOTHING is authoritative from "
                f"the wording above and MUST follow it even where it differs from <Picture {i}> "
                f"({variant.get('label') or variant.get('id')}); do not fall back to the clothing seen "
                f"in <Picture {i}>."
            )
        else:
            retention.append(
                f"<Subject {i}> ([Shot 1]): fully_preserved - facial features, hairstyle, age, "
                f"gender and clothing must match <Picture {i}> exactly; the appearance wording above "
                f"is authoritative and identical to the one used to create <Picture {i}>; do not invent "
                f"any feature that is not visible in <Picture {i}>; do not change the clothing colour."
            )

    subjects = ", ".join(f"<Subject {i}>" for i in range(1, len(chars) + 1))
    pictures = ", ".join(f"<Picture {i}>" for i in range(1, len(chars) + 1))
    summary = (
        f"[reference generation] The target video is a single continuous shot of {subjects}. "
        f"The reference still(s) {pictures} provide identity and appearance for the defined "
        f"subject(s); no reference frame is copied as a concrete frame of the target video."
    )

    # 纪律前移：紧贴 detailed_description 段标题前（NiliX 实测段尾纪律服从度弱）
    cam = _camera_phrase(camera)
    pos = _position_phrase(chars)
    if cam:
        if "static" in cam.lower():
            retention.append(
                f"CAMERA DISCIPLINE: this shot's camera performs {cam}; the camera stays locked "
                f"but on-screen character action or environmental motion must keep every second of "
                f"the frame alive"
            )
        else:
            retention.append(
                f"CAMERA DISCIPLINE: this shot's camera performs {cam}; keep that camera movement "
                f"visible from the first frame to the last frame - never settle into a static "
                f"locked-off frame"
            )
    if pos:
        retention.append(f"POSITION DISCIPLINE: {pos}")

    # ★ 场景锚定（2026-09-23）：同场景的每一镜注入**同一段**英文场景描述。
    #   用文字锚定而非参考图：H3 的 <Picture N> 语义是"主体"，场景图不是主体，
    #   塞进去会占掉角色槽位（每镜上限 3 个）。文字没有这个限制。
    dd = detailed_description.strip()
    # 道具锚定：同道具出现的每一镜注入同一段外观描述（避免"金箍棒每镜长得不一样"）
    prop_txt = " ".join(
        f"[PROP — {c.name}] {c.description.strip()}"
        for c in (props or []) if c is not None and (c.description or "").strip()
    )
    if prop_txt:
        dd = prop_txt + " " + dd
    if scene is not None and (scene.description or "").strip():
        # ★ 场景锚定的**从属措辞**（2026-09-23）：
        #   锚定文字是前置的，若不加约束会**压过镜头自己的描述** ——
        #   实测「云隐寺」16 镜里 15 镜其实在殿内，而锚定写的是"山门/院落/杂草"；
        #   更危险的是锚定里的具体物件（断头佛像、香案）会被搬到根本不存在的镜头里。
        #   所以明确定义优先级：**镜头描述说了算，场景串只负责地点/时间/色调的一致性**。
        dd = (
            f"[SETTING — {scene.name} · background reference only] {scene.description.strip()} "
            "(use this setting only for location type, time of day, light direction and colour tone; "
            "the shot description below is authoritative for what actually appears in frame — "
            "do not add setting features that the shot description does not mention) "
            + dd
        )

    return (
        "subject_definitions: " + "\n".join(defs) + "\n"
        "summary: " + summary + "\n"
        "retention_analysis: " + "\n".join(retention) + "\n"
        "detailed_description: " + dd + "\n"
        "overall_soundscape: " + overall_soundscape.strip() + "\n"
        "non_diegetic_music: " + (non_diegetic_music.strip() or "none") + "\n"
    )


_D_TAG_RE = re.compile(r"<d>(.*?)</d>", re.S)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_SUBJECT_REF_RE = re.compile(r"<Subject\s+(\d+)>")
_PICTURE_REF_RE = re.compile(r"<Picture\s+(\d+)>")
# 这两类硬问题可以机械修复（把原文补/纠正到 <d> 里），其余不行
_REPAIRABLE_PREFIXES = ("台词没有逐字出现在", "旁白没有逐字出现在", "detailed_description 没有引用")


def check_dialogue_tags(prompt: str, dialogue: str) -> list[str]:
    """
    校验「台词只出现在 <d>…</d> 里」。返回问题列表（空 = 通过）。

    为什么需要（这是调研条目 **A4 的正确形态**）：
    A4 原文来自 wind-comic："台词原文不进主 visualPrompt"，防「AI 把台词画成乱码字」。
    但**我们的模型是 H3，`<d>…</d>` 正是它的原生配音语法** —— 照搬把台词移出画面段，
    会连配音机制一起砍掉（角色就不说话了）。
    实测本片 6 个有台词镜头抽中间帧检查：**画面干净、零乱码字**，
    说明 H3 理解 `<d>` 是"要说的话"，不是"要画的字"。

    所以正确做法是**保留 `<d>`，但加一道确定性校验**：
      ① 台词必须至少出现在一个 `<d>` 里（否则配音会漏）
      ② 台词与 `<d>` 里的原文必须逐字一致
      ③ 台词**不得**在 `<d>` 之外再次出现（那才会被当成画面内容画出来）
    纯字符串比对、零成本，可进 CI 门禁。
    """
    issues: list[str] = []
    d = (dialogue or "").strip()
    if not d:
        return issues
    tags = re.findall(r"<d>(.*?)</d>", prompt or "", re.S)
    if not tags:
        issues.append(f"台词 {d[:20]!r} 没有出现在任何 <d></d> 里 —— 角色不会说话")
        return issues
    if not any(d in t.strip() for t in tags):
        issues.append(f"台词 {d[:20]!r} 与 <d> 里的原文不逐字一致")
    outside = re.sub(r"<d>.*?</d>", "", prompt or "", flags=re.S)
    if d[:8] in outside:
        issues.append(f"台词 {d[:20]!r} 在 <d> 之外再次出现 —— 会被当成画面内容画成乱码字")
    return issues


def _repair_missing_subjects(row: dict, payload: dict) -> list[str]:
    """
    机械补齐缺失的 <Subject k> 引用。

    为什么需要：镜头在 chars 里声明了角色，却不在 detailed_description 里引用
    <Subject N>，参考图就**完全不起作用**（等于白挂），角色形象会跨镜漂移。
    但确实存在合理的"人不在画面里"的镜头（道具特写、空镜），连写两稿都不引用
    是正确镜头语言 —— 所以不能把整章判死。

    做法：两稿之后仍缺，就补一句中性的在场绑定（只声明"在画面内"与朝向，
    **不编造动作**，具体表演仍由正文主体决定）。补过的镜头会记进
    stats["repaired"]，人工审片时一眼能看到哪几镜被动过。
    """
    dd = str(payload.get("detailed_description") or "")
    n = len(row.get("chars") or [])
    if not n:
        return []
    used = {int(m.group(1)) for m in _SUBJECT_REF_RE.finditer(dd)}
    missing = [k for k in range(1, n + 1) if k not in used]
    if not missing:
        return []
    adds = " ".join(
        f"<Subject {k}> (S{k}) remains within the frame." for k in missing
    )
    payload["detailed_description"] = (dd.rstrip() + " " + adds).strip()
    return [f"补挂 {', '.join(f'<Subject {k}>' for k in missing)} 引用（否则参考图不生效）"]


def _repair_missing_lines(row: dict, payload: dict) -> list[str]:
    """
    机械修复"台词/旁白没进 <d>"：能对上就替换回原文，对不上就补一句。

    为什么要有这个兜底：48 镜里只要有一镜被 LLM 反复写歪，整章拆镜就全废
    （实测 1-24-01 的「师父放心。」连写两稿都没进去，白烧一次 3 分钟 + 95k tokens）。
    修复是**保内容的**：台词还是正文原文，只是换个位置让它一定被念出来。
    说话人归属按谨慎优先：单角色镜用 <Subject 1>；多角色镜不知道是谁在说，
    就用画外音形式，绝不张冠李戴（沙僧那句被写成唐僧说的教训）。
    """
    dd = payload.get("detailed_description") or ""
    repairs: list[str] = []
    for label, text in (("台词", row.get("dialogue") or ""), ("旁白", row.get("narration") or "")):
        if not text:
            continue
        if any(text in tag for tag in _D_TAG_RE.findall(dd)):
            continue
        target = _norm_speech(text)

        state = {"done": False}

        def _sub(m: re.Match) -> str:
            inner = m.group(1)
            body = re.sub(r"^\s*\[[A-Za-z ]+\]\s*", "", inner)
            body = re.sub(r"^\s*[^:：]{1,14}[:：]\s*", "", body)
            if _norm_speech(body) != target:
                return m.group(0)
            lang = re.match(r"^\s*(\[[A-Za-z ]+\])", inner)
            prefix = (lang.group(1) + " ") if lang else ""
            state["done"] = True
            return f"<d>{prefix}{text}</d>"

        new_dd = _D_TAG_RE.sub(_sub, dd)
        if state["done"]:
            dd = new_dd
            repairs.append(f"{label}标点/前缀已改回原文")
            continue

        if label == "旁白":
            dd = dd.rstrip() + (
                f" The narrator says in an off-screen voiceover: <d>{text}</d> "
                "while the on-screen characters' lips remain completely closed."
            )
        elif re.search(r"off[- ]?screen|画外", row.get("action") or "", re.I) or len(row.get("chars") or []) != 1:
            dd = dd.rstrip() + (
                f" A voice says off-screen: <d>{text}</d> "
                "while the on-screen characters' lips remain completely closed."
            )
        else:
            dd = dd.rstrip() + f" <Subject 1> (S1) says: <d>{text}</d>"
        repairs.append(f"{label}整句缺失，已补回（说话人请人工确认）")

    payload["detailed_description"] = dd
    return repairs


def _shot_prompt_issues(row: dict, payload: dict, cards: dict[str, CharCard]) -> list[str]:
    """
    校验阶段②输出（硬问题 → 重写；软问题由调用方降级成 warning）。

    硬问题都能在"烧 GPU 之前"确定会劣化结果：
      台词没进 <d> → H3 不念或念错；<Subject N> 缺/越界 → 参考图不生效（回到脸崩）。
    """
    issues: list[str] = []
    dd = str(payload.get("detailed_description") or "").strip()
    sound = str(payload.get("overall_soundscape") or "").strip()
    if not dd:
        return ["detailed_description 为空"]
    if not sound:
        issues.append("overall_soundscape 为空（写环境音；确实没有也要写 room tone 之类）")

    n = len(row["chars"])
    used = {int(m.group(1)) for m in _SUBJECT_REF_RE.finditer(dd)}
    missing = [k for k in range(1, n + 1) if k not in used]
    if missing:
        # 硬约束（2026-09-23 用户要求）：声明了角色就必须引用 <Subject N>，
        # 否则参考图完全不起作用（等于白挂），角色形象跨镜漂移。
        # 但确实存在合理的"人不在画面里"的镜头（道具特写/空镜），连写两稿都不引用
        # 是正确镜头语言，不该把整章判死 —— 所以配了机械兜底
        # `_repair_missing_subjects`：两稿之后仍缺就补一句中性绑定，保证参考图生效。
        issues.append(
            "detailed_description 没有引用 "
            + ", ".join(f"<Subject {k}>" for k in missing)
            + f"（本镜 {n} 个登场角色，参考图只有在正文引用 <Subject N> 时才生效）"
        )
    over = sorted(k for k in used if k > n)
    if over:
        issues.append(
            "detailed_description 引用了不存在的 "
            + ", ".join(f"<Subject {k}>" for k in over)
            + f"（本镜只有 {n} 个 <Subject>）"
        )
    bad_pics = sorted({int(m.group(1)) for m in _PICTURE_REF_RE.finditer(dd)} - set(range(1, n + 1)))
    if bad_pics:
        issues.append("detailed_description 引用了越界的 <Picture>：" + ", ".join(map(str, bad_pics)))

    dtags = _D_TAG_RE.findall(dd)
    for label, text in (("台词", row["dialogue"]), ("旁白", row["narration"])):
        if not text:
            continue
        if not any(text in tag for tag in dtags):
            issues.append(f"{label}没有逐字出现在 <d> 标签里：{text[:30]}（禁止改写/加标点）")
        if dd.count(text) > 1:
            issues.append(f"{label}在提示词里出现了 {dd.count(text)} 次（同一句只能出现一次）")

    # 画外音归属：action 说了 off-screen，就不能写成画面 <Subject N> 在说
    # （实测踩过：沙僧的画外音被写成 唐僧 <Subject 1> says，成片会变成唐僧说沙僧的台词）
    if row["dialogue"] and re.search(r"off[- ]?screen|画外", row["action"], re.I):
        if re.search(r"<Subject\s+\d+>[^.<]{0,80}?says", dd):
            issues.append(
                "action 指明这句台词是画外音（off-screen），但 detailed_description 写成了 "
                "<Subject N> says：请改成「稳定音色描述 + (Sx)」的画外音写法，并写明画面角色嘴闭合"
            )

    # 软问题：六段式只允许 <d> 内与画面可见文字是中文
    outside = _D_TAG_RE.sub("", dd)
    if _CJK_RE.search(outside):
        issues.append("__soft__detailed_description 的 <d> 之外出现了中文字符（六段式正文要求全英文）")
    return issues


def _shot_user_prompt(
    *,
    row: dict,
    cards: dict[str, CharCard],
    style: str,
    prev_row: dict | None,
    feedback: str = "",
) -> str:
    char_lines = []
    for i, name in enumerate(row["chars"], 1):
        card = cards.get(name)
        if card:
            char_lines.append(f"<Subject {i}> = {name}（外貌见角色卡，不要在正文里重写）: {card.appearance}")
        else:
            char_lines.append(f"<Subject {i}> = {name}（无文字卡，外貌交给 <Picture {i}>，不要编造）")
    prev_txt = ""
    if prev_row:
        prev_txt = (
            f"上一镜画面：{prev_row['action']}\n"
            f"上一镜收尾：{prev_row.get('_tail') or '(无)'}\n"
            "本镜开头应自然承接上一镜的构图与人物位置（约 1 秒），不要凭空换构图。"
        )
    part = f"""【本镜分镜行】
scene={row['scene']} | shot_size={row['shot_size']} | camera={row['camera']} | sec={row['sec']}s
chars={row['chars']}
dialogue={row['dialogue'] or '(无台词)'}
narration={row['narration'] or '(无旁白)'}
action={row['action']}

【本镜角色顺序 → Subject 编号】
{chr(10).join(char_lines) or '(无)'}

【全局风格句（detailed_description 开头用它起一句）】
{style}

【参考图绑定】<Picture 1..{len(row['chars'])}> 依次对应上面角色；外貌/服装完全由参考图与角色卡决定。

【上一镜衔接】
{prev_txt or '(本镜是本章第一镜，直接建立开场构图)'}

【输出 JSON】
{{"detailed_description": "...", "overall_soundscape": "...", "non_diegetic_music": "..."}}"""
    if feedback:
        part += f"\n\n【上一版不合格，必须修正】\n{feedback}"
    return part


def _gen_shot_payload(
    cfg: dict,
    *,
    row: dict,
    cards: dict[str, CharCard],
    style: str,
    prev_row: dict | None,
    log: Any,
    stats: dict,
) -> dict:
    """阶段②：写一镜正文，不合格按意见重写 1 轮。"""
    feedback = ""
    last_issues: list[str] = []
    for round_no in (1, 2):
        obj, meta = _llm_json(
            cfg,
            SHOT_SYSTEM,
            _shot_user_prompt(row=row, cards=cards, style=style, prev_row=prev_row, feedback=feedback),
            temperature=float((cfg.get("llm") or {}).get("temperature", DEFAULT_TEMPERATURE)) if round_no == 1 else 0.2,
            log=log,
            tag=f"镜 {row['_id']} 第{round_no}稿",
        )
        stats["llm_calls"] += 1
        _accumulate_usage(stats, meta)
        payload = {
            "detailed_description": str(obj.get("detailed_description") or "").strip(),
            "overall_soundscape": str(obj.get("overall_soundscape") or "").strip(),
            "non_diegetic_music": str(obj.get("non_diegetic_music") or "").strip(),
        }
        issues = _shot_prompt_issues(row, payload, cards)
        hard = [i for i in issues if not i.startswith("__soft__")]
        soft = [i[8:] for i in issues if i.startswith("__soft__")]
        if not hard:
            stats["warnings"].extend(f"镜 {row['_id']}：{s}" for s in soft)
            return payload
        last_issues = hard
        last_payload = payload
        feedback = "\n".join(f"- {i}" for i in hard)
        if round_no == 1:
            stats["warnings"].extend(f"镜 {row['_id']}：{s}" for s in soft)
            _log(log, f"    ↻ 镜 {row['_id']} 第 1 稿不合格，按意见重写：{'；'.join(hard)[:160]}")
    stats["rewrites"] += 1
    # 最后一档兜底：只有"台词没进 <d>"这类可机械修复的问题时，修好继续跑，
    # 并把修复记录进 stats（人工审片时能一眼看到哪几镜被动过）。
    # 其余硬问题（空段、越界 Subject、重复台词…）仍然报错，不静默放行。
    if last_issues and all(i.startswith(_REPAIRABLE_PREFIXES) for i in last_issues):
        repairs = _repair_missing_lines(row, last_payload)
        repairs += _repair_missing_subjects(row, last_payload)
        stats["repaired"].append(f"镜 {row['_id']}：{'；'.join(repairs)}")
        stats["warnings"].append(f"镜 {row['_id']} 提示词经机械修复：{'；'.join(repairs)}")
        _log(log, f"    🔧 镜 {row['_id']} 重写 2 稿仍有问题，已机械修复：{'；'.join(repairs)}")
        return last_payload
    # 报错时带上最后一稿正文（截断），否则用户只知道"不合格"却看不到模型写了什么
    raise PlanError(
        f"镜 {row['_id']} 提示词重写 1 轮后仍不合格：\n"
        + "\n".join(f"  - {i}" for i in last_issues)
        + f"\n  最后一稿 detailed_description（前 400 字）：{last_payload['detailed_description'][:400]}"
    )


def _accumulate_usage(stats: dict, meta: dict) -> None:
    # setdefault：调用方若没预置 usage（例如只做单步抽取的脚本），
    # 不该因为"统计字段不存在"就让整个抽取失败 —— 统计是附属品，不是前置条件。
    acc = stats.setdefault("usage", {})
    usage = meta.get("usage") or {}
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        v = usage.get(k)
        if isinstance(v, int):
            acc[k] = acc.get(k, 0) + v
    stats["model"] = meta.get("model", stats.get("model", DEFAULT_MODEL))


# ── 主入口 ──────────────────────────────────────────────────────────────────


def plan_chapter(
    proj: Project,
    chapter_path: Path,
    cfg: dict,
    log: Callable[[str], None] | Any = None,
) -> tuple[list[Shot], list[CharCard], dict]:
    """
    小说章节 → (镜头表, 角色卡, 统计)。写盘到 proj.shots_dir / proj.prompts_dir。

    cfg 可以是整个 project.json（取 cfg["llm"]），也可以带 cfg["plan"] 覆盖拆镜参数：
      chapter / seed / style / chars_per_sec / shots_file / allow_new_characters / max_shots
    """
    t0 = time.time()
    proj = proj.ensure()
    chapter_path = Path(chapter_path)
    pcfg = cfg.get("plan") if isinstance(cfg.get("plan"), dict) else {}
    if not chapter_path.is_file():
        raise PlanError(f"读不到章节正文：{chapter_path}")
    chapter_text = chapter_path.read_text(encoding="utf-8").strip()
    if not chapter_text:
        raise PlanError(f"章节正文为空：{chapter_path}")

    title = chapter_path.stem
    m = re.search(r"^(第[零一二两三四五六七八九十\d]+[章回])\s*(.*)$", chapter_text.splitlines()[0].strip())
    if m:
        title = (m.group(1) + " " + m.group(2)).strip()

    allow_new = bool(pcfg.get("allow_new_characters", False))
    chars_per_sec = float(pcfg.get("chars_per_sec", CHARS_PER_SEC))
    base_seed = _as_int(pcfg.get("seed", cfg.get("seed", 3100)), 3100) or 3100
    ch_no = chapter_number(chapter_path, cfg)

    # ── 角色卡：已有卡是权威，LLM 不许改外貌 ──
    locked = load_char_cards(proj)
    refs_dir = Path(proj.refs_dir)
    ref_names = sorted(p.name[len("char_") : -len(".png")] for p in refs_dir.glob("char_*.png")) if refs_dir.is_dir() else []
    if not locked and not ref_names:
        _log(log, "  ⚠️ 没有发现任何角色卡或参考图：chars 只能由 LLM 新建角色（质量不可控）")
    for name in ref_names:
        if name not in locked:
            _log(log, f"  ⚠️ 角色「{name}」有参考图但没有 prompts/char_{name}.txt："
                      "文字外貌无法与参考图对齐（文字会压过参考图），建议先补角色卡")
    # ── style 解析必须在 stats 之前 ──
    # stats 的字面量里要用到 style（`"style": style`），所以顺序不能反。
    # （第一版把 stats 提到前面，结果 UnboundLocalError: style referenced before assignment。）
    # 顺序：项目级显式配置 → "auto" 自动推导 → 已有卡的风格 → 默认
    _style_cfg = str(pcfg.get("style") or cfg.get("style") or "").strip()
    if _style_cfg.lower() == "auto":
        # 自动推导需要一个可写的 stats 容器（它会计 token 与告警）——
        # 这里先建 stats，推完再把 style 回填进去。
        stats: dict[str, Any] = {}
        style = (_derive_style(cfg, chapter_text, log, stats)
                 or _card_style(locked.values())
                 or _style.get(_preset_of(cfg, pcfg))["sentence"])
    else:
        stats = {}
        style = (_style_cfg or _card_style(locked.values())
                 or _style.get(_preset_of(cfg, pcfg))["sentence"])

    stats.update({
        "chapter": str(chapter_path),
        "title": title,
        "chapter_no": ch_no,
        "chapter_chars": len(chapter_text),
        "model": str((cfg.get("llm") or {}).get("model") or DEFAULT_MODEL),
        "style": style,
        "locked_characters": sorted(locked),
        "allowed_characters": [],
        "new_characters": [],
        "unknown_characters": [],
        "chars_per_sec": chars_per_sec,
        "shots": 0,
        "llm_calls": 0,
        "rewrites": 0,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "sec_adjusted": [],
        "auto_split": [],
        "repaired": [],
        "warnings": [],
        "ref_warnings": [],
        "elapsed_sec": 0.0,
    })


    # ── 角色卡补齐（allow_new_characters）────────────────────────────────
    # 为什么把"建卡"拆成独立一步：让拆镜表 LLM 顺手建卡时，它会把"没有卡的配角"
    # 当成画外音/背景糊掉（实测把沙僧的台词整句丢了、还让他只当模糊剪影）。
    # 先专门抽一次全章角色 → 新角色立刻有卡有参考图 → 拆镜时就能正常让他出镜。
    cards: dict[str, CharCard] = dict(locked)
    if allow_new:
        bg = str(pcfg.get("portrait_background") or _card_background(locked.values(), _preset_of(cfg, pcfg)))
        for c in _gen_new_cards(chapter_text, locked, cfg, log, stats):
            cards[c["name"]] = CharCard(
                name=c["name"],
                appearance=c["appearance"],
                costume=c["costume"],
                portrait_prompt=_build_portrait_prompt(c["name"], c["appearance"], c["costume"], style, bg,
                                                               _preset_of(cfg, pcfg)),
            )
            stats["new_characters"].append(c["name"])
    # 有参考图但没有文字卡的角色：中立卡（外貌交给参考图，文字不描述 → 文字压不过图）
    for name in ref_names:
        if name not in cards:
            cards[name] = CharCard(
                name=name, appearance="", costume="",
                portrait_prompt=_build_portrait_prompt(
                    name, "", "", style,
                    str(pcfg.get("portrait_background")
                        or _card_background(locked.values(), _preset_of(cfg, pcfg))),
                ),
            )
    allowed = sorted(cards)
    stats["allowed_characters"] = allowed
    _log(log, f"  📖 《{title}》{len(chapter_text)} 字，章节号 {ch_no}；"
              f"锁定角色 {len(locked)} 个" + (f"，新建角色 {stats['new_characters']}" if stats["new_characters"] else "") +
              f"，可用角色 {len(allowed)} 个")

    # ── 阶段①.5：抽场景实体（跨镜场景一致性的锚点）──
    # 数据里的 `scene` 只是场次号（实测 52 镜分成 47 个场次，几乎每镜一个），
    # **"场景"这一层从来没真正建立过** —— 同一间破庙的墙/光/色调每镜各写各的。
    # 这里抽出真正的地点，之后同 scene_id 的镜头注入同一段场景描述。
    scenes = _extract_scenes(cfg, chapter_text, log, stats)
    scene_map = {c.id: c for c in scenes}
    if scenes:
        sp = save_scenes(proj, scenes)
        _log(log, f"  🏞 场景实体 {len(scenes)} 个：" +
             "、".join(f"{c.id} {c.name}" for c in scenes) + f" → {sp.name}")
    else:
        _log(log, "  ⚠️ 未抽出场景实体（plan 继续，但没有跨镜场景锚定）")
    stats["scenes"] = [{"id": c.id, "name": c.name} for c in scenes]

    # ── 阶段①.6：抽关键道具实体 ──
    props = _extract_props(cfg, chapter_text, log, stats)
    prop_map = {c.id: c for c in props}
    if props:
        pp = save_props(proj, props)
        _log(log, f"  🗡 关键道具 {len(props)} 个：" +
             "、".join(f"{c.id} {c.name}" + ("(推断)" if c.inferred else "") for c in props) + f" → {pp.name}")
    else:
        _log(log, "  ⚠️ 未抽出关键道具")
    stats["props"] = [{"id": c.id, "name": c.name, "inferred": c.inferred} for c in props]

    # ── 阶段①：分块拆镜表 ──
    chunks = _chunk_text(chapter_text)
    shots_rows: list[dict] = []
    scene_offset = 0
    for ci, chunk in enumerate(chunks, 1):
        _log(log, f"  ✂️ 拆镜第 {ci}/{len(chunks)} 块（{len(chunk)} 字）…")
        feedback = ""
        rows: list[dict] = []
        cards_out: list[dict] = []
        unknown: list[str] = []
        issues: list[str] = []
        style_issues: list[str] = []
        for round_no in (1, 2):
            obj, meta = _llm_json(
                cfg,
                TABLE_SYSTEM,
                _table_user_prompt(
                    title=title, chapter_text=chunk, chunk_no=ci, chunk_total=len(chunks),
                    cards=cards, allowed=allowed, style=style,
                    unknown_so_far=stats["unknown_characters"], allow_new=allow_new,
                    feedback=feedback, scenes=scenes, props=props,
                ),
                temperature=DEFAULT_TEMPERATURE if round_no == 1 else 0.2,
                log=log, tag=f"拆镜表 块{ci} 第{round_no}稿",
            )
            stats["llm_calls"] += 1
            _accumulate_usage(stats, meta)
            # allow_new=False：角色卡已在阶段①之前建好（含新角色），所以这里
            # 任何"不在可用角色清单"的名字都是真的没卡，必须改写
            rows, cards_out, unknown, issues = _validate_table(
                obj, set(allowed), False, chunk_text=chunk, chars_per_sec=chars_per_sec
            )
            # 只要 LLM 提到"没卡的角色"（不管是用了他的名字，还是主动声明他无卡），
            # 就当场补卡 —— 然后逼它下一稿把有戏份的角色正常写进 chars。
            # 为什么这么绕：拆镜 LLM 对"没有卡的配角"有强烈的降级倾向（把沙僧写成画外音
            # 或模糊剪影），而 Lead 的裁决是"不要为了工具限制扭曲内容"。补卡是唯一的正解。
            if allow_new:
                need_cards = [n for n in unknown if n not in cards]
                if need_cards:
                    added, alias_map = _add_cards_for(
                        need_cards, chapter_text, cards, cfg, style, bg, log, stats
                    )
                    if alias_map:
                        for r in rows:
                            r["chars"] = [alias_map.get(n, n) for n in r["chars"]]
                    if added or alias_map:
                        allowed = sorted(cards)
                        stats["allowed_characters"] = allowed
                        # 同一份 LLM 输出重校验（不多花网络往返），再补一条硬意见逼它重写
                        rows, cards_out, unknown, issues = _validate_table(
                            obj, set(allowed), False, chunk_text=chunk, chars_per_sec=chars_per_sec
                        )
                        for n in added:
                            # 用 __fix__ 而不是硬问题：卡已经补好了（交付物到手），
                            # 这一稿没让他出镜只是"不理想"，不该让整章拆镜失败
                            issues.append(
                                f"__fix__「{n}」有戏份，角色卡已经补好并列入【可用角色】，"
                                f"本稿却仍把他当成无卡角色/画外音：请把他正常写进相关镜头的 chars，让他出镜"
                            )
            hard = [i for i in issues if not i.startswith("__")]
            cover = [i[len("__fix__") :] for i in issues if i.startswith("__fix__")]
            style_issues = [i[len("__style__") :] for i in issues if i.startswith("__style__")]
            if not hard and not cover:
                break
            if round_no == 1:
                # 硬问题 / 对白丢失必须重写；纯风格建议不触发重写（模型可能越改越碎）
                stats["rewrites"] += 1
                feedback = "\n".join(f"- {i}" for i in (hard + cover))
                _log(log, f"    ↻ 块 {ci} 第 1 稿有问题，按意见重写：{'；'.join((hard + cover))[:160]}")
                continue
            if hard:
                raise PlanError(
                    f"拆镜表重写 1 轮后仍不合格（第 {ci} 块）：\n"
                    + "\n".join(f"  - {i}" for i in hard)
                )
            # 对白丢失重写后仍存在：内容缺失必须让用户看见，但不阻断（镜头表可手改）
            stats["warnings"].extend(f"第 {ci} 块（重写后仍未解决）：{s}" for s in cover)
            _log(log, f"    ⚠️ 块 {ci} 重写后仍有对白未入镜：{cover[0][:100]}")
            break
        stats["warnings"].extend(f"第 {ci} 块：{s}" for s in style_issues)

        for u in unknown:
            if u not in stats["unknown_characters"]:
                stats["unknown_characters"].append(u)
        for c in cards_out:
            if c["name"] not in stats["unknown_characters"]:
                stats["unknown_characters"].append(c["name"])

        # 场次号跨块累进（分块只是输出尺寸手段，不该造成 id 冲突）
        if rows:
            chunk_scene_max = max(r["scene"] for r in rows)
            for r in rows:
                r["scene"] += scene_offset
            scene_offset += chunk_scene_max
        shots_rows.extend(rows)

    max_shots = _as_int(pcfg.get("max_shots"))
    if max_shots and max_shots < len(shots_rows):
        _log(log, f"  ⚠️ max_shots={max_shots} 生效：只保留前 {max_shots} 镜（调试用，统计里已记录）")
        stats["warnings"].append(f"max_shots={max_shots} 截断了拆镜结果（调试开关）")
        shots_rows = shots_rows[:max_shots]

    # ── 确定性规整：长台词拆到连续两镜 + 过碎则合并（保证"≤20 字/句"与镜数达标）──
    # 放在语音拆镜兜底之前：先按语义把长台词切好，后面那步就基本不会被触发。
    #
    # ★ 目标镜数必须取"按字数的目标"与"对白句数下限"的较大者。
    #   每个镜头只能承载一句 dialogue，所以镜数下限 = 对白句数。
    #   本章 1676 字 / 90 = 19 镜，但有 31 句对白 → 真实下限 31 镜。
    #   早先只按字数算目标（19），导致"合并到 19 镜"与"单句 ≤20 字"直接冲突，
    #   合并一次都做不成（实测合并 0 次、镜数反而 36→47）。
    total_chars = len(re.sub(r"\s+", "", chapter_text))
    target_by_chars = max(1, round(total_chars / TARGET_CHARS_PER_SHOT))
    target_shots = max(target_by_chars, len(chapter_dialogues(chapter_text)))
    shots_rows = _normalize_rows(shots_rows, target_shots, log=log, stats=stats)

    # ── 兜底：语音装不进单镜上限的镜，按句边界自动拆 ──
    # LLM 重写后仍可能留一两镜超长；这里不阻断、不改台词，只把一句话切成连续几镜。
    split_rows: list[dict] = []
    for r in shots_rows:
        speech_n = speech_chars(r["dialogue"], r["narration"])
        if speech_n and speech_n / chars_per_sec > SEC_MAX:
            parts = _split_speech(r["dialogue"], r["narration"], SEC_MAX * chars_per_sec)
            stats["auto_split"].append(
                f"{r['scene']} 场某镜 {speech_n} 字（约 {speech_n / chars_per_sec:.1f}s）→ 拆 {len(parts)} 镜"
            )
            _log(log, f"  ✂️ 语音超 15s 自动拆镜：{speech_n} 字 → {len(parts)} 镜")
            for d, n in parts:
                nr = dict(r)
                nr["dialogue"], nr["narration"] = d, n
                nr["sec"] = max(
                    SEC_MIN,
                    min(SEC_MAX, int(math.ceil(speech_chars(d, n) / chars_per_sec)) or SEC_MIN),
                )
                split_rows.append(nr)
        else:
            split_rows.append(r)
    shots_rows = split_rows

    # 调试开关：只看拆镜表、不花钱写逐镜提示词（审片/调拆镜提示词时用）
    if pcfg.get("table_only"):
        _log(log, "  🧪 table_only 生效：只产出拆镜表，跳过逐镜六段式与写盘（调试模式）")
        stats["table_debug"] = [
            {k: v for k, v in r.items() if not k.startswith("_")} for r in shots_rows
        ]
        stats["shots"] = len(shots_rows)
        stats["characters"] = sorted(cards)
        stats["elapsed_sec"] = round(time.time() - t0, 1)
        return [], [cards[n] for n in sorted(cards)], stats

    # ── 阶段②+③：逐镜提示词 ──
    # 服装配置（P2）：首次使用时从角色卡引导出 `costumes.json`（幂等，已存在就不动）。
    # 引导时把角色卡的「外貌+服装」拆成 identity + default 变体；
    # 组装回 subject_definitions 时是 identity + ". " + clothing，
    # 与原来的整段 appearance 逐字相同 —— 所以**没有多套服装的项目行为完全不变**。
    costumes = costsys.ensure_from_cards(proj, cards)
    stats["costumes"] = {
        name: [v.get("id") for v in costsys.variants_of(costumes, name)]
        for name in sorted(cards)
    }
    if any(len(costsys.variants_of(costumes, n)) > 1 for n in cards):
        _log(log, f"  👗 服装变体已启用：{costsys.path_of(proj)}")

    shots: list[Shot] = []
    for idx, row in enumerate(shots_rows):
        # 同一场内从 01 起递增，符合「场次-镜号」直觉
        same_scene_before = sum(1 for r in shots_rows[: idx + 1] if r["scene"] == row["scene"])
        shot_id = f"{ch_no}-{row['scene']}-{same_scene_before:02d}"
        row["_id"] = shot_id

        sec, note = fit_sec_to_speech(
            row["sec"], row["dialogue"], row["narration"], chars_per_sec=chars_per_sec
        )
        if note:
            stats["sec_adjusted"].append(f"镜 {shot_id}：{note}")
            row["sec"] = sec
        row["sec"] = max(SEC_MIN, min(SEC_MAX, row["sec"]))

        _log(log, f"  🎬 {shot_id}（{row['sec']}s，{row['shot_size'] or '?'}，"
                  f"{'、'.join(row['chars'])}）写六段式…")
        payload = _gen_shot_payload(
            cfg, row=row, cards=cards, style=style,
            prev_row=shots_rows[idx - 1] if idx > 0 else None, log=log, stats=stats,
        )
        prompt = _assemble_prompt(
            chars=row["chars"], cards=cards, camera=row["camera"],
            detailed_description=payload["detailed_description"],
            overall_soundscape=payload["overall_soundscape"],
            non_diegetic_music=payload["non_diegetic_music"],
            costumes=costumes, shot_costume=row.get("costume") or {},
            scene=scene_map.get(str(row.get("scene_id") or "")),
            props=[prop_map[p] for p in (row.get("prop_ids") or []) if p in prop_map],
        )
        # 存上一镜收尾，供下一镜链式衔接
        row["_tail"] = payload["detailed_description"][-220:]
        shots.append(
            Shot(
                id=shot_id,
                sec=row["sec"],
                chars=row["chars"],
                seed=base_seed + idx,
                prompt=prompt,
                shot_size=row["shot_size"],
                camera=row["camera"],
                dialogue=row["dialogue"],
                narration=row["narration"],
                costume=dict(row.get("costume") or {}),
                conflict=row.get("conflict"),
                scene_id=str(row.get("scene_id") or ""),
                prop_ids=[str(x) for x in (row.get("prop_ids") or []) if x],
            )
        )

    # ── 写盘 ──
    out_name = str(pcfg.get("shots_file") or f"chapter{ch_no:02d}.json")
    shots_path = Path(proj.shots_dir) / out_name

    # ── 覆盖保护（2026-09-24）────────────────────────────────────────────────
    # 为什么必须做：`save_shots` 是原子写但**不备份已有文件**。一个已经渲完 52 镜的
    # 项目，只要在界面上手滑点一次「拆镜」，chapter01.json 就被直接覆盖 ——
    # 52 个指纹全部失配、52 段片段全变「需重渲」，而且**没有任何撤销路径**。
    # 这条是**不可逆**的，触发器却是界面上一个普通按钮，所以按"必要"处理。
    #
    # 策略：**总是先备份**（不阻断正常重跑，只是留后路），
    # 并在备份里带上"当时已渲多少镜"，便于事后判断损失面。
    if shots_path.is_file():
        try:
            n_clips = len(list(Path(proj.clips_dir).glob("*.mp4"))) if Path(proj.clips_dir).is_dir() else 0
            bak_dir = Path(proj.shots_dir) / "_backup"
            bak_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            bak = bak_dir / f"{shots_path.stem}.{stamp}.json"
            _atomic_write_text(bak, shots_path.read_text(encoding="utf-8"))
            (bak_dir / f"{shots_path.stem}.{stamp}.meta.json").write_text(
                json.dumps({"backed_up_at": int(time.time()), "clips_rendered": n_clips,
                            "reason": "plan 覆盖前自动备份"},
                           ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if n_clips:
                _log(log, f"  ⚠️ 覆盖已有镜头表（当时已渲 {n_clips} 段）——已备份到 "
                          f"{bak.relative_to(Path(proj.root))}；"
                          f"重渲前可用它回滚：cp {bak} {shots_path}")
            else:
                _log(log, f"  · 覆盖已有镜头表前已备份 → {bak.name}")
        except OSError as e:
            _log(log, f"  ⚠️ 覆盖前备份失败（继续，但请注意这是不可逆的）：{e}")

    save_shots(shots_path, shots)
    char_paths = write_char_prompts(proj, [cards[n] for n in sorted(cards)])
    _log(log, f"  💾 镜头表 → {shots_path}（{len(shots)} 镜）；角色提示词 {len(char_paths)} 个")

    # ── 剧本视图：由镜头表推导，**不调 LLM**（2026-09-24 改）────────────────
    # 原来剧本层是一条独立的 CLI 旁路：自己调一次 LLM 重抽结构，产出**没有任何消费者**，
    # 于是 UI 那个 tab 永远显示"还没有剧本"。而镜头表里已经有 scene_id/action/dialogue/chars ——
    # 直接推导就能组出同一份视图，**省掉每次几万 token**，
    # 并且它从"旁路产物"变成"plan 结果的可读审计视图"。
    # 这也是"台词零丢失/章节覆盖"校验的落地处：`verify()` 会逐句回正文核对。
    try:
        from vm import script as _SC
        _ch_text = chapter_text or ""
        _sc = _SC.build_from_shots(proj, ch_no, shots, _ch_text, log=log)
        _sp = _SC.save_script(proj, _sc)
        _v = _sc.span_checks or {}
        if _v.get("failed"):
            _log(log, f"  ⚠️ 剧本校验有 {_v['failed']} 句对白在正文里找不到 —— "
                      f"这几句可能被改写过，建议看「剧本」tab 核对")
        stats["script"] = {"file": str(_sp), "scenes": len(_sc.scenes),
                           "beats": sum(len(x.beats) for x in _sc.scenes),
                           "checks": _v}
    except Exception as _e:                                     # noqa: BLE001
        # 剧本视图是**审计**用途，失败不该拖垮拆镜主流程 —— 但要说出来，不静默
        _log(log, f"  ⚠️ 剧本视图生成失败（不影响拆镜）：{type(_e).__name__}: {_e}")
        stats.setdefault("warnings", []).append(f"剧本视图生成失败：{_e}")

    # ── 终检 ──
    problems = validate_shots(shots, refs_dir)
    missing_refs = [p for p in problems if "缺少参考图" in p]
    fatal = [p for p in problems if "缺少参考图" not in p]
    if abs(chars_per_sec - CHARS_PER_SEC) > 1e-9:
        # validate_shots 用 4 字/秒的默认口径；本阶段已按 cfg 的字速延过时长，
        # 口径不同时不要把它的预算告警当成致命错误
        fatal = [p for p in fatal if "台词+旁白" not in p]
    stats["ref_warnings"] = missing_refs
    for p in missing_refs:
        _log(log, f"  ⚠️ {p}（chars 阶段生成后即可渲染）")
    if fatal:
        raise PlanError(
            "拆镜结果未通过终检（镜头表已写盘，便于人工查看）：\n"
            + "\n".join(f"  - {p}" for p in fatal)
        )

    stats["shots"] = len(shots)
    stats["characters"] = sorted(cards)
    stats["shots_file"] = str(shots_path)
    stats["char_prompt_files"] = {k: str(v) for k, v in char_paths.items()}
    stats["elapsed_sec"] = round(time.time() - t0, 1)
    _log(log, f"  ✅ 拆镜完成：{len(shots)} 镜 / {len(cards)} 角色 / "
              f"{stats['llm_calls']} 次 LLM 调用 / {stats['usage'].get('total_tokens', 0)} tokens / "
              f"{stats['elapsed_sec']}s")
    return shots, [cards[n] for n in sorted(cards)], stats
