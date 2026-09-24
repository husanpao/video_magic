# NiliX H3 提示词体系与剧本解析 · 源码调研报告

> 调研对象：`research/NiliX-main`（用户提供 NiliX-main.zip 解压版，权威最新版）
> 调研方式：**只读**。所有结论均带 `文件:行号` 证据，模板/规则均贴真实代码片段。
> 核心文件：`internal/manju/manju_prompt_align.go`(1310) · `manju_script_parse.go`(2720) · `manju_llm.go`(1174,超长行) · `manju_pipeline.go`(8893) · `manju_comfy.go`(558)

---

## 0. 结论速览

| 问题 | 关键结论 | 证据锚点 |
| --- | --- | --- |
| Ref2VA 六段式 | 模板内置在系统提示词里，**LLM 直出**；机器侧只做「存在性」校验 + 渲染前契约对齐兜底 | `manju_llm.go:1056-1087`；`manju_pipeline.go:3674-3716` |
| FL2VA 三段式选择 | 唯一判据 `hasChar := len(s.Characters) > 0`（有角色→六段式，空镜→三段式） | `manju_pipeline.go:3676,3730`；`manju_llm.go:996` |
| 顺序/唯一性校验 | **顺序只由模板与写作规范约束，无代码校验**；唯一性 = 段落存在性 + 「同一句台词整条 prompt 只出现一次」的写作规范 + 台词去重器 | `manju_llm.go:1103-1104`；`manju_script_parse.go:1336` |
| `<Subject N>`/`<Picture N>` 绑定 | 由 `manjuRefContract`（单一事实源）驱动：视图数 = `shotViewRelsFor`，场景槽 = 人物视图之后，音频槽 = `VoiceRoster` | `manju_prompt_align.go:54-98`；`manju_pipeline.go:6222-6259` |
| 队尾 9 条墙 → ≤4 条 | `manjuGuardStrip` 先剥旧 9 条，再追加 4 条紧凑版 | `manju_pipeline.go:797,833-877` |
| CAMERA/POSITION 前移 | 移到 `detailed_description:` 段标题**之前**（高服从位） | `manju_pipeline.go:1135-1193` |
| 运动镜断链 | 非固定运镜镜**不加载 MotionContext pin**，硬切直出 | `manju_pipeline.go:7311-7317,7358-7367` |
| 表演层纪律 | 全部是**提示词写作规范**（规则 29-33 + 节奏模型/近景补偿），无机械强制 | `manju_llm.go:1023-1024,1135-1139` |
| 拆镜原则 | 全部是提示词指导 + `validatePlan` 软校验（进重写闭环，不硬截断） | `manju_llm.go:854`；`manju_pipeline.go:3198-3273` |
| `script_parse_ver` | 整数代数常量 25，plan 落后即强制重解析；解析失败静默回退 LLM | `manju_script_parse.go:264-338`；`manju_pipeline.go:2966-2990,3042-3093` |
| 旁白预算 clamp | `字速 × charsPerSec`，超预算自动延长时长并 **clamp 到 15s** | `manju_script_parse.go:818-840` |
| LLM 层 | DeepSeek `/chat/completions`，3 次指数退避(2s/4s)，300s 超时，JSON 三层修复，审稿不过重写 1 轮 | `manju_llm.go:44-76,99-251`；`manju_pipeline.go:3617-3644` |
| 台词 → 生成 | `<d>[Chinese]原文</d>`；缺失时机械补写；再经语言标签规范化 | `manju_script_parse.go:1150-1240`；`manju_prompt_align.go:893-938` |
| MotionContext 尾音 pin | ComfyUI 节点 `MiniMaxH3MotionContext`，`audio_context_length` 默认 **"1"（≈25ms）** 专门防多重配音叠音 | `manju_comfy.go:467-527` |

---

## 1. Ref2VA 六段式 / FL2VA 三段式

### 1.1 何时用哪个（选择规则）

唯一判据在本镜是否有登场角色：

```go
// manju_pipeline.go:3674-3689  validateShotPrompt
hasChar := len(s.Characters) > 0
if hasChar {
    for _, sec := range []string{"subject_definitions", "summary", "retention_analysis",
        "detailed_description", "overall_soundscape", "non_diegetic_music"} {
        if !strings.Contains(hp, sec) {
            problems = append(problems, "Ref2VA 缺少六段式字段 "+sec)
        }
    }
} else {
    for _, sec := range []string{"integrated_multimodal_description", "overall_soundscape", "non_diegetic_music"} {
        ...
        problems = append(problems, "FL2VA 缺少三段式字段 "+sec)
    }
}
```

同一判据在 `genShotPromptRaw`（`manju_pipeline.go:3730`）分流系统提示词：

```go
// manju_llm.go:1145-1162  manjuShotPromptSystem
func manjuShotPromptSystem(hasChar bool, style string) string {
    ...
    if hasChar {
        sys += strings.ReplaceAll(manjuRef2vaTpl, "{style}", opening) + manjuShotWritingRules
    } else {
        sys += strings.ReplaceAll(manjuFl2vaTpl, "{style}", manjuStyleShot1(style)) + manjuShotWritingRules
    }
}
```

提示词层面对 LLM 的明示（`manju_llm.go:996`）：

> `- 有角色的镜用 Ref2VA 六段式(subject_definitions/summary/retention_analysis/detailed_description/overall_soundscape/non_diegetic_music),无角色的空镜用 FL2VA 三段式(首行对齐指令 + integrated_multimodal_description/overall_soundscape/non_diegetic_music)——模板见下方`

渲染侧模型选择也是同一判据（`manju_comfy.go:372-375`）：`hasChar` → `unet_ref2va`，否则 `unet_fl2va`。

> ⚠️ 注意：`internal/storyboard/generate.go` 是**另一条独立的老路径**（小说→分镜脚本），其模板 `scriptPromptTemplate`（`generate.go:19-54`）硬写「单集 3-8 个镜头；每镜 duration_sec 取 4-12 的整数」（`generate.go:47`）且**只用三段式**（`integrated_multimodal_description`），与 manju 主线的六段式体系不是同一套。调研主线时应以 `internal/manju/` 为准。

### 1.2 Ref2VA 六段式模板原文

`manju_llm.go:1056-1087`（**逐字引用**，含各段填充规则）：

```go
const manjuRef2vaTpl = `【Ref2VA 六段式(有角色,锁人物),严格此顺序】:
subject_definitions:
<Subject 1> is the character in <Picture 1> and <Picture 2> ... with [完整外观：逐字引用角色卡 appearance（发型/眼睛/疤痕/气质/道具等全部特征逐项覆盖，禁止省略/概括/编造）；服装 costume 全字段；【性别强化·仅人类】(species=人)女=feminine facial structure, soft delicate features, long hair（禁男性化），男=masculine jawline, strong brow, broad shoulders（禁女性化）；【物种分档·2026-08-29 审计修复】species=物品 的角色(器物/植物/法宝/灵植)主体写 the item/plant itself + 本体特征(材质/形制/纹理/标志性细节),严禁写人脸/发型/服装/人形身体;兽类主体写兽形本体特征]
[同一角色多视图:该角色有几个参考图就引用几张——<Subject 1> is the character in <Picture 1> (正面/正脸特写), <Picture 2> (全身/侧面/细节), ...;每张视图对应一个 <Picture N> 标签,顺序与 ref_available 该角色的视图顺序一致,全部引用后统一写 with [外观...]]
[多角色镜:每个登场角色一行 <Subject N> is the character in <Picture A> and <Picture B> ...,与参考图顺序一致(角色在前场景在后);画面里谁先出现谁 Subject 号靠前]
[群像镜纪律·强制(...):动作/画面中出现的每一个人物——包括无名群演(牢卒/士兵/侍卫/侍女/随从/路人/仆役)都必须 subject_definitions 逐一定义:有参考图引用 <Picture N>,无参考图写 <Subject N> is [群演身份] with 独立外观描述(年龄/体型/服装颜色,不引用任何 Picture);严禁省略群演、严禁把多人写成复数笼统词(如 two jailers 必须拆成 <Subject N> 与 <Subject N+1> 两个独立个体);每个 Subject 是独立个体,严禁复用/复制其他 Subject 或参考图人物的外观与脸]
[参考图纪律·强制:ref_available 是「角色+视图」的平铺清单,顺序就是参考图传入顺序;<Picture 1..N> 严格对应清单第 1..N 项(同一角色多视图占多个 Picture 编号),Subject 编号与角色一一对应(Subject 1=清单第 1 个角色,依次),禁止调换/跳过/合并视图;清单外的登场角色(本镜参考图不足)写 <Subject N> is [角色名] with 外观描述(不引用任何 Picture),并保持与参考角色不串脸。【官方机制·2026-08-30 源码核验:H3 文本编码器把参考图按挂载顺序自动标为 Picture 1/2/3…列在提示词之前——<Picture N> 是对第 N 张挂载图的指认,编号错位=模型拿错图(场景图当人脸/别人脸当本角色),subject_definitions 编号必须与 ref_available 清单逐一对齐,这是硬性输入契约不是修辞偏好】]
[外观锁定·强制:每个角色的外观只允许出现角色卡 appearance+costume 里的特征,且逐项覆盖(发型/眼睛/疤痕/服装/道具缺一不可);禁止 generic 泛化词(ordinary/plain/sturdy/average/young man 等),禁止编造角色卡没有的特征(白发/换装/错误年龄);多角色镜严禁把其他角色的特征写进本角色(谁的特征写谁)]
[拟漫化硬规则·...:人类角色均为 semi-realistic stylized illustration of an East Asian/Chinese character...禁止写 japanese anime/manga style...]
[场景编号·强制:场景的 Picture 编号 = 全部角色视图总数 + 1(如 2 角色各 2 视图 → 场景在 <Picture 5>);Subject 编号 = 角色数 + 1]
<Subject N+1> is the [场景名] environment in <Picture M>(M=角色视图总数+1), with [空间结构/材质/光线客观描述，引用场景卡]
[关键道具：<Subject M> is the [道具名] in <Picture M>, with 外观描述；说明与角色互动]

summary:
[reference generation] 本镜任务概述（1-2 句英文，说明目标视频与参考主体关系；任务前缀用官方固定值——参考生成为 reference generation，本管线恒用此值；只引用已定义标签，禁在 summary 引入新标签）

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - 面部/发型/服装与 <Picture 1> 完全一致(兽类=毛色/体型/种族特征一致;物品=本体形态/材质/标志性细节一致)
[多角色镜:每个角色一行 retention_analysis,全部 fully_preserved]
<Subject N+1> (appears in [Shot 1]): fully_preserved - 场景布局/光线/背景与 <Picture M>(场景编号,同 subject_definitions) 一致
[道具行同理]（标记只用官方固定四值：fully_preserved / partially_preserved / attribute_transfer / weak_reference；【官方规范】retention_analysis 内禁写 (Sx) 说话者 ID）

detailed_description:
{style}。[实体锁定句：The face, hairstyle, costume of <Subject 1> must remain exactly as in <Picture 1> throughout the shot; the scene layout of <Subject N+1> must match its reference.; 多角色镜加 Each character must keep their own identity from their own reference picture, never swap or blend identities.]
[拟漫化锚句·强制(人类角色):All characters appear as semi-realistic stylized illustrations, East Asian/Chinese facial features, not photorealistic photos, not Japanese anime style — keep the stylized look consistent with the reference character.;本镜含兽类/物品角色时改用:...]
[Shot 1] [官方建议 350-500 英文词(对话密集优先完整台词时间线):开场构图→主体外观位置→动作状态变化→运镜(类型+幅度+速度,句内自然英语)→光影→台词/旁白→收尾；<Subject N> 标签在主体首次出现处插入,后续镜复用同标签不重定义；情感戏/对话优先近景/中景；末尾散文排除项 no subtitles, no text overlays, no watermark；【亮度护栏·强制】...never render the frame nearly black]

