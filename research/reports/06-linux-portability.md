# NiliX Linux 移植可行性评估

- **评估对象**：`/home/max/Projects/PythonProjects/video_magic/research/NiliX-main`（用户提供的 `NiliX-main.zip` 解压版，权威最新版）
- **目标平台**：Ubuntu 22.04 + RTX 5090 24GB（实测型号 `NVIDIA GeForce RTX 5090 D v2, 24455 MiB`）+ ComfyUI 0.36.0 监听 `0.0.0.0:8188` + Go 1.25.6
- **评估方式**：只读静态扫描 + **在 `/tmp` 副本上的真实交叉编译与运行验证**（源目录零修改，已校验 mtime 全部保持 `9月 6 18:34`）
- **规模**：非测试 Go 代码 **38,985 行**，203 个 `.go` 文件，19 个包（`internal/` 下 18 个 + 根 `main`）

---

## 0. 结论摘要（TL;DR）

| 判定项 | 结论 |
|---|---|
| A 类编译期硬阻断 | **24 个文件 / 66 处代码点**；无任何 `//go:build` 标签（全库 `grep "//go:build"` 仅命中 `third_party/`） |
| `internal/` 能否编译 | **17/18 个包可在 Linux 直接编译**（仅需隐藏窗口 stub + Windows 专有文件打标签）；**唯一真正无法编译的是装饰性的 `internal/island`** |
| headless 最小子集 | **成立且已验证**：产出 32.5 MB 原生 Linux ELF，`/`、`/api/stats`、`/api/settings`、`/island/`、`/manage/` 等全部 HTTP 200，且 nvidia-smi 路径**天然可用**（实测读到 5090 温度 29°C、显存 22.3/23.9 GB） |
| 核心业务价值 | ≈ **80% 可直接复用**（manju 29.5k 行 / comfy / render / agent / assemble / verify / storyboard / kb_work / config 全是纯 Go 或外部进程调用） |
| 桌面壳（wails） | wails v3 **有 Linux 实现**（`application_linux.go`），但**本机缺 `pkg-config` + GTK3 + webkit2gtk-4.1 dev**，且窗口几何/DPI 逻辑全是 win32 调用 → 属"可移植但要重写" |
| 灵动岛 | **无 Linux 实现**（`go-webview2` 的 `webview.go` 带 `//go:build windows`）→ 纯装饰，建议 headless 档直接砍掉 |
| 两档工作量 | **headless ≈ 30 文件 / 700–900 行**；**全功能对等 ≈ 42–48 文件 / 2,500–3,500 行** |

**一句话**：这是一个"逻辑层高度可移植、外壳层 100% Windows 绑定"的项目。**headless 落地是低风险且已被实证的**；全功能对等的主要成本在桌面壳重写与设备控制（雷神 EC/NVAPI）无对应物。

---

## 1. 验证方法与关键实证

### 1.1 原始构建失败（只读证据）

```console
$ GOOS=linux GOARCH=amd64 go build ./...
package nilix
	imports golang.org/x/sys/windows: build constraints exclude all Go files in .../x/sys@v0.46.0/windows
package nilix
	imports nilix/internal/autostart
	imports golang.org/x/sys/windows/registry: build constraints exclude all Go files in .../windows/registry
```

### 1.2 `/tmp` 副本上的递进式验证

| 阶段 | 在副本上做的事 | 结果 |
|---|---|---|
| ① 去掉 `rsrc_windows_amd64.syso` | Go 工具链按文件名后缀过滤，**该文件本身不是阻断点** | 无变化 |
| ② 把 32 处 `SysProcAttr{HideWindow:…}` 替换为 `SysProcAttr{}` | — | `util`/`verify`/`assemble`/`comfy`/`paths`/`render`/`config`/`kb_work`/`backend`/`storyboard`/`agent`/`cleanup` **全部转为 OK** |
| ③ 给 5 个 Windows 专有文件打标签 + 写 Linux stub | `elevate.go`/`hwagent.go`/`nvapi.go`/`lhmlifecycle.go`/`lhm.go` | `sysmon`、`api` 转为 OK |
| ④ 修 `manju/fs.go:517` 的 `*syscall.Win32FileAttributeData` | — | `manju`（29.5k 行）转为 OK |
| ⑤ 写约 140 行 headless `main.go` | 砍掉 wails/托盘/灵动岛/控制端口/看门狗 | **`go build` 成功，产出 32.5 MB Linux ELF** |
| ⑥ 实际运行该二进制 | `./NiliX -port 8791` | 见下表 |

### 1.3 headless 二进制实跑结果（决定性证据）

```console
$ file /tmp/nilix_headless
ELF 64-bit LSB executable, x86-64, ... dynamically linked

$ curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8791/
200                          # 62,247 字节 kb 工作台首页

$ curl -s http://127.0.0.1:8791/api/stats | python3 -m json.tool
cpu  : 32 核真实占用率        mem : 24.1/94.1 GB
gpu  : {"present":true,"usage":0,"temp":29,"memUsed":"22.3 GB",
        "memTotal":"23.9 GB","memPercent":93.28}     ← nvidia-smi 路径天然可用
disk : {"percent":0,"used":"","total":""}            ← 唯一失效项（C:\ 硬编码）
hw   : {"ok":false,...}                              ← EC/LHM 已降级，不崩

$ for u in /api/comfy /api/harness /api/island /api/render/jobs /api/outputs \
           /api/novel/status/all /manage/ /island/ /splash/; do ...; done
全部 200
```

> 这一结果把"可行性"从推测变成结论：**服务层可以在 Linux 原生跑起来，且 GPU 遥测走 `nvidia-smi` 自动生效。**

### 1.4 目标机工具链现状

```console
$ which ffmpeg ffprobe nvidia-smi git
/usr/bin/ffmpeg  /usr/bin/ffprobe  /usr/bin/nvidia-smi  /usr/bin/git     # 全在
$ which pkg-config; pkg-config --exists gtk+-3.0; pkg-config --exists webkit2gtk-4.1
未找到命令 / gtk3 MISSING / webkit2gtk-4.1 MISSING                        # 桌面壳的前置缺口
$ curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8188/system_stats   → 200
$ curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8190/system_stats   → 000
```

`verify.go` / `assemble.go` 的 `exec.LookPath("ffprobe"/"ffmpeg")` **先于**硬编码 Windows 兜底路径，所以 Linux 上走的是 `/usr/bin/*`，**硬编码路径实际不可达**（低危）。

---

## 2. A 类｜编译期硬阻断点全量清单

**统计口径**：`grep -rn "syscall\.NewLazyDLL|syscall\.UTF16PtrFromString|syscall\.SyscallN|syscall\.CloseHandle|syscall\.Handle(|Win32FileAttributeData|SysProcAttr{HideWindow|x/sys/windows|jchv/go-webview2|wailsapp/wails" --include="*.go" . | grep -v third_party | grep -v _test.go`

→ **24 个文件 / 66 处代码点**。全库**不存在任何 `//go:build` / `// +build` 约束**（除 `third_party/`）。

### 2.1 A-1：`SysProcAttr{HideWindow / CreationFlags}`（32 处 / 17 文件）

`syscall.SysProcAttr` 在 Linux 上存在，但**没有 `HideWindow` 字段**，编译报 `unknown field HideWindow`。这是数量最大的单类阻断点。

