# 小说→剧集视频：两个开源项目深挖报告

> 调研对象：`chengshudong/storyforge`（Novel2Drama Agent）与 `ChrisChen667788/wind-comic`
> 立场：我们自建 AI 漫剧流水线（小说章节 → DeepSeek 拆镜 → 角色定妆照 → 本地 ComfyUI + MiniMax H3 逐镜渲染 → 机械质检 → concat + 中文字幕，Python，单卡 RTX 5090，极简 Web 控制台）。已有 manifest 三态幂等、渲染断点续跑、抽卡、服装变体、分镜表 JSON。
> **每个结论都标注了依据。标【推断】的是我基于代码的判断，不是项目自己声称的。**

---

## 0. 取证方式与可信度声明

| 项目 | 取证方式 | 实测规模 | 可信度 |
|---|---|---|---|
| storyforge | `git clone --depth 1` **成功**，读真实源码 | 227 个受版本控制文件，2.4MB，纯 Python + Next.js 骨架 | 高（代码级） |
| wind-comic | `git clone` **两次均失败**（下载中途 stall / 对端断连）。改用 GitHub Trees API 取全量文件树（2777 项，866KB JSON，完整未截断）+ `raw.githubusercontent.com` 逐文件拉取 109 个关键源文件 | 2351 个 blob，120MB（含 52MB assets + 21MB videos 媒体）；Next.js 16 + TypeScript | 中高（源码级，但非全量检出；未跑过它的 6010 个测试） |

**明确标注为推断/未能验证的项：**
- wind-comic 声称的「6010 tests green」我**没有运行**（需要 `npm install`，本机未装其依赖）。它 21311 字节的 `types/agents.ts` 里夹带大量「实测」「对抗校验」注释，风格上确实是长期维护的项目，但测试数字本身未经我复核。
- wind-comic 的 `services/hybrid-orchestrator.ts`（**254KB**，全仓最大文件）与 `services/video-composer.ts`（**105KB**）我没有逐行读完——只读了它们的类型契约（`types/agents.ts` 的 `EditResult` 等）和被它们消费的纯函数模块。**这两个巨型文件本身就是它最大的工程债**（见 §7）。
- 两个项目我都**没有实际运行**。所有「行为」结论来自代码静态阅读。

---

## 1. 多智能体架构

### 1.1 两者的架构根本不是一回事

这是本次调研最重要的前置结论：**两个项目的「multi-agent」是两种完全不同的东西。**

- **storyforge 是「真·编排器 + 无状态 Agent 类」**：`LangGraph StateGraph` 做状态机，agent 是无状态的服务类，状态落在 **PostgreSQL**。有 Celery 任务队列。这是标准后端工程做法。
- **wind-comic 是「单一编排器 + 提示词角色」**：8 个 agent 是**同一个 LLM 的 8 段不同 system prompt**，加上一堆确定性纯函数做校验。它的「多智能体」本质是**流程分段 + 契约类型**，没有独立进程/独立状态机。

再叠加一层：wind-comic 仓库里有 **两套并存的 orchestrator**，二者质量差距极大：
- `services/agent-orchestrator.ts`（16KB，**演示版**）：5 个顺序方法 `runDirector → runWriter → runCharacterDesigner → runStoryboardArtist → runVideoProducer`，`startProduction()` 串起来，**所有状态塞在一个 `Map<AgentRole, Agent>` 内存对象里**，无持久化、无断点、无重试。
- `services/hybrid-orchestrator.ts`（**254KB**，**生产版**）：真正跑的那条路。README 的「8 agents」指的是它。

> 依据：`storyforge docs/MASTER_PROMPT.md`、`backend/workflows/*.py`；`wind-comic types/agents.ts` L381-388 注释自陈「病根(🔴-5):hybrid-orchestrator.ts 5471 行 / 113 处 any，其中 6 处在**公开方法签名**上——这是 agent 之间的契约面」。

### 1.2 storyforge：Agent 清单与输入输出契约

**Agent 类（8 个，全部无状态，构造函数注入 `ModelRouter` + `CacheService`）**

| Agent | 文件 | 输入 | 输出 |
|---|---|---|---|
| NovelAgent | `backend/agents/novel_agent.py` | 上传的 TXT/DOCX/EPUB | chapters + entities |
| StoryAgent | `story_agent.py` | chapters | `narrative_summary / protagonist_arc / central_conflict / turning_points` + `timeline / conflicts / relationships / world_setting` |
| EpisodeAgent | `episode_agent.py` | story summary + timeline + chapter_count | `episodes[]`（`episode_number/title/summary/chapter_range/cliffhanger/key_scenes`） |
| SceneAgent | `scene_agent.py` | episode + characters + timeline + world_setting | `scenes[]`（见 §2） |
| CharacterAgent | `character_agent.py` | chapter summaries + relationships + entities + scene characters | `{characters[], issues[]}` |
| ImageAgent | `image_agent.py` | phase + profile/storyboard + seed | ComfyUI assets |
| VoiceAgent | `voice_agent.py` | character + dialogue | wav + metadata |
| VideoAgent | `video_agent.py` | scene + storyboard + character image + voice | mp4 |

**状态怎么传：PostgreSQL + JSONB，不是消息队列也不是文件。**
- 8 张主表：`projects / episodes / scenes / characters / character_versions / props / assets / voices / videos / jobs / logs`（`backend/domain/models.py`）。
- `Scene.storyboard` 是 **JSONB**（`models.py:72`），把整张分镜塞进一列——分镜 schema 没有强类型约束。
- `Project.status` 是一个 13 态枚举驱动全局：`PENDING→PARSING→SUMMARIZING→EPISODES→SCENES→CHARACTERS→ASSETS→VOICE→VIDEO→EDITING→COMPLETED/FAILED/CANCELLED`（`models.py:14-27`）。

**LangGraph 状态对象**（`backend/workflows/state.py`）：`ProjectState` 只是**把 ORM 对象列表打包**（`episodes/scenes/characters/assets/voices/videos/jobs`），`to_dict()` 只吐计数。**注意：每个 workflow 文件自己另定义了一个 dataclass**（`ImageGenerationState` / `VideoGenerationState` / `VoiceGenerationState`），各自带 `phases: list[str]` 字段做「跑哪几段」的开关。这是一个**值得注意的好设计**——见 Top10 #7。

### 1.3 storyforge：LLM / 确定性代码的边界划分

这个边界划得**非常清楚**，而且是显式写在目录结构里的：

```
backend/prompts/
  summary.py  extraction.py  episode.py  scene.py  character.py   ← LLM prompts（5 个）
  image.py  video.py  voice.py                                     ← Deterministic prompt builders（3 个）
```

**判据是「语义判断 vs 字符串拼接」：**
- 需要**理解叙事**的 → LLM。剧本拆分、角色设计、情绪映射、连续性检查。
- 需要**稳定可复现**的 → 纯 Python 字符串拼接，**明确标注 "No LLM involved"**。

`backend/prompts/image.py:39-44` 是最干净的例证：

```python
class CharacterRefPrompt:
    """Deterministic SDXL prompt builder for character reference portraits.
    Phase: char_ref — generates the reference portrait used as InstantID face input.
    No LLM involved; all text is constructed from structured profile data."""
```

它把 `profile["appearance"]` 的 7 个字段用 `_build_physique()` 拼成逗号分隔串（`image.py:4-18`），服装走 `_build_outfit()`（`image.py:21-36`）。**同样的结构化数据，绝不二次问 LLM「帮我写个 SDXL prompt」**。

`backend/prompts/video.py:4-17` 同理，注释直接写明哲学：
> "The IMAGE provides character identity via the keyframe. The prompt describes the **MOTION** and **SCENE CONTEXT**."

**这是我们应该照抄的核心边界。** 我们的分镜表 JSON 已经有 `id/sec/chars/seed/prompt`，但 prompt 是 LLM 给的。storyforge 的做法是：LLM 只产**结构化字段**（camera/emotion/location/props/character_actions），最终送进 SDXL/视频模型的 prompt 由**确定性代码**从这些字段拼。

### 1.4 storyforge：受控词表（抑制 LLM 发散的关键手法）

LLM 输出字段被**硬性枚举**约束（`backend/prompts/scene.py:68-79`）：

| 字段 | 受控词表 | 数量 |
|---|---|---|
| `camera` | wide, medium, close-up, POV, tracking, pan, dutch angle, over-the-shoulder, two-shot, establishing | 10 |
| `emotion` | tense, joyful, melancholic, suspenseful, romantic, ominous, peaceful, chaotic, tender, angry, fearful, hopeful | 12 |
| `transition` | cut, fade, dissolve, match_cut, wipe | 5（默认 cut） |
| `asset_refs` | `SCREAMING_SNAKE_CASE` 符号标识符，如 `BG_OLD_STUDY`, `PROP_LETTER`, `CHAR_JOHN_SITTING` | 自由但格式受限 |

`asset_refs` 这个设计很聪明：**LLM 只负责声明「这里需要什么资产」，用符号名占位，资产由后续阶段解析生成**。这是解耦叙事与资产的干净做法。

### 1.5 wind-comic：8 个 Agent 角色与契约

`types/agents.ts:1-11` 定义 `AgentRole` 枚举：

```
DIRECTOR  · 总控，连线到每个环节
WRITER    · 编剧
CHARACTER_DESIGNER  · 角色设计师
SCENE_DESIGNER      · 场景设计师
STORYBOARD          · 分镜师
VIDEO_PRODUCER      · 视频制作
EDITOR              · 剪辑师
PRODUCER            · 制片人（最终审核、成片确认）
```

**契约是 TypeScript interface，逐 agent 定死**（`types/agents.ts:390-472`，v12.225 专门做的一刀「神类拆分」）：

| 产物类型 | 关键字段 |
|---|---|
| `EditResult` | `timeline[{shotNumber, videoUrl, duration, baseDuration, transition, transitionDurationS, effect, emotion, act, dialogue, speaker, emotionTemperature, tensionLevel}]` + `totalDuration` + `designedTotalDuration` + `finalVideoUrl` + `musicUrl` + `voiceoverClips[]` + `highlightAnalysis[]` + `audioWarnings[]` + `qualityReport{healthScore, degradedShots[], shotReasons}` |
| `CharacterDesignerResult` | `character / prompt / imageUrl`（+ 可选 name/description/appearance） |
| `SceneDesignerResult` | `sceneId / name / description / imageUrl` |
| `DirectorReview` | `overallScore / summary / items[] / status / passed` + `producerReports{continuityFlags[], assetLedger, rhythmReport, runtimeReport, characterBibleSize}` |
| `GateResult` | `{action: 'continue'\|'edit', editedData}` ← **人工闸门的返回契约** |

**这组契约的注释值得单独看**：`types/agents.ts:318-325` 记录了为什么 `ProjectId` 改可选、为什么补了 `'passed'` 状态，理由都是「按实现**实测形状**校正（依据：并行侦察 + 对抗校验，逐字段有 file:line 证据）」。`GateResult.editedData` 甚至标注「刻意保持 `any`……换成 unknown 会 TS2322（对抗校验实测），**勿收紧**」。

**对我们的启发**：我们的分镜表 JSON 是「弱契约」。wind-comic 明确把「契约面」当成一等公民治理——**注释里写明字段是谁产的、谁消费、为什么这个类型不能收紧**。这是纯工程收益，成本极低。

### 1.6 wind-comic：LLM / 确定性代码的边界

比 storyforge 更激进——**大量「零 LLM 成本」的纯函数审计层**：

| 模块 | 做什么 | 是否 LLM |
|---|---|---|
| `lib/pacing-audit-v2.ts` | 冲突曲线形状、拖沓段、开场密度、时长节奏 | ❌ 纯函数，注释写明「零 LLM 成本、零额外调用」 |
| `lib/dialogue-coverage.ts` | 对话覆盖度 / 正反打缺失检测 | ❌ 纯函数（词典 + 规则 + 正则） |
| `lib/consistency-policy.ts` | cref/sref/cw 选取决策 | ❌ 纯函数（优先级链） |
| `lib/story-intake.ts` | 长篇→分集（章节标记/长度打包） | ❌ 纯函数（正则 + 贪心） |
| `lib/script-parser.ts` | 剧本→结构化（台词提取） | ❌ 纯函数（正则） |
| `lib/tts-prosody.ts` | 情绪→TTS speed/pitch/vol | ❌ 纯函数，注释解释「TTS 调用本就是逐 shot 串行的瓶颈，再塞个 LLM 就是灾难」 |
| `lib/elements-registry.ts` | 元素→各引擎多参 payload 适配 | ❌ 纯函数「不碰网络、不碰 DB」 |
| `lib/budget-guard.ts` / `lib/task-budget.ts` | 预算裁决 | ❌ 纯函数 |
| `lib/pipeline-stages.ts` | 4 环节状态推导 + 下游失效计算 | ❌ 纯函数「可单测」 |
| `lib/character-traits.ts` | 从剧本**反向推理**角色 6-8 维特征 | ✅ LLM |
| `lib/character-dna.ts` | 三视图→8 维视觉签名 | ✅ Vision LLM |
| `lib/vision-audit.ts` / `lib/style-audit.ts` / `lib/cameo-vision.ts` | 逐镜画面评分 | ✅ Vision LLM |

**边界判据非常清晰，我总结成一句话：**
> **凡是「用已有结构化字段能算出来的」，一律纯函数；只有「必须看图」或「必须理解自然语言」的，才调 LLM。**

`lib/tts-prosody.ts:16-21` 把这条原则说得最直白：
> "为什么不用 LLM 去算：TTS 调用本就是逐 shot 串行的瓶颈，再塞个 LLM 就是灾难。这个函数纯规则 <1ms 出结果。"

---

## 2. 小说 → 剧本 → 分镜 的拆解链

### 2.1 storyforge：3 级拆解 + Map-Reduce 摘要

```
小说（N 章）
   ↓ ① MAP：逐章摘要      N 次调用，每次 in≈8K / out≈500   （章节原文截断 12K 字符）
   ↓ ② REDUCE：层级合并    log₂(N) 次，batch_size=6（story_agent.py:149）
   ↓ ③ 故事摘要            1 次，in≈4K / out≈1.5K
   ↓ ④ 结构化抽取          1 次（timeline/conflicts/relationships/world_setting）
   ↓ ⑤ 分集                1 次 → episodes[]（含 chapter_range/key_scenes/cliffhanger）
   ↓ ⑥ 每集拆场景          1 次 → scene_beats[]（scene_number/scene_beat/characters_present/estimated_duration）
   ↓ ⑦ 逐场景分镜          N 次（Semaphore(3) 并发）→ camera/emotion/location/dialogue/props/transition/character_actions/asset_refs
   ↓ ⑧ 连续性校验          1 次（5 维度）
```

**token 预算是有账的**（`docs/PROMPTS.md`，40 章小说）：46 次调用，输入 342.5K / 输出 28.9K，DeepSeek 约 **$0.025**。

### 2.2 storyforge：「过碎/过粗」的自检

**只有一个**，就是 `SceneValidatePrompt`（`backend/prompts/scene.py:107-135`），检查 5 个维度：
1. 角色连续性——不能无入场/退场就出现
2. 地点一致性——场景切换需被叙事驱动
3. 时间推进——时间线逻辑向前
4. 情绪弧——情绪在场景间自然过渡
5. 道具连续性——道具不能无故消失

**但它的执行是「非阻塞」的，这是重大缺陷**（`scene_agent.py:242-248`）：

```python
validation = await self._validate_continuity(project_id, scenes)
if not validation.get("valid", True):
    issues = validation.get("issues", [])
    logger.warning("continuity validation found %d issues ...", len(issues))
    for issue in issues:
        logger.warning(...)
return list(scenes)          # ← issues 只写进日志，然后原样返回
```

注释也明说了：「non-blocking on failure (returns `validation_passed=True` even if valid=false, issues logged as WARNING)」。**发现问题 → 写日志 → 照常继续。没有重生、没有反馈给用户、没有阻塞。** 这是一个「装了但没通电」的质检。