overall_soundscape:
环境底噪/动作音效（1-4 句英文连续段落，禁重复台词）

non_diegetic_music:
纯器乐配乐（1-3 句：乐器+速度+节奏+动态，禁抽象情绪词；无配乐写 N/A）【BGM 定向文案·强制】...`
```

### 1.3 FL2VA 三段式模板原文

`manju_llm.go:1089-1096`：

```go
const manjuFl2vaTpl = `【FL2VA 三段式（空镜/转场，无主角），严格此顺序】：
第一行对齐指令（两位小数）：How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video.（尾帧锚定时补 Picture 2 (from Shot N) aligns with the S.SS-second mark，S.SS=镜头时长两位小数）
空一行后：
integrated_multimodal_description:
{style} + 画面延续首帧（首帧锚定→动作展开→收尾）+ 动作/运镜/光影 + 台词/旁白 <d>…</d>（d 标签内=中文原文,H3 原生配音;时长严格=镜头秒数）+【亮度护栏】...

overall_soundscape:
non_diegetic_music:【BGM 定向文案·强制】...`
```

注意 FL2VA 的 `overall_soundscape:` / `non_diegetic_music:` 在模板里是**空标题**（内容由 LLM 填），而 Ref2VA 每段都给了示例内容。

`{style}` 的填充来自 `manjuStyleDesc(style).opening`（Ref2VA）与 `manjuStyleShot1(style)`（FL2VA），分别见 `manju_llm.go:1154-1157` 与 `manju_llm.go:1052-1054`。

### 1.4 顺序与唯一性如何校验

**顺序**：只有两层约束，均**无代码级顺序断言**：
1. 模板字面顺序 `【Ref2VA 六段式(有角色,锁人物),严格此顺序】`（`manju_llm.go:1056`）。
2. 系统提示词输出体积硬约束：`- 六段式必须完整(字段齐全);subject_definitions 逐项列角色/场景(不展开;每个 <Subject> 一行)`（`manju_llm.go:1148`）。

**唯一性**：代码里只有「段落存在性」校验（`manju_pipeline.go:3678-3682` 的 `strings.Contains`，**不检查顺序也不检查是否重复出现**）。真正的唯一性靠三条：
1. 写作规范第 3 条：`同一句台词在整条提示词中只能出现一次，禁止重复贴原文（重复句会被 H3 念两遍）`（`manju_llm.go:1103`）。
2. 写作规范第 4 条：`同一句台词/旁白/内心独白在整条提示词中只能出现一次`（`manju_llm.go:1104`）。
3. 机械去重器 `scriptDedupShotLines`（`manju_script_parse.go:1336`，注释见 `1317-1335`）+ 内心/旁白双写去重（`manju_script_parse.go:1275-1312`）。

**完整性**：`validateShotPrompt` 六段逐字段 `Contains` + 契约校验 `validatePromptContract`（Picture 槽位 / 说话者跳号 / 时码越界 / 语言标签 / 内心戏 Q 版），见 `manju_pipeline.go:3711-3714` → `manju_prompt_align.go:952-1030`。

### 1.5 `<Subject N>` / `<Picture N>` 如何绑定参考图

**单一事实源是 `manjuRefContract`**（`manju_prompt_align.go:54-98`）：

```go
// manju_prompt_align.go:54-98
type manjuRefContract struct {
	Chars       []manjuCharSlot // 登场角色(≤3,顺序=挂载顺序=plan characters 顺序)
	SceneName   string          // 场景中文名(场景行归属锚)
	SceneSlot   int             // 场景图槽位号(1 起;无场景图=0)
	PicSlots    int             // 参考图总槽位数
	VoiceRoster []string        // 有音色音频的角色 id(登场顺序;音频挂载顺序=此序)
}

func (ctx *manjuCtx) refContractFor(s manjuShot) manjuRefContract {
	c := manjuRefContract{SceneName: s.Scene}
	n := len(s.Characters)
	if n > 3 { n = 3 }
	slot := 0
	for i, cid := range s.Characters {
		if i >= 3 { break }
		cs := manjuCharSlot{ID: cid}
		if k := len(ctx.shotViewRelsFor(s, cid, i, n)); k > 0 {
			cs.PicStart = slot + 1
			cs.PicEnd = slot + k
			slot += k
		}
		c.Chars = append(c.Chars, cs)
	}
	if s.Scene != "" && fileExists(filepath.Join(ctx.assetsDir, "scenes", s.Scene+".png")) {
		slot++
		c.SceneSlot = slot
	}
	c.PicSlots = slot
	...
}
```

**实际挂载与提示词共用同一数据源**（`manju_pipeline.go:6215-6259`）：

```go
// manju_pipeline.go:6217-6239  charRefNames
// 同一角色多视图按 <Picture N..N+k> 顺序传入,与 prompt 的 subject_definitions 一一对应。
func (ctx *manjuCtx) charRefNames(s manjuShot) []string {
	var out []string
	n := len(s.Characters)
	if n > 3 { n = 3 }
	for i, cid := range s.Characters {
		if i >= 3 { break }
		for j, rel := range ctx.shotViewRelsFor(s, cid, i, n) {
			name := fmt.Sprintf("dir_char_%d_%d_%d.png", s.ID, i, j)
			_ = copyFile(filepath.Join(ctx.assetsDir, rel), filepath.Join(ctx.comfyInput, name))
			out = append(out, name)
		}
	}
	return out
}
```

`shotViewRelsFor`（`manju_pipeline.go:6245-6259`）是视图预算的单一数据源：内心戏镜（`s.Narration` 含 `内心·`）→ `[front, q]`；真身形态镜 → `[form2]`；否则 `charViewRels`。`charRefNames`（实际挂载）与 `shotRefViews`（提示词 Picture 清单）**共用它**，这是「编号对齐」的结构性保证（注释 `manju_pipeline.go:6219-6221`）。

**三层防御**（因为 LLM 直出会写错编号）：

1. **源头自修**：`validatePromptContract` 校验 `<Picture N> > expectSlots`、`(Sx)` 跳号、时码越界、`<d>` 缺语言标签 → 携带问题清单让 LLM 重写（`manju_prompt_align.go:944-1030`）。
2. **机器重排** `alignPictureRefs`（`manju_prompt_align.go:192-298`）：按行归属（场景行含 `environment` 或场景中文名；人物行含 `manjuPersonSignalWords` 且不含 `manjuEnvSignalWords`；其余低置信行不动）把 `<Picture N>` 重写到契约槽位。**行级替换**而非全文替换，这正是 2026-09-03 修「换脸」的关键：

```go
// manju_prompt_align.go:287-296
// 2026-09-03 行级替换根治换脸(递了三千年 EP01 镜8 实锤):旧实现按 remap 全文
// 替换 <Picture N>,前提假设"同一旧号全 prompt 同义"——但多主体错位场景下
// Subject 1 的 <Picture 4> 需改 1、Subject 2 的 <Picture 4> 本就正确(4→4 不登记),
// 全文替换把 Subject 2 正确的 4 也改成 1 → 两角色同指一张定妆照 = 换脸。
for line, m := range lineMaps {
    hp = strings.ReplaceAll(hp, line, replacePictureTags(line, m))
}
```

3. **权威清单注入** `injectAttachmentManifest`（`manju_prompt_align.go:344-375`）：在 `subject_definitions:` 段首插入机器生成的确定性事实句，直接对冲 LLM 编号错位：

```go
// manju_prompt_align.go:354-366
parts = append(parts, fmt.Sprintf("<Picture %d-%d> are the character %s (multiple views of the same person)", cs.PicStart, cs.PicEnd, cs.ID))
...
parts = append(parts, fmt.Sprintf("<Picture %d> is the scene/environment reference, NOT a person - never copy a face from it onto any character", c.SceneSlot))
manifest := manjuAttachmentManifestKey + ": " + strings.Join(parts, "; ") +
    ". Use these exact tags with these exact meanings in every section below."
```

4. **超界剥除** `manjuStripDanglingPictureRefs`（`manju_pipeline.go:1839-...`）：`n > picSlots` 的引用整段删除并清理悬空连接词。

测试锚点：`manju_prompt_align_test.go:229-262`（重排到挂载序/场景槽）、`371-395`（行级替换防换脸）、`160-174`（权威清单）。

---

## 2. 渲染纪律注入

### 2.1 纪律条目清单（全文逐字）

全部纪律常量集中在 `manju_pipeline.go:652-815` 与 `1195-1400`：

| 纪律 | 定义行 | 文案（截断） |
| --- | --- | --- |
| `manjuFrameGuard`（完整版） | 658 | `FRAME DISCIPLINE: this shot contains ONLY the characters listed in subject_definitions; every face in the frame belongs to these characters alone - absolutely no other people, no extra faces, no bystanders...` |
| `manjuConsistencyGuard` | 665 | `IDENTITY CONSISTENCY: the characters' facial features, hairstyles, outfit styles and colors, accessories, body proportions, positions in the frame, and the scene layout and lighting direction must remain unchanged throughout this shot, matching the reference pictures exactly - no facial drift, no hair or costume changes, no lost accessories` |
| `manjuMotionGuard` | 673 | `MOTION DISCIPLINE: maintain continuous visible motion through every second of this clip until the final frame...` |
| `manjuChainGuard` | 685 | `CHAIN DISCIPLINE: if this clip opens on pinned continuation frames from the previous clip, hold that exact closing composition for about one second...` |
| `manjuExecutionGuard` | 696 | `EXECUTION DISCIPLINE: act out every scripted action in detailed_description visibly and completely...` |
| `manjuLipGuard` | 702 | `LIP DISCIPLINE: speech is performed ONLY by the on-screen character who is visibly speaking...` |
| `manjuAudioGuard` | 777 | `AUDIO DISCIPLINE: every spoken line in this clip comes ONLY from the text inside <d> tags...` |
| `manjuNoRefGuard` | 783 | `REFERENCE NOTE: no reference picture is attached to this clip...` |
| `manjuAudioLipGuard`（合并版） | 801 | `AUDIO & LIP DISCIPLINE: ...` |
| `manjuSilentShotGuard` | 808 | `AUDIO & LIP DISCIPLINE: this clip is a silent-acting shot - it contains NO dialogue and NO human voice of any kind...` |
| `manjuMotionSeamGuard`（合并版） | 812 | `MOTION & SEAM DISCIPLINE: ...` |
| `manjuFrameGuardCompact` | 815 | `FRAME DISCIPLINE: this shot contains ONLY the characters listed in subject_definitions - no other people, no extra faces...` |
| `CAMERA DISCIPLINE` | 1151 | 动态拼接（见 2.3） |
| `POSITION DISCIPLINE` | 1184 | `POSITION DISCIPLINE: every on-screen character must appear at a specific screen position - left/center/right third of the frame combined with foreground/midground/background depth - with a clear facing direction; no character may float without a position, and once the relative arrangement of characters is set it must not flip within the shot` |
| `manjuCinematographyGuard` | 1201 | `CINEMATOGRAPHY: photorealistic cinematic film look - motivated natural lighting, true skin tones and material textures, shallow depth of field, subtle film grain, muted filmic grading, physically grounded motion weight; no anime, cartoon, CGI or over-stylized rendering.` |
| `CROWD DISTANCE` | ~1290 | 远景人海避脸正向写法（`injectCrowdDiscipline`） |
| `SCREEN CONTENT` | ~1360 | 屏幕内容兜底（`injectScreenDiscipline`，`manju_pipeline.go:1326-1389`） |
| `REFERENCE NOTE`（场景图变体） | 871 | `the only attached picture is a scene/environment reference, NOT a person...` |