| 文件 | 处数 | 文件 | 处数 |
|---|---|---|---|
| `internal/comfy/comfy.go` | 5 | `internal/manju/manju_pipeline.go` | 2 |
| `internal/comfy/comfy_install.go` | 4 | `internal/api/harness.go` | 3 |
| `internal/comfy/comfy_versions.go` | 2 | `internal/api/zcode.go` | 3 |
| `main.go` | 3 | `internal/sysmon/format.go` | 1 |
| `internal/manju/manju.go` | 1 | `internal/manju/manju_agent.go` | 1 |
| `internal/manju/fs.go` | 1 | `internal/manju/manju_qcghost.go` | 1 |
| `internal/manju/novel_skill.go` | 1 | `internal/manju/skill_update.go` | 1 |
| `internal/verify/verify.go` | 1 | `internal/assemble/assemble.go` | 1 |
| `internal/util/util.go` | 1 | | |

**代表性代码**

```go
// internal/comfy/comfy.go:250
cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}

// internal/verify/verify.go:16-20  （assemble.go:14-18 / sysmon/format.go:9-13 同款）
// hideWindow windowsgui 父进程 spawn 子进程若不隐藏会弹黑窗(用户反馈"黑窗反复闪"),
// 所有 exec 统一加 HideWindow。非 Windows 平台忽略。
func hideWindow(cmd *exec.Cmd) *exec.Cmd {
	if cmd != nil && cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	}
	return cmd
}
```

**Linux 后果**：直接编译失败。
**替代方案**：新建一对文件注入，然后机械替换 32 处：

```go
// internal/proc/attr_windows.go
//go:build windows
package proc
func Hide(cmd *exec.Cmd) { cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true} }
// internal/proc/attr_unix.go
//go:build !windows
package proc
func Hide(cmd *exec.Cmd) {}   // Linux 无"黑窗"概念，no-op 即可
```

### 2.2 A-2：`syscall.NewLazyDLL` / `UTF16PtrFromString` / `SyscallN` / `CloseHandle` / `Handle`（25 处）

| 文件:行 | 原始代码 | 说明 |
|---|---|---|
| `internal/island/island.go:16,25,29` | `user32 = syscall.NewLazyDLL("user32.dll")` / `gdi32 = …` / `kernel32 = …` | 包级 `var`，无法靠删函数绕过 |
| `internal/sysmon/elevate.go:14,15,37` | `var advapi = syscall.NewLazyDLL("advapi32.dll")` … | UAC 提权 |
| `internal/sysmon/elevate.go:26` | `defer syscall.CloseHandle(syscall.Handle(token))` | — |
| `internal/sysmon/elevate.go:40-43` | `verb,_ := syscall.UTF16PtrFromString("runas")` ×4 | `ShellExecuteW` 提权重启 |
| `internal/sysmon/hwagent.go:44,46,61` | `k32 := syscall.NewLazyDLL("kernel32.dll")` / `UTF16PtrFromString("Global\\NiliX.hwagent")` / `CloseHandle` | 全局命名互斥体 |
| `internal/sysmon/nvapi.go:71,102` | `mod := syscall.NewLazyDLL("nvapi64.dll")` | NVAPI 超频 |
| `internal/sysmon/nvapi.go:83,92,120,167` | `syscall.SyscallN(initAddr)` 等 | — |
| `internal/sysmon/lhmlifecycle.go:50` | `k32 := syscall.NewLazyDLL("kernel32.dll")` | Job Object 回收 |
| `internal/watchdog/watchdog.go:21,23` | `windows.NewLazySystemDLL("kernel32.dll")` / `("user32.dll")` | 单实例 + MessageBox |
| `main.go:286,493,751` | `user32Lazy := syscall.NewLazyDLL("user32.dll")` 等**三处包级 var 块** | 即使删掉使用函数也必须处理 |
| `main.go:419,1544` | `syscall.UTF16PtrFromString(mainWinTitle)` | 窗口图标/关闭 |

**替代方案**：这些文件整体属"Windows 专有实现"，**最小改动是加 `//go:build windows` 并配 Linux stub 文件**，而不是逐行改写。详见 §5 与 §6。

### 2.3 A-3：`golang.org/x/sys/windows`（3 处 import / 2 个整包）

```go
// main.go:42
"golang.org/x/sys/windows"                      // 用于 windows.OpenProcess/WaitForSingleObject/CloseHandle (1589,1599,1600)

// internal/watchdog/watchdog.go:12-23  —— 整个包都是 Windows 实现
import "golang.org/x/sys/windows"
var (
	kernel32         = windows.NewLazySystemDLL("kernel32.dll")
	procCreateMutexW = kernel32.NewProc("CreateMutexW")
	user32           = windows.NewLazySystemDLL("user32.dll")
	procMessageBoxW  = user32.NewProc("MessageBoxW")
)

// internal/autostart/autostart.go:8   —— 整个包都是 Windows 实现
import "golang.org/x/sys/windows/registry"
const runKey = `Software\Microsoft\Windows\CurrentVersion\Run`
```

**Linux 后果**：`build constraints exclude all Go files`。
**替代方案**：`watchdog` → `flock(LOCK_EX|LOCK_NB)` 或 `O_EXCL` pidfile / Unix domain socket 单实例；`autostart` → 写 `~/.config/autostart/nilix.desktop`（或 `systemctl --user enable`）。

### 2.4 A-4：`*syscall.Win32FileAttributeData`（1 处）

```go
// internal/manju/fs.go:517
func fileCreateUnix(info os.FileInfo) int64 {
	if info == nil { return 0 }
	if st, ok := info.Sys().(*syscall.Win32FileAttributeData); ok {
		return st.CreationTime.Nanoseconds()/1e9 - 11644473600
	}
	return info.ModTime().Unix()
}
```

**Linux 后果**：`undefined: syscall.Win32FileAttributeData` → `internal/manju` 整个包编译失败（这也是唯一卡住 29.5k 行的点）。
**替代方案**：拆成 `fs_create_windows.go` / `fs_create_unix.go`。Linux 下 `*syscall.Stat_t` 无创建时间（btime 需 `statx`），最简可直接 `return info.ModTime().Unix()`。

### 2.5 A-5：wails v3 + go-webview2 导入（2 处 / 但影响最大）

```go
// main.go:39-42
"github.com/wailsapp/wails/v3/pkg/application"
"github.com/wailsapp/wails/v3/pkg/events"
webview "github.com/jchv/go-webview2"
"golang.org/x/sys/windows"

// internal/island/island.go:11
webview "github.com/jchv/go-webview2"
```

```go
// go.mod
replace github.com/jchv/go-webview2 => ./third_party/go-webview2
// third_party/go-webview2/webview.go:1-2
//go:build windows
// +build windows
```

**Linux 后果**
- `go-webview2`（含本地 replace 补丁）**整体 Windows-only** → `webview.NewWithOptions` / `WebViewOptions` / `WindowOptions` 全部 `undefined`。`internal/island` **完全无法编译**。
- wails v3 **不是**编译硬阻断：`pkg/application/` 存在 `application_linux.go`、`systemtray_linux.go`、`webview_window_linux.go` 等 38 个 Linux 文件。但 `application_linux.go:1` 的约束是：

```go
//go:build linux && cgo && !gtk3 && !android && !server
/*
#include <gtk/gtk.h>
#include <webkit/webkit.h>
```

  即 **需要 cgo + GTK3 + WebKit2GTK 头文件**；本机实测 `pkg-config` 未安装、`gtk+-3.0` 与 `webkit2gtk-4.1` 均缺失，构建报 `exec: "pkg-config": executable file not found in $PATH`。

