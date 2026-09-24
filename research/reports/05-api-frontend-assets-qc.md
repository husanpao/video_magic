# NiliX 源码调研报告 05 —— HTTP API、前端、资产库、质检与合成

- 源码根目录：`/home/max/Projects/PythonProjects/video_magic/research/NiliX-main`
- 项目：NiliX（本地 AI 漫剧生产工作台），Windows 目标，Go 1.25（README 写 Go 1.22+），243 个 `.go` 文件 / 58,258 行
- 调研方式：只读静态阅读（read/glob/grep），未修改任何文件
- 所有结论均附 `文件路径:行号` 证据

---

## 0. 总体架构一句话

单进程 Go 服务（默认 `http://127.0.0.1:8787`）用 `go:embed` 内嵌全部前端与 Python 媒体脚本，通过 `net/http` 标准库 `ServeMux`（Go 1.22+ 方法路由语法 `"GET /api/xxx"`）暴露 **139 条显式路由**（其中 `/api/manju/*` 83 条），前端为**零构建的原生 JS 多页应用**，媒体处理（抽帧/质检/ASR/合成/剪映草稿）全部交给内嵌的 `manju_media.py`（PyAV + numpy，跑在 ComfyUI 的 venv Python 上）。

主入口：`main.go`；路由装配：`internal/api/api.go:64-122` → `manju.RegisterRoutes` / `comfy.RegisterRoutes` / `manju.RegisterFsRoutes` / `registerZcodeRoutes`。

---

## 1. HTTP API 全量路由清单

### 1.1 路由提取口径