### 2.2 「队尾 9 条墙 → ≤4 条紧凑合并」

实现在 `manjuFinalizePromptPure`（`manju_pipeline.go:817-877`）。设计意图与实测依据写在函数注释里：

```go
// manju_pipeline.go:824-832
// 2026-09-02 纪律瘦身重排(我的影子会咬人 EP01 实证:提交全文 8453 字符中 6770 字符
// =80% 是 soundscape 之后的 9 条纪律墙,CAMERA/POSITION 排最后,H3 注意力稀释,
// 运镜/站位纪律实测无效——镜4 横移 0.00px、镜7 center→x0.32):
//   ① 先剥除存量纪律行(旧 9 条+合并版,先删后插自愈,幂等);
//   ② 队尾 ≤4 条紧凑版:AUDIO&LIP 合并 + MOTION&SEAM 合并 + FRAME 紧凑 + IDENTITY
//      (有参考图才注入)+ REFERENCE NOTE(无卡人物镜才注入);
//   ③ CAMERA/POSITION 移到 detailed_description 段标题前(高服从位...)
```

「先删后插」的幂等锚（`manju_pipeline.go:795-797`）：

```go
var manjuGuardStrip = regexp.MustCompile(`(?m)^\s*(?:FRAME DISCIPLINE|REFERENCE NOTE|AUDIO DISCIPLINE|AUDIO & LIP DISCIPLINE|MOTION DISCIPLINE|MOTION & SEAM DISCIPLINE|EXECUTION DISCIPLINE|LIP DISCIPLINE|CHAIN DISCIPLINE|IDENTITY CONSISTENCY|CAMERA DISCIPLINE|POSITION DISCIPLINE):[^\r\n]*\r?\n?`)
```

队尾注入实体（`manju_pipeline.go:841-875`）：

```go
	// ① 剥除存量纪律行(存量 plan 已固化 9 条墙,先删后插自愈到新结构)
	hp = manjuGuardStrip.ReplaceAllString(hp, "")
	hp = regexp.MustCompile(`\n{3,}`).ReplaceAllString(hp, "\n\n")
	hp = strings.TrimRight(hp, " \n")
	hasSubjects := strings.Contains(hp, "subject_definitions:")
	// ② 尾部紧凑纪律(≤4 条;顺序=重要性:台词→身份→人脸→运动接缝)
	if strings.Contains(hp, "<d>") {
		hp += "\n" + manjuAudioLipGuard
	} else {
		hp += "\n" + manjuSilentShotGuard
	}
	if strings.Contains(hp, "<Picture ") {
		hp += "\n" + manjuConsistencyGuard
	}
	if hasChars || hasSubjects {
		hp += "\n" + manjuFrameGuardCompact
	}
	hp += "\n" + manjuMotionSeamGuard
	if !hasChars && hasSubjects {
		if picSlots >= 1 {
			hp += "\n" + "REFERENCE NOTE: the only attached picture is a scene/environment reference..."
		} else {
			hp += "\n" + manjuNoRefGuard
		}
	}
```

注意此处按「本镜有无 `<d>`」分流：**有台词 → `AUDIO & LIP DISCIPLINE`；无台词 → `SilentShotGuard`**（幽灵人声根治，`manju_pipeline.go:850-857`；`manju_pipeline.go:803-808` 注释说明 ASR 实证无台词镜仍自发生成人声）。

### 2.3 「内联 detailed_description 段前高服从位」

`injectCameraDiscipline`（`manju_pipeline.go:1135-1171`）与 `injectPositionDiscipline`（`1176-1193`）都是：先正则剥旧行 → 找 `detailed_description:`（或 `integrated_multimodal_description:`）→ **插到段标题前**。

```go
// manju_pipeline.go:1131-1171
// injectCameraDiscipline 运镜必达纪律注入(2026-08-30 五问整改问题④;2026-09-02
// 位置重排:队尾→detailed_description 段标题前——我的影子会咬人 EP01 实证,队尾
// 9 条纪律墙稀释注意力,运镜纪律排最后实测无效(镜4 横移 0.00px)。先删后插自愈
// 存量 plan 的队尾旧句,幂等;纯固定镜(解析不出短语)不注入。
func injectCameraDiscipline(hp, camera string) string {
	reGuard := regexp.MustCompile(`(?m)^\s*CAMERA DISCIPLINE:[^\r\n]*\r?\n?`)
	hp = reGuard.ReplaceAllString(hp, "")
	hp = regexp.MustCompile(`\n{3,}`).ReplaceAllString(hp, "\n\n")
	hp = strings.TrimPrefix(hp, "\n")
	ph := manjuCameraPhrase(camera)
	if ph == "" {
		return strings.TrimRight(hp, " \n")
	}
	guardLead := "the camera "
	if rs := []rune(ph); len(rs) > 0 && (unicode.IsUpper(rs[0]) || strings.Contains(ph, ",")) {
		guardLead = "this shot's camera performs "
	}
	guard := "CAMERA DISCIPLINE: " + guardLead + ph
	if strings.Contains(strings.ToLower(ph), "static") {
		guard += "; the camera stays locked but on-screen character action or environmental motion must keep every second of the frame alive"
	} else if strings.Contains(ph, "frames the subject") {
		guard += "; the framing holds while on-screen character action or environmental motion keeps every second of the frame alive"
	} else {
		guard += "; keep that camera movement visible from the first frame to the last frame - never settle into a static locked-off frame"
	}
	di := strings.Index(hp, "detailed_description:")
	if di < 0 { di = strings.Index(hp, "integrated_multimodal_description:") }
	if di < 0 { return strings.TrimRight(hp, " \n") + "\n" + guard }
	return hp[:di] + guard + "\n\n" + hp[di:]
}
```

`POSITION DISCIPLINE` 同构（`1176-1193`），条件 `strings.Contains(hp, "subject_definitions:")`（无人物镜不注入），注释明确理由：`镜7 脚本 center 成片 x=0.32 实测站位纪律在队尾无效`（`manju_pipeline.go:1173-1175`）。

同一「段前高服从位」策略还用于 `CINEMATOGRAPHY`（`1227-1234`）与 `CROWD DISTANCE`（注释 `manju_pipeline.go:1247-1248`：`位置在 detailed_description 段标题前(队尾纪律 H3 注意力弱,实测无效)`）。

编排汇点 `finalizeAlignedPrompt`（`manju_pipeline.go:955-995`）按固定顺序串联全部注入：

```go
func (ctx *manjuCtx) finalizeAlignedPrompt(hp string, s manjuShot, picSlots int) string {
	c := ctx.refContractFor(s)
	out := manjuFinalizeAlignedReg(hp, c, s.Duration, s.Dialogue, len(s.Characters) > 0, picSlots, ctx.speakerReg)
	out = markOffscreenSays(out)
	out = ctx.fixSubjectHairColor(out, s)
	out = ctx.fixChibiEmotion(out, s)
	out = manjuOfficializeCameraVerbs(out)
	out = injectCameraDiscipline(out, s.Camera)
	out = injectPositionDiscipline(out)
	out = injectCrowdDiscipline(out, len(s.Characters) > 0)
	out = injectCinematographyDiscipline(out)
	out = injectScreenDiscipline(out)
	return ctx.injectAudioTimbrePhrases(out, c)
}
```

### 2.4 「运动镜断链」修的是什么 bug

**修的 bug**：横移(Pan)等运动镜被 MotionContext pin 上一镜尾帧后，H3 强锚定上一镜收尾构图，把本镜的运镜指令当矛盾丢弃 → 运镜实测无效（背景位移 0.00px）。

判定函数与断链点（`manju_pipeline.go:7309-7317,7358-7367`）：

```go
// manju_pipeline.go:7309-7317
// manjuCameraIsStatic 运镜列是否固定镜(2026-09-02 运动镜断链判定):「固定
// （Static）」类不接缝断链;推/拉/摇/移/跟/环绕等运动镜返回 false。
func manjuCameraIsStatic(camera string) bool {
	c := strings.TrimSpace(camera)
	if c == "" { return true } // 无运镜列视为固定
	return strings.Contains(c, "固定") || strings.Contains(strings.ToLower(c), "static")
}
```

```go
// manju_pipeline.go:7358-7367
	chained := !fresh && idx > 1 && fileExists(h3ContextLatentPath(ctx.comfyOutput, ctx.latentNS(), idx-1))
	// 2026-09-02 运动镜断链(我的影子会咬人 EP01 镜4 实锤:横移 Pan 镜 pin 上一镜
	// 尾帧续写,提交文本内联 pan 句×2 仍被无视,实测背景位移 0.00px——pin 链式下
	// H3 强锚定上一镜收尾构图,与 pinned 帧矛盾的运镜指令按官方「矛盾=并集」逻辑
	// 被丢弃;首镜无 pin 时推近 +6.8% 正常生效)。非固定运镜镜不加载 MotionContext,
	// 断链直出新构图——运动镜要运镜,衔接处允许硬切。
	if chained && !manjuCameraIsStatic(s.Camera) {
		chained = false
		lg.logf("  🎥 镜头 " + strconv.Itoa(s.ID) + " 运动镜断链直出(" + strings.TrimSpace(strings.SplitN(s.Camera, "（", 2)[0]) + ")——不接缝,保证运镜执行")
	}
```

`chained` 随后传入 `h3RenderWorkflow`，为 `false` 时不建 `MiniMaxH3MotionContext` 节点（`manju_comfy.go:467-488`）。

### 2.5 其它机械修正（渲染前兜底）

在 `manjuFinalizeAlignedReg` 链内（`manju_prompt_align.go:114-131`），按注释声明的依赖顺序执行：

```go
// manju_prompt_align.go:102-131
// manjuAlignShotPrompt 契约对齐编排(纯函数,幂等)。顺序有依赖:
// Picture 重排最先(行归属解析依赖原始 subject_definitions 结构)→ 权威清单注入 →
// Sx 镜内重编(Audio 行的 Sx 需要画面段定序结果)→ Audio 规范化(Sx 用 dialogue
// 发声角色序重写——Audio 行内旧 Sx 本身是 LLM 乱编值,不可信)→ 语言标签 → 时码。
func manjuAlignShotPromptReg(hp string, c manjuRefContract, durationSec int, dialogue string, reg map[string]string) string {
	if hp == "" { return hp }
	hp = repairRetentionMarkers(hp)
	hp = alignPictureRefs(hp, c)
	hp = injectAttachmentManifest(hp, c)
	hp = alignSpeakerIDsReg(hp, c, reg, dialogue)
	hp = alignAudioDefsReg(hp, c, dialogue, reg)
	hp = alignDialogueLangTags(hp)
	hp = alignShotTimecodes(hp, durationSec)
	hp = fixShotPromptContent(hp, c, dialogue)
	return hp
}
```

七个模块 + 内容级兜底：

| 模块 | 函数 | 行 | 修什么 |
| --- | --- | --- | --- |
| 一 | `repairRetentionMarkers` | 150-159 | `(appears in Shot 1])` 括号损坏 → `(appears in [Shot 1])` |
| 二 | `alignPictureRefs` | 192-298 | 编号错位/换脸（行级替换） |
| 三 | `injectAttachmentManifest` | 344-375 | 权威挂载清单（确定性事实） |
| 四 | `alignSpeakerIDsReg` | 400-483 | 镜内/全局说话者重编（EP01 实锤 S6/S11 悬空引用） |
| 五 | `alignAudioDefsReg` | 676-798 | Audio 绑定改 `<Subject M> (Sx)`、编号压缩、幽灵定义删除 |
| 六 | `alignDialogueLangTags` | 899-938 | `[中文]`→`[Chinese]`；裸中文补标签 |
| 七 | `alignShotTimecodes` | 1037-1090 | 全片时码 → clip-local；retention 段保护 |
| 兜底 | `fixShotPromptContent` | 1155-1162 | chibi 空引用清理 + 画外台词强制 off-screen |