**时长控制**：`SceneSplitPrompt` 硬约束「Scene duration: 20-60 seconds」（`scene.py:21`），并要求「Each key_scene beat → 1-2 actual scenes」。**镜头时长完全由 LLM 一次性给出，没有任何后续校验或再平衡。**

### 2.3 storyforge：对白零丢失——**没有做，也没有声称做**

`backend/prompts/scene.py:70` 对 `dialogue` 的全部约束只有：
> "dialogue: array of {character, line} objects. If no dialogue, use empty array []."

**没有任何「保持原文对白」的机制。** 事实链：
1. 小说原文在 `_summarize_chapter()` 阶段就被截断到 12K 字符并被 LLM 摘要（`story_agent.py`）——**原文此时已经丢失**。
2. 之后所有环节（分集、拆场景、分镜）都**只基于摘要和 beat 描述**工作，原文再也不出现。
3. 最终的 `dialogue` 是 LLM 从 `scene_beat`（2-4 句描述）**重新创作**出来的，不是从原文提取的。

`grep -rn "dialogue" backend/` 的结果也证实：`dialogue` 只作为 JSON 字段被存储（`models.py:71`）、被传给 voice agent（`workflows/voice_generation.py:152-177`）、被 API 读写（`api/v1/scenes.py`），**从头到尾没有一处引用小说原文**。

> **结论：storyforge 完全不能保证原文对白零丢失。它是一套「重新创作」管线，不是「忠实改编」管线。** 这是它对我们**最大的警示**——我们做漫剧改编，对白保真度是产品底线，绝不能照抄它的摘要式链路。

### 2.4 wind-comic：拆解链

```
一句话 / 小说
   ↓ ① 长篇→分集（splitIntoEpisodes，纯函数，lib/story-intake.ts:58）
   ↓ ② 叙事模式选择（dialogue / first_person / narrator，注入 directive）
   ↓ ③ Director：genre/style/characters/scenes/storyStructure{acts,totalShots}
   ↓ ④ Writer：Script{title,synopsis,shots[]}
   ↓ ⑤ 节奏审计 v2（纯函数）+ 对话覆盖度审计（纯函数）→ rewriteHints
   ↓ ⑥ Style Bible Frame（1 张 canonical key art）
   ↓ ⑦ Character Designer（三视图 + 8 维 DNA + 6-8 维 traits）
   ↓ ⑧ Scene Designer
   ↓ ⑨ Storyboard Artist（Vision Audit <70 自动重生）
   ↓ ⑩ Video Producer（多引擎竞速）
   ↓ ⑪ TTS + Lipsync
   ↓ ⑫ Editor（j-cut/l-cut + BGM + CJK 字幕烧入）
```

**分集是纯确定性的**（`lib/story-intake.ts:58-104`）：
- 路径 A：正则认章节标记 `第X章/回/集/节/幕`、`Chapter/Episode/Part N`、markdown 标题，**≥2 个标记才走这条路**；开篇若 ≥80 字符且无标记则单独成「开篇」集。
- 路径 B：无标记则按 `targetChars`（默认 2000）**贪心打包**；切分单元逐级降级「段落 > 行 > 句子」（`toUnits`, L45-53），保证长无换行文本也能切。
- **末集过短（< target × 0.3）并入上一集**（L98-102）——这是一个明确的「过碎」兜底。

### 2.5 wind-comic：「原文对白零丢失」——**它做了，而且是确定性的**

这是 wind-comic 相对 storyforge 的**质的差别**。`lib/script-parser.ts`（33KB）是一个**确定性剧本文本解析器**：

- `isFullScriptInput()`（L81-112）用多维信号判断输入是不是完整剧本：章节标记、场景标记 `\d+-\d+\s*\S+\s*(日|夜|晨|昏...)`、好莱坞格式 `INT./EXT. ... - DAY/NIGHT`、`△`/`画面：` 动作标记、`(OS)` 标记、中文 `角色：台词` 行 ≥3 且长度 >800、英文全大写说话人行 ≥3 且长度 >500。
- 强信号一票通过；弱信号需组合。**特意注释「单纯 hasDialogue 不够，防止『小说+引述』误判」**。
- `parseScript()`（L117）正则抽取台词，**保留台词原文**：

```python
const dialogueMatch = line.match(/^([\u4e00-\u9fa5a-zA-Z0-9·]{1,20})\s*[：:]\s*(.+)$/);
// → { character: possibleName, line: dialogueMatch[2].trim(), isOS: false }
```
（`script-parser.ts:294-301`，`isOS` 区分画外音/内心独白）

- 台词用于：`extractCharacters()` 统计 `dialogueCount`（**L567 注释：「按台词数排序，主角一般台词最多」**——用台词量反推主角，很实用）、`sampleDialogues` 抽代表作台词、`getDirectorScriptContext()` 把「角色名(台词N句) + 代表台词」喂给下游（L652-660）。
- 台词 ≥8 句的单场景会按**说话人切换点**自动切分（L400-448）：`prevChars` / `nextChars` 各取前后 3 句的角色集合，说话人变化处切场景。

**结论：wind-comic 对「已是剧本格式」的输入能做到台词零丢失（正则提取，不经 LLM）。但对「纯小说」输入，它走的是 LLM 改编路线（`story-analyze.ts` 三段采样 → LLM 出 characters/highlights），同样不保证原文对白。**

> **对我们的直接结论**：两个项目都**没有**解决「小说原文对白 → 分镜对白零丢失」。最接近的做法（wind-comic 的 `script-parser`）只对**剧本格式**输入有效。如果我们要做真正的零丢失，需要自己设计：**在 LLM 拆镜阶段强制输出「原文对白 span 引用」（字符偏移/引号内原文），然后用确定性代码回填校验**。这是两个项目都没占的空位。

### 2.6 wind-comic：时长控制 + 「过碎/过粗」自检（**这是它最强的一块**）

**时长控制有两层：**

**第一层——逐秒 micro-beat（`types/agents.ts:87-116`）**。这是 v12.6.0 的核心设计：

```typescript
export interface MicroBeat {
  ts: string;              // "0-2s"（展示用）
  startSec: number;        // 机器可读，校验时长用
  endSec: number;
  action: string;          // 必须是可被视频引擎执行的动词链，禁止静态描写
  camera: string;          // "CU, low-angle, push-in"（与 action 分离声明）
  dialogue?: string;       // ≤15 字
  audio?: string;
  characters?: string[];   // v12.11.0 黄金模板对齐
  scene?: string;
  mood?: string;
  microExpression?: string;
  speedRamp?: string;      // "0.2x slow-mo on impact"
}
```
约束写在 `ScriptShot.beats` 注释里（L147-149）：**「逐秒 micro-beat 序列(2-4 条,每条 2-5s;时长之和 = duration)」**。这是**显式的时长守恒约束**——比我见过的多数实现都严谨。设计动机（L85-86 注释）：「替代『单段静态描写』，显著改善视频引擎的动作连贯性」。

**第二层——纯函数节奏审计 v2（`lib/pacing-audit-v2.ts`）**。这是全场最值得偷的单个模块。它把「打分」升级成「诊断」，四个维度：

| 函数 | 检测什么 | 关键算法/阈值 |
|---|---|---|
| `analyzeConflictShape(scores)` | 冲突曲线**形状** | 最小二乘斜率 + 峰值位置 + `peakProminence = peak - avg`。分类：`peakProminence < 1.5` → `no-climax`；`slope < -0.15` → `front-loaded`；`slope > 0.15` → `escalating`；否则 `flat` |
| `findDragSegments(scores, {threshold, minRun})` | **拖沓段定位** | 连续 ≥3 镜低于阈值（短剧模式 4 分，普通 3 分），返回 `{fromShot, toShot, length, avgScore}` |
| `auditOpening(scores, reversalPairs, {dramaMode})` | **开场密度** | 取前 1/3，至少 2 镜、至多 5 镜；`minAvg` 短剧 5 分、普通 3.5 分 |
| `auditDurationRhythm(shots, {minCv})` | **时长节奏** | 变异系数 `cv = std/mean`；`cv < 0.12` → 呆板；连续 ≥3 个 >均值×1.4 的长镜 → 拖沓 |

**设计文档里解释了为什么「平均分」不够**（L9-14）：
> "平均分会把「高开低走」和「层层递进」算成同一个数" + "报「平均 5.2 分」没用，要指出第几到第几镜是观众划走的地方" + "完播率由前段决定"

`[1,2,4,9]` 和 `[9,4,2,1]` 平均都是 4，但形状完全不同——这个例子写在 README 里。

**每一条诊断都指向具体镜号**（`auditPacingV2` 的 `actionable[]`，L211-220）：
```
"第 3~5 镜连续 3 镜低冲突(均分 2.4) —— 这是最可能被划走的一段，建议合并、删减或在其中插一次反转。"
```

**全部是纯函数，零 LLM 调用，零额外成本。**

**「过碎」的检测在另一个模块**：`lib/dialogue-coverage.ts`（见 §2.7）。

### 2.7 wind-comic：对话覆盖度审计（专治「AI 一遍跑完」感）

`lib/dialogue-coverage.ts` 解决的问题写在开头（L4-7）：
> "漫剧约 50% 镜头是对话场景。真实导演会用『正反打』……但我们当前 Writer 输出常常一段连续对话用 1 个 wide shot 涵盖，显得非常『AI 一遍跑完』。**这是漫剧『AI 感』最大来源之一。**"

**解法分三步，关键是「不强行让 Writer 改写」**（L11-13 注释）：
1. 写完剧本后 **audit** 对话覆盖度——哪个对话场景缺反打
2. SSE warning + 项目页节奏 tab 展示
3. Writer prompt 加约束（`buildDialogueCoverageBlock()`, L214-223）

**检测逻辑（纯函数）**：
- `detectDialogueScenes(shots)`：把连续对话镜头按 `locationKey` 分组。**同 location 一定并入当前场景**（L122-123 注释：「即使发言人切换——shot/reverse 的本质就是切换说话人」）；中间隔非对话镜则切分。
- `locationKey(shot)`：优先用显式 `location` 字段；否则取 `sceneDescription` 首个逗号前段，再用正则**剥掉镜头语言修饰词**（wide|long|full|establishing|medium|close|cu|mcu|extreme|aerial|panoramic|two-shot / shot|frame|view|angle|stage）。
- 两类问题：
  - `needsReverseShot` = 多角色对话但**只有 1 镜**
  - `needsCloseUp` = 多角色对话 ≥2 镜但**全是 wide，无特写**
- `coverageScore = (multiChar.length - needsReverseShot.length - needsCloseUp.length) / multiChar.length × 100`

**warning 直接给可执行的重写提示**（L175-192）：
```
"🎬 第 5 镜: 林小满 / 陆沉 对话场景只有 1 镜, 缺正反打 — 真实导演会切到对方反应"
rewriteHint: "在 shot 5 之后插一镜: 对话听众 (陆沉) 的反应特写 (CU 表情) — 切镜后回到 林小满"
```

**注入 Writer 的硬规则**（`buildDialogueCoverageBlock()`, L214-223）：
```
### 🎬 对话场景覆盖度 (硬规则)
- **2+ 角色对话至少 2 镜**: 不要一镜 wide shot 涵盖整段对话; 必须切到听众反应
- **正反打结构**: A 说话特写 → 切 B 反应特写 → 切回 A 继续说; 同一场景最少 3 切
- **避免 wide-only**: 多角色对话整段都是远景/全景 = 缺微表情张力, 必须至少 1 镜是 CU/MCU
- **每个 reaction shot 独立成行**: shot.dialogue 可为空, shot.action 写"听者表情"
```

> **这是本次调研中对我们「分镜过粗」问题最直接可用的方案。** 我们已经有分镜表 JSON，加一个「对话场景同地点合并 + 缺反打检测」的纯函数检查器成本极低，且不需要重跑 LLM。

---

## 3. 角色一致性

### 3.1 storyforge：角色卡 schema

**两阶段：先抽取角色清单，再逐角色生成完整 profile。**

**抽取阶段**（`backend/prompts/character.py:4-38`）输出：
```
name / aliases[] / role(protagonist|antagonist|supporting|minor) /
importance(primary|secondary|tertiary|background) / first_appearance /
scene_count / relationship_to_protagonist / narrative_function / is_protagonist
```
规则里有几条很实用：「Consolidate name variants into canonical names. Put variants in aliases.」「Mark exactly one character as is_protagonist=true.」「Include every named entity that appears in the sources.」

**Profile 阶段**（`character.py:53-128`）输出 6 大块：

| 块 | 字段 |
|---|---|
| `appearance` | age_estimate / height / build / hair / eyes / distinguishing_features / typical_expression |
| `voice_profile` | pitch / tempo / accent / tone_quality / speech_patterns[] |
| `personality` | traits[] / motivation / fears[] / quirks[] / moral_alignment（D&D 9 宫格） |
| `emotion_range` | dominant / secondary[] / rarely_shows[] / trigger_situations[] |
| `costume_style` | **era / style / signature_items[] / color_palette[] / notes** |
| `backstory` | 2-3 句 |

**合并与归一化**（两个额外 LLM 阶段）：
- `CharacterMergePrompt`（`character.py:131-174`）：判断两个角色条目是否同一人。规则含「Dr. Smith / John Smith → same person」「A character and their alias (e.g. Batman / Bruce Wayne) are the same person」。
- `CharacterNormalizePrompt`（`character.py:177-214`）：全局一致性审查——年龄一致性（父母>子女、师徒）、**关系对称性（A 是 B 的妻子，则 B 必须是 A 的丈夫）**、时代一致性、无两个角色功能相同。**关键规则：「Flag contradictions as issues — do NOT silently resolve them.」**

**但这里有一个重大实现缺陷**（`character_agent.py:213-305`）——`merge_duplicates()` 的向量去重是**坏的**：

```python
emb_response = await self._router.generate(task="embedding", prompt=identity_text, ...)
# ← 拿到了 embedding，但从未使用这个变量！
results = await self._vector.query(
    collection=collection,
    vector=[0.0] * 384,      # ← 注释自陈 "fallback query — real embedding would be used"
    top_k=5,
)
```
（同款桩也在 `workflows/tasks.py:86`，注释「dummy vector to get all points」）

**这段 Qdrant 查询实际上传了一个全零向量，返回结果 `results` 甚至从未被使用。** 真正的去重退化成「名字相似 or 角色相同」的启发式预过滤 + LLM 决策。**README 宣称的向量去重是未接线的。** 这是「文档承诺 > 代码实现」的典型案例。

### 3.2 storyforge：服装变体 / 多阶段形象——**没有**

`costume_style` 是**单一静态字段**，挂在 character profile 上。schema 里**没有任何 per-scene / per-stage 服装概念**。`Character` 表（`models.py:82-102`）只有 `profile: JSONB`，`CharacterVersion` 存的是 profile 快照（见 §4.1），不是服装变体。

> **这是 storyforge 的空白，恰好是我们已有的能力（服装变体：图锁身份 + 文控服装）。** 我们的做法比它强，不要退回去。

### 3.3 storyforge：多角色同框——**实现是错的**

这是 storyforge **最严重的架构缺陷**。看 `backend/workflows/video_generation.py:90-129`：

```python
for scene in state.scenes:
    characters_present = storyboard.get("characters_present", []) or []
    for char_name in characters_present:          # ← 遍历场景里的每个角色
        char_data = state.character_assets[char_name]
        prompt_id = await video_agent.submit_scene_video(
            scene_id=...,
            character_name=char_name,             # ← 一次只提交一个角色
            character_image_data=char_image,
            storyboard=storyboard,
            seed=hash(f"{state.project_id}:{scene_id}:{char_name}") & 0x7FFFFFFF,
        )
```

**一个场景有 N 个角色就提交 N 个独立视频任务，每个任务只带一个角色的参考图。** 结果：
1. 多角色同框场景**根本无法生成**——每个任务只会出现一个角色。
2. 一个 2 人对话场景产出 2 段各自独立的视频，然后都存成同一个 `scene_id` 下的 video（`_save_node`）。
3. 后续没有合并逻辑。

