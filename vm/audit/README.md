# `vm/audit` —— 零 LLM 渲染前质检层（B 档）

> 行业数据（wind-comic README，5 份调研里最该记住的一条）：
> 「**制作只占总成本 7.5%，投流占 70-85%**」「AI 漫剧爆款率不足 **0.1%**、约 **90%** 公司亏损」
> → 真正的杀手是**废片率**，不是 GPU 秒数。**在烧 GPU 之前**用已有分镜表把结构问题找出来。

纯 Python 标准库，零 token、零 GPU、零网络；核心判定全是**纯函数**（可单测、可重放）。

| 文件 | 职责 |
| --- | --- |
| `__init__.py` | 输入形状兼容（`Shot` dataclass / 原始 dict）、对白形状（`str` / `[{char,quote,src_span}]`）、景别 8 档映射、原子写等共享工具 |
| `dialogue_coverage.py` | **B1** 对话覆盖度：缺正反打 / 全 wide 无特写（移植 wind-comic `lib/dialogue-coverage.ts`） |
| `pacing.py` | **B2** 节奏形状：最小二乘斜率 / 峰值显著度 / 拖沓段 / 开场密度 / 时长 cv（移植 `lib/pacing-audit-v2.ts`） |
| `dialogue_fidelity.py` | **B5** 对白零丢失：覆盖率 + `src_span` 逐字校验 + CI 门禁（自研，两个深挖项目都没做到） |
| `cli.py` | 命令行入口 + 人类可读报告 + `state/audit.json` |
| `tests/` | 66 条单测（`python3 -m unittest discover -s vm/audit/tests -t .`） |

## 用法

```bash
python3 -m vm.audit.cli 西游记                  # 报告 + 落盘 projects/西游记/state/audit.json
python3 -m vm.audit.cli 西游记 --json            # 附带完整 JSON 到 stdout
python3 -m vm.audit.cli 西游记 --gate            # 门禁：有 error 级判定 → 退出码 1
python3 -m vm.audit.cli 西游记 --scores-mode explicit --drag-threshold 4.0
python3 -m vm.audit.cli projects/西游记 --chapter novel/第一章_古寺夜哭.md --no-write
```

退出码：`0` 通过 / `1` `--gate` 判定失败 / `2` 输入、用法错误。

```python
from vm.audit.dialogue_coverage import audit_dialogue_coverage
from vm.audit.pacing import audit_pacing
from vm.audit.dialogue_fidelity import audit_dialogue_fidelity, to_manifest_entry

b1 = audit_dialogue_coverage(shots)          # shots: [dict] 或 [vm.shots.Shot] 都可以
b2 = audit_pacing(shots, mode="auto")
b5 = audit_dialogue_fidelity(shots, chapter_text)
manifest_line = to_manifest_entry(b5)        # 可直接塞进 manifest 的标量行
```

## 输出契约

`state/audit.json` 顶层键：`schema_version / tool / generated_at / project / inputs /
summary / manifest_entry / b1 / b2 / b5 / findings / limitations`。

- 每条判定都有 `code / severity(error|warning|info) / module / from_shot / to_shot /
  message / rewrite_hint`；`ext=true` 表示"本仓库扩展"而非上游移植项。
- `manifest_entry` 是给 `vm/state.py` 的压缩行（**B5 独立于 `ledger` 门禁**）：
  `{source_quotes, adopted, missing, coverage_pct, extraneous_chars, span_checks,
   span_failures, gate_passed}`。
- `limitations` 是机器可读的自曝清单（"未接线的能力必须显式标注"）。
- 判定结果**确定性**（同输入同输出），只有 `generated_at` 时间戳不稳定。

### 给 Lead 的三个可选接口（都已在消费侧接好线）

| 接口 | 加在哪 | 审计侧行为 |
| --- | --- | --- |
| `conflict: 0-10`（逐镜） | `vm/plan.py` 拆镜时顺带输出（复用同一次 LLM 调用，**零额外 token**） | `--scores-mode auto` 自动优先消费；B2 形状判定由 `info` 升到 `warning`，判别力才够（**强烈建议做**） |
| `location: "<地点>"`（逐镜） | schema v2 | B1 分组从 `consecutive` 升级为上游原语义 `location`（换地点切分） |
| `dialogue: [{char, quote, src_span:[a,b]}]` | schema v2 | B5 从"字符流核算"升级为 `chapter_text[a:b] == quote` **逐字校验**（`error` 级） |

**没接线时的行为已显式标注**：没有 `conflict` → 代理分 + 自动降级 + 自曝"区分度低"；
没有 `location` → `consecutive` 分组（只漏报不误报）；没有 `src_span` → 安静退化为拼串核算。

## 真实 52 镜实测（`projects/西游记/shots/chapter01.json`，52 镜 / 6 角色 / 264s）