> ⚠️ 勘误：wails v3 提供 `-tags server` 的纯 HTTP 无 GUI 模式（`application_server.go:1 //go:build server`），但它**不提供 `app.Window` / `app.SystemTray`**，无法承载本项目的窗口代码，因此**不能**用 `-tags server` 直接救活 `main.go`。

**替代方案**
- headless 档：**完全移除 wails 与 go-webview2 依赖**，用 `net/http` + `xdg-open` 打开浏览器（我已在 §1.3 实证可行）。
- 全功能档：`apt install pkg-config libgtk-3-dev libwebkit2gtk-4.1-dev libayatana-appindicator3-dev`，保留 wails 并重写窗口层。

### 2.6 非阻断项（澄清）

| 项 | 判定 |
|---|---|
| `rsrc_windows_amd64.syso` | **不是**阻断点。Go 按文件名 `_windows_amd64` 后缀过滤资源文件，`GOOS=linux` 时自动忽略 |
| `third_party/` 目录 | `replace` 指令只在被 import 时才需编译；移除 main/island 的 import 后不影响 Linux 构建 |
| `golang.org/x/text/encoding/simplifiedchinese`（GBK 解码） | **纯 Go，Linux 完全可用**，无需改动 |

---

## 3. B 类｜编译通过但运行期失效

### 3.1 Windows 命令行工具

| 文件:行 | 原始代码 | Linux 后果 | 替代方案 |
|---|---|---|---|
| `internal/comfy/comfy.go:110` | `cmd := exec.Command("netstat", "-ano")` | 命令不存在 → `findPortPID` 恒返回 0。**连锁**：ComfyUI 端口占用检测、`stopComfy` 兜底、`api/harness.go:117` 的 `FindPortPID("3080")` 全失效 | 读 `/proc/net/tcp` 匹配 listening inode → `/proc/*/fd`；或改用 `net.Listen` 探测 + 记自己的 PID |
| `internal/comfy/comfy.go:291,300` | `exec.Command("taskkill", "/PID", …)` | 停止 ComfyUI 失效，残留进程占显存 | `cmd.Process.Signal(syscall.SIGTERM)` → 超时后 `SIGKILL`；或 `syscall.Kill(-pgid, …)` 杀进程组 |
| `internal/comfy/comfy.go:321` | `powershell … Get-CimInstance Win32_Process … Stop-Process` | 孤儿 python worker 清扫失效 | 读 `/proc/*/cmdline` 匹配 `paths.ComfyRootDir` 后 kill |
| `internal/comfy/comfy_versions.go:224,268` | `SysProcAttr{HideWindow…}` + `git.exe` 调用 | 仅 HideWindow 编译问题；`git` 在 Linux 存在 | 同 A-1 |
| `internal/api/harness.go:121,131` | `taskkill` ×2 | 停止 DSH Harness 失效 | SIGTERM |
| `internal/api/zcode.go:41,52` | `taskkill` ×2 | ZCode 启停失效（ZCode 本身是 Windows 应用） | 该功能在 Linux 无意义 → 建议整体禁用/隐藏 UI |
| `internal/util/util.go:116` | `exec.Command("tasklist", "/FI", fmt.Sprintf("PID eq %d", pid), "/NH")` | `IsPidAlive` 恒 false | `syscall.Kill(pid, 0)`（`ESRCH` 即不存在）—— 3 行即可 |
| `internal/sysmon/format.go:12` + `ecwmi.go:139,195,238` | `hiddenCmd("powershell", …)` | EC/WMI 采样恒空，**优雅降级不崩**（已实测） | C 类，见 §4 |
| `internal/sysmon/gpu.go:51` | `powershell … SharedUsage` | `SharedGPUMem` 恒 0 | 无 Linux 对应，返回 0 即可 |
| `internal/manju/fs.go:285` | `powershell -STA … FolderBrowserDialog` | **`/api/fs/select` 选目录功能失效** | `zenity --file-selection --directory`（Ubuntu 需 `apt install zenity`），或改为前端输入框（前端已支持手输） |
| `main.go:1627` | `rundll32 url.dll,FileProtocolHandler` | **已有 `runtime.GOOS` 分支**，Linux 走 `xdg-open` ✅ | 无需改动 |
| `main.go:1651,1662` | `taskkill /IM ZCode.exe` / `/PID` | ZCode 启停失效 | 移除或改 `pkill` |
| `internal/comfy/comfy.go:111,250 …` | `CreationFlags: 0x08000000` (CREATE_NO_WINDOW) | A 类已覆盖 | — |

### 3.2 硬编码 Windows 路径 / 盘符（29 处，含前端）