**图像侧同样如此**（`workflows/image_generation.py:139-188` `_char_scene_node`）：遍历 `char_present`，每个角色单独生成 `char_scene` 资产。背景另走 `_bg_node`（**无角色**）。

也就是说，storyforge 的资产模型是「**角色单体图 × N + 空背景图 × 1**」，**没有「多角色同框的一张图」这个概念**。`asset_refs` 里那个 `CHAR_JOHN_SITTING` 式的符号标识符，暗示原设计有更复杂的合成意图，但从未实现。

> 对我们的启示：多角色同框是漫剧的核心难点，storyforge **在这道题上交了白卷**。wind-comic 的处理方式（多参挂载 + 每角色独立评分）才是正解，见 §3.5。

### 3.4 wind-comic：三层角色一致性体系

这是 wind-comic 的**核心技术资产**，README 概括为 `cref + sref + 8-dim DNA + Cameo Vision Retry`。拆开看是三个独立机制：

**第 1 层：Style Bible Frame（全局画风锚）——`lib/style-bible.ts`**

问题定义（L5-7）：
> "之前每个 shot 独立生成，MJ/Flux 看到的『风格』只有一段文字 + 最近 2 张图。6 个 shot 看起来像 6 部不同的剧，因为每个 shot 都在重新协商 'what does cinematic mean'。"

解法：Director plan 拿到后**立刻渲染 1 张 canonical key art 帧**（`buildStyleBiblePrompt()`, L75-97），然后把这张图作为后续**每一次** generateImage 的**第 1 张 sref**（`prependStyleAnchor()`, L121-129）。

prompt 只描述 look + mood + grade，**不含任何具体主体**：
```
"cinematic key art poster, single canonical look frame" + styleKeywords + moodWords
+ "no specific characters, no faces, no body figures, environmental abstract composition"
+ "this frame defines the visual identity for the entire series"
+ " --ar {aspect} --s 250 --no people --no person --no character --no face"
```

`getMoodWordsForGenre()`（L38-67）按题材给不同的色温/光调锚词：
- 古装 → `warm amber lighting, soft volumetric haze, ink-wash painterly grade, jade and crimson palette, low contrast highlights`
- 赛博/科幻 → `neon cyan and magenta bloom, deep teal shadows, anamorphic lens flares, high contrast cinematic grade, wet asphalt highlights`
- 恐怖/悬疑 → `cold steel-blue shadows, single source key light, hard shadow falloff, desaturated palette, grainy film noise`
- 校园/青春 → `soft golden hour backlight, pastel palette, low contrast film grain, dreamy bokeh, lifted shadows`

**关键优势**：README 强调「大多数竞品只做 2 帧滚动链——shot 6 不知道 shot 1 长什么样」。Style Bible 是**全局单锚**，不是滚动链。

**第 2 层：Character DNA（8 维视觉签名）——`lib/character-dna.ts`**

问题定义（L4-6）：
> "即使 Style Bible + cref/sref 都给了，跨镜头主角的具体长相（眼型/嘴型/下颌）仍会 drift，因为 MJ/Minimax 在不同 prompt 下对『同一参考图』的解读不同。"

解法：给每个角色的**三视图过一次 Vision LLM**，抽出 8 个**结构化**字段（`CharacterDna.signature`, L27-36）：

| 字段 | 示例 |
|---|---|
| `eyeShape` | "almond, slightly upturned" |
| `jawShape` | "soft oval, defined chin" |
| `noseShape` | "straight bridge, slight upturn" |
| `mouthShape` | "thin lips, curved smile" |
| `hairStyle` | "long ponytail with side bangs" |
| `hairColor` | "deep black with auburn highlights" |
| `skinTone` | "fair, cool undertone" |
| `signatureOutfit` | "silver jade pendant + crimson hanfu collar" |

**System prompt 里的反幻觉约束值得照抄**（L56-57）：
> "每个字段 ≤ 60 字符英文，描述视觉特征而不是情绪/气质。**拒绝 "beautiful" / "elegant" 等抽象词。** 未识别的字段写空字符串 ""——不要用 null。"

**输出长度硬控**（L96-98）：`buildPromptBlock()` 把 8 个字段拼成 `"{name} visual DNA: eyes: ...; jaw: ...; ..."`，**上限 200 字符**，注释理由：「给 Minimax 1500 字 cap 留余地」。

**双保险注入**（L10）：模型同时收到**参考图 + 自然语言 anchor**，两层锁定。注入逻辑 `injectDnaIntoPrompt()`（L225-240）：多角色同框时**拼接所有命中角色，各占 1 段，用 ` | ` 分隔**，同一 DNA 命中多个别名只拼一次。

**名字匹配的鲁棒性**（这段细节很实用，L194-219）：
```typescript
export function normalizeCharacterName(s) {
  return (s||'').toLowerCase()
    .replace(/[\s,.，。、:：;；!！?？\-—()（）\[\]【】<>《》「」『』"'""'']/g, '')
    .trim();
}
// matchDnaForName 三级匹配：
//   1. 原样精确（快路径）
//   2. 归一精确
//   3. 子串双向（≥2 字符，避免单字误匹配）
```
注释解释了修的是什么 bug：「修『林小满(镜头) vs 小满(dnaMap)』这类**静默漏注入**」。

**批量抽取**（L169-188）：并发 2 路，失败的角色返回 null 不影响其他。整个 DNA 抽取是**异步非阻塞**的——「失败/没 key 不影响主流程」。

**第 3 层：cref/sref/cw 选取策略——`lib/consistency-policy.ts`**

这个模块把「选哪张参考图、用多大权重」**集中决策**，因为原来逻辑分散在 orchestrator 多处「容易漏选或选错」（L7）。解决的 4 个具体问题（L6-13）：
1. cref/sref 选取逻辑分散
2. 场景匹配用 `sceneDesc.includes(sceneName)`——场景名「阁楼」但镜头写「昏黄的房间」就完全匹配不上
3. 用户上传的主角脸应该用更高 cw，但所有镜头都是 cw=100 没区分
4. cref 主图无法堆叠

**优先级链（写死在代码里）：**
```
cref:  用户上传脸 > 该镜头出场角色的三视图 > 第一个角色三视图
sref:  该镜头场景的概念图 > 同 location 之前镜头的渲染图 > 第一个场景概念图
cw:    用户锁脸 → 125(强约束); 普通主角色 → 100; 配角 → 80
```

**cw 分级**（L203-220）：
| 情况 | cw | tier |
|---|---|---|
| 命中 lockedCharacters | 该角色自己的 cw（`Math.max(25, Math.min(125, round(cw)))`） | `matched-locked` |
| 用户单角色锁脸 | 125 | `locked` |
| 主角镜头 | 100 | `protagonist` |
| 其他 | 80 | `supporting` |

配角用 80 的理由（L218 注释）：**「配角放松一点——防止 MJ 把所有人都画成主角脸」**。

**`SceneAnchorRegistry`（场景锚点注册表）** 是一个我很欣赏的小设计（L231-…）：
- `register(location, {url, description})`：**同一 location 多次注册时保留首张**，注释理由「作为该地点的『基线锚点』，**防风格漂移**」。
- `lookupByLocation()` / `lookupByDescriptionSubstring()`：后者做双向子串匹配，比原来的 `includes(name)` 鲁棒。
- **可持久化**：`toEntries()` / `seed(entries)`——注释「v12.2.1 从持久化条目回灌（rerun/重启复用上次场景锚）」，且「只补未注册的（首张基线优先，不覆盖）」。

**多角色同框的处理**（`matchLockedCharactersInShot`, L114-145）：
- 遍历 shot 的角色名，每个名字尝试匹配 lockedCharacters（归一精确 → 子串双向，锁定名 ≥2 字符「避免『安』匹配所有人」）。
- 返回 `[primary, ...extras]`：**primary 决定 cref + cw，extras 放进 `extraCrefs`** 供上游塞进 MJ 的 `referenceImages` / Minimax 的 `subjectReferences`。
- `ConsistencyPick.reason` 字段记录选取来源（`crefSource` / `srefSource` / `cwTier` / `matchedLockedName`），**"仅作日志/调试"**——这是一个很好的可观测性设计。

**第 4 层（评分侧）：Cameo Vision Retry**

`Storyboard` 类型里的评分痕迹（`types/agents.ts:191-221`）：
```typescript
cameoScore?: number;          // 0-100, vision 比对生成图与参考图的一致性分数（多角色 = min 分）
cameoRetried?: boolean;       // 是否触发了重生（一次重生上限）
cameoAttempts?: number;       // 实际跑了几次生成
cameoFinalCw?: number;        // 重生时实际使用的 cw，调试用
cameoReason?: string;         // vision 给低分的理由
cameoNeedsReview?: boolean;   // v12.2.8: 重生跑完仍 < 阈值 → 待人工复核
cameoPerCharacterScores?: Array<{ name?: string; score: number|null; reasoning?: string }>;
```
规则（README）：**shot 的角色匹配分 < 75 自动重生并 boost cw**；**一次重生上限**；仍不达标则标「待复核」交给人工，而不是无限重试。`cameoPerCharacterScores` 按 `[primary, ...additional]` 顺序展开，**多角色镜头逐角色独立评分**。

**同级的 Style Audit**（`types/agents.ts:208-221`）：
```typescript
styleAuditScore?: number;     // 0-100, 4 个维度的 min
styleAuditDims?: { palette, lighting, colorTemperature, texture };
// <70 自动重生, <85 给 warning
```

### 3.5 wind-comic：元素注册表（统一多参挂载）

`lib/elements-registry.ts` 是「跨引擎多参适配层」。设计动机（L4-11）：
> "工业级 AI 短剧流水线都用「挂载元素」统一管理角色/场景/道具——给每个元素一个稳定 id（`@人物{陆晚晚}`），配一组参考图（正面/侧面/3-4 角度），分镜里按 id 引用、复用，生成时再按各引擎的『多参语法』适配。"

**数据模型**（L20-41）：
```typescript
type ElementType = 'character' | 'scene' | 'prop' | 'style';
type AssetRole = 'frontal' | 'side' | 'three_quarter' | 'primary' | 'detail';

interface RegistryElement {
  id: string;            // `@人物{陆晚晚}`
  type: ElementType;
  name: string;
  traits?: string;       // 外观/特征（锁一致性的文字锚点）
  assets: ElementAsset[]; // [{role, url}]
}
```

**稳定 id 语法**：`@人物{陆晚晚}` / `@场景{...}` / `@道具{...}` / `@风格{...}`，`elementId()` 生成，`parseElementId()` 用正则 `^@(人物|场景|道具|风格)\{(.+)\}$` 解析。

**各引擎多参语法差异**（注释 L8-11，非常有价值的情报）：
| 引擎 | 多参语法 |
|---|---|
| Seedance 2.0 | 位置数组 `image_urls[]/video_urls[]`，prompt 里 `@Image1/[Image1]` 按序引用 |
| Kling 3.0 | `elements[]`（每个 `frontal_image_url` + `reference_image_urls[]` 多角度）→ `@Element1` |
| Veo 3.1 | 统一 `reference_images[]`（≤3，`reference_type='asset'`），**无 @ 标记，靠 prompt 点名** |
| Minimax S2V | `subject_reference[]`（单主角锁定） |

**诚实标注**（L16-17）：注释直言「统一 `VideoGenerateInput` 契约目前每个 subject 只接受 1 个 url，故 Kling 多角度 `reference_image_urls` 暂只在本层备好；真正喂进 Kling 需扩契约（Phase 2.1 跟进）」。**这是很好的工程诚信示范——标注了「已备好但未接线」。**

### 3.6 wind-comic：换装 / 多阶段形象

**结论：没有专门的多阶段形象系统。** 服装分散在三处：
- `CharacterTraits.costume`（`lib/character-traits.ts:37`）：「服饰——包括款式/颜色/配件」，`≤30 字`
- `Character.visual.outfit`（`types/agents.ts:51`）——McKee 11 维之一
- `CharacterDna.signature.outfit` → `signatureOutfit`：「该角色标志性的服饰元素 1-2 件（最辨识的）」

**三者都是单一静态值，没有 per-shot / per-act 服装变体概念。**

> **所以「服装变体」这道题，两个项目都不如我们现有的实现**（图锁身份 + 文控服装）。这是我们为数不多的**领先项**，要守住。

---

## 4. 资产库

### 4.1 storyforge：资产库 + 真版本管理

**这是 storyforge 相对 wind-comic 明显更强的一块。**

**`Asset` 表**（`models.py:136-162`）字段设计得相当完整：
```
project_id / character_id / scene_id / asset_type / file_path / file_size /
prompt / negative_prompt / seed / generation_params(JSONB) /
variation_of(自引用FK!) / batch_id / selected / favorite /
locked / locked_at / asset_ref / status
```

几个亮点：
- **`variation_of` 自引用外键**——候选图之间有谱系关系，能追溯「这张是从哪张变来的」。
- **`batch_id`**——把一次抽卡的 N 张归为一个 batch（`GenerationBatch` 表另有 `total_assets / completed_assets` 计数）。
- **`selected` / `favorite` / `locked` 三态**——选中、收藏、锁定是**三个独立布尔位**，不是互斥状态。
- **`generation_params` JSONB**——保存完整生成参数（`state.params.model_dump()`）。

**`AssetType` 枚举**：`image / character_image / storyboard / cover / other`。

**真版本管理（`CharacterVersion` 表）**（`models.py:105-115`）：
```python
character_id / version_number / profile_snapshot(JSONB) / diff(JSONB) / created_at / created_by
```
配合 `Character.version: int`（默认 1）。并且有**回滚 API**：`POST /api/v1/characters/{id}/rollback`（回滚到版本 N）、`GET /api/v1/characters/{id}/versions`（版本历史）。

**锁定语义做得很细**：
- `Character.locked / locked_at / locked_by`——**记录谁在什么时候锁的**。
- `PATCH /characters/{id}` 有 **lock check 409**。
- `Asset.locked`——`DELETE /assets/{id}` 锁定时返回 **409**（`api/v1/assets.py:209-210`）；`DELETE /voices/{id}` 已 selected 时返回 **409**。
- `scene.storyboard` 里也带 `locked` 字段并在重生成时保留（`api/v1/scenes.py` 第 64 行：`"locked": scene.storyboard.get("locked", False)`）。

**这套「lock → 409 拒改 → 需要显式解锁」的语义，是我们应该直接照抄的**——比我们现在「任何产物都能被覆盖」要安全得多。

**跨章节/跨项目复用**：storyforge 是 **project 独占模型**。`MASTER_PROMPT.md` 明确写「Data Ownership: Everything belongs to project. Projects own: episodes/scenes/characters/assets/videos」。**没有跨项目资产库。**

### 4.2 wind-comic：全局资产记忆库（跨项目复用）

**这是 wind-comic 更强的一块。**

**`GlobalAsset` 类型**（`types/agents.ts:524-540`）：
```typescript
type GlobalAssetType = 'character' | 'scene' | 'style' | 'prop' | 'template';
interface GlobalAsset {
  id / userId / type / name / description / tags[] / thumbnail /
  visualAnchors: string[];       // 3-5 个关键视觉特征（用于一致性 prompt 注入）
  embedding?: number[];          // 768 维特征向量
  metadata: Record<string, any>; // 类型特定数据
  referencedByProjects: string[];// 被哪些项目引用
}
```

**`referencedByProjects` 字段设计得很实用**——记录「哪些项目用过该资产」，注释说用于「未来热度统计」，但实际它同时能解决**「删资产前警告会破坏哪些项目」**。

**`visualAnchors: string[]`** 是「3-5 个关键视觉特征」，直接用于 prompt 注入——这比塞整个 profile 更省 token。

**嵌入与余弦检索**（README v12.2.x）：
> "给 `global_assets.embedding` 死列通电（BYO 文本嵌入 + 内存余弦检索）"
> "建角色入口「相似角色」推荐一键复用（防重复建/跨集漂移）"
> "**身份漂移检测**（逐镜视觉 embedding 余弦距离标 outlier 漂移镜）"
> "全程**无 key 走确定性地板（精确名+文本匹配），有 key 向量增强**，诚实降级"