对全部 `.go` 源码做正则提取 `"(GET|POST|PUT|DELETE|PATCH) /..."`，去重后 **139 条**（`/tmp/routes.txt` 口径）。另有 3 条不符合该模式的注册：`OPTIONS /`（两处）、`/manage/`、`/`、`/island/`、`/splash/` 静态目录。`README.md:71` 自称“50+ /api/manju/*”，实际 **83 条**（统计命令：`grep -c "^GET /api/manju\|^POST /api/manju"`），README 是保守说法。

路由注册集中在 6 处：

| 注册点 | 文件:行 |
| --- | --- |
| 主服务通用路由 | `internal/api/api.go:64-121` |
| 漫剧核心路由（`RegisterRoutes`） | `internal/manju/manju.go:3542-3714` |
| 漫剧清理路由 | `internal/manju/manju_cleanup.go:367-398` |
| 漫剧 2K/剪映路由 | `internal/manju/manju_upscale.go:425-469` |
| 智能体路由（在 `manju.go` 内调用） | `internal/manju/manju_agent.go:2138-2600` |
| ComfyUI 路由 | `internal/comfy/comfy.go:420-427` |
| 文件系统路由（`RegisterFsRoutes`） | `internal/manju/fs.go:656-663` |
| ZCode/Bot 路由 | `internal/api/zcode.go:59-93` |

### 1.2 主服务 / 系统（11 条）

| 方法 | 路径 | 作用 | 证据 |
| --- | --- | --- | --- |
| GET | `/api/settings` | 读设置（Key 掩码 + `api_key_set`） | `internal/api/api.go:66`, `302-316` |
| PUT | `/api/settings` | 写设置并同步 ComfyUI 参数/局域网开关/全局 agent | `internal/api/api.go:67`, `318-349` |
| POST | `/api/settings/test` | LLM + ComfyUI 连通测试（30s 超时） | `internal/api/api.go:68`, `351-381` |
| GET | `/api/stats` | 系统监测快照（CPU/内存/GPU/磁盘/网络 + hw 段） | `internal/api/api.go:69`, `internal/api/sysmon.go:11-14` |
| POST | `/api/hwctl` | 设备控制：`mode`(0/1/2)、`cool`(bool)、`oc`(NVAPI +200MHz/+1000MHz) | `internal/api/sysmon.go:50-99` |
| GET | `/clips/{file}` | 渲染产物单文件下载/预览（拒绝 `..` 与分隔符） | `internal/api/api.go:97`, `render.go:54-64` |
| GET | `/api/outputs` | 成品列表（`clips/` 下 `final_*`） | `internal/api/api.go:96`, `render.go:66-93` |
| GET | `/manage/` | 旧版管理页（内嵌 `web/index.html`，注入 token） | `internal/api/api.go:112`, `277-300` |
| GET | `/` | 知识库工作台主页（FileServer + tokenInject） | `internal/api/api.go:113-118` |
| GET | `/island/` | 灵动岛页（FileServer + tokenInject） | `internal/api/api.go:102-107` |
| GET | `/splash/` | 启动动态窗口（静态、无 token） | `internal/api/api.go:108-111` |

另：`OPTIONS /` 预检兜底（`main.go:1073`、`main.go:1171`）。

### 1.3 小说创作 / 脚本（12 条）

| 方法 | 路径 | 作用 | 证据 |
| --- | --- | --- | --- |
| POST | `/api/script/generate` | 小说 → 一集视频脚本（调 LLM，`storyboard.Generate`） | `internal/api/script.go:17-43` |
| GET | `/api/script/styles` | 可选风格列表 | `internal/api/script.go:45-48` |
| GET | `/api/novel/status/all` | 全部小说进度（创作进度轮询） | `internal/api/api.go:91` |
| POST | `/api/novel/create` | 新建小说 | `internal/manju/manju.go:3706` |
| POST | `/api/novel/analyze` | 小说分析 | `internal/manju/manju.go:3707` |
| POST | `/api/novel/review` | 审稿（不过自动重写） | `internal/manju/manju.go:3708` |
| POST | `/api/novel/chapter` | 生成单章 | `internal/manju/manju.go:3709` |
| POST | `/api/novel/delete` | 删除小说 | `internal/manju/manju.go:3710` |
| GET | `/api/novel/progress` | 单本进度 | `internal/manju/manju.go:3711` |
| POST | `/api/novel/auto` | 全自动创作启动 | `internal/manju/manju.go:3712` |
| POST | `/api/novel/auto/stop` | 全自动停止 | `internal/manju/manju.go:3713` |
| GET | `/api/novel/auto/status` | 全自动状态 | `internal/manju/manju.go:3714` |

### 1.4 渲染任务（旧渲染管线，4 条）

| 方法 | 路径 | 作用 | 证据 |
| --- | --- | --- | --- |
| POST | `/api/render` | 提交逐镜渲染任务（异步，`render.Manager`） | `internal/api/render.go:18-36` |
| GET | `/api/render/jobs` | 任务列表 | `internal/api/render.go:38-41` |
| GET | `/api/render/jobs/{id}` | 单任务状态 | `internal/api/render.go:43-52` |
| — | — | `render.Manager` 内部用 `internal/assemble` + `internal/verify` 合成/质检 | `internal/render/manager.go:126`, `138` |

### 1.5 漫剧项目管理 / 方案 / 状态（22 条）

| 方法 | 路径 | 作用 | 证据 |
| --- | --- | --- | --- |
| GET | `/api/manju/projects` | 项目列表 | `internal/manju/manju.go:3542` |
| GET | `/api/manju/find` | 按小说反查项目 | `internal/manju/manju.go:3545` |
| GET | `/api/manju/project` | 单项目详情 | `internal/manju/manju.go:3546` |
| POST | `/api/manju/create` | 新建漫剧项目 | `internal/manju/manju.go:3549` |
| POST | `/api/manju/probe-dir` | 目录探测（自动识别全本/分镜/提示词/立项.json） | `internal/manju/manju.go:3550`, `manju_probe.go:90` |
| POST | `/api/manju/delete` | 删除项目 | `internal/manju/manju.go:3551` |
| POST | `/api/manju/render` | 保存渲染配置 | `internal/manju/manju.go:3547` |
| GET | `/api/manju/style` | 风格信息 | `internal/manju/manju.go:3548` |
| GET/POST | `/api/manju/settings` | 项目设置读写 | `internal/manju/manju.go:3552-3553` |
| GET | `/api/manju/novel` | 小说信息 | `internal/manju/manju.go:3554` |
| POST | `/api/manju/novel/save` | 保存小说路径 | `internal/manju/manju.go:3555` |
| GET | `/api/manju/script` | 脚本状态 | `internal/manju/manju.go:3556` |
| POST | `/api/manju/script/save` `/clear` `/import-from-novel` `/scan-dir` `/import-dir` `/import-all-dir` | 脚本直出：保存/清空/从小说导入/扫目录/导入目录/全部导入 | `internal/manju/manju.go:3557-3562` |
| GET | `/api/manju/models` | 模型清单 | `internal/manju/manju.go:3563` |
| POST | `/api/manju/env` | 环境信息 | `internal/manju/manju.go:3564` |
| POST | `/api/manju/run` | 启动六阶段管线（`all` 或单阶段） | `internal/manju/manju.go:3565`, `manju_pipeline.go:2009-2058` |
| GET | `/api/manju/status` | 运行状态 | `internal/manju/manju.go:3566` |
| POST | `/api/manju/log/clear` | 清空内存日志 | `internal/manju/manju.go:3587` |
| GET | `/api/manju/flow` | 流程/前置检查 | `internal/manju/manju.go:3610` |
| POST | `/api/manju/kill` | 停止任务 | `internal/manju/manju.go:3611` |
| GET | `/api/manju/outputs` | 产物列表 | `internal/manju/manju.go:3612` |
| GET | `/api/manju/plan` | 方案（direct_plan） | `internal/manju/manju.go:3613` |
| GET | `/api/manju/notes` / POST | 便签读写 | `internal/manju/manju.go:3639-3640` |
| GET | `/api/manju/paths` / POST | 目录与部署配置读写 | `internal/manju/manju.go:3641-3642` |
| POST | `/api/manju/skill/update` | 技能更新 | `internal/manju/manju.go:3643` |
| GET/POST | `/api/manju/notify` + `/api/manju/notify/test` | 微信升级通知配置/测试 | `internal/manju/manju.go:3652-3705` |

### 1.6 镜头（shots）与产物调试（17 条）

| 方法 | 路径 | 作用 | 证据 |
| --- | --- | --- | --- |
| GET | `/api/manju/shots` | 镜头表 | `internal/manju/manju.go:3614` |
| POST | `/api/manju/shots/clear` | 清空镜头缓存 | `internal/manju/manju.go:3615` |
| GET | `/api/manju/shot/workflow` | 单镜工作流 JSON | `internal/manju/manju.go:3617` |
| POST | `/api/manju/shot/override` | 单镜覆盖（定点改提示词） | `internal/manju/manju.go:3618` |
| GET | `/api/manju/shot/overrides` | 覆盖清单 | `internal/manju/manju.go:3619` |
| GET | `/api/manju/shot/asset` | 单镜资产静态调试 | `internal/manju/manju.go:3620` |
| GET | `/api/manju/shot/video` | 单镜视频 | `internal/manju/manju.go:3621` |
| GET | `/api/manju/shot/templates` | 镜头模板列表 | `internal/manju/manju.go:3623` |
| POST | `/api/manju/shot/template/save` `/delete` `/apply` | 模板保存/删除/应用 | `internal/manju/manju.go:3624-3626` |
| GET | `/api/manju/shot/intermediates` | 中间产物 | `internal/manju/manju.go:3627` |
| GET | `/api/manju/shot/frame` | 单帧图 | `internal/manju/manju.go:3628` |
| POST | `/api/manju/shot/edit` | 镜头编辑 | `internal/manju/manju.go:3630` |
| GET | `/api/manju/scenes` | 场景列表 | `internal/manju/manju.go:3631` |

### 1.7 角色资产库（char-lib，7 条）

| 方法 | 路径 | 作用 | 证据 |
| --- | --- | --- | --- |
| GET | `/api/manju/char-lib/list` | 全局角色库清单（跨项目） | `internal/manju/manju.go:3633`, `manju_char_lib.go:351-354` |
| GET | `/api/manju/char-lib/detail` | 单角色详情（卡+文件清单） | `manju.go:3634`, `manju_char_lib.go:356-365` |
| GET | `/api/manju/char-lib/asset` | 库内 PNG 预览 | `manju.go:3635`, `manju_char_lib.go:332-349` |
| POST | `/api/manju/char-lib/import` | 库角色导入当前项目（合并角色卡+复制资产） | `manju.go:3636`, `manju_char_lib.go:415-496` |
| POST | `/api/manju/char-lib/delete` | 删除单角色 | `manju.go:3637`, `manju_char_lib.go:367-380` |
| POST | `/api/manju/char-lib/delete-batch` | 批量删除 / `all:true` 清空 | `manju.go:3638`, `manju_char_lib.go:382-413` |
| POST | `/api/manju/char/prompt` | 取角色提示词 | `internal/manju/manju.go:3648` |

### 1.8 抽卡（gacha）与配音（voice）（8 条）

| 方法 | 路径 | 作用 | 证据 |
| --- | --- | --- | --- |
| POST | `/api/manju/gacha` | 抽卡（生成候选定妆照） | `internal/manju/manju.go:3644` |
| POST | `/api/manju/gacha/upload` | 上传候选 | `internal/manju/manju.go:3645` |
| POST | `/api/manju/gacha/adopt` | 采纳候选 | `internal/manju/manju.go:3646` |
| POST | `/api/manju/gacha/plan` | 抽卡计划 | `internal/manju/manju.go:3647` |
| GET | `/api/manju/voice/list` | 音色库列表 | `internal/manju/manju.go:3649` |
| POST | `/api/manju/voice/gen` | 生成配音 | `internal/manju/manju.go:3650` |
| POST | `/api/manju/voice/prepare` | 准备音色参考 | `internal/manju/manju.go:3651` |

### 1.9 智能体（agent）（9 条）

| 方法 | 路径 | 作用 | 证据 |
| --- | --- | --- | --- |
| GET | `/api/manju/agent` | 智能体状态/配置 | `internal/manju/manju_agent.go:2144` |
| POST | `/api/manju/agent/settings` | 智能体设置（视觉模型/及格线/返工轮数） | `internal/manju/manju_agent.go:2197` |
| POST | `/api/manju/agent/style` | 风格分析 | `internal/manju/manju_agent.go:2138` |
| GET | `/api/manju/agent/health` | 项目体检（纯本地、秒回、不调 LLM） | `internal/manju/manju_agent.go:2140`, `manju_agent_health.go:31` |
| POST | `/api/manju/agent/health/fix` | 一键修复并回写 config.json | `internal/manju/manju_agent.go:2141`, `manju_agent_health.go:598-629` |
| POST | `/api/manju/agent/chat` | 三层路由智能对话 | `internal/manju/manju_agent.go:2142`, `manju_agent_health.go:646` |
| POST | `/api/manju/agent/judge` | 审片官判分 | `internal/manju/manju_agent.go:2322` |
| POST | `/api/manju/agent/resolve` | 终审拍板 | `internal/manju/manju_agent.go:2355` |
| POST | `/api/manju/agent/vision-test` | 视觉模型连通测试 | `internal/manju/manju_agent.go:2552` |

### 1.10 合成 / 2K / 剪映 / 预告片 / 清缓存（11 条）

| 方法 | 路径 | 作用 | 证据 |
| --- | --- | --- | --- |
| POST | `/api/manju/jianying` | 剪映草稿导出（视频轨+字幕轨，同步） | `internal/manju/manju_upscale.go:426`, `473-529` |
| GET | `/api/manju/upscale2k/estimate` | 云端 2K 预校验/预估 | `internal/manju/manju_upscale.go:427-446` |
| POST | `/api/manju/upscale2k` | 云端 2K 升格任务 | `internal/manju/manju_upscale.go:447-468` |
| POST | `/api/manju/trailer` | 预告片生成 | `internal/manju/manju_agent.go:2465` |
| POST | `/api/manju/output/delete` | 删除产物 | `internal/manju/manju_agent.go:2386` |
| POST | `/api/manju/ir_expand` | IR 扩展 | `internal/manju/manju_ir.go:297` |
| POST | `/api/manju/cleanup` | 按类别清理（gacha/frames/2k） | `internal/manju/manju_cleanup.go:27-78`, `368` |
| POST | `/api/manju/cache/clear` | 一键清缓存（含 `advanced` 高级清场） | `internal/manju/manju_cleanup.go:128-330`, `370` |
| GET | `/api/manju/cleanup/sizes` | 清理目标目录占用统计 | `internal/manju/manju_cleanup.go:372-398` |

### 1.11 ComfyUI / Harness / 灵动岛 / ZCode（17 条）

| 方法 | 路径 | 作用 | 证据 |
| --- | --- | --- | --- |
| GET | `/api/comfy` | ComfyUI 状态 | `internal/comfy/comfy.go:420` |
| POST | `/api/comfy/start` `/stop` | 启停 ComfyUI | `internal/comfy/comfy.go:421-422` |
| POST | `/api/comfy/install/start` `/stop` | 一键安装启停 | `internal/comfy/comfy.go:423`, `425` |
| GET | `/api/comfy/install/status` | 安装进度 | `internal/comfy/comfy.go:424` |
| POST | `/api/comfy/install` | 安装入口（旧） | `internal/api/api.go:73` |
| GET | `/api/comfy/versions` | 版本/模型/插件/工作流检测 | `internal/comfy/comfy.go:426` |
| POST | `/api/comfy/plugins/check` | 插件检测 | `internal/comfy/comfy.go:427` |
| GET | `/api/harness` | DSH Harness 探测 | `internal/api/harness.go:162-164` |
| POST | `/api/harness/start` `/restart` | 启动/重启 DSH（`node bin.js web --no-open`） | `internal/api/harness.go:166-184`, `91-115` |
| GET | `/api/island` / POST | 灵动岛开关读写 | `internal/api/island.go:24-55` |
| POST | `/api/zcode/start` `/stop` | ZCode 桌面端启停（按 PID 杀进程树） | `internal/api/zcode.go:60-75`, `36-44` |
| POST | `/api/bot/stop` `/restart` | Bot 运行时停止（restart 同 stop，靠 ZCode 自动重建） | `internal/api/zcode.go:76-92` |

### 1.12 文件系统（fs，6 条）+ 知识库（2 条）

| 方法 | 路径 | 作用 | 证据 |
| --- | --- | --- | --- |
| GET | `/api/fs/list` | 列目录（白名单校验） | `internal/manju/fs.go:524-567`, `657` |
| GET | `/api/fs/analyze` | 目录分析（书/项目结构、章节、字数、封面） | `internal/manju/fs.go:569-580`, `149-188` |
| POST | `/api/fs/select` | 弹系统目录选择框（POST 防 CSRF）并动态注册白名单 | `internal/manju/fs.go:582-590`, `659` |
| GET | `/api/fs/read` | 读文本（≤2MB） | `internal/manju/fs.go:592-613`, `660` |
| GET | `/api/fs/file` | 读媒体/文本（扩展名白名单） | `internal/manju/fs.go:615-640`, `661` |
| GET | `/api/fs/media` | 媒体资产扫描分类 | `internal/manju/fs.go:642-653`, `662` |
| GET | `/api/page` | 知识库单页 | `internal/api/kb.go:10-18` |
| GET | `/api/asset` | 知识库图片资产（`cat`+`p` 双重护栏） | `internal/api/kb.go:21-49` |

### 1.13 独立控制端口（非 8787，共 11 条）

这三个端口**独立于主服务**，`/api/manju/*` 不在其中：

- **8799 主窗口控制**（`main.go:1071-1168`）：`OPTIONS /`、`GET /open`（单实例唤起）、`GET /nx`（窗口是否打开 + 版本/端口/PID JSON）、`GET /minimize`、`GET /maximize`、`GET /close`、`GET /move?dx&dy`、`GET /resize?w&h`。统一包 `controlGuard`（校验本机 Host + 本机 Origin，`main.go:1053-1068`），**无 token**。
- **8788 灵动岛胶囊控制**（`main.go:1170-1207`）：`OPTIONS /`、`GET /size?w&h`（展开/收起动画）、`GET /close`（退出整个应用）。同样 `controlGuard`。
- **8190 ComfyUI**：由本服务代管启动，默认 `--listen 127.0.0.1`（`internal/comfy/comfy.go:242`）。

---

## 2. 鉴权与安全

### 2.1 X-NiliX-Token 会话令牌机制

- **生成**：启动时 `rand.Read(16 字节)` → `hex.EncodeToString` = 32 位 hex（`main.go:650-654`），存包级变量 `sessionToken`（`internal/api/api.go:53`）。
- **注入前端**：HTML 响应里的占位符 `/*__NILIX_TOKEN__*/` 被替换为真实 token（`internal/api/api.go:177-228` 的 `tokenInject`，`287-291` 的 `/manage/` 直写路径）。注入点覆盖：根路径 `/`、`*.html`、`/island/`（`api.go:179-180`）。前端写入 `window.NILIX_TOKEN` 并同时落 `localStorage.nilix_token`（`web/kb/index.html:4-6`、`web/island/index.html:6`）。
- **校验**：中间件 `auth` 只对**非 GET** 请求校验（`internal/api/api.go:231-242`），头名 `X-NiliX-Token`，用 `crypto/subtle.ConstantTimeCompare` 恒定时间比较（`api.go:245-250`，防时序侧信道）。失败返回 **401** 与 `{"error":"会话失效,请刷新页面"}`。
- **前端携带**：`window.nilixHeaders()` / `window.nilixTok()`（`web/kb/index.html:4-6`），各处手动带（`web/kb/js/dirview.js:14`、`manju.js:256`、`app.js:211/409-420`、`comfy.js:270/399`）；灵动岛 `httpPost`/`httpGet`（`web/island/js/app.js:15-30`）。
- **GET 不校验的取舍**：源码注释明确“读操作 GET 不校验——真正的读保护由 fs 白名单承担”（`internal/api/api.go:51-52`）。
- **测试覆盖**：`internal/api/security_token_test.go:16-53`（无/错/对 token 与 GET 放行）、`:55-69`（manage 注入）、`:72-108`（根路径注入、静态 js 透传、token 为空不替换）、`:111-133`（注入后不得截断 HTML）、`:136-162`（fs 白名单 403/200）。

### 2.2 旁路与纵深防御（同一中间件链共 4 层）

`return s.localHostOnly(limitRequestBody(s.auth(s.recoverPanic(mux))))`（`internal/api/api.go:121`）——注意**执行顺序由外到内**为：Host 校验 → body 限流 → token 鉴权 → panic 兜底。

1. **DNS rebinding 防线 `localHostOnly`**：只接受 `Host` 为 `127.0.0.1` / `localhost` / `::1`（含带端口），否则 403（`internal/api/api.go:149-173`）。动机：恶意域名 A 记录指向 127.0.0.1 时浏览器视其同源，注入 HTML 的 token 会被攻击者页面读走。
2. **请求体上限 `limitRequestBody`**：`http.MaxBytesReader` 统一 64MB 兜底（`internal/api/api.go:137-147`）；上传类接口另设更严上限。
3. **panic 恢复 `recoverPanic`**：handler panic 时写 500 JSON 并 `log.Printf` 落 `[crash]` + `debug.Stack()`，而非断连（`internal/api/api.go:124-135`）。回归测试 `internal/api/api_recover_panic_test.go:12-35`（500 + 合法 JSON）与 `:38-54`（不干扰正常请求）。
4. **CSP / X-Frame-Options**：HTML 响应统一下发 CSP（`default-src 'self'`；`script-src 'self' 'unsafe-inline'`；`connect-src`/`frame-src` 显式放行 `http://127.0.0.1:*`，否则 ComfyUI iframe 与 8799/8788 跨端口 fetch 被拦；`frame-ancestors 'none'`；`base-uri 'self'`）+ `X-Frame-Options: DENY`（`internal/api/api.go:189-195`；`/manage/` 同策略 `280-286`）。