「同一句台词只出现一次」的机械层还有 `scriptValidateShots` 的内心/旁白双写去重（`manju_script_parse.go:1275-1312`）。

**发色校正** `fixSubjectHairColor`（`manju_pipeline.go:1563`，注释 `1532-1562`）：以角色卡 `image_prompt` 的发色为权威，纠正 LLM 直出 `subject_definitions` 的发色（实锤：沈照卡 platinum-white，LLM 写 black → 白发变黑）。

---

## 3. 表演层纪律（逐条定位）

> **重要结论**：以下六项**全部是提示词写作规范（LLM 软约束）**，我在代码中**未找到任何机械强制/注入实现**（无 `inject*` 函数、无 Go 侧校验）。它们通过 `manjuDirectSystem` / `manjuScriptSystem` / `manjuShotPromptSystem` 三套系统提示词进入 LLM。唯一擦边的机械实现是 `fixChibiEmotion`（按 narration 情绪给 chibi 行注英文表情短语，`manju_pipeline.go:1710`）与机械质检 WARN（`manju_script_parse.go:1270-1273`，有台词但画面描述无动作/表情词 → 告警）。

### 3.1 情绪三层

`manju_llm.go:1030`（脚本模式规则段）与 `manju_llm.go:1135`（逐镜写作规范第 29 条）：

```
29. 【表演层·情绪三层拆解·强制】(2026-08-24 知识库「H3提示词优化5层结构方法论」整合:专治蜡像脸/假表情)H3 把抽象情绪形容词当低质量指令——禁止直接写"她很伤心/愤怒/害怕"(模型只会出呆滞假脸),必须把情绪翻译成**三层物理细节**:①外部动作(可观察:转身/握拳/低头/咬唇);②生理反应(不可控真相:瞳孔收缩/喉结滚动/下眼睑微红/鼻翼轻颤);③量化指标(振幅<1mm/时长1.5s/眉心上聚2mm/单侧嘴角下沉0.5°)。detailed_description 人物镜按此三层写表演,禁止情绪形容词单独出现
```

`manju_llm.go:1030` 的同义简版：

```
【表演层·强制】(2026-08-24 知识库整合,专治蜡像脸)情绪镜禁止情绪形容词,按三层物理细节拆解:①外部动作(转身/握拳/低头/咬唇)②生理反应(瞳孔收缩/喉结滚动/下眼睑微红/鼻翼轻颤)③量化指标(眉心上聚2mm/单侧嘴角下沉0.5°/振幅<1mm)。表情=眉眼/嘴角/肌肉/呼吸/光影五维组合;哭戏四梯度(强忍→无声→抽泣→崩溃);非对称+克制中断去 AI 感;约束写可见终态不写"保持一致"
```

### 3.2 微表情（五维拆解）

`manju_llm.go:1136` 第 30 条：

```
30. 【微表情五维拆解·强制】(2026-08-24 知识库「AIGC人物微表情设计指南」整合)表情=眉眼/嘴角/面部肌肉/呼吸节奏/光影质感五要素组合,不是单个情绪词。人物特写/近景镜至少覆盖 3 个维度:眉眼状态(眉位高低/眉形收放/眼部张力/视线聚焦)、嘴角唇部(上扬下压幅度/唇部紧绷/嘴型张合)、面部肌肉(额部/下颌线/鼻唇沟的收紧松弛颤动)、呼吸节奏(平稳/短促/屏息/抽泣停顿)、光影配合(侧光勾情绪/顶光压氛围)。同一情绪分克制/爆发双档(愤怒克制版=眉心紧锁+眼白微露+嘴角紧绷后张开;爆发版=眼裂放大+瞳孔收缩+面部肌肉强烈)
```

配套第 32 条「非对称与克制中断」（`manju_llm.go:1138`）：

```
32. 【非对称与克制中断·强制】①非对称:情绪只让半边脸动,明确"眼部不参与/左脸不动"——全脸同步动=AI 味;②克制与中断:动作启动后写中断点,不写"摇头否认",写"摇头启动后在第 10° 突然减速停止"。情绪演出带肌肉层次和克制(泪锁在睫毛边缘不滑落/笑到一半收住),比写满更真
```

### 3.3 哭戏梯度（四档 + 变体）

`manju_llm.go:1137` 第 31 条：

```
31. 【哭戏四梯度·强制】(2026-08-24 知识库「AIGC人物微表情设计指南」整合:哭戏的情绪刻度表)哭戏按强度分四档写,禁止笼统"哭了":①强忍泪水(隐忍哭)=眉尾下垂+眼睑轻颤+下眼睑泛红+鼻翼微颤+泪水不落;②无声落泪(安静哭)=泪珠缓慢滑落+眼尾泛红+嘴角微下垂;③抽泣哭(压抑哭)=肩部胸廓起伏+鼻翼煽动+嘴角抽搐;④崩溃大哭(爆发哭)=眼裂放大+泪水滚落+面部张力强。变体:哽咽哭(喉结滚动/泪珠挂睫毛/说不出话)、喜极而泣(嘴角带笑+泪珠眼尾滑落)、委屈哭(下唇轻突+眼睑微颤)。情绪越深越要"少一点更准"(隐忍心动=目光轻回+嘴角极轻上扬+耳尖微红)
```

（`manju_llm.go:1030` 简版作「哭戏四梯度(强忍→无声→抽泣→崩溃)」。）

### 3.4 节奏模型（5s≈3-4 拍 / 10s≈5-7 拍）

**两处**，措辞不同：

`manju_llm.go:1023`（脚本直出系统提示词，简版）：

```
【节奏模型·强制】(2026-08-24 知识库「官方风格技能与漫剧优化」整合)每镜内部多拍节奏:5 秒镜=3-4 beat、10 秒镜=5-7 beat(含 1-2 峰值+1-2 刹车)、15 秒镜=6-9 beat(含 2-3 峰值+安静刹车);节奏意图词 setup/establish/prepare/impact/brake/settle,峰值镜前必有蓄势,高潮后必接刹车
```

`manju_llm.go:901`（小说解析系统提示词，详细版）：

```
【节奏模型·强制】(2026-08-24 知识库「官方风格技能与漫剧优化」节奏模型整合)每镜内部必须有多拍节奏,禁止一镜一个动作平铺直叙:5 秒镜=3-4 个 beat(建立→动作→收尾);10 秒镜=5-7 个 beat 且含 1-2 个峰值+1-2 个刹车(静止/空拍);15 秒镜=6-9 个 beat 且含 2-3 个峰值+安静刹车。节奏意图词:setup(建立)/establish(定位)/prepare(蓄势)/impact(冲击)/brake(刹车)/settle(落定)——每镜 action 按 beat 组织,峰值镜前必有蓄势镜,高潮后必接刹车,禁止高潮镜直接切下一镜无缓冲
```

另有第 35 条链式衔接里的接缝时间预算（`manju_llm.go:1141` ⑤）：

```
⑤接缝时间预算:渲染出片比采样短 0.92s(pin 头 22 帧),动作节拍按早 0.92s 预算(写给 4.0s 的节拍成品在 3.08s)
```

### 3.5 峰值刹车

**没有独立的「峰值刹车」规则条目**，它内嵌在节奏模型里：
- `含 1-2 峰值+1-2 刹车`、`含 2-3 峰值+安静刹车`（`manju_llm.go:1023`）
- `峰值镜前必有蓄势,高潮后必接刹车`（`manju_llm.go:1023`）
- `峰值镜前必有蓄势镜,高潮后必接刹车,禁止高潮镜直接切下一镜无缓冲`（`manju_llm.go:901`）
- 节奏意图词 `impact(冲击)/brake(刹车)`（`manju_llm.go:901`）
- `directing.peak_device` 字段：`全片情绪最高点使用的手法(一句话,写手法不写题材)`（`manju_llm.go:896`、`984`），并有防同质化约束 `peak_device 写手法本身(摘面具/脱帽/亮武器),不要写题材(防化服/武侠)`（`manju_llm.go:903`）。

### 3.6 近景补偿

`manju_llm.go:1024`（脚本模式，带 token 数学论证）：

```
【近景补偿·强制】(2026-08-24 知识库「H3长镜连续与工作室实战」整合)情感戏/对白戏/表情戏一律近景或特写(shot_size=近景/特写,机位对准面部),禁止中景/全景拍情绪;脸部特写是情绪演出主要载体
```

`manju_llm.go:902`（小说模式，论证更完整）：

```
【近景补偿·强制】(2026-08-24 知识库「H3长镜连续与工作室实战」人脸 token 数学整合)H3 VisualVAE 32× 空间下采样,中景人脸仅约 2 token、眼睛约 0.28 token——拉近景比加大画幅更有效。情感戏/对白戏/表情戏(哭/怒/恐惧/心动/内心挣扎)一律强制近景或特写(shot_size=近景/特写,机位对准面部),禁止用中景/全景拍情绪;中景起步 ≥1024×576;脸部特写是该角色情绪演出的主要载体,表演层细节(情绪三层拆解/五维微表情/哭戏梯度)写在特写镜里
```

配套 `manju_llm.go:1107` 第 7 条「景别↔运镜匹配」：`特写/近景→小幅推近/缓摇/固定（情绪聚焦）`。

### 3.7 补充：位置锚定（第 36 条，非表演层但同属高服从位纪律）

`manju_llm.go:1142`：

```
36. 【位置锚定纪律·强制】(2026-08-30 ver14,H3 官方 Reference Anchors:人物点位错乱的根治面——官方 3d-animation 技能逐镜必填「屏幕相对位置+朝向」)①每个登场角色在 detailed_description 首次清晰出现处,必须写**屏幕位置**(画面左/中/右 + 前景/中景/背景)+**朝向**(facing camera / facing left / facing right,背对时写 turned away),禁止只写动作不写位置;②场景固定地标(门/窗/桌/柜台)写屏幕相对位置(如 door-frame at the right third of the frame),同一场景跨镜沿用,位置变化写明确连续说明;③非首镜开头人物位置必须与上一镜收尾一致,需要换位时写明确换位动作过渡(如 she steps from the left side to the center),禁止无过渡的左右翻转;④同一镜内人物相对位置(谁在左谁在右)一旦确定,镜内不得翻转
```

---

## 4. 拆镜原则在代码中如何固化

### 4.1 规则的权威文本（提示词注入）

`manjuDurationRule`（`manju_llm.go:841-855`）是时长/语音预算/拆镜密度的**唯一生成点**，`min_shot_seconds` / `max_shot_seconds` / `chars_per_sec` 从 `cfg["render"]` 读取（默认 `lo=4, hi=12, cps=4.0`）：

```go
// manju_llm.go:854（逐字）
return fmt.Sprintf("【拆镜密度·内容完整优先·强制】(2026-08-30 用户规则:分镜可以多,保证小说内容完整表达)按正文/脚本逐节拍完整拆镜:约每 80-130 字一镜,覆盖全部情节/对白/动作/情绪/细节,禁止为控制镜数合并节拍或挑关键点压缩剧情——**内容表达完整优先,宁多镜不压缩,拆镜数不设上限**;一个节拍放不下就拆两镜,长对白拆多句、多动作拆多镜,保证每镜单一清晰画面;每镜登场角色 ≤3(H3 参考图上限,超员必须拆镜),每句对白 ≤20 字(超长对白拆成多句对话或旁白承接)。\n【时长硬约束】duration 由台词/动作量决定:中文语音约 %.0f 字/秒(20 字≈5 秒;60 字≈12 秒),台词+旁白总字数 ÷ %.0f 不得超过时长(%d-%d 秒);超预算必须加时长或拆镜,旁白同速折算计入。台词被截断=废镜。", cps, cps, lo, hi)
```