**最后这句是关键设计哲学**：**没有 embedding key 时功能退化但可用，不是报错。** 这个「graceful degradation」模式贯穿 wind-comic 全文。

**注意对比**：storyforge 有 `Asset.variation_of` 谱系和 `CharacterVersion` 快照（真版本管理），但**只在项目内**；wind-comic 有跨项目 `GlobalAsset`，但**`ProjectAsset.version` 只是一个 `number` 字段，没有快照表、没有回滚 API**（`types/agents.ts:279`）。

> **结论：两者各有一半。理想设计 = storyforge 的版本/锁定语义 + wind-comic 的跨项目全局库。** 我们要两个都拿。

### 4.3 wind-comic：资产账本（Asset Ledger）

`lib/asset-ledger.ts` + `DirectorReview.producerReports.assetLedger`（`types/agents.ts:344-360`）是一个**逐镜资产完备性矩阵**：

```typescript
entries: Array<{
  shotNumber: number;
  characterRef: 'missing' | 'draft' | 'approved';
  sceneRef:     'missing' | 'draft' | 'approved';
  storyboardImg:'missing' | 'draft' | 'approved';
  videoClip:    'missing' | 'draft' | 'approved';
  dialogue:     'missing' | 'draft' | 'approved';
  voiceover?:   'missing' | 'draft' | 'approved';
  musicCue:     'missing' | 'draft' | 'approved';
}>;
totalShots / missingCount / draftCount / approvedCount / blockers[];
```

**这是「哪一镜缺哪一环」的完整台账**，7 个维度 × 每镜。比我们现在的「机械质检」信息密度高得多——它不仅能告诉你「第 5 镜有问题」，还能告诉你「第 5 镜的角色参考图还只是 draft」。

---

## 5. 人机协作

### 5.1 storyforge：API 层完备，UI 层几乎空白

**API 层的人机协作面做得相当完整**（README API 表）：

| 干预类型 | 端点 |
|---|---|
| 编辑分镜字段 | `PATCH /scenes/{id}`（title/description/dialogue/storyboard 直接改） |
| **带反馈重生成** | `PATCH /scenes/{id}` 带 `feedback` 字段 → 走 `SceneEditPrompt` |
| 编辑单集 | `EpisodeRegeneratePrompt`（含相邻集上下文） |
| 编辑角色 | `PATCH /characters/{id}`（lock check 409） |
| 角色回滚 | `POST /characters/{id}/rollback` |
| 选角/抽卡 | `POST /assets/select`（approve）、`POST /assets/favorite` |
| 资产锁定/编辑 | `PATCH /assets/{id}`（locked / feedback / regenerate） |

**「不满意，重来」的表达方式**：`PATCH /scenes/{id}` + `feedback: str`。`SceneEditPrompt`（`scene.py:138-163`）的规则设计得不错：
```
- Preserve scene_number and characters_present from the original.
- Fully incorporate the feedback provided.
- Maintain continuity with adjacent scenes.
- If a field is not mentioned in feedback, keep the original value.   ← 关键
```
最后一条很重要：**只改 feedback 提到的字段，其他保持不变**——避免「重生一次全变了」。同时它把**相邻镜头**（前/后各一）作为上下文传入（`api/v1/scenes.py` 第 32-42 行），保证编辑不破坏连贯性。

**但 UI 几乎是空的。** `frontend/src` 全部只有 **431 行 TypeScript/TSX**：
```
app/page.tsx  app/dashboard/page.tsx  app/upload/page.tsx  app/layout.tsx
components/header.tsx  components/providers.tsx  components/ui/button.tsx
lib/api.ts (27 行)  lib/store.ts  lib/i18n/index.ts (141 行)
```
**只有 3 个页面路由（home / dashboard / upload）。** 分镜审阅、抽卡选图、角色编辑这些 API 都**没有对应的 UI**。前端是一个脚手架。

> **结论：storyforge 的「人机协作」是「API 已备、产品未做」。** 它的 API 设计（尤其 `feedback` 部分更新语义 + 409 锁定）值得学，但它不是「UI 长什么样」的参考对象。

### 5.2 wind-comic：完整的人机协作产品（11 个 tab 的导演台）

**这是 wind-comic 的绝对强项。**

**交互形态**：Next.js Web 应用（非 CLI、非纯 chat）。README 描述项目页有 **11+ 个 tab**：
> "导演台 · 剧本 · 角色 · 场景 · 分镜 · 连贯性 · 视频 · 镜头工坊 · Cinema 时间线 · 节奏分析 · 成片质检 · 技术监看 · 参数联动 · 评论协作 · 完整播放"

**核心是人机闸门（Gate）机制** ——`types/agents.ts:461-472`：
```typescript
export type GateData = Record<string, unknown>;      // 人工闸门入参
export interface GateResult {
  action: string;        // 运行时为 'continue' | 'edit'
  editedData?: any;      // 人工编辑后的数据
}
```
`hybrid-orchestrator.ts` 里有 `waitForGate()` 方法，把数据 spread 进 SSE 事件推给前端，**等人响应 `continue` 或 `edit`**。这是「人在环中」的干净抽象：编排器在某一步挂起，前端展示，人改完回传 `editedData` 继续。

**Director Console（导演台）的 stale-detection** —— `lib/pipeline-stages.ts`：
把主流程抽象成 **4 个环节**（`script` → `assets` → `storyboard` → `final`），由项目资产推导每个环节的**三态**：
```typescript
export type StageStatus = 'empty' | 'ready' | 'stale';
```
- `empty` = 无资产
- `ready` = 有且不旧
- `stale` = 有但 (a) 被显式标记失效，或 (b) **比某个上游环节旧**（`derivePipelineStages`, L75-87 的时间戳比较）

**最关键的是「重跑某环节会让哪些下游失效」**（`buildRerunPlan`, L122-133）：
```typescript
export function downstreamStages(id: StageId): StageId[] {
  const order = PIPELINE_STAGES.map(s => s.id);
  const i = order.indexOf(id);
  return i < 0 ? [] : order.slice(i + 1);
}
// RerunPlan = { target, invalidates[], affectedAssetIds[], sequence[] }
```
**`affectedAssetIds` 会算出具体哪些资产 id 需要被标记失效。** README：「一个一键重跑，它**知道下游哪些会失效**」——UI 会**预告**下游影响。

**「不满意，重来」的多种表达方式**（按粒度从细到粗）：
1. **改 prompt 重生单镜** → Shot Workshop（`screenshot-workshop`：改 prompt + 参考图上传 + 4K 重渲）
2. **片段重拍（Segment Retake）** → 只重拍 8 秒里的 2 秒
3. **单环节重跑** → Director Console 的 stage rerun
4. **对话式迭代** → 创作工作台有 chat side-rail
5. **整片重生** → 重生循环（rebirth-plan）

**Segment Retake 的工程设计特别漂亮**（`lib/segment-retake.ts` / `lib/shot-segment-retake.ts`）：

问题定义（`segment-retake.ts:9-16`）：
```
① 引擎有最短时长，想生成 2 秒是生成不出来的。
② 缝回后总时长必须一字不差地不变。
```
解法：
- `planSegmentRetake()` 算出**要向引擎请求的时长**（可能大于 patch 长度，因为引擎下限），同时保证 **`totalAfterS === shotDurationS`（不变量）**。
- 其余部分 **`-c copy` 字节复制**，不重编码。
- **核心收益**（`shot-segment-retake.ts:9-14`）：由于时长不变，**压缩时间轴（`computeXfadeTimeline`）不用重算，配音延迟/字幕起点/EDL record-in 都不用重算**；下游只需作废两样：**成片 + 该镜的口型对齐分**。
- 切点先**吸附到帧栅格**再算时长；帧提取用**精确 seek**（`-ss` 放在 `-i` 之后，而非关键帧快速 seek），保证「你选的帧就是它切的帧」。
- Takes 有版本（可 adopt / rollback），像 voice retake 一样。

**Frame-by-frame inspection**（v12.328-330）：逐帧步进已完成镜头，框选坏的那段，把精确区间交给 segment retake。

**评论协作**：project 级 + per-shot 线程评论、@-autocomplete、通知铃；Yjs CRDT 实时多人（光标、presence、`Y.Map` 段锁防两人同时编辑同一段）；项目邀请分 viewer/commenter/editor 角色。

**撤回「过度声称」的诚实**（README 竞品表后）：
> "**🔴 We overclaimed last round — four 'only we have this' claims retracted (verified 2026-09-03).** This is not competitors catching up; the claims were **too broad to begin with**"

并列出了 4 条撤回的具体声称（EDL/AAF 非独有、非唯一 MIT 自托管、BYO 注册表非独有、节奏审计非独有）。**这种自我纠错在产品 README 里很少见，但它让剩余声称的可信度显著提高。**

> **可偷的具体点**：`pipeline-stages.ts` 的「三态 + 下游失效计算」是一个**约 160 行的纯函数模块**，能直接搬到我们的 Web 控制台，立刻获得「哪些环节过期了、重跑影响什么」的可见性。

---

## 6. 配音与音频

### 6.1 storyforge：CosyVoice / GPT-SoVITS + 情绪向量映射

**Provider 层**：`CosyVoiceAdapter` / `GPTSoVITSAdapter`（`backend/providers/voice/`）。README 的 API：`POST /voices/generate` 做「clone + synthesize + preview」三段。

**情绪映射有一个独立且设计精良的模块**（`backend/prompts/voice.py`）：

- **`EmotionResolver`**（L27-84）：把 storyboard 的 emotion 文本映射到 `(emotion_tag, emotion_vector)`。
  - 有**受控 emotion tag 集合**：`happy|sad|angry|soothing|mysterious|determined|neutral`
  - 每个 tag 对应一个**三维向量** `{pitch, rhythm, timbre}`，例如 `"emotionless": ("neutral", {"pitch": 0.95, "rhythm": 0.90, "timbre": 0.55})`
  - **`needs_llm()`（L80-84）**：先查表，命中则**零 LLM 成本**；未命中才走 LLM。这又是一个「确定性优先」的例证。
- **`EmotionLLMPrompt`**（L149-186）：LLM 兜底，把复杂情绪描述映射到最近的受控 tag。**按 `(emotion_description + character_id)` 缓存 24h**。
- **`VoiceProfileMapper.apply_character_baseline()`**（L132-147）：在情绪向量上叠加**角色个人的 dominant emotion 基线偏移**——同一个「愤怒」，内敛角色和外放角色表现不同。
- **`ReferenceTextPrompt`**（L4-…）：从角色 profile 构造**音色克隆的参考文本**。

**`Voice` 表 schema**（`models.py:176-203`）非常完整：
```
character_id / scene_id / dialogue_index / provider / speaker /
speed / pitch / emotion / version / selected / voice_params(JSONB) /
file_path / file_size / duration / duration_ms / preview_path / reference_audio_path
```
**`dialogue_index` 关联到 scene.dialogue 数组的下标**——这是「配音挂到具体某句台词」的关键外键，配合 `ix_voices_scene_dialogue` 复合索引（alembic 005）。**台词级别的音频寻址**，这个设计值得学。

**口型对齐：没有。** `grep` 全仓无 lipsync/wav2lip 相关实现。有 `video_agent.composite_audio()`（`video_agent.py:95-142`）用 ffmpeg 把对白音轨 mux 进视频：
```python
cmd = ["ffmpeg", "-y", "-i", video, "-i", audio,
       "-c:v", "copy", "-c:a", "aac", "-shortest", "-loglevel", "error", out]
```
**`-c:v copy`（视频流不重编码）+ `-shortest`（以短的为准）**——最小可行做法，但**没有任何音画同步/口型处理**。

**BGM / 音效：没有。** 全仓无 BGM/SFX 模块。

**字幕：没有。** `grep srt|subtitle` 只命中两处：`prompts/video.py` 的**负面提示词**里包含 `subtitles`（防止模型画字），以及——**没有实际字幕生成**。

**拼片（concat）：没有。** `grep concat` 零命中。README 的 Phase 表里 `TASK_010 Editing` 是 **🔲 未完成**。**storyforge 不能产出成片**，只能产出散装镜头。

### 6.2 wind-comic：完整的音频链

**TTS 是多 provider 插件式**（`lib/tts-providers/registry.ts`、`types.ts`、`builtins.ts`、`example-elevenlabs.ts`、`vectorengine-tts.ts`）。

**`deriveProsody()` 情绪→韵律映射（`lib/tts-prosody.ts:77-…`）** —— 这个模块我认为是**最值得直接移植的一个**：

三层叠加：
1. **情绪关键词 → 基线 prosody**：`EMOTION_BASELINE` 是 16 条正则规则（L37-54）：
   ```
   /狂怒|暴怒|爆炸|咆哮/  → { speed: 1.18, pitch: +2, vol: 1.00 }
   /悲痛|绝望|崩溃/       → { speed: 0.85, pitch: -3, vol: 0.72 }
   /温柔|温暖|轻柔|亲切/  → { speed: 0.96, pitch: +1, vol: 0.80 }
   ```
2. **`emotionTemperature`（-10~+10）→ 连续细调**：归一化 `t ∈ [-1,+1]` 后 `speed ±0.08 / pitch ±3 半音 / vol ±0.05`，再 clamp 到 **MiniMax 合法区间 `speed 0.5~2.0 / pitch -12~+12 / vol 0.3~1.0`**
3. **角色性别/年龄偏置**（`characterProsodyBias`, v12.203）：注释解释「男角默认女声语调、老者用少年语速的**廉价感**，低成本靠角色名纠偏。保守幅度，叠加后 clamp」

**`EMOTION_LABELS`** 导出为 UI 可选项（16 个中文情绪标签），**每个都命中基线的一档**——注释：「改标签 → `deriveProsody` 直接出新 prosody」。这是「UI 选项与算法档位一一对应」的好设计。

**音色定妆表（Voice Cast）——`lib/voice-cast.ts`**，这个设计我特别欣赏：

核心语义（L1-7 注释）：
> "它记录的是**成片事实**，不是配置：这一集已经播出去的声音就是这样。所以
> · 已在表里的角色，后续一律读表，不再参与轮转（**哪怕阵容变了**）；
> · 表里没有的新角色，在**避开已占用音色**的前提下分配，并写回表；
> · 用户手动覆盖（voice-overrides）优先级仍在定妆表之上——那是人的明确意志。"

`resolveWithCast()`（L31-66）纯函数逻辑：
1. 表里有的 → **直接锁定**（「这是成片事实，阵容怎么变都不改」）
2. 新角色 → 先按 `buildVoiceRouting()` 算，再把**与已占用音色冲突的往后顺延**——注释解释了为什么：「轮转算法本身只在『一次性拿到全部阵容』时才保证不撞」
3. 性别/年龄线索只影响**新角色**分配（`hints` 参数），已锁定的永不改

**`VOICE_CAST_TYPE = 'voice-cast'` 作为一个 asset type 落库**——用资产系统存「成片事实」，跨集复用。

**Lipsync 是完整管线，不是单点调用**（README + `lib/lipsync-plan.ts` 13.5KB / `lipsync-align.ts` / `lipsync-qc.ts` / `lipsync-segments.ts` / `lipsync-providers/local-2d.ts`）：
```
per-character voice routing → TTS → viseme keyframe track
→ measured mouth-vs-audio alignment score (Web Audio)
→ drift auto-correct → 引擎 render（wav2lip / SadTalker / MuseTalk，BYO LIPSYNC_API_URL）
→ 写回 timeline
```
- **有一个零配置内置 2D 引擎**（`lipsync-providers/local-2d.ts`，「no BYO key」就能跑出「indicative 2D lip bar」）
- **Vision QC self-heal loop**：弱镜自动重渲
- **诚实的限制披露**（README）：「dialogue audio must be ≥ 2 seconds and the source video must be reachable at a **public URL**；**ja/ko/ru currently degrade to no lip-sync（honest skip, surfaced in the engine-weather panel）**」
- **`lipsync-language-gate.ts`**：语言门——不支持的语言直接跳过而不是产出坏结果