| 文件:行 | 原始代码 | 后果 / 替代 |
|---|---|---|
| `internal/paths/paths.go:28-32` | `legacyManjuRoot = "C:\Mi\Ai\WorkBench\manju"`、`legacyNovelRoot`、`legacyComfyRoot = "C:\Users\Administrator\AppData\Local\Comfy-Desktop\ComfyUI-Installs\ComfyUI (1)\ComfyUI"`、`legacyComfyShared`、`legacyNovelSkill` | `pickPath()` 在"无显式配置 + 无自包含目录"时**回落到这些死路径**。**已实测复现**：`/api/comfy` 返回 `"root":"C:\Users\Administrator\AppData\Local\Comfy-Desktop\…"`。→ 把 legacy 常量改为 `""` 或改为 XDG 路径 |
| `main.go:561` | `kbRoot := flag.String("kb", `C:\Mi\Ai\WorkBench\zhishiku`, "知识库根目录")` | 默认知识库根 = 死路径 → 改为 `filepath.Join(exeDir(), "zhishiku")` |
| `internal/comfy/comfy.go:27` | `comfyDesktopLog = `C:\Mi\Ai\Comfy Desktop\logs\_comfyui_server.log`` | 日志兜底读不到，无害 |
| `internal/comfy/comfy.go:186,196` | `filepath.Join(paths.ComfyRootDir, ".venv", "Scripts", "python.exe")` | **关键**：Linux venv 是 `.venv/bin/python` → ComfyUI 启动/管线 python 全失效 |
| `internal/util/util.go:102-109` | `filepath.Join(".venv","Scripts","python.exe")`、`filepath.Join("python_embeded","python.exe")` | 同上；`python_embeded` 是 Windows portable 布局 |
| `internal/manju/paths.go:48` | `"venv": fileExists(filepath.Join(paths.ComfyRootDir, ".venv", "Scripts", "python.exe"))` | 就绪检测恒 false → UI 显示"python 未就绪" |
| `internal/manju/manju_shot_intermediate.go:125` | `const manjuFFmpegBinFallback = `C:\Users\Administrator\…\ffmpeg-9.0-full_build\bin\ffmpeg.exe`` | 兜底不可达，但 `LookPath` 先行 ✅ |
| `internal/manju/manju_shot_intermediate.go:129`；`manju_pipeline.go:7538` | `filepath.Join(paths.ComfyRootDir, ".venv", "Scripts", "ffmpeg.exe")` | 不可达；Linux 用系统 ffmpeg |
| `internal/verify/verify.go:24`；`assemble.go:24` | `defaultFFprobe/defaultFFmpeg = `C:\Users\Administrator\AppData\Local\Microsoft\WinGet\…`` | `LookPath` 先行 → 不可达 ✅（低危） |
| `internal/sysmon/sysmon.go:280,285` | `disk.Usage(`C:\`)` / `c.diskRate(`C:`)` | **已实测复现**：stats 里 `disk.percent=0, used="", total=""` → 改为 `runtime.GOOS` 分支取 `/` |
| `internal/sysmon/sysmon.go:422` | `p := `C:\Mi\Ai\DeepSeekHarness\node_modules\@deepseek-ai\dsh\package.json`` | DSH 版本号读不到（本机实际在 `~/.nvm/.../lib/node_modules/@deepseek-ai/dsh/`）→ 用 `exec.LookPath` / npm root -g |
| `internal/sysmon/sysmon.go:445` | `const botLocksDir = `C:\Users\Administrator\.zcode\v2\bots-runtime-locks`` | Bot PID 探测失效 → C 类 |
| `internal/sysmon/lhm.go:36` | `` `C:\Mi\Ai\WorkBench\sysmon-widget\lhmsensor\bin\` + lhmExe `` | 兜底路径，C 类 |
| `internal/sysmon/sysmon.go:442` | `const zcodeExe = "ZCode.exe"` | Windows 进程名，C 类 |
| `internal/manju/manju_llm.go:261-263` | 三条 `C:\Mi\Ai\WorkBench\zhishiku\创作管理\AI漫剧` 等 | 提示词模板查找兜底失效 |
| `internal/manju/novel_create.go:31` | `return `C:\Mi\Ai\WorkBench\novel`` | 小说默认根兜底 |
| `internal/api/harness.go:26,27` | `harnessNodeExe = `C:\Mi\Ai\nodejs\node.exe``；`harnessRoot = `C:\Mi\Ai\DeepSeekHarness`` | **DSH 启动/重启功能失效**；本机 node 在 nvm 下 → 改用 `exec.LookPath("node")` |
| `internal/api/zcode.go:21`；`main.go:1636` | `exe := `C:\Mi\Ai\ZCode\ZCode.exe`` | ZCode 启动失效（无 Linux 版） |
| `internal/comfy/comfy_install.go:59-60,260-261` | `7zr.exe` 下载（7-Zip） | Linux 有 `unzip`/`tar`；该安装器本身是 Windows 便携版 ComfyUI 方案，**Linux 应整体禁用** |
| `internal/manju/manju_pipeline.go:8415` | `"comfy_url": "http://127.0.0.1:8190"` | 生成的项目 config.json 模板硬编码端口 → 见 §8.1 |
| `internal/comfy/comfy_versions.go` | `git.exe` 语义 | 命令名硬编码为 `git`（Linux 同名）✅ |
| `web/kb/index.html:780,791` | `placeholder="C:\Mi\Ai\WorkBench\novel"` / `…manju` | 仅占位符文案，无害但误导 |
| `web/kb/js/dirview.js:1548-1549` | `root: NILIX_PATHS.novelRoot \|\| "C:\\Mi\\Ai\\WorkBench\\novel"` | **前端兜底死路径** → 后端注入 `NILIX_PATHS` 后不触发；若注入失败则目录视图空 |
| `web/kb/js/manju.js:5947` | `\|\| "C:/Mi/Ai/WorkBench/novel"` | 同上 |

### 3.3 注册表 / 单实例 / 自启

| 文件:行 | 代码 | 后果 | 替代 |
|---|---|---|---|
| `internal/autostart/autostart.go:20,38,49,63` | `registry.OpenKey(registry.CURRENT_USER, runKey, …)`，`runKey = Software\Microsoft\Windows\CurrentVersion\Run` | 整包无法编译（A 类） | `~/.config/autostart/nilix.desktop`（XDG，桌面环境原生支持） |
| `main.go:629` | `autostart.SelfHeal(exe)` | 启动时静默失败（已 return） | XDG 等价实现 |
| `internal/watchdog/watchdog.go:35` | `windows.UTF16PtrFromString("Local\\" + name)` + `CreateMutexW` | 整包无法编译 | `flock` / 抽象 socket |
| `internal/watchdog/watchdog.go:61-62` | `MessageBoxW`（`Alert`） | 无 Linux 实现 | `zenity --error` 或纯日志 |
| `main.go:581-599` | `guard, err := watchdog.SingleInstance("NiliX")` … 失败时 HTTP 唤起 `8799/open` | 依赖 A 类包 + `8799` 控制端口（headless 无） | 换成 flock；第二实例改为 HTTP 唤起 `8787` |

### 3.4 编码假设（GBK）

| 文件:行 | 代码 | 评估 |
|---|---|---|
| `internal/manju/manju.go:27` | `"golang.org/x/text/encoding/simplifiedchinese"` | **纯 Go，Linux 可用** ✅ |
| `internal/manju/manju.go:1621-1642` | `toUTF8`：UTF-8 合法则原样，否则 GBK/GB18030 解码 | **兼容性行为，不是假设** → Linux 下更有用（读旧 Windows 文件）✅ |
| `internal/comfy/comfy.go:251` | 注释：中文 Windows ANSI=GBK，`PYTHONIOENCODING=utf-8` 规避 emoji 崩溃 | Linux 默认 UTF-8，该 env 多余但**无害** ✅ |

**结论：不存在需要修复的编码阻塞点。**

---

## 4. C 类｜可选功能（缺失即降级）

| 功能 | 文件 | Windows 实现 | Linux 命运 | 建议 |
|---|---|---|---|---|
| **lhmsensor 温度** | `internal/sysmon/lhm.go:29` `lhmExe="lhmsensor.exe"`、`lhmlifecycle.go:50` Job Object | LibreHardwareMonitor .NET 子进程 | `.exe` 无法运行 → 无核心温度 | 用 `lm-sensors`（`sensors -j`）或 NVML 取 GPU；CPU 温度可选 |
| **雷神 EC 设备控制** | `internal/sysmon/ecwmi.go`（`.dll` 逆向的 WMI ACPI（`root\wmi`）字节协议）、`elevate.go`（UAC）、`hwagent.go`（提权助手） | WMI + UAC 提权 | **无对应硬件抽象**。Linux 无雷神控制中心，`root\wmi` 不存在 → 采样恒空、模式切换/快速制冷不可用 | 建议**整体降级为 stub**；性能模式可改 `nvidia-smi -lgc` / `powerprofilesctl`（仅 GPU 侧近似） |
| **NVAPI 超频** | `internal/sysmon/nvapi.go:71,102` `nvapi64.dll` | NVIDIA NVAPI DLL | Linux 无 `nvapi64.dll`（无公开 Linux NVAPI） | 用 `nvidia-settings -a GPUGraphicsClockOffset` 或 `nvidia-smi -lgc`（RTX 5090 支持锁频） |
| **托盘** | `main.go:1263-1367 buildTray` + `1368-1542` 状态球位图生成 | wails SystemTray + Win32 位图 | wails 有 `systemtray_linux.go`（DBus StatusNotifierItem），**但 GNOME 42 默认不显示托盘，需装 AppIndicator 扩展** | headless 档砍掉；全功能档保留但需提示装扩展 |
| **灵动岛** | `internal/island/island.go`（329 行）+ `web/island/`+`8788` 控制端口 | 置顶/圆角/透明 WebView2 胶囊 | **无法编译**；且 Wayland 下透明+置顶+无边框受限 | headless 档砍掉（`/island/` 静态页仍可 200，见 §1.3）；全功能档需 layer-shell（GTK4）重写 |
| **ZCode / Bot 控制** | `internal/api/zcode.go`、`main.go:1635-1666`、`internal/sysmon/sysmon.go:442,445` | Windows 桌面 app + taskkill + 锁目录 | 无 Linux 版 ZCode → 功能无意义 | UI 隐藏即可 |
| **DSH Harness 启停** | `internal/api/harness.go:26-27,92-137` | 硬编码 `C:\Mi\Ai\nodejs\node.exe` | 本机 node 在 nvm 下，路径不存在 → 启停失效（**监控/探测本身走 HTTP 3080，可用**） | 改 `exec.LookPath("node")` + `npm root -g` |
| **ComfyUI 一键安装** | `internal/comfy/comfy_install.go`（7zr.exe + 便携版布局） | Windows 便携 ComfyUI | Linux 应禁用（本机 ComfyUI 已存在） | 后端直接返回"不支持" |

---

## 5. 逐包可编译性判定

判定基于 §1.2 的实证构建（隐藏窗口 stub 后 + Windows 专有文件打标签后）。

| 包 | 非测试行数 | Linux 直接编译？ | 缺什么 / 需要做什么 | 业务重要性 |
|---|---|---|---|---|
| `internal/manju` | **29,520** | ❌→✅ | 唯一阻断：`fs.go:517` `*syscall.Win32FileAttributeData`（拆 build tag）。另有 7 处 `HideWindow`、`fs.go:285` powershell 选目录、`Scripts/python.exe` 路径 | 🔴 核心（六阶段流水线） |
| `internal/comfy` | 1,598 | ❌→✅ | 6 处 `HideWindow`（编译期）；运行期 `netstat`/`taskkill`/`powershell`/`Scripts\python.exe`/`7zr.exe` | 🔴 核心（ComfyUI 代管） |
| `internal/sysmon` | 1,678 | ❌→✅ | `elevate.go`/`hwagent.go`/`nvapi.go`/`lhmlifecycle.go`/`lhm.go` 需 `//go:build windows` + Linux stub；`format.go:12` HideWindow；`sysmon.go:280/285` 盘符。**`ecwmi.go` 可原样编译**（仅 PowerShell 调用，已实测） | 🟡 遥测（stub 后不崩） |
| `internal/api` | 1,028 | ❌→✅ | 6 处 `HideWindow`（`harness.go`/`zcode.go`）；仅因依赖 `sysmon` 而连带失败。**HTTP 路由表本身 100% 平台中立** | 🔴 核心（HTTP 层） |
| `internal/agent` | 789 | ✅ | 无（纯 HTTP LLM 客户端） | 🔴 核心 |
| `internal/render` | 461 | ❌→✅ | 仅因依赖 `assemble`/`verify` 的 HideWindow | 🔴 核心 |
| `internal/config` | 402 | ✅ | 无（AES-GCM 纯 Go） | 🔴 核心 |
| `internal/kb_work` | 340 | ✅ | 无（README 亦标注"纯标准库"） | 🔴 核心 |
| `internal/island` | 329 | ❌ **无法** | `syscall.NewLazyDLL` ×3 + `go-webview2`（Windows-only 模块）。无 Linux 实现 | ⚪ 纯装饰 |
| `internal/backend` | 277 | ✅ | 无 | 🔴 核心 |
| `internal/storyboard` | 158 | ❌→✅ | 仅因依赖 `backend` 链 | 🔴 核心 |
| `internal/verify` | 151 | ❌→✅ | 1 处 `HideWindow`；硬编码 ffprobe 路径（LookPath 先行，低危） | 🟡 质检 |
| `internal/util` | 147 | ❌→✅ | 1 处 `HideWindow`；`tasklist`（IsPidAlive）；python.exe 路径 | 🔴 核心 |
| `internal/paths` | 112 | ✅ | 无（仅 legacy 常量是 Windows 字符串，不影响编译） | 🔴 核心 |
| `internal/assemble` | 108 | ❌→✅ | 1 处 `HideWindow`；硬编码 ffmpeg 路径（低危） | 🔴 核心 |
| `internal/cleanup` | 80 | ✅ | 无（`len(d) <= 3` 守卫注释提 "C:\" 但 Linux 同样安全） | 🟢 维护 |
| `internal/autostart` | 69 | ❌ | 整包 `x/sys/windows/registry` → 需 Linux 重写 | 🟡 便利功能 |
| `internal/watchdog` | 65 | ❌ | 整包 `x/sys/windows` → 需 Linux 重写（flock） | 🟡 健壮性 |
| `main`（根） | 1,673 | ❌ | wails + go-webview2 + `windows` 导入；3 处包级 `NewLazyDLL` var 块；5 处 `UTF16PtrFromString`；3 处 `HideWindow` | 🔴 入口 |