```
[B1] 对话镜 40/52，对话场景 6 组（多角色 6 组）；分组模式 consecutive
     上游两项检查：缺正反打 0 处 / 全 wide 无特写 0 处 → coverageScore = 100.0
     info ×5：no-two-shot(第45~51镜) + ambiguous-speaker(第8/14/24/42镜)
[B2] 打分模式 proxy（mean 5.994，σ 0.873）
     形状 no-climax（peak=7.475@第4镜，prominence=1.481 <1.5，borderline）
     拖沓段（阈值4.0）：无；校准阈值5.121：无；最低洼窗口：第42~44镜 均分4.467
     开场密度：前5镜 6.24 ✓（门槛5.0）；时长 cv=0.1538 ✓（呆板线0.12，1.281×）
[B5] 原文 29 句，采纳 29 句，遗漏 0 句；自创/改写台词 0 字；src_span 0 处；门禁 PASS
汇总：error 0 / warning 0 / info 9
```

## 诚实评估：这些结论到底能不能信？

**总结：B5 强、B1 上游两项可信但本片是"通过"、B1 扩展中等、B2 代理分最弱（有一条结论是误导）。**

| 结论 | 判定 | 理由 |
| --- | --- | --- |
| B5 29/29 零丢失 | ✅ 真阳性（确认） | 顺序化字符流核算正确；**朴素 substring 判据在同一份数据上会误报 3 处**（第 28/36/40 镜把原文里中间夹着叙述的两句对白合并进一镜），这正是升级的必要性证据 |
| B1 缺正反打 0 / 全 wide 0 | ✅ 真通过 | 该表确实逐句交替说话人、近景/中景交替、有 5 个双人同框镜；"AI 一遍跑完"病理不存在。**价值是回归门禁**：单测已证明「3 镜全 wide 的多角色对话」会报 `wide-only-dialogue`、「单镜涵盖多角色对话」会报 `needs-reverse-shot` |
| ambiguous-speaker ×4（第 8/14/24/42 镜） | ✅ 真阳性、低影响 | 同框 ≥2 角色且无 speaker 字段，**TTS 音色归属真的不可自动判定**；但在人工检查里多数能看出谁在说。价值主要在推动 schema v2 的 `speaker` |
| no-two-shot ×1（第 45~51 镜） | ⚠️ 弱/可能噪声 | 结尾就是单人正反打，这类段落不需要双人同框；且统计窗口只看"扩展场景"，窗口外的三人全景陈述镜（第 37 镜）不算。**建议不要为此改稿** |
| B2 `no-climax` | ❌ 不可信（指标失效） | prominence 1.481 卡在阈值边缘（模块自己报了 `shape-borderline`），且它把峰值判在**第 4 镜**（开场对话）。真实高潮（第 29~41 镜井边对峙）代理均分 5.75，**反而低于全片均值 5.99** —— 代理分看不见剧情。已强制降为 `info` 并要求显式 `conflict` 分复核 |
| B2 最低洼窗口 42~44 | ❌ 误导 | 第 42~44 镜是"指骨"揭露 + 唐僧/悟空反应，是剧情转折点，不是拖沓处。代理分把"短镜+近景+台词密"当成了高冲突的副作用 |
| B2 时长 cv=0.1538（1.281×） | ✅ 真阳性、可执行 | 52 镜里 42 镜是 5-6s，"每镜都 5-6 秒"是真的；这是 B2 在真实数据上唯一建议照做的结论 |

**阈值取舍（都写进了代码注释与 `limitations`）**

1. 代理分驱动的形状判定一律 `info`（显式 `conflict` 分才升 `warning`）——把 σ=0.87 的代理分当证据，就是本项目反对的"文档承诺 > 代码实现"。
2. 新增自曝两条：`shape-borderline`（显著度距阈值 ≤0.25）、`proxy-low-discrimination`（σ<1.0）。**指标失效要说出来，而不是输出一个像模像样的判定。**
3. 拖沓段同时报**上游绝对阈值** 4.0 与**按本片分布校准**的 `mean−σ=5.121`；两条都没触发就不硬造 finding，改用"最低洼窗口"回答"只改一处改哪里"。
4. 建议施工顺序：**B5（零丢失门禁）> B1 上游两项 > B1 扩展 > B2**。B2 要真正可用，前置条件是 `plan.py` 输出 `conflict` 分。

## 单测

```bash
python3 -m unittest discover -s vm/audit/tests -t . -v   # 66 passed
```

覆盖：全 wide 对话→报警 / 正常正反打→不报 / 单镜多角色→缺正反打 / 同 location 并入 /
非对话镜切分 / 扩展场景桥接 / 显式 location 才切分 / `[1,2,4,9]` vs `[9,4,2,1]` 形状不同 /
prominence 与斜率边界 / 拖沓段阈值严格小于 / cv 呆板线边界（含"严格小于"语义）/
显式 conflict 分优先与归一化 ×10 / 覆盖率 100% 与遗漏 / 改写 vs 整句丢失 /
`src_span` 完全一致 / 只差标点 / 越界 / 顺序错乱 / 无正文时门禁必须 FAIL /
CLI 落盘、`--gate` 退出码、`--json` 可解析。

## 硬约束遵守

- 只**新建** `vm/audit/**`；唯一写出的项目内文件是 CLI 的 `projects/<名>/state/audit.json`（原子写，新建）。
- 不启停 ComfyUI、不调 `/free` `/queue clear` `/interrupt`、不碰 `/home/max/ComfyUI/output/`、不跑 GPU。
- `dialogue_fidelity` 复用 `vm.plan.chapter_dialogues / _norm_speech`（只读导入，抽句规则不会与 plan 阶段漂移）。