**BGM**：
- `lib/bgm-multi-act.ts`（7KB）：**按幕（act）分段配乐**——不同幕用不同 BGM
- `lib/audio-ducking.ts`：**sidechain 自动闪避**（BGM 在旁白/对白时自动压低）。README 提到实测「响度归一 -14 LUFS（实测 -13.62/-1.42 dBTP 命中平台标准）」
- `lib/impact-sfx.ts`：音效
- `lib/audio-silence.ts` / `audio-health.ts`：**成片音频体检自愈**（ffprobe 缺流补轨）

**字幕是它的招牌之一**（README 亮点 #4）：
> "**Real CJK subtitle burning**（v2.22）— The garbled-Chinese-text-in-AI-video problem solved properly: we **strip dialogue text from the video prompt**（so the model doesn't try to draw garbled glyphs）+ add aggressive negatives（`--no text --no chinese --no captions`）+ **post-bake real subtitles with ffmpeg `subtitles` filter using a system CJK font**（PingFang / Noto Sans CJK）"

配套模块：`lib/subtitle-burn.ts`（9.7KB）、`lib/ass-karaoke.ts`（**词级扫光 karaoke**，7.3KB）、`lib/text-in-frame.ts`、`lib/caption-style.ts`。README 还提到「karaoke 长台词**折行**+行内**缩字**（libass 实渲验证）」和「抖音/小红书**安全区避让**」。

**口型在成片里的位置**（`VideoClip.nativeAudio`, `types/agents.ts:233-234`）：
```typescript
/** v12.29.0(P1):本镜成片是否带原生音频(真由原生音频引擎出片)→ 跳 TTS + composer 取真音轨。 */
nativeAudio?: boolean;
```
**这是应对 MiniMax H3 / Veo 原生音画一体的关键开关**——如果引擎自带音频，就跳过 TTS 和口型。这个字段对我们（用 MiniMax H3！）**直接适用**。

### 6.3 音频侧对比结论

| 能力 | storyforge | wind-comic |
|---|---|---|
| TTS | CosyVoice / GPT-SoVITS | 多 provider 插件式 |
| 情绪→韵律映射 | ✅ 情绪 tag + 三维向量 + 角色基线 | ✅ 16 规则 + 温度连续细调 + 性别年龄偏置 |
| 台词级音频寻址 | ✅ `dialogue_index` + 复合索引 | ✅ `voiceoverClips[{shotNumber, audioUrl}]` |
| 音色跨集复用 | ❌ | ✅ **voice-cast 定妆表（成片事实语义）** |
| 口型对齐 | ❌ **无** | ✅ 完整管线（viseme/align/drift/QC/2D 兜底） |
| BGM | ❌ **无** | ✅ 分幕 BGM + sidechain 闪避 + 响度归一 |
| 音效 | ❌ **无** | ✅ impact-sfx |
| 字幕 | ❌ **无**（只在负面词里防画字） | ✅ libass CJK 烧入 + karaoke 词级扫光 + 安全区 |
| 成片拼合 | ❌ **无**（TASK_010 未完成） | ✅ video-composer（105KB） |

---

## 7. 工程化

### 7.1 storyforge：教科书式的后端工程规范

**这块 storyforge 明显强于 wind-comic。** 它有一份 `docs/MODEL_POLICY.md`，是本次调研中**最有复用价值的单份文档**。

**① 任务队列：Celery + 显式任务清单**（`backend/workflows/tasks.py`）
```python
@celery_app.task(name="workflows.story_generation.run",   bind=True, max_retries=3, default_retry_delay=60)
@celery_app.task(name="workflows.scene_generation.run",   bind=True, max_retries=3, default_retry_delay=60)
@celery_app.task(name="workflows.character_generation.run",bind=True, max_retries=3, default_retry_delay=60)
@celery_app.task(name="workflows.image_generation.run",   bind=True, max_retries=2, default_retry_delay=120)
@celery_app.task(name="workflows.voice_generation.run",   bind=True, max_retries=2, default_retry_delay=120)
```
**不同阶段给不同重试次数和退避**：文本类 3 次/60s，GPU 类 2 次/120s（后者更贵，少重试）。

**② 断点续跑**
- LangGraph 编译时带 `checkpointer=MemorySaver()`（`image_generation.py:460`、`video_generation.py:348`）——**注意这是 `MemorySaver`，进程重启即丢**。【推断】这是一个未完成的实现（生产应换 `PostgresSaver`/`RedisSaver`）。
- 真正有效的是 **`phases: list[str]` 开关**：每个 workflow state 有一个 phases 列表，节点开头检查 `if PHASE_X not in state.phases: return {..., "status": "X_skipped"}`。**这让「只重跑某一段」成为一等能力**（`POST /videos/generate` 可传 phases）。
- `regenerate: bool` 参数贯穿各任务。

**③ 失败重试：显式区分可重试 / 不可重试**
```python
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
# 非可重试：401/403/404/413/422 → 立即 break，换 provider（不浪费重试）
```
退避公式（`router.py:94-98`）：
```python
delay = min(1.0 * (2.0 ** attempt), 30.0)
jitter = random.uniform(0, delay * 0.1)
return delay + jitter
```
文档给了 attempt 0-3 的具体延时（0s / 1s / 2s / 4s），并**按 provider 分别配置**（DeepSeek 3 次/30s，Anthropic 2 次/20s，**Local 1 次/5s**）。

**④ 可观测性：三层**
- **`Job` 表 + progress 百分比**（`models.py:244-255`）：`job_type / status / progress / result(JSON) / error / celery_task_id`
- **`Log` 表**（`models.py:261-267`）：`job_id / level / message / timestamp`——**结构化日志落库，与 job 关联，可被 API 查询**
- **`middleware/`：Request ID + 异常处理器**——每个请求有 trace id
- **`CostLogger` 落 JSONL**（见下）

**⑤ 成本统计：完整的 JSONL schema + 按日轮转**（`services/cost_logger.py` + `docs/MODEL_POLICY.md §6`）

每条记录强制字段：
```json
{"request_id","project_id","provider","model","task","tokens_input","tokens_output",
 "cost_usd","duration_ms","cached","retry_count","status","error"(可选)}
```
落盘到 `logs/cost/{YYYY-MM-DD}.jsonl`，**按日轮转**。另有「per-provider per-minute token counter in Redis，alert threshold: 80% of tier limit」。

**⑥ 缓存：内容寻址 + 分级 TTL**

Cache key 格式：`cache:model:{task}:{project_id}:{content_hash}`

| 实体 | TTL | 理由 |
|---|---|---|
| embedding | permanent | 确定性 |
| summary | 24h | 项目级，昂贵 |
| episode plan | 24h | 派生自 summary |
| character profile | 24h | 只抽一次 |
| **scene script** | **1h** | 可能被重生成 |
| model list | 1h | |
| health status | 30s | 瞬时 |

**`X-Bypass-Cache: true` header 跳过所有缓存层**——专门用于「重生成请求 / 调试 / provider 测试」。这是一个很干净的设计。

**⑦ Model Gateway：统一入口 + 6 级 fallback + 熔断 + 降级**
```
DeepSeek(primary) → OpenAI → Anthropic → Gemini → OpenRouter → Local → AllProvidersExhausted
```
- **健康检查**：每个 adapter 实现 `health()`，**后台每 30s 检查**，`GET /v1/models` limit=1，超时 5s；不健康则**跳过 60s 后重试**
- **熔断状态**：`healthy` / `unhealthy` / `unconfigured` / `rate_limited`（429 则跳过 30s）
- **降级表**（`MODEL_POLICY §8`）：全部云端失败时 `summary→Local`、`episode→Skip(manual)`、`scene→Local`、**`character→Regex extraction`**（很聪明：角色抽取可以用正则兜底）

**⑧ 配置管理：SecretLoader + 启动校验**
- Secret **只在 `.env`**，禁止硬编码/config/migration/日志/错误信息/API 响应
- `SecretLoader.validate()` 启动时检查：至少一个 provider 有 key、格式匹配、**拦截占位值**（`change-me` / `your-key-here`）
- `SecretLoader.mask()` 只显示后 4 位
- **Fail fast on invalid config — do not start with bad secrets**

**⑨ 测试**：308+ tests，分 Unit（mock transport，无网络）/ Integration（需真 key，`@pytest.mark.skipif`）。文档里列了 13 个必测场景（`test_missing_key` / `test_timeout` / `test_non_retryable_error` / `test_cache_bypass` / `test_secret_mask` / …）。

### 7.2 wind-comic：功能极全但工程债显著

**① 任务队列 / 断点续跑：靠「资产表就是检查点」**

`lib/pipeline-checkpoints.ts`（v10.4.2）的设计很巧妙——**不引入独立检查点存储，而是把资产表当检查点读**：

> "续跑（job attempts > 1）时，各阶段先看这里有没有已落库产物：有则装载 + 跳过重新生成（**不重复计费**），没有才真跑。「写」侧配对改动 = `saveAsset` 走 `upsertAsset`。"

`loadCheckpoints()` 从 9 种 asset type 并行装载（`Promise.all`）：
```typescript
plan / script / styleBible / character / scene / storyboard / video / final_video / timeline
```
**关键的形状还原约定**（L7-19 注释，写得很清楚）：
```
plan        ← type 'plan'（v10.4.2 新增落库，导演计划此前只在内存）
script      ← type 'script' { synopsis,title,shots,theme }
characters  ← type 'character'[] { name, description, appearance } + 图
scenes      ← type 'scene'[] { name, description } + 图
storyboards ← type 'storyboard'[] { description→prompt, planData, duration, cameo* } + 图
               有图 = 已渲染；无图 = 仅规划（storyboardRender 只补渲染缺图镜头）  ← 很实用
videos      ← type 'video'[] { duration,status,coverImageUrl } + 片
editResult  ← type 'timeline' data + hasFinalVideo ← type 'final_video' 存在性
review      ← projects.director_notes(JSON)
```
**「有图 = 已渲染，无图 = 仅规划」这个约定让部分完成状态可表达**，比我们的三态 manifest 更细。

`dedupeLatest()`（L61-70）容忍「v10.4.1 时期的历史重复行」，按 `updated_at` 取最新。`assetUrl()` **优先 `persistent_url`**，注释理由：「外链/tmp 会过期，本地副本稳定」。

**② 成本控制：三层护栏（这是我看到的最完整的成本体系）**

| 层 | 文件 | 粒度 | 作用 |
|---|---|---|---|
| 月级 | `lib/budget-enforce.ts` | 按用户按月 | 超了拒绝新请求 |
| 单任务级 | `lib/task-budget.ts` (v12.413) | 一次「一键成片」内 | **超预算暂停等人确认**，而非硬失败 |
| 操作级 | `lib/budget-guard.ts` (v9.3.3) | 单次操作 | 软上限 warn / 硬上限 block |
| 归因 | `lib/cost-attribution.ts` (v9.6.0) | 项目级 | 逐阶段成本拆解 + 省钱提示 |
| 聚合 | `lib/cost-rollup.ts` | 月度 | 正交视图 |

**`task-budget.ts` 的设计动机讲得极好**（L4-19）：
> "`lib/budget-enforce.ts` 管的是**按用户按月**的金额上限……那道闸是对的，但**粒度太粗**——一次「一键成片」内部会连着发几十次付费调用，**中途没有任何刹车点**；等月上限拦住时，这一单已经花掉了。
> 这里补的是**单次任务内**的闸，借鉴 Devin 的 ACU：
> · 每次付费动作先报一次预估花费；
> · 超过任务预算时**暂停等人确认，而不是硬失败**——硬失败会把前面已经花钱生成的东西一起丢掉，那是双输；
> · 里程碑（导演→编剧→分镜→视频）是天然的确认点"

**并且有一条从真实事故学来的约束**（L15-19）：
> "**暂停必须产出可被人看见、可被人回应的东西。** v12.401 那次的教训是：**一道通向不了人的告警，和没有告警是一回事。**
> 所以 `pause` 不是简单地 return false——它带出「已花多少 / 卡在哪一步 / 再放行多少才能继续」，调用方据此才可能给出一个**真能点的按钮**。"

对应 schema：
```typescript
interface TaskBudgetSnapshot {
  limitCny / spentCny / remainingCny;
  state: 'ok' | 'paused' | 'stopped';
  pausedAtStage?: string;      // 卡住时停在哪个阶段
  neededCny?: number;          // 还差多少才能放行 —— 没有这个数字，人无法决定批不批
  byStage: Record<string, number>;  // 各阶段已花明细，里程碑汇报用
}
```
另有 `HARD_MULTIPLIER = 10`：「即便人一直批，也不该无限追加」。

**`cost-attribution.ts` 的省钱提示规则**（L82-91）：
```typescript
if (top.category === 'video' && pct >= 50)
  hints.push(`视频生成占 ${pct}% —— 缩短单镜时长 / 降帧率 / 多引擎竞速取最快达标，可显著省`);
else if (top.category === 'image' && pct >= 40)
  hints.push(`图像分镜占 ${pct}% —— 复用 Style Bible + cref 链减少重生、Vision 自愈只重拍弱镜`);
```
`classifyEngineCategory()` 用**顺序敏感的正则**把 engine 字符串归类（注释：「顺序敏感：口型 > TTS > 视频 > 图像 > LLM，**避免 "gpt-sovits" 误判成 LLM**」）——这个 bug 防御很实在。

**③ 「不使用」成本观测的收益（README 的诚实分析）**

README 有一段我认为**比任何技术细节都重要**的行业分析：
> "**① 制作只占总成本 7.5%，投流占 70-85%。** 前瞻网口径：平台广告投放 82.5%、制作 7.5%、演员 5%、剧本 1.5%。**这意味着「把制作成本再砍一半」对项目盈亏几乎无影响** —— 任何以「更便宜地出片」为唯一卖点的工具，**价值天花板极低**。
> **② 单位经济已经很薄。** CPM 从 2025 下半年约 60 元/千次跌到 2026 年 15-30 元；标准 ROI 仅 1.03-1.07……约 90% 的 AI 短剧公司处于亏损，AI 漫剧爆款率不足 0.1%
> **③ 平台正在给「纯 AI 生成」降权。** 2026 年抖音/爱奇艺/腾讯视频推出分级分账新政，**削减无真人出镜的全 AI 短剧保底资源**"

于是它的价值主张转向（README）：
> "1. **拉高成品率与质量下限，去够那 <0.1% 的爆款率。** ……都是在**减少废片**，而**废片率才是这门生意的真实杀手**。
> 2. **能交付进专业剪辑线**……EDL / FCPXML / 真二进制 AAF
> 3. **自托管 + 开源 + BYO key**"

**这个「降废片率 > 降成本」的判断，直接决定了工程资源该投向审计/质检而不是省钱。** 我认为这对我们的路线选择有实际参考价值。

**④ 多引擎调度与竞速**（`lib/video-providers/registry.ts`）

`selectProviders()` 的筛选链（L55-83 有完整注释）：
```
1. available() === true
2. 满足请求的 capability（I2V / T2V / FLF / S2V）
3. maxDurationSec >= request durationSec
4. 不在 exclude 集合里
5. 按 priority 升序排序
6. 若 prefer 命中，把它顶到第 1 位
```
外加 **软熔断**：`isProviderHealthy(p.id)` —— 冷却中的 provider 跳过。

**`diagnoseEmptyChain()`（v12.422）是一个非常有价值的失败诊断模式。** 它解决的问题（L42-60 注释）：
> "`selectProviders` 会把 `maxDurationSec < 请求时长` 的 provider 全部过滤掉。于是请求超过全链上限时，链是空的 → `dispatchVideoGenerate` 返回 `{result: null, tried: []}` —— **`tried` 是空数组**。上游把 `tried` 拼成原因，拼出来是空串，于是报「video plugin chain empty / all-failed: **no providers**」，然后静默走 fallback，出一个 8-10s 的片子。
> 结果就是：**剧本要 30s，成片是 10s，而决策日志上没有任何一行说时长被降过。**「no providers」这句话还把人往错的方向引 —— 听起来像没配 key 或者引擎都挂了，而**真因是一个静态可知的事实**：没有任何引擎支持这个时长。"

**「把『为什么链是空的』变成一句人话」——这个模式我们应该直接抄。**