这个字符串通过 `{MANJU_DURATION_RULE}` 占位符进 `manjuScriptSystem`（`manju_llm.go:1038`）与 `manjuDirectSystem`（`manju_llm.go:862`）。

测试锚点 `manju_pipeline_upgrade3_test.go:99-100`：

```go
if !strings.Contains(rule, "拆镜密度") || !strings.Contains(rule, "内容完整优先") || !strings.Contains(rule, "拆镜数不设上限") {
    t.Fatalf("拆镜密度指引缺失, got: %s", rule)
}
```

### 4.2 四条数值约束的代码级固化情况

| 规则 | 提示词文本 | 代码级强制 | 性质 |
| --- | --- | --- | --- |
| **11-17 镜/章** | ❌ **不存在** | ❌ 不存在 | 该数字在源码中**查无实据**；现行规则是「约每 80-130 字一镜、拆镜数**不设上限**」（`manju_llm.go:854`），`script_parse_ver` 第 15 代明确「拆镜密度放宽（每 80-130 字一镜，内容完整优先，**拆镜数不设上限**）」（`manju_script_parse.go:308`）。文档里唯一出现「单集 3-8 个镜头」的是**旁路** `internal/storyboard/generate.go:44`，与 manju 主线无关 |
| **单镜 ≤3 角色** | ✅ `每镜登场角色 ≤3(H3 参考图上限,超员必须拆镜)`（`manju_llm.go:854`） | ✅ **有校验（WARN→重写闭环）**：`if n := len(s.Characters); n > 3 { problems = append(..., "镜头 %d 登场角色 %d 个超 3(H3 参考图上限,多余角色无参考图必脸崩,须拆镜)") }`（`manju_pipeline.go:3222-3224`） | 进 `validatePlan` 问题清单 → 「带意见修复重试」 |
| **台词 ≤20 字** | ✅ `每句对白 ≤20 字(超长对白拆成多句对话或旁白承接)`（`manju_llm.go:854`） | ✅ **有校验（WARN→重写闭环）**：`if rc := len([]rune(stripSpeechPunct(content))); rc > 20 { problems = append(..., "对白 %d 字超 20 字/句...") }`（`manju_pipeline.go:3247-3250`） | 进 `validatePlan` 问题清单 |
| **单镜 4-15s** | ✅ `台词+旁白总字数 ÷ cps 不得超过时长(%d-%d 秒)`（`manju_llm.go:854`，区间可配） | ✅ **有校验**：脚本直出 `lo, hi = 4, 15`（API 硬域），LLM 模式 `lo, hi = ctx.minSec, ctx.maxSec`；`if s.Duration < lo \|\| s.Duration > hi { problems = append(..., "镜头 %d 时长 %d 超出 %d-%d 秒") }`（`manju_pipeline.go:3208-3227`） | 进 `validatePlan` 问题清单 |

`validatePlan` 的产出如何进入重写闭环（`manju_pipeline.go:3185-3192` 附近注释）：`⚠️ 修复后仍不达标,沿用原方案继续(渲染/质检兜底)` —— 即 **不硬截断、不阻断**。

### 4.3 其它硬约束（数量级）

| 约束 | 位置 | 值 |
| --- | --- | --- |
| 单镜 API 时长上限（旁白补偿 clamp） | `manju_script_parse.go:831-833` | `15` |
| 叙事块组内时长和上限 | `manju_script_parse.go:781-783` | `12s`（GPU 实证：15s/360 帧挂死） |
| 单镜编辑时长域 | `manju_shot_edit.go:159-161` | `1~60` 秒 |
| `chars_per_sec` 配置域 | `manju.go:119` | `[2, 8]`，默认 4 |
| 参考图角色数上限 | `manju_pipeline.go:6224-6231`、`manju_prompt_align.go:68-75` | `3`（超出截断，不报错） |
| 生成并发上限 | `manju_pipeline.go:3589` | `4` |
| 叙事块镜数 | `manju_script_parse.go:763-764` | `≥2` 且严格连续、块间不重叠 |

---

## 5. 脚本直出解析器

### 5.1 `script_parse_ver` 代数指纹机制

常量与历代注释（`manju_script_parse.go:264-338`，**共 25 代**）：

```go
// manju_script_parse.go:264-268
// manjuScriptParseVer 脚本程序化解析器代数:写入 plan.script_parse_ver,ensurePlan 复用
// 校验发现版本落后 → 强制重新解析替换旧 plan。背景(2026-08-26 用户实测:16 分镜脚本
// 只渲染 8 个,普通一条龙):旧版解析器对脚本解析失败时静默回退 LLM 直出,LLM 拆镜数
// 不受脚本约束(旧版无拆镜密度强制,8 镜常见),plan 落盘后 chapters="script"+指纹一致
// → 永久复用,升级解析器也不自愈。bump 此值即可让全部脚本直出项目自动重解析。
// 1=初版(无版本标记的存量 plan);2=分镜表跨行/9列时长/FormatB/镜号去重/时长语音补偿/
...
// 25:台词列「无」占位符规范化(按空台词,幽灵人声检测不再逃逸)
const manjuScriptParseVer = 25
```

写入 plan（`manju_script_parse.go:1099`）：`"script_parse_ver": manjuScriptParseVer`。

升级自愈（`manju_pipeline.go:2966-2990`）：

```go
		if reuse {
			if planC == "script" && ctx.scriptMode {
				if v, hasVer := manjuToInt(plan["script_parse_ver"]); !hasVer || v < manjuScriptParseVer {
					if p, perr := ctx.scriptParsePlan(lg); perr == nil {
						newShots, _ := planShots(p)
						oldN := len(anyArr(plan["shots"]))
						if len(newShots) != oldN {
							lg.logf(fmt.Sprintf("🔄 脚本解析器已升级:重新解析替换旧方案(旧 %d 镜 → 脚本 %d 镜;...)", oldN, len(newShots)))
						} else {
							lg.logf("🔄 脚本解析器已升级:重新解析刷新方案(六段式逐字保留)")
						}
						plan = p
						if werr := ctx.writePlan(plan); werr != nil { return nil, werr }
					} else {
						lg.logf("  ⚠️ 解析器升级后重新解析失败(" + perr.Error() + "),沿用现有方案")
					}
				}
			}
```

**关键性质**：重解析**只有在成功时才替换**（失败沿用旧 plan），且新 plan 与旧镜产物提示词指纹不同 → 渲染阶段 manifest 判 stale 自动删旧重渲（注释 `manju_pipeline.go:2970-2971`）。

测试：`manju_script_take_test.go:96-132`（`TestScriptParseVerUpgradeReplacesStalePlan`）、`manju_narrative_block_test.go:71-74`。

### 5.2 解析失败如何兜底 LLM

`scriptParsePlan` 的契约（`manju_script_parse.go:340-345`）：

```go
// scriptParsePlan 脚本直出程序化解析入口。
// 解析出 characters/scenes/shots/directing/episode_title/chapters=script。
// 任一步关键缺失(无分镜表行 / 无 Shot 代码块)返回 error → 调用方回退 LLM。
// 2026-08-31 双格式:JSON 分镜脚本(技能侧新格式,.json 或首字符 {)结构化解析;
// Markdown 分镜脚本(分镜表 + ### Shot N 六段式)原有解析。JSON 后 md 弃用。
```

双格式分支（`manju_script_parse.go:356-378`）：

```go
	// ---- JSON 分镜脚本分支(2026-08-31 技能侧新格式) ----
	if strings.HasSuffix(ctx.novel, ".json") || strings.HasPrefix(trimmed, "{") {
		raws, blocks, err := parseScriptJSON(text)          // 行 632
		if err != nil { return nil, fmt.Errorf("JSON 分镜脚本解析失败: %w", err) }
		if len(raws) == 0 { return nil, fmt.Errorf("JSON 分镜脚本无镜头(shots 为空)") }
		lg.logf(fmt.Sprintf("  📦 JSON 分镜脚本: %d 镜结构化解析(字段直读,零表格断行风险)", len(raws)))
		plan, err := ctx.buildPlanFromRaws(raws, text, lg)  // 行 817
		...
		if attachNarrativeBlocks(plan, blocks, lg) > 0 { ... }   // 行 741
		return plan, nil
	}
```

调用方兜底（`manju_pipeline.go:3042-3093`）：

```go
	// 优先程序化解析:直接采用脚本内嵌六段式,素材抽角色/场景卡;解析失败才回退 LLM 直出。
	...
		if p, perr := ctx.scriptParsePlan(lg); perr == nil {
			...
		} else {
			// 2026-08-26 可发现性:回退 LLM 后拆镜数不受脚本约束(旧版曾把 16 镜脚本渲成 8 镜)
			lg.logf(fmt.Sprintf("  ⚠️ 脚本程序化解析失败(%s),回退 LLM 直出——脚本分镜表约 %d 行,LLM 拆镜数不受脚本约束,成片镜数可能对不上,请检查脚本格式(分镜表 8/9 列 + ### Shot N 六段式)后重新生成方案", perr.Error(), n))
			lg.logf("  ⚠️ 脚本程序化解析失败(" + perr.Error() + "),回退 LLM 直出")
		}
```

**重要收紧**（`manju_script_parse.go` 注释 + `manju_pipeline.go:3539-3544`）：脚本直出模式下**逐镜 h3 缺失不再用 LLM 补**：

```go
	// 2026-09-01 收紧(用户规则:分镜产出统一交给技能侧,不再 LLM 直出):
	// 脚本直出模式 h3 缺失=脚本不完整,报错提示修正脚本,不用 LLM 补(补的 h3 丢
	// 技能侧站位/运镜/特效锚定,且镜数漂移);LLM 直出/agentMode 才允许逐镜生成。
	if ctx.scriptMode && !ctx.agentMode {
		return fmt.Errorf("脚本直出模式检出 %d 镜缺 h3_prompt(镜 %v)——技能侧脚本为唯一权威,请修正脚本(每镜六段式完整)后重跑;或改用 AI 一条龙", len(missing), min(len(missing), 6))
	}
```

超长脚本不受 20000 字 LLM 上限约束（`manju_pipeline.go:3013-3021`）：程序化解析全文生效，只有回退 LLM 时才有截断风险。

### 5.3 叙事块（narrative_blocks）解析

`parseScriptJSON`（`manju_script_parse.go:632`）返回 `(raws, blocks, err)`；`scriptJSONBlock` 结构在 `manju_script_parse.go:590` 一带（`NarrativeBlocks []scriptJSONBlock \`json:"narrative_blocks"\``）。

`attachNarrativeBlocks`（`manju_script_parse.go:732-812`）的降级校验（**绝不丢镜**）：

```go
// manju_script_parse.go:737-784
// 校验不过的块降级丢弃、组内逐镜独立渲染(安全回退,绝不丢镜):
// 镜号存在且严格连续 / ≥2 镜 / 组内时长和 ≤12s / 块级六段式含 [Shot 切点标记 / 块间不重叠。
		switch {
		case len(b.Shots) < 2:
			bad = "组内 <2 镜"
		case prompt == "" || !strings.Contains(prompt, "[Shot "):
			bad = "块级六段式缺失或无 [Shot 切点"
		default:
			sum := 0
			for i, id := range b.Shots {
				d, ok := dur[id]
				if !ok || used[id] { bad = "镜号不存在或块间重叠"; break }
				if i > 0 && id != b.Shots[i-1]+1 { bad = "镜号不连续"; break }
				sum += d
			}
			if bad == "" && sum > 12 {
				bad = fmt.Sprintf("组内时长和 %ds 超 12s 上限(2026-09-04 GPU 实证:15s/360帧挂死;10s/240帧 84s/step 稳定实证)", sum)
			}
		}
```