**发现的缺口**：`/splash/` 与静态 js/css 不经 `tokenInject` 的 CSP 分支（`api.go:108-111` 直接 `noCacheHTML`），即 splash 页无 CSP。属低风险（纯静态、无用户内容）。

### 2.3 局域网暴露警告

- **ComfyUI 无鉴权**，因此默认绑 `127.0.0.1`；设置项 `render.lan_access` 打开后才 `--listen 0.0.0.0`（`internal/comfy/comfy.go:44-55`、`240-243`）。风险注释明确写着“0.0.0.0 会暴露给局域网任意设备——可提交任务烧 GPU/读产物”。
- 配置字段与默认值：`LanAccess bool json:"lan_access,omitempty"` 默认 false（`internal/config/config.go:110-113`）；启动注入 `main.go:625`，保存设置时同步 `internal/api/api.go:343`（下次启动 ComfyUI 生效）。
- **主服务本身不提供局域网开关**：`addr := "127.0.0.1:" + *port` 硬编码（`main.go:657`），端口仅本机。
- README 未把“局域网暴露”写成显式警告，只在 `README.md:60` 提“ComfyUI（端口 8190，由本服务代管启停）”；真正警告在代码注释（`comfy.go:50-51`、`config.go:111-112`）。

### 2.4 白名单目录校验 `fsPathAllowed` 与绕过防护

`internal/manju/fs.go:445-508`：

- **注册源**：启动注入 `SetFSRoots(kbRoot, paths.NovelRootDir, comfyIn, comfyOut)`（`main.go:655`）+ 漫剧项目根恒允许（`fs.go:479-481`）+ 用户选择器动态 `addFSRoot`（`fs.go:588`，实现 `fs_files.go:77-89`）+ 项目 `config.json` 内绝对路径动态注册（`manju_pipeline.go:341-353`）。
- **绕过防护 1 —— 符号链接/junction**：比对前对路径与根都做 `filepath.EvalSymlinks`（`fsNormRoot` `fs.go:458-464`；`fsPathAllowed` `fs.go:494-496`）。不这么做时根内 junction 指向 `C:\Users\.ssh` 会因字符串前缀命中而放行。
- **绕过防护 2 —— 路径边界**：大小写不敏感比较，仅 `lp == lr` 或 `lp` 以 `lr + 分隔符` 开头才算命中（`fs.go:499-507`），避免 `C:\root2` 命中 `C:\root`。
- **绕过防护 3 —— 动态扩白名单收紧（审查 F1）**：`novel`/`novel_dir` 必须先过 `manjuGuardNovel` 才注册，防历史/恶意 config 借助免 token 的 GET 入口 `newManjuCtx` 扩白名单后任意读文件（`manju_pipeline.go:344-350`）。
- **绕过防护 4 —— 独立 config/novel 守卫**：`manjuGuardConfig` 要求路径位于 `paths.ManjuRootDir` 内、basename 必须为 `config.json`、项目名不得含分隔符且不在 `manjuSkipDirs`（`internal/manju/manju_guard.go:22-36`）；`manjuGuardNovel` 要求位于小说库根内（`manju_guard.go:40-47`）。被清理/2K/剪映/health-fix 等写操作复用（如 `manju_cleanup.go:42`、`379`；`manju_upscale.go:443/482`；`manju_agent_health.go:611`）。
- **`/api/asset` 的 cat+p 双重护栏**：`cat` 必须单段且不含 `..`/分隔符；`p` 经 `filepath.Clean("/"+p)` 归一后拒绝 `..`；最后拼出的绝对路径必须落在 `<kbRoot>/<cat>/` 之下（`internal/api/kb.go:28-47`）。
- **读大文件防护**：`/api/fs/read` 限 2MB（`fs.go:603`），`textWords` 单文件读上限 4MB（`fs.go:263`）。
- **`/clips/{file}`**：拒绝空/`..`/`filepath.Base(name) != name`（`internal/api/render.go:59-61`）。
- **`/api/manju/char-lib/asset`**：`name` 不含分隔符、`file` 不含 `..`，且强制 `filepath.Base(file)`（`manju_char_lib.go:337-341`）。

**发现的残余风险**：