**统计**：`internal/` 18 个包中 **11 个零/近零成本可编译**，**6 个只需隐藏窗口 stub**，**1 个（island）不可编译**；再加 `autostart`/`watchdog` 两个整包重写。

---

## 6. 桌面壳替代方案与 headless 最小子集

### 6.1 main.go 各模块能力盘点

| 模块 | 行范围 | 提供的能力 | 分类 |
|---|---|---|---|
| embed 静态资源 | `56-69`（`indexHTML`/`iconICO`/`kbFS`/`islandFS`/`splashFS`） | 内嵌 Web 工作台 | 🔴 **核心必需** |
| HTTP 主服务 | `656-680`（`api.NewServer` + `srv.ListenAndServe` 于 `:8787`） | 全部 API + 静态页 | 🔴 **核心必需** |
| 核心装配 | `601-655`（config/paths/comfy/manju/render/sysmon/kb_work + token + FS 白名单） | 业务装配 | 🔴 **核心必需** |
| `--hwagent` 子进程模式 | `549-556` | 提权硬件助手 | ⚪ Windows 专有 |
| `--mainwin` 子进程模式 | `540-548` + `256-478` | **死代码**：全库无任何 `exec` 调用 `--mainwin`（已被 wails 取代） | ⚪ 可整体删除 |
| `wails application.New` | `693-707` | 应用容器 + 图标 | 🟠 壳 |
| splash 窗口 | `712-713` + `824-889` | 启动动画 | ⚪ 纯装饰 |
| 主窗口（WebView2） | `713` + `890-966` | 承载 `http://127.0.0.1:8787` | 🟠 **可用浏览器替代** |
| 灵动岛胶囊窗口 | `716` + `967-1002` + `1220-1262` | 悬浮 HUD | ⚪ 纯装饰 |
| 控制端口 `8799` | `1049-1143` | 窗口 最小化/最大化/关闭/移动/缩放/唤起 | ⚪ 纯壳（frameless 需要） |
| 控制端口 `8788` | `1177-1207` | 胶囊展开/收起/退出 | ⚪ 纯装饰 |
| 托盘 | `1263-1367` + `1368-1542` | 状态灯 + 菜单（开工作台/Comfy 启停/自启开关/退出） | 🟠 便利 |
| `gApp.Run()` | `737-739` | 阻塞 + 消息泵 | 🟠 用 `signal.Notify` 替代 |
| `onExit` | `1553-1582` | graceful_exit 标记 + HTTP Shutdown + 杀 LHM/ComfyUI | 🔴 **核心必需**（去掉窗口部分） |
| `runWatchdog` + `restartNiliX` | `1583-1623` | 崩溃自愈 | 🟡 可选 |
| `openBrowser` | `1624-1634` | **已有 `runtime.GOOS` 分支** ✅ | 🔴 有用 |
| `setupLogFile`/`exeDir` | `1667-1673`/`1024-1031` | 日志与路径 | 🔴 **核心必需** |

### 6.2 headless 最小可运行子集（块级边界建议）

> 我在 `/tmp` 副本上按此边界写出约 140 行 `main.go`，**编译并运行成功**（§1.3）。

**保留（几乎原样）**