落地形态：`plan["narrative_blocks"]`、`plan["takes"]`、`plan["takes_src"]="blocks"`，并把块级六段式**覆盖到组头镜的 h3_prompt**（`manju_script_parse.go:794-798`），保证 finalize 汇点（纪律注入/音色兜底/对齐层）自然作用于块提示词（注释 `734-736`）。

测试：`manju_narrative_block_test.go:19-118`（合法块 1 组、其余 3 个非法块全部降级）。

### 5.4 旁白预算 `chars_per_sec` 超限 clamp

配置读取（`manju_pipeline.go:399-402`）：

```go
	ctx.charsPerSec = 4.0
	if f, ok := manjuToFloat(R["chars_per_sec"]); ok && f >= 2 && f <= 8 {
		ctx.charsPerSec = f
	}
```

clamp 本体（`manju_script_parse.go:818-840`）：

```go
// buildPlanFromRaws 从分镜表行(raws)组装方案...
	// 2026-08-26 语音预算自动补偿:台词+旁白总字数 ÷ 字速 > 时长 → 自动延长该镜时长
	// (clamp 到 15s API 上限)。此前旁白零预算,超预算镜 H3 念一半就切(「画面有字无配音」
	// 的渲染侧诱因);脚本为权威不改动内容,只补时长让语音念得完。
	for i := range raws {
		chars := manjuSpeechChars(raws[i].Dialogue, raws[i].Narration)
		if chars == 0 || ctx.charsPerSec <= 0 { continue }
		need := int(math.Ceil(float64(chars) / ctx.charsPerSec))
		if need <= raws[i].Duration { continue }
		adj := need
		if adj > 15 { adj = 15 }
		if need > 15 {
			lg.logf(fmt.Sprintf("  ⚠️ 镜头 %d 语音 %d 字约需 %ds,超出 API 上限,已按 15s 渲染(建议精简台词或拆镜)", raws[i].ID, chars, need))
		} else {
			lg.logf(fmt.Sprintf("  ⚠️ 镜头 %d 语音 %d 字约需 %ds > 脚本时长 %ds,已自动延长时长(防台词截断)", raws[i].ID, chars, need, raws[i].Duration))
		}
		raws[i].Duration = adj
	}
```

字数统计口径 `manjuSpeechChars`（`manju_pipeline.go:3278-3294`）：**dialogue + narration 合并**，各自剥「说话人:」/「旁白:」/「内心·角色名:」前缀，去标点后按 rune 计（一字一音节）。

同口径在 `validatePlan` 二次校验（`manju_pipeline.go:3228-3236`）：

```go
		// 语音预算(2026-08-26 升级):台词+旁白总字数 ÷ 字速 ≤ 时长——此前只算 dialogue、
		// 旁白(narration)完全无预算,旁白超预算的镜 H3 念一半就切(「画面有字无配音」诱因)
		speech := manjuSpeechChars(s.Dialogue, s.Narration)
		if speech > 0 && ctx.charsPerSec > 0 {
			need := float64(speech) / ctx.charsPerSec
			if need > float64(s.Duration) {
				problems = append(problems, fmt.Sprintf("镜头 %d 台词+旁白 %d 字约需 %.1fs 但时长仅 %.1fs(%.1f 字/秒,可能截断)", s.ID, speech, need, float64(s.Duration), ctx.charsPerSec))
			}
		}
```

测试：`manju_pipeline_upgrade3_test.go:4`（P0-2 语音预算）、`:42`、`:93`、`:137-166`（配置域合并）。

### 5.5 脚本机械质检（`scriptValidateShots`）

`manju_script_parse.go:1118` 起，7 类检查：
1. 时间戳递增 / 2. 时长多样性 / 3. **台词/旁白 ↔ 六段式同步（缺失自动补写 `<d>`）** / 4. 站位完整性 WARN / 5. 运镜 WARN / 6. 动作-对白协调 WARN / 7. 内心/旁白双写去重。

台词自动补写（`manju_script_parse.go:1150-1240`）是本报告第 7 节的关键前置。

---

## 6. LLM 调用层

### 6.1 客户端构造与 DeepSeek 缺省

`manju_llm.go:22-76`：

```go
type manjuLLM struct {
	apiKey      string
	baseURL     string
	model       string
	temperature float64
	maxTokens   int
	timeout     time.Duration
	client      *http.Client
	onUsage func(model string, u agent.Usage)
	stopped func() bool
}

func manjuLLMFromCfg(cfg map[string]any) *manjuLLM {
	L, _ := cfg["llm"].(map[string]any)
	defBase, defModel := manjuDefaultLLMService()
	baseURL := strings.TrimRight(str(L["base_url"]), "/")
	if baseURL == "" { baseURL = defBase }
	model := str(L["model"]); if model == "" { model = defModel }
	temp := 0.4
	if v, ok := manjuToFloat(L["temperature"]); ok { temp = v }
	maxTok := 8192
	if n, ok := manjuToInt(L["max_tokens"]); ok { maxTok = n }
	to := 300 * time.Second
	if n, ok := manjuToInt(L["request_timeout"]); ok && n > 0 { to = time.Duration(n) * time.Second }
	return &manjuLLM{ apiKey: str(L["api_key"]), baseURL: baseURL, model: model,
		temperature: temp, maxTokens: maxTok, timeout: to,
		client: &http.Client{Timeout: to} }
}
```

默认服务（`manju_llm.go:78-91`）：

```go
// manjuDefaultLLMService 读「存为默认」的服务配置(settings.json),缺省回退 DeepSeek
func manjuDefaultLLMService() (baseURL, model string) {
	baseURL, model = "https://api.deepseek.com", "deepseek-chat"
	if b, err := os.ReadFile(manjuSettingsFile); err == nil { ... }
```

**关键点**：`base_url`/`model` 完全可配（支持 GLM 等），`temperature` 默认 `0.4`，`max_tokens` 默认 `8192`，`request_timeout` 默认 **300s**（同时用于 `http.Client.Timeout`）。

### 6.2 请求体与重试/超时

`chat`（`manju_llm.go:99-214`）关键片段：

```go
// manju_llm.go:103-113
	body := map[string]any{
		"model":       l.model,
		"temperature": temp,
		"max_tokens":  l.maxTokens,
		"messages": []map[string]string{
			{"role": "system", "content": system},
			{"role": "user", "content": user},
		},
		"response_format": map[string]string{"type": "json_object"},
		"stream":          false,
	}
```

```go
// manju_llm.go:115-121
	// 审计 H12:瞬时失败(429/5xx/网络抖动)指数退避重试,单次抖动不再打崩整条管线
	// (此前零重试:plan 一次 429 即中断 AI 一条龙;修复师失败按原提示词白烧 GPU)
	// 修复:http.Request 的 Body 只能读一次——每次重试必须重建请求(新 bytes.Reader),
	// 否则第 2/3 次尝试发送空 body,退避重试实际失效(审查 P1)。
	const maxAttempts = 3
```

- **重试次数**：`maxAttempts = 3`（`manju_llm.go:121`）
- **退避**：`time.Sleep(time.Duration(attempt*2) * time.Second)` → 2s、4s（`manju_llm.go:163, 206`）
- **重试条件**：网络错误（`154-165`）与 `429 || StatusCode >= 500`（`203-209`）
- **不重试**：其他 4xx（`return "", fmt.Errorf("LLM HTTP %d: %s", ...)`, `211`）
- **截断识别**：`if r.Choices[0].FinishReason == "length" { return "", errLLMTruncated }`（`manju_llm.go:190-193`；`errLLMTruncated` 定义 `95-96`），调用方可「精简重试」
- **停止感知**：每 200ms ticker 轮询 `l.stopped()` → `reqCancel()`，在飞请求（最长 300s）立即中断（`manju_llm.go:119-146`）
- **响应读取**：`io.LimitReader(resp.Body, 4<<20)`（4MB 上限，`172, 200`）；**不能在读完 body 前 cancel**（注释 `167-170` 记录了这个实测 bug）

`http.Client{Timeout: to}`（`manju_llm.go:74`），无 per-call 覆盖；`context.WithCancel` 只用于停止，不设额外 deadline。

### 6.3 JSON 修复（三层）

`chatJSON`（`manju_llm.go:216-251`）**逐字**：

```go
// chatJSON 请求 JSON 对象(剥离可能的 ```json 围栏)
func (l *manjuLLM) chatJSON(system, user string, temp float64) (map[string]any, error) {
	text, err := l.chat(system, user, temp)
	if err != nil { return nil, err }
	text = strings.TrimSpace(text)
	// 审计 5.3:先整体解析——输出正文若先出现 `{`(错误说明/示例),直接截取会切到
	// 错误位置;整体解析失败才做围栏/花括号剥离(容错启发式)
	var out map[string]any
	if err := json.Unmarshal([]byte(text), &out); err == nil { return out, nil }
	// 剥 ```json 围栏
	if i := strings.Index(text, "```"); i >= 0 {
		if j := strings.Index(text[i+3:], "```"); j >= 0 {
			text = text[i+3 : i+3+j]
		} else {
			text = text[i+3:]
		}
		text = strings.TrimSpace(text)
		if err := json.Unmarshal([]byte(text), &out); err == nil { return out, nil }
	}
	// 最后兜底:截取首个 { 到末个 } 之间
	if i := strings.Index(text, "{"); i >= 0 {
		if j := strings.LastIndex(text, "}"); j > i {
			text = text[i : j+1]
			if err := json.Unmarshal([]byte(text), &out); err == nil { return out, nil }
		}
	}
	return nil, fmt.Errorf("LLM 输出非 JSON: %v (前 200 字: %s)", err, truncate(text, 200))
}
```

三层依次是：① 整体 `json.Unmarshal` → ② 剥 ` ```json ` 围栏 → ③ 首个 `{` 到末个 `}` 截取。**没有** trailing-comma / 单引号 / 转义修复（无此类 helper）。

旁路 `internal/storyboard/generate.go:96-114` 有自己的一份 `extractJSON`（围栏 + 花括号），逻辑等价但独立实现。

### 6.4 审稿不过自动重写

**两个层次**，容易混淆：

**(A) plan 级（`validatePlan` → 带意见修复重试）**：`manju_pipeline.go:3170-3192` 调用 `ctx.validatePlan(...)`（另有 `3067` 的预检点），问题清单交给 LLM 修复重试；失败则 `沿用原方案继续(渲染/质检兜底)`（`3187`）。`validatePlan` 检查项见 `3198-3273`（角色卡缺失/≤3 角色/时长域/语音预算/说话人归属/≤20 字/反同质化/重复提示词）。

**(B) 逐镜 h3_prompt 级（`validateShotPrompt` → `genShotPromptWithFix`）**，这是本主题要的「审稿不过自动重写」：

```go
// manju_pipeline.go:3617-3644
		// 审计升级 P0:提示词结构校验——六段/三段字段齐全、<d> 台词、<Picture N> 参考标签;
		// 不达标串行修复重试一次(坏提示词进条件缓存会污染 .pt 且难排查)
		var repair []manjuShot
		for _, s := range todo {
			hp := prompts[strconv.Itoa(s.ID)]
			if hp == "" { continue }
			if probs := ctx.validateShotPrompt(s, hp); len(probs) > 0 {
				repair = append(repair, s)
				lg.logf(fmt.Sprintf("  ⚠️ 镜头 %d 提示词结构校验未过(%d 项),修复重试", s.ID, len(probs)))
			}
		}
		if len(repair) > 0 {
			for _, s := range repair {
				fix := "【提示词结构校验未过,逐条修正后重新输出】\n" + strings.Join(ctx.validateShotPrompt(s, prompts[strconv.Itoa(s.ID)]), "\n")
				hp2, err2 := ctx.genShotPromptWithFix(s, charMap, sceneMap, fix, prevOf[s.ID])
				if err2 == nil && len(ctx.validateShotPrompt(s, hp2)) == 0 {
					prompts[strconv.Itoa(s.ID)] = hp2
					if m := objOf[s.ID]; m != nil { m["h3_prompt"] = hp2 }
					lg.logf(fmt.Sprintf("  ✅ 镜头 %d 提示词修复通过(%d 字)", s.ID, len([]rune(hp2))))
				} else {
					lg.logf(fmt.Sprintf("  ⚠️ 镜头 %d 提示词修复仍不达标,沿用原稿(渲染时注意检查)", s.ID))
				}
			}
		}
```