- `handleFSSelect` 会把用户在系统对话框里选的**任意目录**加入白名单且**永久有效**（进程生命周期内），无回收（`fs.go:582-590`）。用户误选 `C:\`（`fsPathAllowed` 不拒绝根本身，只要求 `lp == lr`）即把整盘加入白名单。`fsPathAllowed` 只对 `p == ""` 返回 false，**没有**像 `cleanup.Start` 那样的“绝对路径 + 长度 > 3”护栏（对比 `internal/cleanup/cleanup.go:24`）。
- `handleFSFile` 的扩展名白名单包含 `.json`，配合白名单根可读取根内任意 JSON（如 `settings.json` 若在某个已注册根内）。
- `removed` 状态：`addFSRoot` 的注释块 `fs.go:484-486` 之间函数体被移到 `fs_files.go`，说明做过拆包，但 `fs.go` 残留了空注释段（无功能影响）。

### 2.5 其它安全相关

- ZCode/Bot 停止按 PID + `/T` 杀进程树，而非 `taskkill /IM` 杀全部同名实例（避免误杀用户手动开的第二个实例）：`internal/api/zcode.go:33-44`，审计注 `:33-35`。
- `handleClipFile` 注释记录了一次误伤修复：原来 `strings.Contains(name,"..")` 会误伤 `final_1..v2.mp4`，改为 Base 校验（`internal/api/render.go:57-59`）。
- 写设置时 API Key 掩码用精确 `== maskKey(old)` 判断，而非 `strings.Contains("****")`（`internal/api/api.go:326-331`）。
- LLM Key 在 `settings.json` 中 AES-GCM 加密（`internal/config/config.go:320/373`），但 `server/settings.json` 明文（`README.md:101`）。
- 响应统一 `Cache-Control: no-store`（`internal/api/api.go:388`）。

---

## 3. 前端技术栈

### 3.1 技术栈：**纯原生 JS，零框架零构建**

- `web/` 下**没有** `package.json`、`node_modules`、`*.config.js`、打包产物（已用 find 确认）。全部为静态 HTML/CSS/JS，直接 `go:embed` 进二进制：`main.go:56`（`web/index.html`）、`:62`（`web/kb`）、`:65`（`web/island`）、`:68`（`web/splash`）。
- **模块化方式**：全局对象字面量 + IIFE，非 ES module、无 import/export。例：`const I18N = {...}`（`web/kb/js/i18n.js:2`）、`const NovelView`/`ManjuView`/`ComfyView` 全局单例（`web/kb/js/app.js:958-975` 按名字引用）、灵动岛 `(function(){ "use strict"; ... })()`（`web/island/js/app.js:2`）。
- **DOM 操作**：原生 `document.getElementById` / `querySelectorAll` + `classList.toggle`，无虚拟 DOM。路由靠 `location.hash` 手写（`web/kb/js/app.js:947-956`）。
- **CSS 方案**：CSS 变量 + `data-theme` 属性切换，原生级联。`<html data-theme="nebula" data-style="default">`（`web/kb/index.html:2`），主题/材质/主样式三层拆分：`themes.css`(292 行)/`materials.css`(101 行)/`app.css`(5497 行)。README 称 8 套深色银河主题（`README.md:41`）。
- **Markdown 渲染**：自研 `markdown.js`（117 行）而非引入 marked/markdown-it（`web/kb/index.html:839`）。
- **Web Components**：未使用。
- **无 Web 框架的代价**：主逻辑文件巨大——`web/kb/js/manju.js` **6057 行**、`app.js` 1091 行、`dirview.js` 1549 行，全部手写 DOM。

### 3.2 文件规模（`wc -l`）

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `web/kb/index.html` | 846 | 主窗口（三页视图 + 内联 SVG 图标库 + 首帧 boot-loader） |
| `web/kb/js/manju.js` | 6057 | 视频管理：项目/渲染配置/管线/镜头/角色库/抽卡/配音 |
| `web/kb/js/app.js` | 1091 | 主控制器：hash 路由、设置弹窗、主题、Toast、吉祥物、i18n 初始化 |
| `web/kb/js/dirview.js` | 1549 | 目录视图：书架/阅读器/项目/媒体（小说管理） |
| `web/kb/js/comfy.js` | 429 | ComfyUI 页：状态轮询、日志行、内嵌 iframe、启停 |
| `web/kb/js/harness.js` | 187 | DSH Harness 服务监控（页面入口已从 HUD 移除，见 `web/island/js/app.js:57`） |
| `web/kb/js/markdown.js` | 117 | 自研 Markdown → HTML |
| `web/kb/js/winctl.js` | 118 | frameless 窗口边缘拖拽/缩放（调 8799） |
| `web/kb/js/i18n.js` | 42 | 语言包加载 + `data-i18n*` 应用 |
| `web/kb/css/app.css` | 5497 | 主样式 |
| `web/kb/css/themes.css` | 292 | 主题变量 |
| `web/kb/css/materials.css` | 101 | 材质/纹理 |
| `web/index.html` | 441 | 旧版 `/manage/` 管理页（剧本 + 设置两个 tab） |
| `web/island/index.html` | 184 | 灵动岛（胶囊 + 展开面板） |
| `web/island/js/app.js` | 875 | 灵动岛逻辑 |
| `web/island/css/app.css` | 755 | 灵动岛样式 |
| `web/splash/index.html` | 107 | 启动动态窗口（纯展示） |

`web/kb/assets` 占 **16MB**（`web/kb` 总体 17MB），其中 `assets/styles/*.gif` 14MB（8 个风格演示 gif）+ `anim01.webp` 2.2MB。

### 3.3 页面结构

三页在同一 HTML 内以三个 `<section class="view">` 呈现，靠 `is-active` class 切换，**单页应用式**但无框架：

- **视频管理** `#view-manju`（`web/kb/index.html:252`），默认路由 `#/manju`；布局：顶栏系统监测条 + 左栏目录/项目 + 中栏六阶段执行 + 右栏角色/镜头。入口对象 `ManjuView.enter()` + `ManjuWorkbench.enter()`（`app.js:968-970`）。
- **小说管理** `#view-novel`（`index.html:226`），路由 `#/novel`；入口 `NovelView.enter()`（`app.js:967`）。
- **ComfyUI** `#view-comfy`（`index.html:174`），路由 `#/comfy`；含内嵌 `<iframe id="cfy-frame">`（`index.html:213`）、常驻日志行 `#cfy-log-bar`、产物面板；入口 `ComfyView.enter()`（`app.js:966`）。
- 导航：三个 `<a class="nav-link" data-route="...">`（`index.html:135/138/142`），hash 变化 → `route()`（`app.js:956-985`）。
- **设置**：`#settings-btn`（`index.html:153`）打开全局设置弹窗；另有 `#manju-settings`（`index.html:292`）项目级设置。
- **灵动岛**：独立窗口 `web/island/`。胶囊态显示 C/M/G + 时钟，展开态为完整监测面板（`web/island/index.html:11-60`）。功能桥接：原 go-webview2 的 Go 绑定 → 回退到 HTTP API（`web/island/js/app.js:7-60`）：`setIsland`→8788 `/size`、`closeWin`→8788 `/close`、`startComfy`/`stopComfy`→8787 `/api/comfy/*`、`startZCode`/`stopZCode`→8787 `/api/zcode/*`、`stopBot`/`restartBot`→8787 `/api/bot/*`、`openComfy`→8190、`openKB`→8787。Harness 桥接已随 HUD 监控行删除（`app.js:57` 注释）。
- `web/index.html`（`/manage/`）是**旧版管理页**（标题“小说转视频 · 生产流水线”，`web/index.html:6`；tab：剧本/设置 `:103-105`），承载 `storyboard`/`render.Manager` 旧链路；与 `web/kb/` 主工作台并存。
- 路由注释明确：“`harness` 已删除（2026-08-26 用户要求）”（`web/kb/js/app.js:946`），但 `harness.js` 与 `/api/harness*` 后端仍在。

### 3.4 静态资源版本化 `?v=` 机制

- **手工版本号，无自动生成**：全站 `?v=` 仅 15 处，两个版本串——`202609041922`（12 处，kb 页：`index.html:10-13` + `:838-844`）与 `202608282100`（3 处，island：`web/island/index.html:7` 及 css/js）。**没有**构建脚本/哈希注入逻辑（无 package.json/build 工具），改前端必须手动 bump。
- README 把它写成硬规则：“静态资源版本化：HTML `no-cache` + 资产 `?v=` 参数，改前端必须 bump 版本号”（`README.md:114`）。
- **与后端双保险**：`noCacheHTML` 中间件给所有本地静态资源下发 `Cache-Control: no-cache, must-revalidate`（`internal/api/api.go:252-260`），动机是“改完代码但 WebView2 命中旧缓存”的经典坑；HTML 内 `?v=` 作双保险（注释 `api.go:253-254`）。API 响应另走 `no-store`（`api.go:388`）。
- `tokenInject` 在替换 token 后**必须丢弃 `Content-Length`**（占位符 21B → token 32B 长度变化），否则浏览器按旧长度截断 HTML → “页面 JS 缺失窗口一片黑”（`internal/api/api.go:216-219`；回归测试 `security_token_test.go:111-133`）。

### 3.5 前端鉴权携带

`web/kb/index.html:4-6` 定义三个全局：
```js
window.NILIX_TOKEN = "/*__NILIX_TOKEN__*/";   // 服务端注入；>8 位则落 localStorage
window.nilixTok  = function() {...}            // token 或 localStorage 兜底
window.nilixHeaders = function(h) {...}        // 合并 X-NiliX-Token
```
使用点：`dirview.js:14`、`manju.js:256`（各自取 `window.NILIX_TOKEN`）、`app.js:211`（fs/select 手动头）、`app.js:409-420`（`apiGet` 统一带）、`comfy.js:270/399`（`_vmToken()` + POST 头）。灵动岛 `nilixTok()`（`web/island/js/app.js:15-20`）+ `httpPost`/`httpGet`（`:21-27`）；控制端口调用走 `ctlGet`（**故意不带**自定义头，避免跨端口 CORS 预检；8788 只注册了 `GET /size` 无 OPTIONS handler，`app.js:28-33`）。

### 3.6 i18n

- 实现：`I18N` 对象，`init()` 读 `localStorage["kbw-lang"]`（默认 `zh`）→ `fetch('/i18n/<lang>.json')` → `apply()` 按 `data-i18n`/`data-i18n-placeholder`/`data-i18n-title` 三属性回填 `textContent`/`placeholder`/`title`，并同步 `<html lang>`、`document.title`、`.lang-btn.is-active`（`web/kb/js/i18n.js:2-42`）。`t(key)` 缺失时回退 key 本身（`i18n.js:39-41`）。
- 语言包：`web/kb/i18n/zh.json`、`web/kb/i18n/en.json`。
- 覆盖范围：**仅 kb 主页面**（island/manage 页无 i18n 脚本）。切语言派发 `i18n:changed` 事件（`i18n.js:19`）。

### 3.7 winctl.js 与窗口控制

`web/kb/js/winctl.js`（118 行）实现 frameless 窗口的边缘拖拽/缩放 → 调 `http://127.0.0.1:8799/resize?w=&h=`（`main.go:1147-1167`，含最小 900×600 约束）。顶栏按钮直接内联 `onclick="fetch('http://127.0.0.1:8799/minimize')"`（`web/kb/index.html:163-165`）。

---

## 4. 知识库（zhishiku）

### 4.1 Markdown 解析与双链：**纯标准库**

`internal/kb_work/parser.go` 的 import 只有 `fmt/os/path/filepath/regexp/sort/strings/sync/time/unicode/utf8`（`parser.go:3-13`）——**无第三方 Markdown 库**。README 也标注“kb_work/ 知识库解析/双链(纯标准库)”（`README.md:86`）。前端另有自研 `markdown.js`。

数据结构：`Category`（两级，`Parent` 空=顶层大类）、`Page`（`ID/Title/Category/Color/Links/RawLinks/Words/Mtime/RelPath/Snippet/Markdown`）见 `internal/kb_work/models.go:1-26`。

### 4.2 解析流程

1. **扫描**（`parser.go:116-181`）：读根目录；`README.md`（大小写不敏感）作为索引节点，用专用色 `indexColor="#5DADE2"`（`parser.go:25`、`129-135`）；跳过点开头目录（`:139`）；每个目录成为 `Category`（`:142-148`），递归 `scanCatDir`（`:184-211`）——直接 `.md` 归当前分类，子目录变成子分类（`Parent=父分类`），跳过点开头目录与 `assets`（`:193-195`）。
2. **分类配色**：`readLegend` 从 `README.md` 的 Markdown 表格解析 `| 分类名 | ... \`#RRGGBB\` |`（正则 `legendRe` `parser.go:30`，实现 `:256-271`）；缺失时用 `defaultColors` 6 色兜底（`:16-23`）+ 兜底色 `#5DADE2`（`:273-278`）。
3. **单页解析** `parsePage`（`:214-253`）：ID=文件名去 `.md`；Title=首个 `^#\s+(.+)$`（`titleRe` `:29`）；字数 `countWords`（先剔代码块/行内代码 `codeRe` `:31`，再剔 `[#>*_\[\]|(){}!-]` `mdStripper` `:32`，最后按空白折叠后计 rune）；Snippet=首个非空/非标题/非引用行截断 100 rune（`:288-302`）；Mtime 转 UTC RFC3339。
4. **双链实现**（`[[目标]]`）：正则 `linkRe = \[\[([^\]]+)\]\]`（`parser.go:28`）。**审计 H17 修复**：先 `codeRe.ReplaceAllString(text," ")` 剔除代码块/行内代码再提取，避免代码示例/HTML 注释里的 `[[x]]` 生成幽灵边（`parser.go:229-233`）。链接解析为两类：`RawLinks` 存全部原文（含 `|别名`），`Links` 在扫描末尾解析——按 `TrimSpace(SplitN(raw,"|",2)[0])` 取目标，大小写不敏感匹配 `byBase`，指向首个同名页，最后 `dedupe`（`parser.go:165-176`）。别名（`[[目标|显示]]`）与 `snippet` 中链接降级为纯文本（`:294`）。
5. **同名页 ID 去重**：追加 `~N` 后缀，注释说明原因是“ECharts 图谱对重复节点 ID 会崩（Cannot set properties of undefined (setting 'dataIndex')）”（`parser.go:151-164`）。
6. **并发安全**：`Store` 用 `sync.RWMutex`；`Snapshot`/`Page` 返回深拷贝（`Links`/`RawLinks` 也复制），避免外部并发读写（`parser.go:36-113`）。
7. **排序**：分类按名升序；页面按 mtime 降序（`parser.go:178-179`）。

### 4.3 小说素材自动利用的“五类扫描”

README 表述：“小说素材自动利用（人物/场景/设定集/封面五类扫描注入方案生成，日志「📎已利用小说素材」）”（`README.md:31`）。

实现：`scanNovelAssets(root)` + `manjuNovelAssets`（`internal/manju/manju_llm.go:616-736`）。结构体字段即分类：`CharPrompt` / `ScenePrompt` / `ExtraPrompt` / `Setting` / `CoverPrompt`，另有 `StylePrompt` / `NegPrompt` / `Files`（`manju_llm.go:624-633`）。扫描逻辑：

| # | 来源 | 归类字段 | 证据 |
| --- | --- | --- | --- |
| 1 | `素材/渲染提示词总集.md`（统一单文件，优先） | `StylePrompt`/`NegPrompt`/`CharPrompt`/`ScenePrompt`/`ExtraPrompt` | `manju_llm.go:658-667`；解析器 `parseRenderPromptMaster` `:738+` |
| 2 | `素材/` 下其余 `.md`（文件名含「人物/角色」） | `CharPrompt` | `manju_llm.go:672-682` |
| 3 | `素材/` 下含「场景/背景」的 `.md` | `ScenePrompt` | `manju_llm.go:683-687` |
| 4 | `素材/` 下其它 `.md` | `ExtraPrompt` | `manju_llm.go:688-693` |
| 5 | `设定集/*.md`（全部合并） | `Setting` | `manju_llm.go:695-721` |
| 6 | `封面/封面提示词.md` | `CoverPrompt` | `manju_llm.go:723-727` |

细节与阈值：每段 `readTrunc` 截断 **8000 字**（rune，`manju_llm.go:647-650`）；设定集另有总量 **20000 字** 截断（`:711`）；总集解析五节映射：一、渲染风格 → `StylePrompt`；二、全局负面 → `NegPrompt`；三、角色（`### 3.N` + 代码块）→ `CharPrompt`；四、场景（表格 `场景|提示词`）→ `ScenePrompt`；五、H3 Ref2VA 六段式母版 → `ExtraPrompt`（`manju_llm.go:733-737`）。节标题切分兼容中文数字「一、二、三…」与阿拉伯数字「1. 2. 3.」两种编号（`:731-732`）。`Files` 收集发现的素材清单用于日志展示。