| 行范围 | 内容 | 备注 |
|---|---|---|
| `1-54` | import 块 | 删 `39-42`（wails/events/webview/windows），其余保留 |
| `56-69` | 5 个 `//go:embed` | 不动 |
| `518-538` | main() 开头的 Chdir + `--watchdog` 分派 | `530-533` 改为 flock 守卫 |
| `559-562` | `flag` 定义 | `561` 的 `kb` 默认值改为 `exeDir()/zhishiku` |
| `566-578` | 日志初始化 + 清 graceful_exit | 不动 |
| `601-625` | config/paths/comfy 参数注入 | 不动 |
| `631-639` | cleanup/manju settings/renderMgr | 不动 |
| `645-680` | kbStore/embed sub/token/FS白名单/`api.NewServer`/**HTTP 服务 goroutine** | 不动（`676-678` 的 `gApp.Quit()` 删掉） |
| `685-688` | `manju.AutoRecoverRendering()` 延迟恢复 | 不动 |
| `1024-1031` | `exeDir()` | 不动 |
| `1553-1582` | `onExit()` | 删 `1571`（`closeMainWindow()`）、`:1569-1573`（LHM/ShutdownHWAgent 可留为 no-op）；保留 graceful_exit 标记 + `httpServer.Shutdown` + `comfy.ComfyStop()` |
| `1624-1634` | `openBrowser()` | 不动（天然跨平台） |
| `1667-1673` | `setupLogFile()` | 不动 |

**砍掉**

| 行范围 | 内容 | 理由 |
|---|---|---|
| `256-517` | legacy `--mainwin` 全套 + `user32Lazy`/`gdi32Lazy`/`w32RECT` 等包级 var 与 win32 辅助 | 死代码 + A 类阻断源 |
| `479-516` | `gSysmon` 之外的 `procEnumWindows`/`findMainWindow` | 窗口枚举 |
| `690-741` | `application.New` → `gApp.Run()` | wails 全壳 |
| `743-1023` | `gApp`/`httpServer` 之外的全部 wails 窗口全局与 `createSplashWindow`/`createMainWindow`/`createCapsuleWindow`/`ensureMainWindow`/`openMainWindow`/`isMainWinMaximized`/`loadMainWinState`/`mainWinStateValid`/`animateCapsule` | 窗口管理 |
| `1033-1047` | `validLocalHost`（main 私有副本） | 仅控制端口用；`api` 有独立实现 |
| `1049-1262` | `startControlServers`（8799 + 8788） | 全部是窗口操作 |
| `1263-1367` | `buildTray` | 托盘 |
| `1368-1542` | `comfyState`/`comfyProbeState`/`dotIcon*` | 仅供托盘位图 |
| `1543-1552` | `closeMainWindow`（`FindWindowW`/`PostMessageW`） | win32 |
| `1583-1623` | `runWatchdog`/`restartNiliX`（`windows.OpenProcess`） | 改 flock + 可选 systemd 重启 |
| `1635-1666` | `startZCode`/`stopZCode`/`stopBot`（`ZCode.exe` + `taskkill`） | ZCode 是 Windows 应用；**注意 `internal/api/zcode.go` 有并行实现需要一并处理** |
| `419`、`1544` | `syscall.UTF16PtrFromString` 调用点 | 随宿主函数删除 |

**替换**

| 位置 | 原实现 | headless 替代 |
|---|---|---|
| `563-564` | `island.EnablePerMonitorDPI()` | 删除（DECORATIVE） |
| `581-599` | `watchdog.SingleInstance` + `8799/open` | `flock(LOCK_EX\|LOCK_NB)` on `logs/nilix.lock`；第二实例改为 `GET http://127.0.0.1:8787/` 唤起浏览器 |
| `629-630` | `autostart.SelfHeal(exe)` | 删除 |
| `637-641` | `sysmon.NewCollector()` + `gSysmon` | 保留（Linux stub 已足够，实测不崩） |
| `642-644` | `go sysmon.AutoEnsureHWAgent()` | 保留为 no-op（C 类） |
| `712-722` | splash/主窗口/胶囊/控制端口/托盘 | 全部删除；`712` 位置插入 `if *open { go openBrowser(url) }` |
| `726-735` | ComfyUI 未运行则拉起 | **建议删除**（Linux 上 ComfyUI 是常驻独立服务；见 §8.1） |
| `737-741` | `gApp.Run()` + `onExit()` | `signal.Notify(SIGINT,SIGTERM)` → `<-sig` → `onExit()` |

**落地形态建议**（把 Windows 外壳完整保留给 Windows 构建）：把现有 `main.go` 原样改名为 `main_windows.go` 并加 `//go:build windows`，新增约 200 行 `main_linux.go`（headless）。这样 Windows 版本**零回归**，两档可并行维护。

### 6.3 全功能对等的桌面壳替代路线

```bash
sudo apt install pkg-config libgtk-3-dev libwebkit2gtk-4.1-dev \
                 libayatana-appindicator3-dev zenity
```

| 能力 | Linux 方案 | 风险 |
|---|---|---|
| 主窗口 | 保留 wails v3，重写 `createMainWindow` 的窗口几何/DPI/最大化逻辑（`procIsZoomed`/`procGetSystemMetrics`/`procGetWindowRect` → wails 原生 API） | 🟡 中（wails v3 仍 beta） |
| frameless 拖拽 | `8799` 控制端口可保留（`net/http` 平台中立），但 `/move`、`/resize` 里的 `GetSystemMetrics` 需换成 `app.Screen` API | 🟡 |
| 托盘 | wails `systemtray_linux.go`（DBus SNI） | 🟠 **GNOME 需 AppIndicator 扩展**，否则图标不显示 |
| 灵动岛 | 需改用 GTK4 layer-shell（`gtk4-layer-shell`）独立小程序；**Wayland 下透明+置顶+click-through 无法用 `go-webview2` 实现** | 🔴 **高**（建议放弃或降级为独立窗口） |
| 自启 | `~/.config/autostart/nilix.desktop` | 🟢 低 |
| 单实例 | `flock` / 抽象 Unix socket | 🟢 低 |
| 设备控制（EC/NVAPI） | `nvidia-smi -lgc` / `nvidia-settings`；EC 无对应物 | 🔴 **高**（功能不可对等） |

---

## 7. 工作量估算

### 档位 1：让服务跑起来（headless）— **低风险，已实证**

| 工作项 | 文件数 | 预估行数 |
|---|---|---|
| `internal/proc` 隐藏窗口注入（2 新文件 + 32 处替换） | 19 | ~40 + 32 |
| `internal/sysmon`：5 文件打 `//go:build windows` + 新 Linux stub | 6 | ~110 |
| `internal/manju/fs.go` 创建时间拆 build tag | 3 | ~25 |
| `internal/watchdog` 拆分（Windows 原样 + Linux flock 实现） | 3 | ~60 |
| `internal/autostart` 拆分（Windows 原样 + Linux XDG .desktop） | 3 | ~70 |
| `internal/island` 打标签 + Linux no-op stub | 2 | ~30 |
| `main.go` 拆为 `main_windows.go`（原样）+ `main_linux.go`（headless） | 2 | ~200 新 + 移动 ~1,100 |
| `internal/comfy`：端口检测（`/proc/net/tcp`）、SIGTERM 杀进程、`.venv/bin/python` | 3 | ~90 |
| `internal/util`：`IsPidAlive` → `syscall.Kill(pid,0)`；python 路径 | 1 | ~20 |
| `internal/api`：`harness` node 路径 `LookPath`；`zcode` 禁用 | 2 | ~30 |
| `internal/sysmon/sysmon.go`：盘符 `C:\` → `/`；DSH 版本路径 | 1 | ~15 |
| `internal/paths`：legacy 常量清空 + XDG 默认 | 1 | ~20 |
| `internal/manju/fs.go`：`pickDir` → zenity | 1 | ~25 |
| 端口默认 8190 → 8188（config + sysmon 常量来源统一） | 3 | ~20 |
| 构建脚本：`Makefile` / `build.sh` 替代 `build.bat` | 1-2 | ~30 |

**合计**：≈ **30 个文件**（含 8 个新增），**≈ 700–900 行新增/改写**（另有 ~1,100 行 `main.go` 代码位移）。
**实测校准**：我在副本上用约 140 行 headless `main.go` + ~50 行 python 补丁脚本 + ~120 行 stub 即达成可运行构建 → 估算包含工程化（build tag 拆分、双平台 CI、文档）余量。

**风险点**
1. 🔴 **ComfyUI 生命周期语义必须变更**：原设计是"代管启停 + 杀进程 + 端口避让"。Linux 上 ComfyUI 是本机常驻服务（8188），**继续 `ComfyStop()` 会杀掉用户精心配置的实例**。必须改为"只探测、不启停"。
2. 🟠 **main.go 拆分后的 Windows 回归**：拆分处必然是双平台编译，建议 CI 同时构建 `GOOS=windows` 与 `GOOS=linux`。
3. 🟠 **sysmon 类型归属**：`ECHW`/`ECHWSample`/`ECModeName` 定义在 `ecwmi.go`。若给 `ecwmi.go` 打 Windows 标签，这些类型必须移入 `stub`（我实证过：留着 `ecwmi.go` 可编译，只需补 `SetMode`/`SetQuickCool` 之外的 stub）。建议**保留 `ecwmi.go` 不标签**，只标签 5 个真正含 win32 的文件。
4. 🟡 **manju 29.5k 行未经 Linux 运行验证**：静态编译通过 ≠ 业务正确；`manju_shot_intermediate.go`、`manju_qcghost.go` 里的 python/ffmpeg 调用参数需实测。
5. 🟡 **写请求鉴权**：`tokenInject` 依赖 `web/kb/index.html` 里的 `/*__NILIX_TOKEN__*/` 占位符——实测根页面 200，但需确认写操作可用（我未做写测试以避免副作用）。

### 档位 2：全功能对等 — **中高风险**

| 追加工作项 | 文件数 | 预估行数 |
|---|---|---|
| GTK/webkit 依赖安装 + 文档 | 1 | ~20 |
| wails 主窗口 linux 重写（几何/DPI/最大化/frameless 拖拽缩放） | 3-4 | ~400 |
| 控制端口 `8799`/`8788` 去 win32 化（`GetSystemMetrics` → wails Screen API） | 2 | ~150 |
| 托盘 linux 适配 + GNOME 扩展提示 | 1-2 | ~120 |
| 灵动岛 linux 重写（GTK4 layer-shell 独立程序，或降级为普通窗口） | 3-5 | ~600–1,200 |
| splash 窗口适配 | 1 | ~60 |
| `watchdog`/`autostart` 已在档位 1 完成 | — | — |
| 设备控制替代：`nvidia-smi -lgc`/`nvidia-settings` 超频 + `lm-sensors` 温度 + UI 改造 | 3-4 | ~300 |
| 前端文案/占位符去 Windows 化（`dirview.js`/`manju.js`/`index.html` 兜底路径） | 3 | ~50 |
| 安装/打包（`.desktop`、AppImage/deb、systemd user unit） | 2-3 | ~200 |

**档位 2 合计（在档位 1 之上）**：+ **12–18 个文件**，+ **1,800–2,600 行**
**两档总计**：≈ **42–48 个文件**，**≈ 2,500–3,500 行**

**风险点**
1. 🔴 **灵动岛在 Wayland 下无法对等**：透明 + 置顶 + 无边框 + 不抢焦点需要 `wlr-layer-shell`；GNOME 42 的 Mutter **不实现 layer-shell**。若目标机跑 X11 则可用 GTK override-redirect 近似。**建议明确降级**（如做成普通小窗或直接砍掉）。
2. 🔴 **雷神 EC / NVAPI 无对等物**：`ecwmi.go` 的字节协议是逆向雷神控制中心 DLL 得到的 Windows WMI ACPI 通道。Linux 上无此驱动接口 → "设备控制"面板只能保留 GPU 侧（`nvidia-smi -lgc`），**功能不可对等**，需产品决策。
3. 🟠 **wails v3 linux 成熟度**：版本为 `v3.0.0-beta.11`；`application_linux.go` 等 38 个文件存在，但 beta 期 API 变动与 GTK4/webkit2gtk-4.1 vs 4.0 差异需验证（Ubuntu 22.04 默认 `webkit2gtk-4.0`，需确认 wails 要求的是 `4.1`）。
4. 🟠 **打包与 GPU 依赖**：RTX 5090 需较新驱动；ComfyUI 的 `nvfp4`/`int8` 量化模型（`qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`）在 Linux + CUDA 12.x 的兼容性需单独验证——**这与 Go 移植无关，但决定端到端可用性**。
5. 🟡 **托盘在 GNOME 42 默认不可见**，需 `gnome-shell-extension-appindicator`；用户预期管理成本。

---

## 8. 其他重要差异

### 8.1 ComfyUI 端口：README 说 8190，本机在 8188

**端口配置点全清单**（`8190` 出现在 7 个非测试位置）：

| 文件:行 | 代码 | 作用 | `settings.json` 能否覆盖 |
|---|---|---|---|
| `internal/config/config.go:163` | `ComfyURL: "http://127.0.0.1:8190"` （`json:"comfy_url"`，见 `config.go:84`） | **默认值源头**，`config.Default()` 返回，`Load()` 以其打底 | ✅ **这是唯一正确的配置入口** |
| `internal/comfy/comfy.go:25` | `comfyURL = "http://127.0.0.1:8190"` | 包级兜底（在 `SetComfyParams` 注入前） | ⚠️ 间接 |
| `internal/comfy/comfy.go:169` | `return "8190"` | `currentPort()` 解析失败时的兜底 | ❌ 硬编码 |
| `internal/comfy/comfy.go:218` | `for try := 8190; try <= 8209; try++` | **端口避让扫描起点** | ❌ 硬编码 |
| `internal/comfy/comfy.go:218` 区域 | `startComfy()` 用 `--port <port>` 拉起 ComfyUI | 决定**由本项目启动时**用哪个端口 | ⚠️ 跟随 comfyParams |
| `internal/comfy/client.go:21` | `base = "http://127.0.0.1:8190"` | 客户端兜底 | ⚠️ |
| **`internal/sysmon/sysmon.go:333`** | **`const ComfyURL = "http://127.0.0.1:8190"`** | **`pollComfy()` 用它填 `/api/stats` 的 `comfy` 字段（灵动岛/HUD 状态灯）** | ❌ **完全独立于 settings.json** |
| `internal/manju/manju.go:2123` | `base := "http://127.0.0.1:8190"` | 停止任务时 `/interrupt` 的兜底（优先读项目 `config.json` 的 `comfy_url`） | ⚠️ |
| `internal/manju/manju_pipeline.go:8415` | `"comfy_url": "http://127.0.0.1:8190"` | **新建项目时写入 `config.json` 的模板默认值** | ❌ 硬编码 |
| `internal/manju/manju_agent_health.go:895` | 错误提示文案 `(默认 127.0.0.1:8190)` | 仅文案 | — |

**结论：存在两个互不相通的 ComfyUI 地址来源。**

1. `settings.json` 的 `render.comfy_url` → 经 `main.go:624 comfy.SetComfyParams(...)` 注入 → 驱动 `/api/comfy`、渲染提交流程、`ComfyOnline`、`ComfyBusy`。
2. `sysmon.ComfyURL` **常量** → 驱动 `/api/stats` 的 `comfy` 字段（灵动岛/"ComfyUI 状态灯"）。

**实测印证**：headless 运行后 `/api/comfy` 返回 `"url":"http://127.0.0.1:8190","err":"offline"`；`/api/stats` 的 `comfy` 也是 `{"online":false}`——而真实 ComfyUI 就在 `:8188` 返回 200。

**修改方式**

- **最小**（配置层，零改码）：在 NiliX 目录放 `settings.json`，写 `{"render":{"comfy_url":"http://127.0.0.1:8188"}}`。**但 HUD 状态灯仍会显示离线**（因为 `sysmon.ComfyURL` 是常量）。
- **建议**（代码层）：把 `sysmon.ComfyURL` 改为可注入变量，由 `main` 在 `comfy.SetComfyParams` 旁一并 `sysmon.SetComfyURL(cfg.Render.ComfyURL)`；并把 `comfy.go:169`/`:218`、`manju.go:2123`、`manju_pipeline.go:8415` 的硬编码改为跟随配置。
- **语义决策**：Linux 上 ComfyUI 常驻，应**删除 `startComfy` 的端口避让逻辑**（`comfy.go:206-239`）与 `--port` 代管，改为"配置即真值 + 只读探测"。

### 8.2 其他 Windows 特有资产

| 资产 | 文件 | Linux 处理 |
|---|---|---|
| `build.bat` | 调用 `powershell tools\build_refresh_version.ps1` 刷新前端 cache-buster，再 `go build -ldflags "-H windowsgui -s -w"` | `-H windowsgui` 在 Linux 无意义；需 `Makefile`/`build.sh` 复刻 cache-buster（可用 `date +%s`） |
| `启动服务.bat` | `start "" "NiliX.exe" -config settings.json -port 8787` | 改为 `./NiliX -config settings.json -port 8787` |
| `停止服务.bat` | `taskkill /F /IM NiliX.exe` | 改为 `pkill -f NiliX` 或用 systemd user unit |
| `rsrc_windows_amd64.syso` | Windows 资源（图标/清单） | 自动忽略；Linux 图标走 `.desktop` 的 `Icon=` |
| `icon.ico` | 被 `//go:embed`（`main.go:59-60`），`icoToPNG` 手写解码 | **纯 Go ICO 解码，实测 Linux 编译运行正常** ✅ |
| `tools/build_refresh_version.ps1` | PowerShell 构建脚本 | 需 bash 等价物 |
| `lhmsensor/` | Windows .exe 目录 | 不可用（C 类） |
| `internal/manju/scripts/*.py` | 通过 `go:embed` 运行时释放的 Python 脚本（抽帧/质检/ASR） | **平台中立 ✅**（依赖 ComfyUI venv 的 PyAV/whisper，需 Linux venv 装齐） |