**⑤ 配音/视频的 provider 插件契约**（`lib/video-providers/types.ts`）

`VideoGenerateInput` 的 capability 字段设计得很完整：
```typescript
prompt / firstFrameUrl?(I2V) / lastFrameUrl?(FLF) / durationSec / resolution /
aspectRatio / mode(standard|professional) / style /
subjectReferences?: Array<{imageUrl, name?, refImageUrls?}> /   // 多主体一致性
referenceImages?: string[] /                                     // 通用多参
nativeAudio?: boolean /                                         // 原生音画一体
spokenDialogue?: string;                                        // 仅原生音频 provider 读取
```
**`spokenDialogue` 的注释很关键**（L64-68）：
> "要被「念出来」的台词原文 —— 仅原生音频 provider 读取（拼进自身 prompt 让其发声），**不进主 visualPrompt**（非原生引擎看不到 → **不会把 CJK 渲染成画面文字**）"

**这是「台词既要被念出来、又不能被画成乱码字」的解法**，而且和我们用 MiniMax H3（原生音频）直接相关。

**⑥ CI 门禁：`npm run gate:consumer`（零容忍）**

README 提到把「改了守卫却没跟到消费方」这个病**固化成 CI 入库门禁**，且「上线即抓到 2 个人肉复检漏掉的真 SSRF」。`lib/consumer-gate/` 目录存在（`contracts.ts` 19KB、`scan.ts` 8KB、`baseline.ts`、`paid-baseline.json`）。**「契约变更必须同步消费方」作为硬门禁**——这个思路值得借鉴。

**⑦ 工程债（必须说清楚）**
- `services/hybrid-orchestrator.ts` = **254KB**，注释自陈「5471 行 / 113 处 any，其中 6 处在**公开方法签名**上」
- `services/video-composer.ts` = **105KB**
- `lib/i18n.ts` = **154KB**（单文件 i18n）
- `lib/db.ts` = **53KB**
- `lib/style-presets.ts` = **63KB**
- `lib/mckee-skill.ts` = **71KB**
- `lib/script-parser.ts` = **34KB**

**多个超过 30KB 的单文件**。`types/agents.ts` 里那段 v12.225 的注释本质上是在给一个巨大 orchestrator **事后补类型契约**（「神类拆分第一刀」）。这是「功能先行、结构后补」的典型代价。

**对比 storyforge**：227 个文件、2.4MB、最长的 `tasks.py` 39KB，且有 `docs/RULES.md` 规定「All prompts: backend/prompts/. No inline prompt.」——**storyforge 的代码组织纪律明显更好，尽管功能少得多。**

### 7.3 工程化对比总结

| 维度 | storyforge | wind-comic |
|---|---|---|
| 任务队列 | ✅ Celery，按阶段配重试 | 自有 pipeline-worker |
| 断点续跑 | ✅ phases 开关（好）；checkpointer 用 MemorySaver（**未完成**） | ✅ 资产表即检查点 + 部分完成状态 |
| 成本统计 | ✅ JSONL + 强制字段 + 按日轮转 | ✅ **三层护栏 + 逐阶段归因 + 省钱提示** |
| 失败重试 | ✅ 可重试/不可重试分类 + 分 provider 退避 | ✅ 瞬时可重试 vs 直接切兜底（有意排除 timeout） |
| 可观测性 | ✅ Job 表 + Log 表 + Request ID | ✅ decision log + SSE 进度 + 技术监看 tab |
| 配置管理 | ✅ **SecretLoader + 启动校验 + 掩码** | ✅ 全 env，BYO 零代码切换 |
| 缓存 | ✅ **内容寻址 + 分级 TTL + bypass header** | ✅ 结果缓存 LRU、DNA/embedding 缓存 |
| Provider 抽象 | ✅ 接口 + 适配器目录 | ✅ 插件注册表 + capability 筛选 + 软熔断 |
| 失败诊断 | 一般 | ✅ **`diagnoseEmptyChain` 把空链变人话** |
| 代码组织 | ✅ **纪律好，文件小** | ❌ **多个 30KB+ 巨型文件** |
| 文档 | ✅ 13 份专题文档，非常规范 | ✅ README 极详尽但自我推销成分重 |

---

## 8. 成本控制

### 8.1 storyforge：token 侧抠得很细

| 手法 | 位置 | 具体做法 |
|---|---|---|
| **Map-Reduce 摘要** | `docs/PROMPTS.md` | 逐章摘要后再层级合并（batch_size=6），避免把整本小说塞进单次 context |
| **原文截断** | `story_agent.py` | 单章截断 **12K 字符**；理由：「保证 LLM context 又控成本」 |
| **分层 TTL 缓存** | `MODEL_POLICY §5` | embedding 永久 / summary+episode+character 24h / **scene 1h**（因易重生） |
| **内容寻址 key** | `cache_service.hash_content` | 同内容必命中，跨项目也能命中（key 含 project_id 则不能） |
| **缓存 bypass header** | `X-Bypass-Cache: true` | 只在重生成时绕过，避免误用缓存 |
| **并发限流** | `scene_agent.py:219` | `asyncio.Semaphore(3)`；profile 生成 `Semaphore(5)`（`character_agent.py:24`） |
| **情绪映射先查表** | `prompts/voice.py:80-84` | `EmotionResolver.needs_llm()`——命中受控 tag 则零 LLM 调用 |
| **角色 profile 只喂相关章节** | `character_agent.py:136-154` | 过滤出「提到该角色的章节」，无命中才 fallback 到前 3 章 |
| **LLM 兜底用正则** | `MODEL_POLICY §8` | `character → Regex extraction`（全 provider 挂掉时的降级） |
| **确定性 prompt 构建** | `prompts/image.py` / `video.py` | 结构化字段拼 prompt，**不问 LLM**，零 token |
| **文档给出成本账** | `docs/PROMPTS.md` | 40 章小说 ≈ 46 次调用 ≈ **$0.025**（DeepSeek）；每集分镜 ≈ **$0.0010** |

**⚠️ 但有一个反向发现**：`services/model_router/router.py` 的 retry 逻辑里，**超时也走完整退避重试**（L126-134）：
```python
except asyncio.TimeoutError:
    delay = self._backoff_delay(attempt)   # 1s/2s/4s
    await asyncio.sleep(delay)
```
**而 wind-comic 明确反其道而行**（`lib/llm-client.ts:61-65`）：
> "注意：**故意不含 'timeout'** —— 超时重试同端点**代价高**，直接切兜底更划算。"

【推断】storyforge 在长 context 任务（summary 的 read_timeout 120s）上超时重试，代价很高（可能白等 120s + 7s 退避）。**wind-comic 的处理更合理。**

### 8.2 wind-comic：GPU 与 token 双侧

**省 token：**
| 手法 | 位置 | 说明 |
|---|---|---|
| **两级模型分层** | `lib/config.ts` / README | 创意档 `deepseek-v4-pro`（writer/director）+ **快档 `deepseek-v4-flash`**（草稿/润色，秒级、推理 token 少）+ 通用档 `claude-sonnet-4-6`（高频规划/校验） |
| **长文三段采样** | `lib/story-analyze.ts:20-30` | 百万字 → **头 40K + 中 20K + 尾 20K 字符**；注释给理由「人物/设定多在开头立起，关系演变看中段，结局张力看尾段」，且**采样偏差诚实标注**（`sampledOnly` 字段） |
| **DNA prompt ≤200 字符** | `lib/character-dna.ts:96-98` | 注释：「给 Minimax 1500 字 cap 留余地」 |
| **traits 每字段 ≤30 字** | `lib/character-traits.ts:71` | 「build/skinTone/appearance/costume/personality/signature 是自然语言，但要简练」 |
| **剧本截断 12K** | `lib/character-traits.ts:105-106` | 「够大部分短剧，既保证 LLM context 又控成本」 |
| **审计全部纯函数** | `pacing-audit-v2` / `dialogue-coverage` / `tts-prosody` | 零 LLM 调用。`pacing-audit-v2.ts:16`：「全部纯函数 + 既有字段，**零 LLM 成本、零额外调用**」 |
| **`<think>` 剥离** | `lib/llm-client.ts:53-56` | reasoning 模型的 `<think>` 块不给下游 |
| **同网关备用模型优先** | `lib/llm-client.ts:29-35` | 主模型 429/503 时**先切同网关的备用模型**（秒级、同 key），再落慢的 MiniMax 兜底（推理模型 40-100s）——**避免为了容错付慢响应代价** |
| **健康/配额缓存** | `lib/llm-health.ts` / `lib/gateway-budget.ts` | 「已破产网关（配额耗尽/欠费）整段跳过，**省重复 403 往返**」 |
| **懒加载** | `lib/llm-client.ts:98-100` | `await import('@/lib/llm-health')` 动态导入 |

**省 GPU / 少重生：**
| 手法 | 位置 | 说明 |
|---|---|---|
| **Style Bible 全局单锚** | `lib/style-bible.ts` | 一张锚图替代「猜风格」，**降低画风 drift → 减少因风格不一致的重生** |
| **Sketch-Lock 先草图后成图** | `lib/storyboard-sketch.ts` / `lib/stage-sketch.ts` | 每镜先渲 B/W 构图草图，成图**锁定该构图**。「Camera language finally survives the diffusion lottery」 |
| **Vision Audit 只重拍低分镜** | `lib/vision-audit.ts` | `<70` 自动重生，`<85` warning；**不是全片重跑** |
| **Cameo Vision Retry 一次上限** | `types/agents.ts:193` | `cameoRetried` 单次重生上限，避免无限刷 |
| **重生跑完仍不达标 → 待人工复核** | `cameoNeedsReview` | 停止自动重试，交人判断（省额度） |
| **Segment Retake 只重拍坏的 2 秒** | `lib/segment-retake.ts` | **其余 6 秒 `-c copy` 字节复制，不重编码、不重新生成**；时长不变 ⇒ 时间轴/配音延迟/字幕起点/EDL 全不用重算 |
| **抽帧封面（零 T2I 额度）** | README v12.82-120 | 「成片抽帧封面精选（VLM 打分，**零 T2I 额度**）」 |
| **图像尾梯队** | README | 「Seedream 4.5 图像尾梯队（720x1280 原生竖屏，**实测 14s/张**）」 |
| **B-roll 结果缓存 LRU** | README | 素材检索结果缓存 |
| **多引擎竞速取最快达标** | README + registry | 「Seedance / Kling / Veo / Vidu — **first good clip wins**」 |
| **`nativeAudio` 跳过 TTS** | `types/agents.ts:233-234` | 「本镜成片是否带原生音频 → **跳 TTS + composer 取真音轨**」 |
| **确定性降级地板** | README v12.2.x | 「全程**无 key 走确定性地板（精确名+文本匹配），有 key 向量增强**」——没 key 也能跑 |
| **模型雷达（同家族自动升级）** | README v10.6.3 | 一键扫描最新模型 + 同家族自动升级（四护栏 + 回滚） |
| **并发可调 + 诚实的取舍说明** | README | `GEN_CONCURRENCY`（默认 2，max 8），但明确警告「⚠️ 更高的视频并发更快但**削弱关键帧链连续性**（shot N 拉 shot N-1 的末帧）——跨镜衔接重要时保持 1-2」 |

**最后这条特别值得注意**：它把「并发↑ ⇒ 一致性↓」的**权衡关系明确写出来并给了建议值**，而不是无脑拉高并发。这是很成熟的产品判断。

**⚠️ 成本控制的诚实披露**（README）：
> "**H3 is pay-as-you-go only** — MiniMax 的定价页说 Token Plan 不覆盖它（re-confirmed 2026-09-22，$0.08/s at 768P, $0.13/s at 2K）。**On a Token Plan key every H3 call is refused**，于是管线回落到 Hailuo-2.3（v12.446 让那个回落**真的生效**了 —— 它此前**死了 16 天**，因为只认英文错误文本）"

**「兜底分支死了 16 天没人发现」——这是一个关于「兜底路径必须有测试」的绝佳反面教材。** 同段还提到「reference-video motion transfer（v12.448）**完全跑不起来**」。

---

## 9. 值得偷的 Top 10（按「对我们 5090 + ComfyUI + MiniMax H3 + Python 单卡」的性价比排序）

### 🥇 #1 — 把「审计」做成零 LLM 的纯函数层（wind-comic `lib/pacing-audit-v2.ts` + `lib/dialogue-coverage.ts`）

**理由**：这是我们**最缺、最便宜、收益最大**的一块。我们已有分镜表 JSON（`id/sec/chars/seed/prompt`），做审计**不需要任何新数据**。而 wind-comic 用两个纯函数模块证明了：**分镜质量问题可以不花一分钱 token 检测出来**，而且能精确到镜号。

**具体怎么改**：
1. 新建 `audit/` 模块，纯 Python、无 I/O、可单测。
2. 先移植 `dialogue-coverage`：
   - 「对话场景」定义 = 连续镜头 + 同 location（显式 `location` 字段优先，否则取 description 首个逗号前段并剥掉镜头语言词）+ 角色集合有重叠。
   - 检测两类病：**多角色对话只有 1 镜**（缺正反打）、**多角色对话 ≥2 镜但全 wide 无特写**。
   - 输出带镜号的 `rewriteHint`，直接塞回我们的 DeepSeek 拆镜 prompt 作为硬规则（照抄它的 `buildDialogueCoverageBlock()` 四条）。
3. 再移植 `pacing-audit-v2` 四件套：`analyzeConflictShape`（最小二乘斜率 + 峰值突出度）、`findDragSegments`（连续 ≥3 镜低分）、`auditOpening`（前 1/3）、`auditDurationRhythm`（变异系数 + 长镜堆叠）。
   - 需要先有「冲突分」。wind-comic 用**中文冲突词典 + 情绪极性反转**打分。我们可以在 DeepSeek 拆镜时让它顺带输出每镜 `conflict_score: 0-10` 和 `polarity`——**复用同一次调用，零额外 token**。
4. 阈值照抄：短剧模式 `dragSegments threshold=4`、`opening minAvg=5`、`durationRhythm minCv=0.12`。

**为什么排第一**：因为我们现在的「机械质检」是对**已渲染视频**的质检（贵、晚）。这两个模块是**渲染前的分镜质检**——发现「第 3~5 镜连续低冲突」时，我们还没花一分钱 GPU。**把废片拦在渲染之前**，这正好对上 wind-comic README 那句判断：「废片率才是这门生意的真实杀手」。

---

### 🥈 #2 — 逐秒 micro-beat 分镜契约（含时长守恒校验）（wind-comic `MicroBeat`）

**理由**：我们现在的分镜单位是「镜头」（`id/sec/chars/seed/prompt`）。wind-comic 的 `MicroBeat` 把镜头**再往下拆一层**到 2-4 个 2-5 秒的动作段，且**时长之和必须 = 镜头时长**。它解决的是「单段静态描写导致视频引擎动作不连贯」——这是视频生成质量的直接杠杆，且**不增加生成次数**（还是那一次 I2V，只是 prompt 结构更好）。

**具体怎么改**：
1. 分镜表 JSON 每镜增加 `beats: [{ts, startSec, endSec, action, camera, dialogue?, audio?, characters?, mood?, microExpression?, speedRamp?}]`。
2. 在 DeepSeek 拆镜 prompt 里加约束：**2-4 条，每条 2-5s，`sum(endSec - startSec) == sec`**。
3. **加一个确定性校验函数**（这是关键，别只靠 prompt）：Python 检查 `abs(sum - sec) < 0.05`，不满足则**要么本地按比例缩放，要么带错误信息让 LLM 重出**——不要默默放过。
4. `action` 字段的约束照抄其注释：**「必须是可被视频引擎执行的动词链，禁止静态描写」**。
5. `camera` 与 `action` **分离声明**——这样我们的 prompt builder 可以把「运镜」和「动作」拼到 prompt 的不同位置，而不是混在一句话里。

**对我们 ComfyUI + MiniMax H3 的额外价值**：H3 是 I2V/首尾帧，beats 天然映射到「首帧 → 中间动作 → 尾帧」，可以让我们的首尾帧策略更精确。

---