知识库模板注入：`internal/manju/manju_llm.go:255-311`，默认路径候选 `C:\Mi\Ai\WorkBench\zhishiku\创作管理\AI漫剧`（+`\提示词模板`）、旧路径 `...\zhishiku\AI漫剧生产` 兜底（`manju_llm.go:261-263`），注入格式 `【知识库模板《名》要点(仅作设定/风格参考...)】`（`:311`）；模板缺失时聚合警告（`manju_pipeline.go:3037`）。

---

## 5. 角色资产库

### 5.1 目录布局与权威性

权威库根：`paths.CharLibDir = <exeDir>/asset_lib/characters`（`internal/paths/paths.go:46-48`），与音色库 `asset_lib/voices` 同属 `asset_lib`（`:42-48`）。旧分立目录 `char_lib/`、`voice_lib/` 启动时幂等迁入（不覆盖同名、移空后删旧目录，`paths.go:57-80`）。`manjuCharLibDir()` 在未解析时回退 `ManjuRootDir/asset_lib/characters`（`manju_char_lib.go:45-50`）。

每角色目录（`manju_char_lib.go:12-23`）：`card.json` + `<名>.png`(主定妆) + `_front`/`_full`/`_side`/`_detail`/`_q`/`_form2`(真身) 共 7 类后缀（`charLibCardSuffixes` `manju_char_lib.go:61`）。另有库级汇总索引 `asset_lib/characters/index.json`，写操作实时全量重建、读侧缺失惰性重建（`manju_asset_index.go:35-110`）；索引条目含七字段摘要 + 指纹 + 资产清单 + 来源项目（`manju_asset_index.go:20-33`、`64-76`）。音色库同构 `asset_lib/voices/index.json`（`:150-184`）。路径迁移完成钩子 `paths.OnPathsMigrated` 由 manju 包 `init()` 注入以重建索引（`manju_char_lib.go:498-503`，`paths.go:97-105`）。

### 5.2 定妆照 → 角色板 → 多视图流程（**角色板已删除**）

`stageAssets`（`internal/manju/manju_pipeline.go` 约 5360-5560）实际顺序：

1. **代数自愈清理**（文件存在即跳过是主逻辑，代数升级强制清旧重出）：
   - `views_gen`：只删 `_full/_side/_detail/_q/_board` 五类视图（`manju_pipeline.go:5372-5393`，常量定义 `:3910-3930`）。
   - `q_gen`：只删 `_q.png`（`:5394-5414`，常量 `:3930/3965`）。
   - `portrait_gen`：只删主图与 `_form2.png`（`:5415-5435`，常量 `manjuPortraitGen`）。
2. **库复用优先**：先 `ctx.manjuCharLibReuse(m, cmap, lg)`，命中则跳过渲染（`:5446`、`manju_char_lib.go:166-213`）。
3. **主定妆照**：`charSeed(cid,"main")` + `portraitWF(image_prompt,...)` → `<cid>.png`（`:5447-5457`）。
4. **双形态定妆**：`second_form` 非空 → `<cid>_form2.png`，独立 seed `charSeed(cid,"form2")`（`:5458-5471`）。
5. **正脸特写**：非物品角色调 `ensureFaceCrop`（`:5472-5477`）。
6. **多视图**：`viewSet := []string{"full","side","detail","q"}`（`:5515`），全部基于主图 img2img，`viewStrength`：full=0.93 / side=0.90 / detail=0.7（`:5500`），Q 版统一 0.93 且用方形同幅（`:5552`）。视图若早于主图 mtime 则重出（`:5535-5544`）。

**关键事实：角色板（`_board.png`）已于 2026-08-27 用户裁决删除**——“板零消费纯成本（渲染参考只用 front/full/detail/主图，前端过滤不显示），存量 `_board.png` 由 views_gen 清理逻辑统一删除”（`manju_pipeline.go:5501-5504`）。而 README 仍写“定妆照 → 角色板（板基于主图 img2img，展示置顶）→ 多视图”（`README.md:34`）——**README 过时**。`board_prompt` 字段仍在 LLM 角色卡 schema 中（`manju_llm.go:874/961`），`manjuBoardPromptFor` 等函数仍在（`manju_pipeline.go:704-742`），但不再产出资产。视图清理清单里仍保留 `_board.png` 以清理存量。

### 5.3 YuNet 人脸检测（go:embed onnx）

- **模型内嵌**：`//go:embed scripts/face_detection_yunet_2023mar.onnx` → 变量名却是 `manjuHaarFaceXML`（历史命名遗留），注释说明动机：“opencv-python 5.x 移除 `CascadeClassifier` 且 pip 包不带模型数据，改为内嵌随 exe 释放到媒体脚本同目录，py 侧 `FaceDetectorYN` 加载”（`internal/manju/manju_pipeline.go:37-42`）。模型文件 232,589 字节（`internal/manju/scripts/face_detection_yunet_2023mar.onnx`）。
- **运行时释放**：`ensureMediaHelper()` 每次覆盖写 `manju/logs/media/manju_media.py` 与同目录 onnx（`manju_pipeline.go:7998-8007`），保证脚本随 exe 版本更新。
- **调用链**：Go `ensureFaceCrop(cid, beast, lg)`（`manju_pipeline.go:8030-8065`）→ 组 args `facecrop --src 主图 --dst _face.png --ratio <w>x<h>`（`:8051`）→ Python `cmd_facecrop`（`internal/manju/scripts/manju_media.py:1115+`）→ `_detect_face_rect`（`:1082+`）用 `FaceDetectorYN` 检测。
- **兽类跳过**：`beast=true` 时不做人脸检测（兽脸必然不命中，白跑一次检测+刷告警），物品类同样跳过（`manju_pipeline.go:5469-5475`、`8028` 注释）。
- **阈值**：注释记录 YuNet 阈值由 0.5 降到 **0.4**（`:3975`）。
- 另有 `_face` 是主图副本的识别（`filesEqual` MD5，`manju_pipeline.go:8009-8016`），用于判断是否需要真正重裁。

### 5.4 双形态契约

契约链：素材「真身提示词：」→ 角色卡 `second_form` 字段 → 定妆 `<id>_form2.png` → 渲染遇「真身·角色名」镜切换参考图（`shotRefViews`）→ 前端「真身·角色名」切换按钮。证据：`manju_pipeline.go:5452-5466`（生成）、`manju_char_lib.go:22/61`（库后缀 `_form2`）、`README.md:34`。`second_form` 是形象指纹的参与字段之一（见下）。

### 5.5 跨项目形象指纹 sha256 复用

- **指纹算法** `manjuCharFingerprint`：7 字段按 `\x00` 连接后 `sha256`，取前 **12 字节** hex（24 hex 字符）：`id`、`image_prompt`、`q_form`、`second_form`、`species`、`gender`、`age`（`manju_char_lib.go:63-77`）。
- **跨名形象指纹** `manjuCharLookFingerprint`：同上但**不含 id**（六字段），用于“同脸不同名”复用；用户实锤场景：宋明堂×裴照两本书各写近似提示词各渲一张脸（`manju_char_lib.go:79-95`）。
- **匹配 `manjuCharLibMatch` 两级**（`manju_char_lib.go:107-164`）：① 同名 + 主指纹一致，且库内有主图 `<cid>.png` → 命中；② 名字不同但 look 指纹逐字一致 → 跨名命中（先排除六字段全空的空卡乱命中，`:131-134`）。
- **复用 `manjuCharLibReuse`**（`:166-213`）：按 `charLibCardSuffixes` 复制到项目 `assets/characters/`，跨名时按本项目角色名重命名落地（`:184`），并回写 `asset_map.json`（`:196-203`）。**内容相同不覆盖**（`filesEqual`）——修复“每次运行 mtime 刷新 → ensureFaceCrop 判正脸早于主图重裁 → 参考图指纹全 stale → 整个续跑全量重渲”的根因（`:186-195`）。
- **入库 `manjuCharLibStore`**（`:215-259`）：角色全部资产生成完成后复制入 `char_lib/<名>/`，幂等覆盖；同形象（look 指纹命中不同名）**不新建重复条目**（`:229-231`）；写 `card.json`（含 `Card`/`Fingerprint`/`CreatedAt`/`SourceProject`）+ 重建索引（`:249-257`）。结构定义 `manjuCharLibCard` `:52-58`。
- **删除/详情**：非空且不含 `\/` 的角色名校验（`:271`、`:286`、`:337`）；详情返回卡+PNG 清单+主图选择（`:285-330`）。
- **导入当前项目** `manjuCharLibImportHandler`（`:415-496`）：① 角色卡合并进 `plan.characters`（同名替换、新名追加，此处实现有个 bug——同名分支里 `m = card` 只改局部变量，实际未写回数组，见 `:459-469`）；② 复制资产 + 回写 asset_map；③ 落盘 plan + characters JSON。
- 索引指纹展示前 8 位（`manju_asset_index.go:117-120`）。

---

## 6. 机械质检

**重要澄清**：`internal/verify/verify.go` **不含**段尾冻结检测（README `:83` 写“verify/ 成片机械质检(含段尾冻结检测)”是错的）。`verify` 只用于旧渲染链路的成片基础校验（`internal/render/manager.go:138`），全部冻结/近黑/静音/OCR 阈值都在 `manju_media.py` 里。

### 6.1 `internal/verify/verify.go`（151 行，4 项检查）

`Verify(path)`（`verify.go:61-107`），ffprobe 命令：`-v error -show_entries stream=codec_type,codec_name -show_entries format=duration -of json`（`:68-71`），**30s 超时**（`:66-67`，审计修复挂死）。

| 检查项 | 判定 | 证据 |
| --- | --- | --- |
| 时长 | `duration > 0`（`%.2fs`） | `verify.go:83` |
| 视频流 | 存在 `codec_type=="video"` | `verify.go:85-88`, `94` |
| 音频流 | 存在 `codec_type=="audio"` | `verify.go:90-93`, `95` |
| faststart | `moov` 原子位置在 `mdat` 之前 | `verify.go:97-98`, `109-141` |

`checkFaststart` 实现细节（`:109-141`）：**分块扫描**而非整文件读入（审计 M9，成片 GB 级防 OOM）——先读头部 **64KB**，若同时找到 `moov`/`mdat` 则比较位置；否则再读尾部 **1MB**，拼接后 `bytes.Index` 比较，`moov>=0 && (mdat<0 || moov<mdat)` 为通过。`Report.Passed = 所有 Check.Passed`（`:100-105`）。

### 6.2 `manju_media.py` 的 `check_video`（单镜质检核心）

`check_video(path, threshold=0.5)`（`internal/manju/scripts/manju_media.py:43-118`）。用 PyAV **视频+音频交错单遍解码**（`:71-79`，注释：先解完视频再解音频会拿不到音频帧）。