### 8.3 编码假设总结

**无阻塞点。** 唯一编码相关代码是 `internal/manju/manju.go:1621-1642` 的 `toUTF8`（BOM → UTF-8 → GBK → GB18030 逐级降级），是**兼容性容错**而非假设，Linux 上无需改动（`x/text` 纯 Go）。`comfy.go:251` 的 `PYTHONIOENCODING=utf-8` 在 Linux 上是冗余但无害的加固。

---

## 9. 推荐实施顺序

| 步 | 动作 | 验收 |
|---|---|---|
| 1 | `internal/proc`（或各包内）隐藏窗口 build-tag 对 + 32 处替换 | `GOOS=linux go build ./internal/...` 中 11 个包转 OK |
| 2 | `sysmon` 5 文件加 `//go:build windows` + Linux stub（**保留 `ecwmi.go` 不打标签**） | `sysmon`/`api` 编译通过；`/api/stats` 返回 CPU/RAM/GPU 真值 |
| 3 | `manju/fs.go` 创建时间拆 build tag | `manju`（29.5k 行）编译通过 |
| 4 | `main.go` → `main_windows.go`（原样 + 标签）+ 新 `main_linux.go`（按 §6.2 边界） | **`nilix` 二进制构建 + 全端点 200**（本报告已实证） |
| 5 | `watchdog` / `autostart` 双平台拆分 | 单实例 + 自启在 Linux 生效 |
| 6 | ComfyUI 语义改造：停用代管启停、端口统一跟随 `settings.json`、`sysmon.ComfyURL` 可注入 | `/api/comfy` 与 `/api/stats.comfy` 在 8188 下同时 `online:true` |
| 7 | 平台化 B 类残余：`netstat`→`/proc`、`taskkill`→SIGTERM、`tasklist`→`kill(0)`、`Scripts/python.exe`→`bin/python`、盘符→`/`、`pickDir`→zenity | 端到端跑通一集渲染 |
| 8 | （档位 2）GTK 依赖 + wails 窗口层 + 托盘；灵动岛与 EC 单独决策 | 见 §7 风险 |