### 🥉 #3 — 角色一致性三层体系：全局风格锚 + Vision 抽 DNA + 参考图优先级链（wind-comic）

**理由**：这是 wind-comic 的**核心技术资产**，而且**三层是解耦的，我们可以只拿其中一层**。我们的现状是「角色定妆照 + 图锁身份 + 文控服装」，缺的是：**全局画风锚**（跨镜风格漂移没管）和 **Vision 抽结构化 DNA**（我们的角色描述还是文字，没有从图里反向固化）。

**具体怎么改**（按性价比推荐顺序）：

**(a) 先做参考图优先级链** —— 成本最低，收益立竿见影。直接照抄 `lib/consistency-policy.ts` 的优先级：
```
cref:  用户锁定的角色图 > 该镜出场角色的定妆照 > 第一个角色定妆照
sref:  该镜场景的概念图 > 同 location 之前镜头的渲染图 > 第一个场景概念图
cw:    用户锁脸 → 125; 主角 → 100; 配角 → 80
```
关键细节：
- **配角用较低 cw（80）**——注释理由「防止把所有角色都画成主角脸」。我们做服装变体时可能也踩了这个坑（权重太高导致脸被锁死、服装改不动）。
- **`SceneAnchorRegistry`：同一 location 只保留首张作为基线锚点**（防风格漂移），且**可持久化 + 重跑时回灌**（`toEntries`/`seed`，首张优先不覆盖）。这正好补上我们「跨镜/跨集场景一致性」的缺口。
- **`reason` 字段记录选取来源**（`crefSource`/`srefSource`/`cwTier`），落进 manifest——出问题时能一眼看出「这张图为什么用了这个参考」。

**(b) 再做 Style Bible 全局单锚** —— 在我们渲染任何角色/分镜之前，先生成 **1 张不含人物/不含脸的 canonical key art**，之后**每次**图生图都把它作为**第 1 张参考图**。照抄 `getMoodWordsForGenre()` 的题材锚词表（古装/赛博/恐怖/校园/言情/职场/民国/动画 8 类）作为起点。
> ⚠️ 注意它有个**重要 caveat**：Style Bible 帧必须「no specific characters, no faces, no body figures」——否则会被误当场景，反而污染。prompt 里那三行 `no specific characters, no faces, no body figures` + `--no people --no person --no character --no face` 要保留。

**(c) 最后做 Vision 抽 DNA** —— 用我们的角色定妆照过一次 Vision（可以是本地 VLM，省 API），抽 8 维：`eyeShape/jawShape/noseShape/mouthShape/hairStyle/hairColor/skinTone/signatureOutfit`。要点：
- **反抽象词约束**（照抄）：「描述视觉特征而不是情绪/气质，**拒绝 "beautiful"/"elegant" 等抽象词**」；未识别写空串不用 null。
- **输出硬限 200 字符**——注入 prompt 时不能太长（我们本地 ComfyUI 的 CLIP 77 token 上限更紧张，可能需要压到 100 字符内）。**【推断】这一点我们需要比它更激进地压缩。**
- **多角色同框用 ` | ` 分隔拼接**所有命中角色的 DNA，同一 DNA 只拼一次。
- **名字三级匹配**（原样精确 → 归一精确 → 子串双向 ≥2 字符）——它修的是「林小满(镜头) vs 小满(dnaMap)」的**静默漏注入**。这个 bug 我们极可能有（我们用中文角色名做 key）。

---

### #4 — 全局资产库 + 「成片事实」语义的跨集复用（wind-comic `GlobalAsset` + `voice-cast`）

**理由**：我们有「跨章节复用」的需求但这个能力还弱。wind-comic 有两个可直接搬的设计：

**(a) `GlobalAsset` schema**：
```python
{id, type: character|scene|style|prop|template, name, description,
 tags[], thumbnail, visual_anchors: string[],   # 3-5 个关键视觉特征，直接用于 prompt 注入
 embedding: number[],                            # 可选，走向量增强
 metadata: {}, referenced_by_projects: []}       # 被哪些项目引用
```
- **`visual_anchors`（3-5 个关键特征）** 比塞整个角色 profile 省 token 得多，专门为 prompt 注入设计。
- **`referenced_by_projects`** 能解决「删资产前警告会破坏哪些项目」。
- **降级策略照抄**：「**无 key 走确定性地板（精确名 + 文本匹配），有 key 向量增强**」。

**(b) `voice-cast` 的「成片事实」语义** —— 这个我认为是**本次调研中最精巧的一个设计**：
> 「它记录的是**成片事实**，不是配置：这一集已经播出去的声音就是这样。所以
> · 已在表里的角色，后续一律读表，不再参与轮转（**哪怕阵容变了**）；
> · 表里没有的新角色，在**避开已占用音色**的前提下分配，并写回表；
> · 用户手动覆盖优先级仍在定妆表之上——那是人的明确意志。」

**三处「不变量」值得学**：
1. **已落盘的不动**（哪怕配置变了）——保证已发布内容的声音不漂移。
2. **新加入的避开已占用**（`taken` 集合 + 顺延）——注释指出真正的坑：「轮转算法本身只在『一次性拿到全部阵容』时才保证不撞」。
3. **人的意志 > 算法**（override 优先级最高）。

**具体怎么改**：把 `voice-cast` 抽象成通用的「**成片事实表（as-shipped registry）**」，不只用于音色——**角色的最终定妆照、场景的基线锚图、关键道具的外观**都适用同一语义。这样跨集复用就有了统一的、防漂移的机制。落库复用我们的 manifest。

---

### #5 — 三层成本护栏：单任务预算必须能「暂停等人批」（wind-comic `lib/task-budget.ts`）

**理由**：我们跑单卡 5090，主要成本是**时间**（GPU 小时）而不是 API 费用，但**「一次整片生成中途没有刹车点」这个问题我们完全一样**——一旦启动 20 镜渲染，中途发现剧本有问题，只能全部作废。wind-comic 的解法是**暂停而非失败**，并且强调「暂停必须产出可被人看见、可被人回应的东西」。

**具体怎么改**：
1. 定义 `TaskBudgetSnapshot`（照抄字段）：
   ```
   limit / spent / remaining / state: ok|paused|stopped /
   paused_at_stage / needed_to_continue / by_stage{}
   ```
2. **把「里程碑」设为我们天然的确认点**：拆镜完成 → 定妆照完成 → 首镜渲染完成 → 全片完成。在每个里程碑汇报「已花多少 / 卡在哪 / 再放行多少能继续」。
3. **`needed_cny` 这个字段必须有**——注释理由「没有这个数字，人无法决定批不批」。对我们应该换成**「预计还需 N 分钟 GPU 时间 / N 次 H3 调用」**。
4. 加 `HARD_MULTIPLIER = 10` 类似的**硬顶**：「即便人一直批，也不该无限追加」。
5. 对我们还要加一层它没有的：**GPU 时间预算**（不只是钱）。因为我们用本地 ComfyUI，边际成本是电费和占用，但**占用的时间是真实成本**。

**为什么这个值得排进前五**：它直接决定我们能不能放心地在大项目上「先跑起来看效果」，而不是「想清楚再跑」。而且它和我们的断点续跑能力（已有）是天然搭配——**预算暂停 + 检查点 = 随时可以停、可以改、可以续**。

---

### #6 — 确定性的「原文对白零丢失」——两个项目都没做，这是我们的空位

**理由**：这是本次调研**最重要的负面结论**，也是最明确的差异化机会。

- **storyforge 完全做不到**：原文在第一章摘要时就被截断到 12K 并被 LLM 摘要（`story_agent.py`），此后所有环节只基于摘要工作。最终 `dialogue` 是 LLM 从 2-4 句 beat 描述**重新创作**的。**它是一条「重新创作」管线，不是「忠实改编」管线。**
- **wind-comic 只对「已是剧本格式」的输入做到了**：`lib/script-parser.ts` 用正则提取 `角色：台词`，保留原文，且用台词数反推主角、按说话人切换自动分场。**但纯小说输入走的是 LLM 改编路线，同样不保证。**

**具体怎么改**（我们的方案，两个项目都没有的）：
1. **在拆镜阶段强制 LLM 输出「对白原文引用」而不是「对白内容」**。分镜表加字段：
   ```json
   {"dialogue": [{"char": "林小满", "src_span": [1234, 1289], "quote": "你到底想干什么？"}]}
   ```
   要求 `quote` **必须是原文的子串**。
2. **用确定性代码校验**：`chapter_text[src_span[0]:src_span[1]] == quote`，不等则**重试或标红**。这是纯字符串比对，零成本。
3. **对白覆盖率审计**：统计原文中所有引号内对白 / `角色：台词` 行，检查有多少被分镜采纳。输出「原文 47 句对白，分镜采纳 44 句，遗漏 3 句：第 N 段…」。**这是「对话零丢失」的量化指标**，可进 manifest，可做 CI 门禁。
4. 复用 wind-comic 的 `isFullScriptInput()` 思路做**输入类型自适应**：如果用户上传的已经是剧本格式，就直接走确定性正则解析（不花 token、零丢失）；如果是纯小说，才走 LLM 改编 + span 校验。

> **这是我认为我们最应该投入的方向**，因为它把「忠实原著」从一个**口号**变成了一个**可验证的指标**，而两个开源项目都留了这个空位。

---

### #7 — 无独立检查点存储：把「manifest / 资产表」当检查点读（wind-comic `lib/pipeline-checkpoints.ts`）

**理由**：我们**已经有 manifest 三态幂等 + 渲染断点续跑**，这个设计我们已经在用了。但 wind-comic 有两个细节能让它更好：

**(a) 「有图 = 已渲染，无图 = 仅规划」的部分完成状态**
> "storyboards ← type 'storyboard'[] { description→prompt, planData, duration, cameo* } + 图。**有图 = 已渲染；无图 = 仅规划**（storyboardRender **只补渲染缺图镜头**）"

**这让「已经规划了 20 镜，只渲染了 7 镜」变成可表达的状态**，且重跑时**只补缺失的那 13 镜**。如果我们的三态是「未开始/进行中/完成」，就表达不了这个，重跑时可能重做已有工作。**检查我们的 manifest 是否支持「逐镜粒度的部分完成」。**

**(b) `persistent_url` 优先于外链**
> "URL 取用优先 `persistent_url`（**外链/tmp 会过期，本地副本稳定**）"

我们本地 ComfyUI 输出应该已经是持久路径，但**如果我们的 manifest 里存过任何临时路径（ComfyUI 的 output 目录、tmp），重跑时会静默失效**。值得检查一遍。

**(c) `dedupeLatest()` 容忍历史重复行**
> "同 (type, shot|name) 取最新一行（**容忍 v10.4.1 时期的历史重复行**）"

**这是一个现实的工程态度**：schema 演进一定会产生重复行，读取侧要能容忍而不是崩溃/取错。

---

### #8 — Provider 能力的显式声明 + capability 筛选 + 空链诊断（wind-comic `video-providers`）

**理由**：我们接的是 **ComfyUI（本地）+ MiniMax H3**，两条路的**能力差异很大**（本地 ComfyUI 无原生音频、时长受显存限制；H3 有原生音频、有时长下限）。如果我们自己写 if/else，很快就会变成意大利面。

**具体怎么改**：
1. **定义 capability 字段**（照抄 `VideoGenerateInput`）：
   ```
   supports_i2v / supports_t2v / supports_last_frame / supports_subject_reference /
   supports_native_audio / max_duration_sec / min_duration_sec / priority
   ```
2. **`select_providers()` 按此筛选 + 排序 + 软熔断**，而不是散落的 if/else。
3. **`diagnose_empty_chain()` —— 这个必须有。** 它解决的是一个极隐蔽的坑：**请求 30s，但所有引擎上限都 < 30s ⇒ 链为空 ⇒ `tried` 也是空数组 ⇒ 上游拼出「no providers」（误导性）⇒ 静默降级出一个 10s 片，而决策日志里没有任何一行说时长被降过。**
   **我们必须确保：任何「能力不满足导致降级」都在 manifest 里留一行显式记录**，而不是静默发生。对我们尤其重要——本地 ComfyUI 的显存上限会直接限制可生成时长。
4. **`native_audio` 分流**（照抄）：H3 能出原生音频时**跳过 TTS 和口型**；ComfyUI 出片时走 TTS + 后配音。这在 manifest 里要**逐镜记录**（wind-comic 用 `VideoClip.nativeAudio`）。
5. **`spoken_dialogue` 与 `visual_prompt` 分离**（照抄，注释很关键）：「台词原文仅原生音频 provider 读取，**不进主 visualPrompt**（非原生引擎看不到 → **不会把 CJK 渲染成画面文字**）」。**这是防「AI 把台词画成乱码字」的结构性解法**，比只在负面提示词里加 `text` 更可靠（storyforge 只做了负面词，wind-comic 两条都做）。
6. **`isTransientVideoError` 与「超时不重试」**（照抄）：`lib/llm-client.ts` 明确「**故意不含 timeout** —— 超时重试同端点代价高，直接切兜底更划算」。**【推断】storyforge 在这里搞反了**（超时也走完整退避重试，长 context 任务上代价很高）。我们本地 ComfyUI 渲染超时更应该直接失败并保留检查点，而不是重试。

---

### #9 — 「导演台」式三态 + 下游失效预告（wind-comic `lib/pipeline-stages.ts`）

**理由**：我们已有「极简 Web 控制台」。wind-comic 这个模块**约 160 行纯函数**，能立刻给控制台加上我们最需要的能力：**「哪些环节过期了、重跑影响什么」。**

**具体怎么改**：
1. 定义环节（对我们）：`章节拆分 → 分镜表 → 角色定妆 → 逐镜渲染 → 质检 → 合成+字幕 → 成片`。
2. 每环节三态：`empty` / `ready` / `stale`。
   - **`stale` 的两个来源**：(a) 被**显式标记**失效（重跑上游时主动置位）——**比时间戳比较更可靠**；(b) 比上游环节旧（时间戳兜底）。
3. **`buildRerunPlan(assets, target)` → `{target, invalidates[], affected_asset_ids[], sequence[]}`**：
   - 我们能算出「重跑『分镜表』会作废『角色定妆 + 逐镜渲染 + 质检 + 成片』，**具体影响这 47 个资产 id**」。
   - UI 在用户点「重跑」**之前**就预告下游影响（照抄它的做法）。
4. **`pipelineHint()` 的一句话提示**（照抄思路）：
   - 有 empty → 「下一步 · 生成『分镜表』」
   - 有 stale → 「建议 · 重生『逐镜渲染』」
   - 全就绪 → 「全链路就绪 · 可导出成片」