抽帧与采样策略：
- 视频每 **6** 帧取 1 帧灰度图（`if n % 6 == 0`，`:72`），降采样 `g[::8,::8]` 存 `frame_sigs`（`:77`）。
- 音频每 **4** 帧取 1 帧（≈128ms 一点，全片均匀，`elif n_a % 4 == 0` `:79`），累积 `rms_sum`/`rms_n` 与最大值 `audio_peak`（`:84-89`）。

| 指标 | 精确阈值 / 公式 | 证据 |
| --- | --- | --- |
| 时长 `duration_s` | `round(v.duration * time_base, 2)`，失败 0 | `:48` |
| 分辨率 `resolution` | `(v.width, v.height)` | `:49` |
| 音轨数 `audio_streams` | `len(streams.audio)` | `:50`, `:112` |
| 近黑帧 `dark_ratio` | 采样帧 `float(g.mean() < 20)` 的比例（**整帧平均亮度 < 20**，非像素占比）；**丢弃最后 5% 采样**（合法 fade out 不计入） | `:76`, `:93-95`, `:114` |
| 冻结 `freeze_ratio` | 取末尾 **25%** 采样点窗口（至少 4 点）；相邻降采样帧**逐像素绝对差均值 < 0.25** 记冻结；占比 = 冻结邻接数 / 窗口邻接总数，round 3 | `:98-103` |
| 音频响度 `audio_rms` | 全片均匀采样帧 `abs(arr)/max` 的 `.mean()` 均值，round 4 | `:104`, `:84-89` |
| 音频峰值 `audio_peak` | 采样帧内样本**最大绝对值**（旧“帧均值”会低估瞬态/短台词） | `:87-89`, `:116` |
| 音轨规格 | `audio_rate`（H3 原生 **32000**Hz）、`audio_channels`（**2**） | `:105-110` |
| 解码帧数 `decoded_frames` | 视频帧计数 `n` | `:114` |

**冻 detection 判据的演进（关键）**：注释明确记录了 2026-09-03 的“判据根治”——旧“整帧灰度均值相邻差”对慢速运镜结构性误报（修仙界 EP01 24/35 镜被误报“段尾冻结100%”，但抽帧 hash 全异=画面在动）；改为**降采样像素差**后真冻结（编码噪声级 <0.1）与慢运镜（≥0.3）分界清晰，阈值取保守侧 **0.25**（`manju_media.py:52-62`）。

### 6.3 **段尾冻结检测**（本报告重点）

分两层实现：

**(a) 检测**：`check_video` 的 `freeze_ratio`（见上，末尾 25% 窗口，阈值 0.25，占比 > 0.6 判失败）。

**(b) 自动截尾修复** `trim_frozen_tail(path, min_tail=0.5, max_cut_ratio=0.4, sample_every=2)`（`manju_media.py:121-183`）：
- 动机：“H3 长镜存在运动衰减，动作早早 settle 后画面趋静止（实测 `freeze_ratio` 0.33~1.0），换 seed 重渲也不解决（模型固有特性）——旧 QC 判不合格触发重渲纯属烧 GPU。正确姿势：程序检测冻结尾巴并 packet-copy 截除（无损重封装，秒级）”（`:122-128`）。
- 算法：每 `sample_every=2` 帧采样降采样签名（`g[::8,::8]`，`:143-144`），算相邻差 `means`，置 `means[0]=0` 对齐游程索引（`:149-150`）；从尾部向前找连续冻结游程，阈值 `TH = 0.25`（`:152`），**容忍 1 个孤立非冻结抖动点**（`outlier` 标志，`:151-161`）。
- 截断条件（三选全部满足才动刀）：`tail_sec >= min_tail(0.5s)` 且 `tail_sec <= total_sec * max_cut_ratio(0.4)` 且 `total_sec - tail_sec >= 1.0s`（保住至少 1s）；`keep_sec = max(1.0, j*sample_every/fps - 0.2)`（留 **0.2s** 余量）（`:162-166`）。
- 实现：**packet-copy 无损截断**——`add_stream_from_template` 复制流模板，按各流 `pts*time_base <= keep_sec` 过滤 mux，写 `path+".trim.mp4"` 后 `os.replace`（`:167-182`），返回 `total_sec - keep_sec`（`:183`）。

**(c) 触发与联动**（`cmd_qc`，`:358-487`）：
- `if r["freeze_ratio"] > 0.6 and not args.no_defreeze:` → `trim_frozen_tail`；成功后 `check_video` 重算，`freeze_ratio <= 0.6` 则记**软告警** `段尾冻结已截尾X.Xs`（不判失败、不重渲）（`:420-426`）。
- 之后仍 `if r["freeze_ratio"] > 0.6: flags.append("段尾冻结X%")`（`:445-446`）→ 判失败 → 触发删旧重渲。
- Go 侧开关 `render.defreeze=false` 关闭（`manju_pipeline.go:7569-7575`：`:7569` 注释、`:7573-7574` 追加 `--no-defreeze`）。

### 6.4 `cmd_qc` 全部检查项与阈值（`:358-487`）

| flag / soft | 精确条件 | 证据 |
| --- | --- | --- |
| `无音轨` | `audio_streams == 0` | `:428` |
| `静音丢台词` | `audio_rms < 0.02` 且该镜**有台词**（plan：`dialogue` 非空且 ≠「无」，或 `h3_prompt` 含 `<d>`）且 `audio_peak < 0.06` | `:429-436`；台词判定 `:388-407` |
| 软告警 `响度偏低` | 同上但 `audio_peak >= 0.06`（峰值窗口有声音，整段均值被前奏/淡出拉低） | `:433-434` |
| 软告警 `静音告警` | `audio_rms < 0.02` 且**无台词**（空镜环境音弱，换 seed 未必出，BGM 可兜底） | `:437-438` |
| `音轨采样率异常` | `audio_rate > 0 且 != 32000` | `:439-440` |
| `音轨非立体声` | `audio_channels > 0 且 != 2` | `:441-442` |
| `近黑帧N%` | `dark_ratio > args.threshold`（CLI 默认 0.5） | `:443-444` |
| `段尾冻结N%` | `freeze_ratio > 0.6` | `:445-446` |
| `解码0帧` | `decoded_frames == 0` | `:447-448` |
| `时长过短` | `duration_s < 0.5` | `:449-450` |
| `字幕位文字×N(...)` | OCR：抽 3 帧，框中心 `cy >= 0.78 且 0.3 <= cx <= 0.7 且 框宽 > 0.02` | `:453-462`；判据实现 `scan_text_bleed` `:243` |

- 静音分级的动机注释：`「台词:无」是技能侧占位契约`，非空字符串真值曾把无台词镜全判成“有台词”（`:399-401`）。
- 报告 JSON：每镜 `{ok, flags, soft_flags, duration_s, dark_ratio, freeze_ratio, audio_streams, audio_rate, audio_channels, audio_rms, decoded_frames, error, text_bleed}`（`:468-473`），落盘由 Go 传 `--json`（`manju_pipeline.go:7583-7585`）。
- **`ok = not flags`**（软告警不置 false，`:468`）；有 flags 即 `exit 1`（`:484-486`）。
- **ASR（台词核对 / 音画同步）** `cmd_asr`（`:1182-1313`）：faster-whisper `small` + `int8` + CPU（`:1191-1197`）；转写 `beam_size=1, vad_filter=True`（`:1268`）；比对前 `norm()` 去空白/全半角标点、转小写（`:1237`）、`opencc t2s` 简繁归一（`:1240`）；判定 `ns == ne` 或 `ne in ns`（允许 whisper 多识别环境音，`:1294-1296`）；额外拦截两类：台词被逐字念出标签（`内心·`/`旁白:`，`:1279`）与同一句重复配音（`ns.count(nln) > 1` 且原句 ≥3 字，`:1287`）；`_naudio == 0` 时无台词镜跳过、有台词镜直接判失败（`:1257`）；`--strict` 下无台词镜要求完全静音（`:1298`）。
- **幽灵人声检测**（无台词镜检出人声）见 §6.5。

### 6.5 QC 结果回流与自愈

- Go `stageQC`（`manju_pipeline.go:7559-7610+`）：传 `--dir <clips/<ep>>`、`--plan <ep>_direct_plan.json`、`--json <qcReportPath>`，可选 `--shots`（定点质检，`expandShotList`）与 `--no-defreeze`（`:7566-7586`）。
- 失败集 `qcFailedShots()` 从报告 `shots[].ok==false` 解析（`manju_pipeline.go:7788-7811`），跳过集 `qcSkipSet()` 剔除；质检通过的镜头清掉历史重试轮数（`:7587-7601`）。
- **基础设施失败防线**：脚本崩溃/报告未落盘时失败集为空，此时 `err != nil` 必须显式报错，绝不静默“通过”放坏片进成片（`:7602-7604`）。
- **重渲**：渲染阶段开头 `qcFailed := ctx.qcFailedShots()`，命中的镜头删旧重渲并换 seed（`manju_pipeline.go:7202-7246`）；`qcReportClear()` 渲染消费后删除报告避免重复触发（`:7813-7816`）。
- **幽灵人声** `qcGhostVoiceCheck`（`internal/manju/manju_qcghost.go:233-317`）：仅无台词镜参与（`ghostVoiceEligible`：dialogue 空或「无」、narration 空、h3 剔除 `AUDIO & LIP DISCIPLINE` 段后不含 `<d>`，`:38-50`）；叙事块组头要求全组无台词（防“头静尾说”无限重渲，`:246-263`）；主判为批量 faster-whisper ASR（`asrGhostVoiceBatch`，`:159-229`，一次进程 stdin 逐行路径、10 分钟超时、`@@GHOST@@` 标记 JSON），判定 `e-s >= 0.5s 且 avg_logprob > -1.0 且 文本 >= 2 字`（`:218`）；ASR 不可用时退回声学双特征（silencedetect `noise=-35dB:d=0.25` + `highpass=200,lowpass=3400`，要求 ≥2 段语音节奏，`:52-97`）；命中写回 `appendQCFlag` 置 `ok=false`（`:319-347`）。
- **视觉抽检** `qcVisualCheck`（`internal/manju/manju_qcvision.go:34-97`）：QC 通过的镜头抽 1 帧（`frames --count 1`）交视觉模型判硬伤（崩坏/花屏/五官错乱/全黑白），命中 `appendQCVisualFlag` 置 `ok=false`（`:100-127`）；`render.qc_vision` 缺省开（`:17-23`）；未配视觉模型静默跳过。
- **成片终检** `agentAssembleCheck`（`internal/manju/manju_agent.go:1661-1686`）：对 `<ep>_成片.mp4` 跑 `qc --file`，报告性质不阻断；烧录字幕时自动加 `--no-ocr`（否则 OCR 会把烧录字幕误报为文字渗漏，`:1667-1672`）。

---

## 7. 合成

**两条并行链路，需区分**：

- **漫剧主管线**用 `manju_media.py assemble`（PyAV 进程内逐帧编码，`internal/manju/manju_pipeline.go:7865` 组 args，`stageAssemble`），**不用 FFmpeg 命令行**。
- **旧渲染链路**用 `internal/assemble/assemble.go`（Go 直接 exec FFmpeg），仅 `internal/render/manager.go:126` 调用。