要点：
- **重写轮数 = 1**（不是循环；校验→一次修复→不再过就沿用原稿）。
- `fix` 字符串就是问题清单原文，前缀 `【提示词结构校验未过,逐条修正后重新输出】`。
- `genShotPromptWithFix` → `genShotPromptRaw(..., fix, prev)`（`manju_pipeline.go:3718-3729`），把 `fix` 作为追加意见拼进 user 消息。
- 校验项 `validateShotPrompt`（`3674-3716`）= 六/三段字段齐全 + 有台词必有 `<d>` + 有角色必有 `<Picture` + 台词首句出现在 `<d>` 中 + `validatePromptContract`（4 项契约）。
- 并发：逐镜生成 4 并发（`3589`），但**修复阶段串行**（`3631`）。

### 6.5 模型路由与 fallback

- **无多模型路由**：全管线单一 `l.model`。任务差异只体现在 `temperature` 参数上（`chat(system, user, temp)`）。
- **无失败 fallback 模型**：`chat` 重试耗尽即返回错误；`chatJSON` 直接把错误上抛；上层（plan/genprompt）把错误变成阶段失败而不是换模型。
- `manju_agent.go:153` 只提到一个**配置坑告警**：`base_url` 缺省回退 LLM 地址可能导致 GLM 模型名打 DeepSeek 端点 401。
- token 用量记账：`l.onUsage(l.model, r.Usage)`（`manju_llm.go:194-196`）。

---

## 7. 台词如何进入生成 + MotionContext 尾音 pin

### 7.1 数据流：脚本/小说 → `manjuShot.Dialogue` → `<d>`

1. **脚本直出**：分镜 JSON/表格的 `dialogue` 列逐字读入 `scriptShotRaw.Dialogue`（`manju_script_parse.go:632` `parseScriptJSON` → `817` `buildPlanFromRaws`）→ `manjuShot.Dialogue`（`manju_pipeline.go:2884` 一带的 plan→shot 映射）。
2. **LLM 直出**：脚本侧的 `h3_prompt` 六段式**逐字保留**（`manju_script_parse.go:380-386`：`每镜六段式代码块 → h3_prompt 逐字保留`）；小说模式则由 `genShotPromptRaw` 把 `"dialogue": s.Dialogue` 放进 data（`manju_pipeline.go:3741`），LLM 按写作规范第 3 条写成 `<d>`。
3. **缺失补写**（机械，确定性）：`scriptValidateShots` 第 3 步（`manju_script_parse.go:1150-1240`）：

```go
	// 3) 台词/旁白 ↔ 六段式同步:未同步句**自动补写**进提示词(对白 <d>/旁白画外音 <d>),
	//    2026-08-26 升级:此前仅告警——分镜表台词列有、六段式没有时,渲染真的没有此句配音
	...
			key := string([]rune(line)[:minInt(8, len([]rune(line)))])
			if len([]rune(key)) < 4 || strings.Contains(r.H3Prompt, key) { continue }
			miss++
			patch += "\n<d>" + line + "</d>"
	...
			// 官方画外音写法(base-en.txt §4.4):旁白/内心由 H3 直出,不用 TTS
			patch += "\nThe narrator says in an off-screen voiceover: <d>" + n + "</d> while the on-screen characters' lips remain completely closed."
		if miss > 0 {
			// 插到最后一个 </d> 之后(与既有对白同段);无 </d> 则追加末尾
			if k := strings.LastIndex(r.H3Prompt, "</d>"); k >= 0 {
				k += len("</d>")
				r.H3Prompt = r.H3Prompt[:k] + patch + r.H3Prompt[k:]
			} else {
				r.H3Prompt += patch
			}
```

注意补写时**只写裸 `<d>台词</d>`**（无语言标签），语言标签由渲染前 `alignDialogueLangTags` 统一补（见下）。

4. **语言标签规范化**（`manju_prompt_align.go:893-938`）：

```go
// alignDialogueLangTags 语言标签规范化(独立可测):
//   ① <d>[中文]X</d> / <d>[chinese]X</d> → <d>[Chinese]X</d>(官方标签为英文写法;
//      历史上中文词标签被逐字念出的根因是标签语种错,不是标签不该存在);
//   ② 裸 <d>中文…</d>(无标签且内容以汉字开头)→ <d>[Chinese]中文…</d>
//      (官方 base-en §4.4:d 标签内只放语言标签+原话;裸文本让模型猜配音语言)。
// 非中文标签([English]/[unclear] 等)原样保留;幂等。
```

5. **画外强制标注** `markOffscreenSays`（`manju_prompt_align.go:1106-1139`）：裸 `(Sx) says:`（无 `<Subject N>` 前缀）→ `(Sx) says in an off-screen voiceover:`，并在该句 `</d>` 后补 `while the on-screen characters' lips remain completely closed.`

6. **画外·前缀台词强制 off-screen** `fixOffscreenDialogueSays`（`manju_prompt_align.go:1215-1310`）：对话列有 `画外·` 说话人时，`detailed_description` 里以 `calls out/announces/shouts/speaks/yells/says/asks` 引导且无 `<Subject` / 无 off-screen 标记的 `<d>` → 插入 ` in an off-screen voiceover` + lips-closed。

### 7.2 `<d>` 的提示词规范原文（真实代码）

`manju_llm.go:1103`（逐镜写作规范第 3 条）：

```
3. 台词写 <d>[Chinese] 中文原文</d>（【官方语言标签·2026-08-30 官方 base-en §4.4 核验】d 标签内=语言标签+原话:标签词必须用英文 Chinese(历史 <d>[中文]…</d> 被逐字念出=标签语种用错,不是标签不该存在);原词原标点，句末以 。？！结束，不译不改写,H3 原生对白配音;裸 <d>中文</d> 无标签禁止——模型会猜错配音语言;听不清的片段写 [unclear] 不许猜写）。【说话人硬约束】分镜 dialogue 的每句台词必须由标注的对应角色开口说出：写该角色 <Subject N> (Sx) says: <d>[Chinese] …</d>——谁说的就是谁，禁止把台词安到别的角色头上、禁止把角色台词改写成旁白/画外音；同一句台词在整条提示词中只能出现一次，禁止重复贴原文（重复句会被 H3 念两遍，2026-08-30 五问整改）
```

`manju_llm.go:1104`（第 4 条，旁白）：

```
4. 【旁白 = H3 原生画外音，不是 TTS，更不是角色台词】：只有分镜 narration 字段的内容才写 The narrator (Sx) says in an off-screen voiceover: <d>[Chinese] …</d>(d 标签内=narration 中文原文+官方语言标签)while the on-screen characters' lips remain completely closed（旁白按镜内发声顺序计入 (Sx)；旁白与台词不同时出现；【硬约束】分镜 dialogue 里的角色台词禁止写成旁白——必须由对应角色开口，画面中该角色嘴唇在动）。【禁止前缀/重复·强制】(2026-08-30 五问整改：内心配音重复根治) d 标签内只写台词/旁白/内心独白的中文原文，禁止带「内心·角色名:」「旁白:」前缀、禁止用中文引号包裹原文（引号会被逐字念出）；同一句台词/旁白/内心独白在整条提示词中只能出现一次——禁止把 narration 既写成 off-screen voiceover 句又在别处重复贴原文（重复句会被 H3 念两遍）；旁白与内心同镜并存时各写一条 off-screen 句、内容互斥不重复
```

`manju_llm.go:999-1000`（官方格式硬规定）：

```
- 说话者稳定 ID (S1)(S2),首次出现给身份描述,发声者写 <Subject N> (Sx);【镜内编号·强制】...台词写 <d>[Chinese] 台词中文原文</d>——【官方语言标签·强制】(2026-08-30 官方 base-en §4.4 核验:d 标签内必须带语言标签,官方写法 <d>[English] ...</d>;中文台词即写 <d>[Chinese]陈默？</d>,标签词用英文 Chinese——2026-08-28 的 <d>[中文]…</d> 被逐字念出事故根因是标签用了中文词而非标签不该存在,裸 <d>中文</d> 无标签会让模型猜配音语言,两者都禁止);画外音写 says in an off-screen voiceover ... while his/her lips remain completely closed
- 【画外音/旁白措辞·硬禁中文】(2026-08-23 实测:直出的 h3_prompt 用中文「画外音/旁白/嘴唇完全闭合」H3 无法识别对白驱动→该镜静音 rms≈0.005;...)
```

跨切点标签（`manju_llm.go:1001`、`1106`）：`<scenetrans>` / `<cutoff>`。

### 7.3 多重配音：Audio 绑定链

**(1) 音频挂载顺序** `charVoiceNames` 按 `<Audio>` 定义行序挂载；`voiceBindingsFor`（`manju_pipeline.go:6670-6690`）按**登场顺序**编号 `Audio 1..k`（≤3）：

```go
// manju_pipeline.go:6670-6690
func (ctx *manjuCtx) voiceBindingsFor(s manjuShot) []voiceBinding {
	// 2026-09-04 无台词镜不绑音色(被论斤 EP01 镜1/2 无台词却出怪配音实锤):
	// 无台词时 ensureVoiceBindings 仍会注入 <Audio N> 定义行(措辞"containing a
	// spoken voiceover"),而 charVoiceNames 按原始 plan 的 Audio 行挂 ref_audios——
	// 原文无此行 → ref_audios 传空。文本宣称有画外音+音频实际为空 = H3 幻觉补出
	// 随机人声。与 ghostVoiceEligible 同口径:无 <d>/无台词/无旁白/无内心 → 不绑。
	if ghostVoiceEligible(s.Dialogue, s.Narration, s.H3Prompt) { return nil }
	var out []voiceBinding
	for i, cid := range s.Characters {
		if i >= 3 { break }
		if ctx.charVoiceRef(cid) == "" { continue }
		out = append(out, voiceBinding{CharID: cid, Audio: fmt.Sprintf("<Audio %d>", len(out)+1), SubN: i + 1})
	}
	return out
}
```

**(2) 定义兜底** `ensureVoiceBindings`（`manju_pipeline.go:6700-6721`）：缺定义的绑定角色逐条补 `<Audio N> is the voice-timbre reference for <Subject M> (Sx), containing a spoken voiceover.`，插在 `summary:` 之前（即 `subject_definitions` 段末）。

**(3) 音色指纹短语校正** `injectAudioTimbrePhrases`（`manju_pipeline.go:1401-1429`）：把 Audio 行的短语替换为角色卡标准短语（`with an elderly woman's voice, warm and crackly, voice style: ...`），修「女主配中年男声」「同档角色同一短语」。短语表 `manjuVoicePhraseFor`（`manju_llm.go:1438` 起，`manju_pipeline.go:1431-1453` 注释）。

**(4) 画外音独立音色** `injectOffscreenVoiceBindings`（`manju_pipeline.go:7040`）+ `manjuOffscreenBindings`（`manju_pipeline.go:6766-...` `manjuOffscreenDescs`）+ `maxAudioNum`（`6737-6745`，从补写后最大编号接续，防冲突）。注释（`manju_pipeline.go:911-913`）说明动机：`画外编号从补写后最大 <Audio N> 接续`。