**它有一条注释我认为是**工程哲学**级别的**（`pipeline-stages.ts:142-148）：
> "抽成纯函数不是为了复用，是为了**可测**：原来这段三元表达式内联在组件里，测试只能断言源码里出现过 `PLACEHOLDER_LABEL` 字样 —— 把判断条件改成 `false`（有示意图也照说「可导出成片」）那条断言依然绿。**锁写法不锁行为，等于没锁。**"

**「锁写法不锁行为，等于没锁」** —— 这句话本身值得贴在我们 CI 的墙上。我们的机械质检如果只断言「某函数被调用过」而不是「给定输入产出正确判定」，就是同一个病。

---

### #10 — 情绪 → TTS 韵律的确定性映射（wind-comic `lib/tts-prosody.ts`）

**理由**：这是一个**约 200 行、零 LLM 成本、效果直接可听**的模块。我们的中文字幕 + 配音链路正好缺「情绪驱动韵律」。而且它的设计理由对我们完全成立：
> "TTS 调用本就是逐 shot 串行的瓶颈，**再塞个 LLM 就是灾难**。这个函数纯规则 **<1ms** 出结果。"

**具体怎么改**（近乎可以逐行移植）：
1. **16 条中文情绪正则 → 基线 `(speed, pitch, vol)`**，直接照抄它的数值表（`EMOTION_BASELINE`），并保留 `EMOTION_LABELS` 16 个中文标签作为 UI 可选项。
2. **`emotion_temperature`（-10~+10）连续细调**：归一化后 `speed ±0.08 / pitch ±3 / vol ±0.05`，clamp 到后端合法区间。
3. **角色性别/年龄偏置**（`characterProsodyBias`）：注释解释「男角默认女声语调、老者用少年语速的**廉价感**」。
4. **情绪必须由分镜产出**——所以配合 #1：让 DeepSeek 拆镜时**逐镜输出 `emotion` 和 `emotion_temperature`**（复用同一次调用，零额外 token），一路传到 TTS。这样「画面走情绪，配音也跟着走」。
5. **配上 `dialogue_index` 级的音频寻址**（storyforge `Voice` 表的做法）：把每句台词的音频按 `(scene_id, dialogue_index)` 落库 + 复合索引，这样才能支持「只重配第 3 句」而不是重配整场。

**加分项（如果做口型）**：wind-comic 的 `lipsync-providers/local-2d.ts` 是一个**零配置内置 2D 引擎**（「indicative 2D lip bar」），不需要 BYO key 就能出个可看的示意口型；真口型再走 wav2lip/SadTalker/MuseTalk。**「先给一个能跑的兜底，再好引擎增强」** 这个模式值得学（也呼应它全局的「确定性地板 + 向量增强」哲学）。

---

## 10. 明确不推荐借鉴的部分

### ❌ 1. storyforge 的「摘要式」小说链路（最严重的反面教材）

**不要学**：把小说逐章摘要后**丢弃原文**，之后全链路只基于摘要工作。

**原因**：原文在第 1 步就被截断到 12K 并被 LLM 压缩，**对白无法保真，细节必然丢失**。对于漫剧改编，「忠于原著」是用户的核心预期。我们做的是小说章节 → 分镜，**必须保留原文对白并做 span 校验**（见 Top10 #6）。这是一个**架构级**的选择，一旦走错后面很难补。

### ❌ 2. storyforge 的「多角色同框 = 每角色一个视频任务」

**不要学**：`video_generation.py:90-129` 遍历 `characters_present`，每个角色单独提交一个只带自己参考图的视频任务。

**原因**：这在**概念上就是错的**——多角色同框场景根本无法生成（每个任务只会出现一个角色），一个 2 人对话场景会产出 2 段互不相干的视频存在同一个 `scene_id` 下，且没有合并逻辑。图像侧同样（`image_generation.py:139-188`）只产出「角色单体图 × N + 空背景图 × 1」，**没有「多角色同框的一张图」这个概念**。

**替代方案**：wind-comic 的做法——**多参挂载**（`subjectReferences[]` / `extraCrefs[]` 一次带上所有同框角色的参考图）+ **逐角色独立评分**（`cameoPerCharacterScores`，多角色取 min）。我们本地 ComfyUI 用 IP-Adapter/InstantID 多参考图，MiniMax H3 用 `subject_reference[]`。

### ❌ 3. storyforge 的「非阻塞质检」

**不要学**：`scene_agent.py:242-248` —— 连续性校验发现 issues 后**只写 log，然后原样返回**。

```python
if not validation.get("valid", True):
    for issue in issues:
        logger.warning(...)
return list(scenes)     # ← issues 被丢弃
```

**原因**：这是「装了但没通电」的质检。**发现问题必须能阻断、能重生、或至少能呈现在用户面前**。wind-comic 的注释把这一点说得很好（`lib/task-budget.ts:15-17`）：
> "**暂停必须产出可被人看见、可被人回应的东西。** 一道通向不了人的告警，和没有告警是一回事。"

**对我们的具体要求**：我们的「机械质检」如果只是打 log，就等于没有。**每一个质检判定都必须三选一：阻塞 / 触发重生 / 写进 manifest 并在 UI 可见。**

### ❌ 4. storyforge 的「宣称向量去重但传全零向量」

**不要学**：`character_agent.py:236-252` —— 拿到了 embedding 却不使用，Qdrant 查询传 `[0.0] * 384`，且返回的 `results` 从未被消费。

**原因**：这是「文档承诺 > 代码实现」的典型案例（README/文档宣称 Qdrant 向量去重，实际退化成启发式）。**同理 wind-comic 也标了 `h3-availability` 之类「已备好但未接线」的东西。**

**对我们的具体要求**：**未接线的能力必须在代码里显式标注**（像 wind-comic 在 `elements-registry.ts:16` 那样注释「Kling 多角度参考图暂只在本层备好；真正喂进 Kling 需扩契约，Phase 2.1 跟进」），而不是让它看起来能用。我们的 manifest 应该记录「这一步用的是哪个真实路径」，而不是声称的能力。

### ❌ 5. storyforge 的 `MemorySaver()` 检查点

**不要学**：`workflows/image_generation.py:460` / `video_generation.py:348` —— `graph.compile(checkpointer=MemorySaver())`。

**原因**：`MemorySaver` 是 LangGraph 的**内存检查点**，**进程重启即全部丢失**。这与「Resumable Workflow」的宣称直接矛盾。有真正作用的是 `phases: list[str]` 开关（那个设计好，见 Top10 相关）。生产应该用 `PostgresSaver`/`RedisSaver`——或者像我们一样，**检查点落在自己的 manifest 里**（我们已有的做法更简单可控）。

### ❌ 6. storyforge 的「超时也走完整退避重试」

**不要学**：`services/model_router/router.py:126-134` —— `asyncio.TimeoutError` 也走 `_backoff_delay(attempt)` 再重试同一 provider。

**原因**：长 context 任务（`summary` 的 read_timeout 是 120s）超时后重试，代价是「再等最多 120s + 7s 退避」。wind-comic 明确反其道（`lib/llm-client.ts:61-65`）：
> "**故意不含 'timeout'** —— 超时重试同端点代价高，直接切兜底更划算。"

**对我们的具体要求**：本地 ComfyUI 渲染超时应该**直接失败 + 保留检查点**（我们的断点续跑能接住），而不是自动重试。重试留给**明确的瞬时错误**（OOM、端口占用、瞬时 5xx）。

### ❌ 7. wind-comic 的巨型单文件架构

**不要学**：把 orchestrator 写成 **254KB**（`hybrid-orchestrator.ts`，自陈 5471 行 / 113 处 any）、composer 写成 **105KB**、i18n 写成 **154KB**、db 写成 **53KB**。

**原因**：`types/agents.ts:381-388` 那段 v12.225 注释本身就是这个架构的**病历**：
> "病根(🔴-5)：hybrid-orchestrator.ts 5471 行 / 113 处 any，其中 6 处在**公开方法签名**上 —— 这是 agent 之间的契约面，一旦是 any，任何调用方拿到的都是无类型对象，**重构无从谈起**。"

而且它是**事后**补类型契约的（「神类拆分第一刀：先把接口定死，实现再逐步搬离编排器」）。

**对照 storyforge**：227 个文件、最长 `tasks.py` 39KB，且有 `docs/RULES.md` 强制「All prompts: backend/prompts/. No inline prompt.」——**代码组织纪律明显更好**。我们做 Python 单体应用，应该学 storyforge 的纪律（**一个关注点一个模块，prompt 全部外置**），而不是 wind-comic 的「功能先行」。

### ❌ 8. wind-comic 的自我推销式 README / 竞品表

**不要学**：README 里大量的 Elo 排名、竞品对比表、市场数据、「我们有而竞品没有」的声称。

**原因**：虽然它**已经做得很诚实**（主动撤回了 4 条过度声称，并披露「H3 Token Plan 不覆盖」「Sora API 退役」「兜底分支死了 16 天」「HappyHorse 的 `size` 参数根本不存在」），但：
1. 大量数字**无法验证**（我没跑它的测试，「6010 tests green」未经复核）。
2. 竞品数据**时效性极强**（Elo 榜、价格、API 退役日期），很快过时。
3. 它会**误导阅读者把宣传当成能力**——比如「8-agent pipeline」听起来像 8 个独立智能体，实际是同一个 LLM 的 8 段 system prompt。

**对我们的具体要求**：我们的内部文档应该**只写「代码实际做了什么」+「哪些是推断/未接线」**。它对的地方（自我纠错、诚实披露限制）值得学；错的地方（大段市场论证、竞品对比）不要抄。

### ❌ 9. wind-comic 的「8 个 agent 但状态全在内存」（演示版 orchestrator）

**不要学**：`services/agent-orchestrator.ts`（16KB）—— 5 个顺序方法 + `Map<AgentRole, Agent>` 内存状态，**无持久化、无断点、无重试**。

**原因**：这是仓库里的**遗留演示版**，但**它和真正的生产版（254KB）并存**，且文件名更「正」（`agent-orchestrator` vs `hybrid-orchestrator`）。注释里还记录了真实的踩坑（`kelingService` 字段名对应 `KlingService`，注释吐槽「同一个供应商的**第二份实现**，而且是残缺的那份……只有 `/api/create` 这条路上的用户，一直在拿 v1 标准档出片」）。

**对我们的启示**：**不要让两个版本的同一职责并存**。如果我们重构拆镜链路，旧路径要么删掉、要么显式标注 `legacy` 且不允许新调用方进入。**「主路径修好了、旁路没跟上」是最隐蔽的 bug 类别之一。**

### ❌ 10. 两个项目都缺的：成片拼合与中文字幕（storyforge）/ 巨型 composer（wind-comic）

**storyforge 侧不学**：它**根本没有** concat / 字幕 / BGM / 音效（`TASK_010 Editing` = 🔲 未完成，`grep concat` 零命中，`grep srt|subtitle` 只命中负面提示词）。**它产出不了成片，只能产出散装镜头。** 对它的架构可以学，但**成片链路完全不能参考**。

**wind-comic 侧要谨慎**：它的 `services/video-composer.ts` 是 **105KB** 的单文件。虽然功能极全（j-cut/l-cut、转场、BGM、字幕烧入、响度归一、ffprobe 体检、音频自愈），但**这个体量的单文件意味着维护成本极高**。

**我们的做法**：成片合成应该拆成独立的、职责单一的模块（我们已有 concat + 字幕，继续保持简单）：
```
compose/transcode.py    # 统一编码参数
compose/concat.py       # 拼接（xfade / 硬切）
compose/subtitle.py     # 字幕生成 + 烧入（SRT→ASS→libass）
compose/audio.py        # 音轨混音 / 闪避 / 响度归一
compose/probe.py        # ffprobe 体检
```
**并且要抄 wind-comic 的两个具体做法**（这两个是真的好）：
1. **字幕：从视频 prompt 里剥离台词文本 + 激进负面词（`no text/no chinese/no captions`）+ 后处理用系统 CJK 字体真烧**。这比只在负面词里防要可靠得多——**「不要让模型画字，字由 ffmpeg 画」**。
2. **响度归一 -14 LUFS**（平台标准）+ 成片 `ffprobe` 体检（aspect/duration/fps/bitrate/audio/降级镜）。

---

## 附录 A：关键文件索引（便于我们直接回看）

### storyforge（本地已检出：`/tmp/research/storyforge`）

| 用途 | 路径 |
|---|---|
| **模型/缓存/成本/重试政策（最有价值）** | `docs/MODEL_POLICY.md` |
| Prompt 设计 + token 预算账 | `docs/PROMPTS.md` |
| 产品愿景与 phase | `docs/MASTER_PROMPT.md` |
| LangGraph 状态契约 | `backend/workflows/state.py` |
| Celery 任务（含 phases 开关） | `backend/workflows/tasks.py` |
| 图像生成 workflow（6 phase） | `backend/workflows/image_generation.py` |
| 视频生成 workflow（**含多角色缺陷**） | `backend/workflows/video_generation.py` |
| 模型路由（fallback/熔断/退避/降级） | `backend/services/model_router/router.py` |
| 成本日志（JSONL schema） | `backend/services/cost_logger.py` |
| 受控词表 + 连续性校验 prompt | `backend/prompts/scene.py` |
| 角色卡 schema（6 大块） | `backend/prompts/character.py` |
| **确定性 prompt builder（LLM/代码边界范例）** | `backend/prompts/image.py`、`backend/prompts/video.py` |
| 情绪→TTS 向量映射 | `backend/prompts/voice.py` |
| 角色 agent（**含零向量去重缺陷**） | `backend/agents/character_agent.py` |
| 分镜 agent（**含非阻塞质检缺陷**） | `backend/agents/scene_agent.py` |
| 数据模型（Asset 版本/锁定语义） | `backend/domain/models.py` |
| 人机协作 API（feedback 部分更新） | `backend/api/v1/scenes.py`、`backend/api/v1/assets.py` |

### wind-comic（逐文件拉取至 `/tmp/research/wcsrc/`，文件名用 `__` 代替 `/`）

| 用途 | 原路径 |
|---|---|
| **节奏审计 v2（最值得移植）** | `lib/pacing-audit-v2.ts` |
| **对话覆盖度审计（治「AI 感」）** | `lib/dialogue-coverage.ts` |
| **角色 DNA（8 维视觉签名）** | `lib/character-dna.ts` |
| **参考图优先级 + cw 分级 + 场景锚注册表** | `lib/consistency-policy.ts` |
| Style Bible 全局风格锚 | `lib/style-bible.ts` |
| 元素注册表（跨引擎多参适配） | `lib/elements-registry.ts` |
| Agent 契约（types，含 MicroBeat/ScriptShot/EditResult） | `types/agents.ts` |
| **情绪→TTS 韵律（可逐行移植）** | `lib/tts-prosody.ts` |
| **音色定妆表（成片事实语义）** | `lib/voice-cast.ts` |
| 长篇→分集（确定性） | `lib/story-intake.ts` |
| **剧本文本解析（对白零丢失，仅剧本格式）** | `lib/script-parser.ts` |
| 角色特征抽取（LLM，反幻觉约束） | `lib/character-traits.ts` |
| 逐镜 Vision 审计 | `lib/vision-audit.ts` |
| 逐镜风格门禁 | `lib/shot-quality-gate.ts` |
| **单任务预算闸（暂停等人批）** | `lib/task-budget.ts` |
| 操作级预算护栏 | `lib/budget-guard.ts` |
| 成本归因 + 省钱提示 | `lib/cost-attribution.ts` |
| **导演台三态 + 下游失效预告** | `lib/pipeline-stages.ts` |
| 断点装载（资产表即检查点） | `lib/pipeline-checkpoints.ts` |
| LLM 统一客户端（fallback/think 剥离/超时不重试） | `lib/llm-client.ts` |
| **视频 provider 契约（capability + nativeAudio）** | `lib/video-providers/types.ts` |
| Provider 链选择 + 空链诊断 | `lib/video-providers/registry.ts` |
| 片段重拍（字节复制 + 时长不变量） | `lib/segment-retake.ts`、`lib/shot-segment-retake.ts` |
| 资产账本（逐镜 7 维完备性） | `lib/asset-ledger.ts` |
| 全局资产库 DAO | `lib/global-assets.ts` |
| ComfyUI 适配（IP-Adapter / ControlNet） | `services/comfyui.service.ts` |

---

## 附录 B：一句话结论

> **storyforge 是一个「架构与工程纪律优秀、但功能完成度低、且有两处架构级错误」的项目** —— 值得学它的 model gateway / 缓存分TTL / 成本 JSONL / 确定性 prompt builder / 受控词表 / 资产版本与锁定语义；**绝不能学**它的摘要式链路（对白必丢）、多角色同框（每角色一个视频任务）、非阻塞质检、MemorySaver 检查点、超时重试。
>
> **wind-comic 是一个「功能极全、产品成熟、但代码组织失控」的项目** —— 值得学它的纯函数审计层（节奏/对话覆盖度，零 LLM 成本）、三层角色一致性、逐秒 micro-beat、三层成本护栏、导演台三态与下游失效、片段重拍、原生音频分流、诚实降级哲学；**绝不能学**它的 254KB orchestrator、双 orchestrator 并存、以及大段无法验证的自我推销。
>
> **两个项目共同的最大空位**：**小说原文对白零丢失**。storyforge 完全没做，wind-comic 只对剧本格式输入做到了。**这是我们该投入的差异化方向** —— 用「LLM 输出原文 span 引用 + 确定性回填校验 + 对白覆盖率量化指标」把它从口号变成可验证的工程指标。