### 7.1 `internal/assemble/assemble.go`（FFmpeg 命令链）

`assembleWith(ctx, clips, output)`（`internal/assemble/assemble.go:53-101`）：

- 基础：`ffmpeg -y -hide_banner -loglevel error` + 每 clip 一个 `-i`（`:62-65`）。
- 视频参数：`-c:v libx264 -crf 18 -pix_fmt yuv420p`（`:67`）。
- 音频参数：`-c:a aac -b:a 128k`（`:68`）。
- faststart：`-movflags +faststart`（`:69`）。
- **单镜头**：`-af "aresample=48000,loudnorm=I=-16:TP=-1.5:LRA=11"`（`:73-75`）。
- **多镜头**：`-filter_complex` 构造 `[0:v][0:a][1:v][1:a]...concat=n=N:v=1:a=1[v][a];[a]aresample=48000,loudnorm=I=-16:TP=-1.5:LRA=11[aout]` + `-map [v] -map [aout]`（`:82-89`）。**即 filter concat（非 demuxer list）**。
- **loudnorm 精确参数：`I=-16 TP=-1.5 LRA=11`，前置 `aresample=48000`**（`:74`、`:87`）。
- 超时：`AssembleTimeout = 30 * time.Minute`（`:36-38`），`AssembleCtx` 包 `context.WithTimeout`（`:41-45`）；错误截断输出 300 字符（`:98`、`:103-108`）。
- **无 BGM、无对白闪避、无字幕烧录、无转场**——这些全在 Python 侧。

### 7.2 `manju_media.py cmd_assemble`（主管线实际使用）

`cmd_assemble(args)`（`manju_media.py:809-1078`）：

- **编码器**：PyAV `o.add_stream("libx264", rate=fps)`，`pix_fmt="yuv420p"`，`options={"crf":"18","preset":"medium"}`（`:929-932`）；音频 `aac @ 32000Hz 立体声 fltp 128kbps`（`:933-937`）；输出选项 `{"movflags": "+faststart"}`（`:929`）。**注意：无 loudnorm，改用自研峰值全局增益**（见下）。
- **镜头截断到分镜表时长**：`plan_frames[shot_id] = round(duration*fps)`，多切点长镜组头 = 组内求和（`:843-862`）；`dur = min(dur, plan_max/fps)`（`:957`），编码时 `if plan_max and cut_v >= plan_max: break`（`:985`），注释说明 H3 帧数按 `17k+5` 网格量化会多出 ≤0.7s 运动衰减尾巴（`:842`）。
- **音量归一化（替代 loudnorm）**：预扫各镜音频峰值 `peak`（`:896-914`）；`peak < 0.02 → gain=1.0`（静音不放大）；`peak < 0.25 → gain = min(4.0, 0.7/peak)`；`peak > 0.9 → gain = 0.85/peak`（压限防爆音）；否则 1.0（`:915-925`，打印 `🔊 音量归一化: 峰值 X → 增益 xY`）。
- **BGM 混音 + 对白闪避（ducking）**：`_BgmMixer(bgm, gain, duck, windows, ramp=0.3)`（`:757-807`，构造 `:761`）。BGM 预载为 32k 立体声 float32（`_load_bgm` `:738-754`）；`_env(t)` 在字幕窗口 `(start,end)` 内取 `duck` 系数，**窗口边界 ±0.3s 线性过渡**（`:781-796`）；混音时循环补齐 + 声道对齐 + `np.clip(out,-1,1)`（`:798-807`）。闪避窗口 = 字幕窗口（对白时段压 BGM，`:971-975`）。BGM 加载失败降级干声合成（`:881-885`）。
- **字幕烧录**：不烧传统字幕——用 PIL 在帧上 `_draw_subtitle` 绘制（`:659-708`），仅在 `subs` 时间窗内（`:1017-1022`）。字幕窗口分配 `_assign_subtitle_windows(cues, dur, film_sec)`——**镜头内 12%~92%**（`wa, wb = film_sec + 0.12*dur, film_sec + 0.92*dur`，`:1507`），按字数占比分配（`:1502-1515`，调用 `:962-966`）；多切点长镜组头承载组内全部台词（`_cues_for_shot` `:633-639`）。可选导出 `.srt`（`:967-968`，`_write_srt` `:1485-1501`）。`--no-subtitle` 时清空 cues（`:855-859`）。
- **打码**：`--mosaic N>1` 时逐帧 `_pixelate(frame, N)`（`:552-574`、`:1015-1016`）。
- **音画同步**：显式按输出时基单调递增 pts（`frame.pts=vpts; vpts+=1`，`:988-992`），跨镜头不重置，注释说明 PyAV 自动分配在时基换算后可能算出相同 dts 导致 mp4 报 EINVAL（`:987`）；音频 `fr.pts=None` 交编码器分配（`:1048`）。
- **跳过镜头**：`--skip-shots "3,5"` 按 `03.mp4` 补零匹配（`:820-834`）。

### 7.3 转场实现

Python 侧自研，**不用 `xfade` 滤镜**（`:869-877` 起）：

- 三种：`cut`（硬切，默认）/ `fade`（闪黑淡入淡出）/ `dissolve`（叠化），`trans_frames = max(1, round(trans_dur * fps)) if transition != "cut" else 0`（`:877`）。
- **硬切边界 = MotionContext 接缝镜起始处**：`--hard-cuts`（集合构造 `:871-876`）由 Go 从 manifest 提取 seam 标记传入（`manju_pipeline.go:7976-7987` 的 `seamHardCuts()`），注释解释“接缝镜头与上一镜画面本就连续，再叠化只会出现重影”（`:869-870`）。
- dissolve：缓存上一剪辑尾 T 帧 `prev_tail`，头部按 `alpha=(cut_v+1)/trans_frames` 与 `prev_tail[cut_v]` 线性混合（`:998-1003`、`:1069`）。
- fade：头部 `arr * alpha`（从黑淡入）；尾部 `* (k/(trans_frames+1))` 淡出（`:1004-1007`）。
- **音频近似 acrossfade**：转场边界按帧在剪辑内时间位置做 5%→100% 增益斜线（`f = min(f, 0.05 + 0.95*t/trans_sec)`，`:1027-1042`），注释明确“近似 acrossfade,防转场处爆音”（`:1029`）。
- 仅转场开启时才数帧（`_count_video_frames`，`:880-881`），关闭时零成本（`:878-879` 注释）。

### 7.4 剪映草稿导出

- **Go 端点** `manjuJianyingExport`（`internal/manju/manju_upscale.go:473-529`）：body `{config, episode}` → `manjuGuardConfig` → `newManjuCtx` → 校验 `hasTopLevelClips` → 输出目录 `<项目 workdir>/剪映草稿`，名称 `<ep>_NiliX` → 组 args `jianying --clips-dir <clips/<ep>> --out-dir <...> --name ... --fps <ctx.fps> --plan <plan路径>`，转场非 cut 时追加 `--transition` 与 `--hard-cuts`（`:499-506`）→ `runMediaOut`。若配置了 `render.jianying_dir`，成功后 `copyTree` 自动复制进剪映草稿目录（`:521-527`）。
- **Python 实现** `cmd_jianying`（`manju_media.py:1697-1789`）：依赖 `pyJianYingDraft`，未装则打印安装指引并 **exit 2**（优雅降级，`:1705-1710`）。
  - 画布尺寸取首镜：竖屏 → **1080×1920**，横屏 → **1920×1080**（`:1729-1731`）。
  - `DraftFolder(out_dir).create_draft(name, width, height, fps, allow_replace=True)`（`:1735-1736`），`os.makedirs(out_dir)` 两次（`:1733-1734`，冗余但无害）。
  - 轨道 API 新旧兼容：优先 `append_track(TrackSpec(...))`，`AttributeError` 回退 `add_track(tt, name)`（`:1738-1743`）。
  - 视频轨：每镜 `VideoMaterial(path)` + `VideoSegment(vm, trange(offset_us, dur_us), source_timerange=trange(0,dur_us), volume=1.0)`（`:1766-1767`）；镜头时长按**逐帧解码计数** `frames/fps` 计算（比元数据准，`:1762-1767`）。
  - 转场：`trans_map = {"fade": TransitionType.闪黑, "dissolve": TransitionType.叠化}`（`:1724`）；接缝镜与最后一镜不加（`:1770-1773`）。
  - 字幕轨：`TrackType.text`，仅当有字幕才建轨（`:1744-1746`）；`TextStyle(size=12.0 竖屏/8.0 横屏, color=白, align=1, bold=True, auto_wrapping=True, max_line_width=0.82 竖屏/0.6 横屏)`（`:1747-1750`）、`TextBorder(黑, width=30.0)`（`:1751`）、`TextShadow(黑, alpha=0.7, diffuse=8.0, distance=3.0, angle=-45.0)`（`:1752`）、`ClipSettings(transform_y=-0.75 竖屏/-0.8 横屏)`（`:1753`）；`TextSegment` **不烧录**、窗口分配与 assemble 同源（`:1775-1782`）。
  - 输出 `{"ok":true,"draft":<路径>,"width":..,"height":..}` JSON 末行（`:1789`），Go 侧 `parseJSONLine` 解析拿 `draft`（`manju_upscale.go:516-520`）。

### 7.5 预告片 / 配音

- `cmd_trailer`（`manju_media.py:1517-1555+`）同样 `+faststart`（`:1556`），字幕窗口 `_assign_subtitle_windows(cues, dur, film_sec)`（`:1502-1516`）。
- 旁白/画外音后期配音 `manju_voiceover`（`:1326-1424`）：edge-tts 合成，`amix=inputs=2:duration=first:dropout_transition=0` + `adelay`（FFmpeg 命令片段 `:1409`）；静音检测 `_audio_rms`（`:1445-1464`）避免 H3 原生画外音 + TTS 双声。

---

## 8. 清理

### 8.1 每日 00:00 自动清理（`internal/cleanup/cleanup.go`，80 行）