---

## 附录 A：A 类阻断点完整文件清单（24 个）

```
main.go
internal/api/harness.go          internal/island/island.go
internal/api/zcode.go            internal/manju/fs.go
internal/assemble/assemble.go    internal/manju/manju.go
internal/autostart/autostart.go  internal/manju/manju_agent.go
internal/comfy/comfy.go          internal/manju/manju_pipeline.go
internal/comfy/comfy_install.go  internal/manju/manju_qcghost.go
internal/comfy/comfy_versions.go internal/manju/novel_skill.go
internal/sysmon/elevate.go       internal/manju/skill_update.go
internal/sysmon/format.go        internal/util/util.go
internal/sysmon/hwagent.go       internal/verify/verify.go
internal/sysmon/lhmlifecycle.go  internal/watchdog/watchdog.go
internal/sysmon/nvapi.go
```

## 附录 B：扫描命令（可复现）

```bash
cd /home/max/Projects/PythonProjects/video_magic/research/NiliX-main

# A 类文件与站点计数
grep -rn "syscall\.NewLazyDLL\|syscall\.UTF16PtrFromString\|syscall\.SyscallN\|syscall\.CloseHandle\|syscall\.Handle(\|Win32FileAttributeData\|SysProcAttr{HideWindow\|x/sys/windows\|jchv/go-webview2\|wailsapp/wails" \
  --include="*.go" . | grep -v "^./third_party/" | grep -v _test.go | wc -l    # → 66
grep -rl … | wc -l                                                             # → 24

# 确认全库无 build tag
grep -rn "//go:build\|// +build" --include="*.go" . | grep -v "^./third_party/" # → 无输出

# B 类：Windows 命令
grep -rn "taskkill\|tasklist\|netstat\|powershell\|rundll32" --include="*.go" . | grep -v "^./third_party/"

# 端口
grep -rn "8190\|8188" --include="*.go" . | grep -v "^./third_party/" | grep -v _test

# 硬编码盘符
grep -rn 'C:\\Mi\|C:\\Users\|C:\\Windows\|C:/' --include="*.go" --include="*.html" --include="*.js" . | grep -v "^./third_party/"
```

> **只读声明**：本报告全部结论来自只读扫描与 `/tmp` 副本实验，源目录 `/home/max/Projects/PythonProjects/video_magic/research/NiliX-main` **未被修改**（所有文件 mtime 保持 `9月 6 18:34`）。