**(5) 归一化** `alignAudioDefsReg`（`manju_prompt_align.go:676-798`）：绑定改官方 `for <Subject M> (Sx)`、编号压缩 1..k（登场序在前、画外音接续）、行内 Sx 用 `dialogueSpeakerIDs` 确定性重写、幽灵定义整行删除、幽灵引用短语剥除。`@offscreen:` 重写必须保留 `the off-screen voice described as` 幂等锚（注释 `748-752`，实锤「同一句念两遍」）。

### 7.4 MotionContext 尾音 pin（多重配音根因修复）

实现在 `h3RenderWorkflow`（`manju_comfy.go:366-528`），**`manju_comfy.go:467-488`**：

```go
	// 接缝:MotionContext(condLoad 条件 + 上一镜 latent)→ conditioning + trim_frames
	trimFramesID := ""
	if chained {
		latLoad := wfAdd(wf, "MiniMaxH3MotionContextLoadLatent", map[string]any{"latent_path": "h3_context/" + latentNS, "clip_index": prevIdx})
		// audio_context_length 默认 1(≈25ms,2026-08-26 多重配音修复):MotionContext 会把上一镜
		// 尾部音频 pin 进本镜,H3 设计上"续念"该音频——与 <d> 标记的本镜台词并行 = 两路人声
		// 交叠(上一镜台词尾音被重复念一遍)。短剧一镜一句台词、台词结尾即切镜,叠音伤害远大于
		// 音频不连续,默认只 pin 25ms 保音画对齐。需要音频连续接缝可配 render.motion_audio_context
		// =24(0.6s)。注意节点语义 a_frames = int(v) or span:传 0 是 falsy 反而取全窗口,禁传 0。
		audioCtx := "1"
		if n, ok := manjuToInt(R["motion_audio_context"]); ok && n >= 1 && n <= 96 {
			audioCtx = strconv.Itoa(n)
		}
		mc := wfAdd(wf, "MiniMaxH3MotionContext", map[string]any{
			"conditioning": refOf(condID), "vae": refOf(vae), "latent": refOf(latentID),
			"context_length": "22", "audio_context_length": audioCtx,
			"context_latent": refOf(latLoad),
		})
		// MotionContext 输出 0=conditioning, 1=trim_frames
		condID = mc + "[0]"
		trimFramesID = mc + "[1]"
	}
```

**尾音 pin 的准确语义（这是本主题最容易误读之处）**：
- MotionContext 把上一镜 latent（**视频+音频**）作为 context pin 进本镜头部；
- **视频** pin 长度 `context_length = "22"` 帧（≈0.92s @24fps）——对应提示词规则第 35 条 ⑤「渲染出片比采样短 0.92s(pin 头 22 帧)」；
- **音频** pin 长度 `audio_context_length` 默认 `"1"`（≈25ms），而不是 24 —— **刻意把尾音 pin 压到最短**，因为短剧「一镜一句台词、台词结尾即切镜」，全量 pin 会让上一镜台词尾音在本镜被「续念」一遍，与本镜 `<d>` 台词形成**两路人声交叠**（这就是「多重配音」bug 的根因）；
- 需要音频连续接缝时才把 `render.motion_audio_context` 配成 `24`（0.6s）；
- ⚠️ 节点语义 `a_frames = int(v) or span`，**传 0 是 falsy 会取全窗口，禁传 0**（代码用 `n >= 1` 守住）；
- 解码后 `MiniMaxH3MotionContextTrim`（`manju_comfy.go:514-519`）裁掉 burn-in 前缀：`{"images": ..., "trim_frames": ..., "audio": ..., "fps": ..., "match_tail": true}`；
- 无论是否接缝都 `MiniMaxH3MotionContextSaveLatent` 存本镜 latent 供下一镜续接（`manju_comfy.go:525-527`），按 `latentNS` 命名空间隔离。

**与「运动镜断链」的耦合**：`chained=false` 时整段 MotionContext 不建（见 2.4）。

测试：`manju_views_fix_test.go:12-31`（`TestMotionAudioContextDefault`：默认 `"1"`，配 24 得 `"24"`，配 0 回退 `"1"`）。

**提示词侧配套**（规则第 35 条，`manju_llm.go:1141`）：非首镜开头 1-2 句用官方延续句式承接上一镜收尾（`continues seamlessly from the previous shot`），气闸 1-2 秒保持上镜构图，矛盾排布会渲染成 **union**（多出不相干人脸）。`prev_shot` 数据由 `genShotPromptRaw` 注入（`manju_pipeline.go:3748-3755`），来自分镜数据而非 LLM 输出（并发安全）。

---

## 8. 风险与缺口（调研附注）

1. **表演层纪律零机械强制**：情绪三层 / 微表情五维 / 哭戏四梯度 / 节奏模型 / 峰值刹车 / 近景补偿 全部只在提示词里，`validatePlan` 与 `validateShotPrompt` 都不检查（唯一擦边是 `manju_script_parse.go:1270-1273` 的「有台词但画面无动作/表情词」WARN，以及 `fixChibiEmotion` 的 chibi 表情注入）。对脚本直出模式（h3_prompt 逐字保留）**完全无效**——脚本怎么写就怎么渲。
2. **六段式顺序/唯一性无代码校验**：只有「字段存在」的 `Contains`。段落顺序错、段落重复出现、`<Subject N>` 编号重复都不会被发现（`manju_pipeline.go:3678-3689`）。
3. **11-17 镜/章 在源码中查无实据**：现行规则是「每 80-130 字一镜、不设上限」（`manju_llm.go:854`），且 `script_parse_ver` 第 15 代明确放宽。若需求方要求该数字，需要新增规则。
4. **4-15s 只在脚本模式成立**：LLM 模式用的是 `ctx.minSec/ctx.maxSec`（配置值，默认 `minSec`/`maxSec` 由 `render.min_shot_seconds`/`max_shot_seconds` 决定，`manjuDurationRule` 默认 `4-12`）。`validatePlan:3210-3213` 的分支是 `if ctx.scriptMode { lo, hi = 4, 15 }`。
5. **修复重写只有 1 轮**：`genShotPrompts` 修复不通过即「沿用原稿」（`manju_pipeline.go:3641`），坏提示词仍会进渲染。
6. **JSON 修复无语法级修补**：不做尾逗号/单引号/注释剥离；若 LLM 输出结构合法但语义不符 schema，无 schema 校验（只 `json.Unmarshal` 成 `map[string]any`）。
7. **两套 storyboard 并行**：`internal/storyboard/`（老，3-8 镜、三段式、独立 `extractJSON`）与 `internal/manju/`（新，六段式）并存，规则数值互相矛盾，需明确以哪套为权威。
8. **`manjuShotTemplate` / `manjuShotEdit` 与提示词无关**：`manju_shot_template.go` 是渲染参数模板（seed/steps/sampler/lora/neg），`manju_shot_edit.go` 是单镜编辑 API；后者 `syncDialogueIntoPrompt`（`manju_shot_edit.go:35-73`）做了台词「旧句→新句」的确定性替换（无 LLM），是台词进入生成链的另一入口。

---

## 附录：关键函数/常量索引

| 名称 | 位置 |
| --- | --- |
| `manjuRef2vaTpl` | `manju_llm.go:1056-1087` |
| `manjuFl2vaTpl` | `manju_llm.go:1089-1096` |
| `manjuShotWritingRules`（1-36 条） | `manju_llm.go:1098-1142` |
| `manjuShotPromptSystem` | `manju_llm.go:1145-1163` |
| `manjuScriptSystem` | `manju_llm.go:952-1040` |
| `manjuDirectSystem` | `manju_llm.go:857-899` |
| `manjuDurationRule`（拆镜密度/时长/语音预算） | `manju_llm.go:841-855` |
| `manjuRefContract` / `manjuCharSlot` / `refContractFor` | `manju_prompt_align.go:44-98` |
| `manjuAlignShotPromptReg`（对齐编排） | `manju_prompt_align.go:114-131` |
| `validatePromptContract` | `manju_prompt_align.go:952-1030` |
| `alignPictureRefs` / `replacePictureTags` | `manju_prompt_align.go:192-321` |
| `injectAttachmentManifest` | `manju_prompt_align.go:344-375` |
| `alignSpeakerIDsReg` / `alignAudioDefsReg` | `manju_prompt_align.go:400-483 / 676-798` |
| `alignDialogueLangTags` / `tagBareChinese` | `manju_prompt_align.go:899-938` |
| `alignShotTimecodes` | `manju_prompt_align.go:1037-1090` |
| `markOffscreenSays` / `fixShotPromptContent` | `manju_prompt_align.go:1106-1139 / 1155-1310` |
| `manjuFinalizePromptPure`（队尾 ≤4 条） | `manju_pipeline.go:817-877` |
| `manjuGuardStrip` | `manju_pipeline.go:797` |
| `finalizeAlignedPrompt`（注入编排汇点） | `manju_pipeline.go:955-995` |
| `injectCameraDiscipline` / `injectPositionDiscipline` | `manju_pipeline.go:1135-1193` |
| `injectCinematographyDiscipline` / `injectCrowdDiscipline` / `injectScreenDiscipline` | `manju_pipeline.go:1207-1235 / 1251-... / 1326-1389` |
| `manjuStripDanglingPictureRefs` | `manju_pipeline.go:1839-...` |
| `validatePlan`（≤3 角色 / 时长 / ≤20 字 / 语音预算） | `manju_pipeline.go:3198-3273` |
| `manjuSpeechChars` | `manju_pipeline.go:3278-3294` |
| `genShotPrompts` / `validateShotPrompt` / `genShotPromptRaw` | `manju_pipeline.go:3514-3671 / 3674-3716 / 3729-...` |
| `charRefNames` / `shotViewRelsFor` / `charVoiceRef` | `manju_pipeline.go:6222-6259 / 6245-6259 / 6265-...` |
| `voiceBindingsFor` / `ensureVoiceBindings` / `injectAudioTimbrePhrases` | `manju_pipeline.go:6670-6690 / 6700-6721 / 1401-1429` |
| `injectOffscreenVoiceBindings` / `manjuOffscreenDescs` | `manju_pipeline.go:7040 / 6766-...` |
| `manjuCameraIsStatic` / 运动镜断链 | `manju_pipeline.go:7311-7317 / 7358-7367` |
| `manjuScriptParseVer = 25` | `manju_script_parse.go:338` |
| `scriptParsePlan` / `parseScriptJSON` / `buildPlanFromRaws` | `manju_script_parse.go:345 / 632 / 817` |
| `attachNarrativeBlocks` | `manju_script_parse.go:741-812` |
| 旁白预算 clamp | `manju_script_parse.go:818-840` |
| `scriptValidateShots` / `scriptDedupShotLines` | `manju_script_parse.go:1118 / 1336` |
| `manjuLLM` / `chat` / `chatJSON` | `manju_llm.go:22-34 / 99-214 / 217-251` |
| `h3RenderWorkflow`（MotionContext 尾音 pin） | `manju_comfy.go:366-528`（尾音 pin 467-488） |
| `syncDialogueIntoPrompt` | `manju_shot_edit.go:35-73` |

**测试证据索引**：`manju_prompt_align_test.go`（全 421 行，七模块 + 幂等 + 换脸 + retention 保护）、`manju_script_mode_test.go:69-92`（官方格式关键点齐全）、`manju_narrative_block_test.go:19-118`（块解析/降级/组头覆盖）、`manju_views_fix_test.go:12-31`（音频 pin 默认值）、`manju_script_take_test.go:96-132`（代数升级自愈）、`manju_pipeline_upgrade3_test.go:42-166`（时长/字速/≤3 角色/≤20 字配置注入）。