- **启动**：`cleanup.Start(cfg.Cleanup.DailyEnabled(), comfyIn, comfyOut, comfy.ComfyBusy)`（`main.go:631`）；`enabled=false` 时静默不注册（`cleanup.go:14-19`）。
- **触发方式**：`go func()` 内**计算到下一个 00:00 的时长并 sleep**，跨零点后复核（睡眠误差/休眠唤醒），用 `lastRun` 字符串（`2006-01-02`）保证**同一天只跑一次**（`cleanup.go:32-48`）。次日时间计算：`time.Date(y,m,d,0,0,0,0)`，若 `!next.After(now)` 则 +24h（`:35-39`）。
- **清理目标**：只清启动时显式传入的 **ComfyUI 共享 `input` 与 `output` 两个目录的内容**（`cleanup.go:22-28`、`53-58`）。README 表述一致（`README.md:40`、`:84`）。
- **保护措施**：
  1. **路径护栏**：`filepath.Clean` 后必须 `filepath.IsAbs` 且 `len(d) > 3`（挡住 `C:\` 根），否则跳过（`:23-27`）；`dirs` 为空直接 return（`:29-31`）。
  2. **忙时跳过**：`busy()`（`comfy.ComfyBusy`）为真则“ComfyUI 队列忙,跳过今日 input/output 清理”（`:49-52`），防止凌晨渲染批次被清。
  3. **只清内容不清目录本身**：`clearDir` 遍历子项 `os.RemoveAll`（`:65-79`）。
  4. **唯一保留项 `h3_context`**：`clearDir` 显式跳过名为 `h3_context` 的目录（`:72-74`），注释解释它是 MotionContext 跨镜/跨天续接的状态数据而非临时产物，误删会断接缝链（`:63-64`）。
  5. 成功清除数量 >0 才打日志（`:55-57`）。

### 8.2 手动清理端点（`internal/manju/manju_cleanup.go`）

**三个端点各自清理范围不同**：

**(a) `POST /api/manju/cleanup`** `manjuCleanupRun`（`:27-78`）—— 按 `targets` 数组精准清三类：
| target | 目录 | 证据 |
| --- | --- | --- |
| `gacha` | `<assets>/characters/_gacha` | `:62-63` |
| `frames` | `<analysis>/_frames` | `:64-65` |
| `2k` | `<clips>/<ep>/2k` | `:66-67` |

护栏：`manjuGuardConfig`（403）、**运行中禁止清理**（409，`manjuStateRunningFor`，`:47-51`）、未知 target 400。`removeTree` 递归删内容保留目录、透出失败数与释放字节（`:80-119`）。

**(b) `POST /api/manju/cache/clear`** `manjuCacheClear`（`:128-330`）—— 导航栏「扫帚」一键清缓存，默认清 5 类、`advanced=true` 再加 9 类：

默认（`:164-232`）：
1. `plan`：`analysis` 目录下方案类 JSON——`_direct_plan.json`/`_characters.json`/`_shots_prompts.json`/`_manifest.json`/`_render_ck.json`/`fingerprints.jsonl`（`removePlanJSONs` `:332-364`），保留 `_frames`/`_gacha` 子目录。
2. `clips`：整个 `<clipsDir>`（全部集镜头 mp4，`removeTreeEx` `:171-176`）。
3. `state`：`<项目>/run_state.json`、`agent_state.json`（非运行态下视为崩溃残留，否则页面一直弹“检测到已有任务”横幅，`:177-184`）。
4. `conditioning`：`<sharedModels>/conditioning/<项目>_*` 前缀（不误删其他项目，`:185-205`）。
5. `latent`：`<comfy_output>/h3_context/<项目>_*` 前缀子目录，同时扫 config 的 `comfy_output` 与运行时 `ctx.comfyOutput` 两处（审计 S6 命名空间，`:206-232`）。

高级 `advanced=true`（`:233-323`）另外：
6. `final`：`workdir` 下含 `_成片`/`_预告片` 且以 `.mp4` 结尾的文件（用户反馈“产物没同步清理”，`:237-264`）。
7. `comfy_input` / `comfy_output`：`paths.ComfySharedDir` 下 `input`/`output` 全清（`:265-271`）。
8. `characters`：项目内 `<assets>/characters` 全清（含主图/视图/角色板/Q版/`_gacha`/`adopted.json` 一并重置，`:272-278`）。
9. `scenes`：项目内 `<assets>/scenes`（场景图无代际，改卡后旧图残留导致趋同，`:279-285`）。
10. `runlog`：内存日志清空 + 遍历 `ManjuRootDir/<项目>/run.log` 全部 `os.Truncate(p,0)`（`:286-308`）。
11. `diagnose`：`manju/logs/diagnose/<项目>_diagnose.json`（`:309-322`）。

响应含 `failed` 计数与 `failedFiles`（最多 8 条），透出被占用文件（`:324-328`、`:112-116`）。

护栏（`:127` 注释明确）：只清上述缓存，**绝不触碰定妆照/场景图/镜头定稿/成片/2k**（默认路径）；高级清场则是有意放开（`:233-239` 注释“彻底清场用,慎用”，但模型权重 `../models` 绝不触碰 `:236`）。

**(c) `GET /api/manju/cleanup/sizes`**（`:372-398`）—— 只读统计，返回 `{gacha, frames, 2k}` 三类的 `{files, bytes}`（`dirSize` 用 `filepath.WalkDir` `:401-413`）。**GET 免 token，因此 config 也强制过 `manjuGuardConfig`**（审计 F4，`:378-383`）。

### 8.3 清理相关的其它保护

- 所有破坏性清理/删除入口都复用 `manjuStateRunningFor` 做“运行中拒绝”（`:48-51`、`155-158`），注释 S7：“与渲染并发撕扯产物/状态”。
- 清理只清固定子目录、以**前缀/后缀白名单**限定，避免误伤其他项目（conditioning/latent 前缀 `reNonWord.ReplaceAllString(project,"_")+"_"`，`:186`、`:210`）。
- `manjuCleanupRun` / `cache/clear` 的 config 均先过 `manjuGuardConfig`（`:42`、`:149`）。

---

## 9. 与 README / 注释不一致或值得注意的点

| # | 位置 | 问题 | 证据 |
| --- | --- | --- | --- |
| 1 | `README.md:83` | 写“verify/ 成片机械质检(含段尾冻结检测)”，但 `verify.go` 只有时长/流/faststart 四项，冻结检测在 `manju_media.py` | `internal/verify/verify.go:59-107` vs `manju_media.py:104-176` |
| 2 | `README.md:34` | 仍描述“定妆照 → 角色板（展示置顶）→ 多视图”，角色板已于 2026-08-27 用户裁决删除 | `internal/manju/manju_pipeline.go:5501-5504` |
| 3 | `README.md:71` | “50+ /api/manju/*”，实际 83 条（保守无碍） | `/tmp/routes.txt` 统计 |
| 4 | `README.md:59` | “Go 1.22+”，任务描述为 Go 1.25 | `README.md:59` |
| 5 | `internal/manju/fs.go:484-486` | 残留空注释块（`addFSRoot` 已拆包到 `fs_files.go`） | `fs.go:484-487`, `fs_files.go:77-89` |
| 6 | `internal/manju/manju_char_lib.go:459-469` | `manjuCharLibImportHandler` 同名角色合并分支 `m = card` 未写回数组，同名导入实际不生效（映射迭代变量为副本） | `manju_char_lib.go:457-469` |
| 7 | `internal/manju/manju_media.py:1745-1746` | 重复 `os.makedirs(args.out_dir)` 两行（无害冗余） | Python 文件 |
| 8 | `internal/manju/manju_llm.go:635` | `func scanNovelAssets(root string) manjuNovelAssets {	var out ...` —— 函数签名与大括号、语句挤在同一行（gofmt 应拆行，疑似格式化遗漏） | `manju_llm.go:635` |
| 9 | `web/kb/js/app.js:946` | 前端注释“harness 已删除”，但 `harness.js`、`/api/harness*`、灵动岛相关能力仍在 | `app.js:946`, `internal/api/harness.go` |
| 10 | `web/kb/index.html:4` / `web/island/index.html:6` | `?v=` 版本号手工维护，仅 2 个版本串、15 处；无自动哈希，README 要求“改前端必须 bump” | `README.md:114` |
| 11 | `internal/api/api.go:108-111` | `/splash/` 静态页未走 CSP 下发路径（低风险） | `api.go:108-111` vs `189-195` |
| 12 | `internal/manju/fs.go:582-590` | `/api/fs/select` 把用户所选任意目录永久加入白名单，无回收；`fsPathAllowed` 对复杂路径未设“绝对+长度”护栏（对比 `cleanup.go:24`） | `fs.go:467-508`, `582-590` |

---

## 10. 关键数字速查

| 项 | 值 | 来源 |
| --- | --- | --- |
| 显式路由总数 | 139（`/api/manju/*` 83） | 源码正则统计 |
| 主服务端口 | 127.0.0.1:8787（硬编码，不对外） | `main.go:657` |
| 控制端口 | 8799（主窗口）/ 8788（灵动岛）/ 8190（ComfyUI） | `main.go:1071-1207`, `comfy.go:242` |
| 会话 token | 16 随机字节 → 32 hex；仅非 GET 校验 | `main.go:650-654`, `api.go:231-241` |
| 请求体上限 | 64MB | `api.go:140` |
| 近黑帧 | 整帧灰度均值 < 20 | `manju_media.py:78` |
| 冻结判据 | 末尾 25% 窗口，降采样像素差均值 < 0.25 | `manju_media.py:98-103` |
| 段尾冻结触发截尾 | `freeze_ratio > 0.6`；`min_tail=0.5s`，`max_cut_ratio=0.4`，留 0.2s 余量 | `manju_media.py:409`, `121` |
| 静音阈值 | `audio_rms < 0.02`；峰值 `<0.06` 判丢台词 | `manju_media.py:418-421` |
| 音轨规格 | 32000Hz / 2ch | `manju_media.py:429-432` |
| 时长下限 | < 0.5s 判“时长过短” | `manju_media.py:445` |
| 字幕文字渗漏 | `cy>=0.78` 且 `0.3<=cx<=0.7` 且 框宽>0.02 | `manju_media.py:186-213` |
| ASR 幽灵人声 | 段 ≥0.5s 且 `avg_logprob > -1.0` 且 文本 ≥2 字 | `manju_media.py:218` |
| 幽灵人声声学兜底 | `silencedetect noise=-35dB:d=0.25`，语音段 0.15~2.5s（≥2 段） | `manju_media.py:52-57`, `116` |
| Go loudnorm | `I=-16 TP=-1.5 LRA=11`（前置 aresample=48000） | `assemble.go:74`, `87` |
| Python 合成编码 | libx264 crf18 preset medium + aac 32kHz 128k + faststart | `manju_media.py:870-879` |
| 音量归一化 | peak<0.25 → `min(4.0, 0.7/peak)`；peak>0.9 → `0.85/peak` | `manju_media.py:912-917` |
| BGM 闪避 | 字幕窗口内压 duck，边界 `ramp=0.3s` 线性 | `manju_media.py:757-796` |
| 字幕窗口 | 镜头内 12%~92%，按字数加权 | `manju_media.py:1502-1516` |
| 剪映画布 | 竖屏 1080×1920 / 横屏 1920×1080 | `manju_media.py:1738-1743` |
| 插帧/视图 denoise | full 0.93 / side 0.90 / detail 0.7 / Q 0.93 | `manju_pipeline.go:5498`, `:5546` |
| 形象指纹 | sha256(7 字段 `\x00` 连接) 前 12 字节 | `manju_char_lib.go:65-77` |
| 跨名形象指纹 | sha256(6 字段，不含 id) 前 12 字节 | `manju_char_lib.go:84-95` |
| YuNet 阈值 | 0.4（由 0.5 下调） | `manju_pipeline.go:3975` |
| 素材截断 | 单段 8000 rune，设定集总 20000 rune | `manju_llm.go:648`, `:711` |
| 视频/音频采样 | 视频每 6 帧、音频每 4 帧 | `manju_media.py:76`, `:84` |
| 每日清理时间 | 00:00（同一天只跑一次），保留 `h3_context` | `cleanup.go:36-48`, `72-74` |

---

*报告结束。所有路径均相对于 `/home/max/Projects/PythonProjects/video_magic/research/NiliX-main`。*
