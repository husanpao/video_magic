#!/usr/bin/env bash
# video_magic 控制台（web 服务）的启停脚本。
#
#   ./serve.sh status                 看它在不在跑（默认动作）
#   ./serve.sh start [项目名]         启动（已在跑就只报告，不双开）
#   ./serve.sh stop                   停止
#   ./serve.sh restart [项目名]       重启
#   ./serve.sh logs [-f] [N]          看日志（默认尾 40 行，-f 跟随）
#
# 这个服务是**普通后台进程，不是 systemd 单元**：机器重启/会话被回收后它不会自己回来，
# 得再 `./serve.sh start`。（今天就是这样：手动起的两个服务隔了一晚都不在了，
# 而 `status` 一眼能看出来 —— 这就是它当默认动作的原因。）
#
# 环境变量：VM_PORT=8801  VM_LOG=$REPO/vm-web.log  VM_PYTHON=python3  VM_HOST=0.0.0.0
#           VM_READY_TIMEOUT=40  VM_STOP_TIMEOUT=15
#
# 注意：`start` 起的总是**真实 projects/** 上的控制台。夹具服务器
#   （`python3 -m vm.tests.e2e_fixture --serve --port 8899`）本脚本认它（status/stop 管得了），
#   但不会替你把它"重建"起来 —— 那要先跑 --build，是另一件事。
#
# ★ 为什么这些检查要写进脚本而不是"记得手动做"（`vm/CONTRACTS.md` 第 1/6/7 条）：
#   · **绝不用 pgrep -f / pkill -f** —— 按命令行匹配会连自己的命令行一起匹配上，
#     本仓库的历史里自杀过 3 次。只走"端口 → pid → 核 /proc/<pid>/cmdline"这一条路。
#   · **先锁定目标再取信号** —— cmdline 必须同时含 pipeline.py / --serve / 本端口，
#     否则宁可拒绝动手，让人来决定。
#   · **绝不碰 ComfyUI** —— 8188 是本脚本的硬禁区；而且 ComfyUI 的命令行是
#     `main.py --listen … --port 8188`，同样含 "main.py"，所以匹配写得比"看着像"更严。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${VM_PORT:-8801}"

# 监听地址提示：VM_HOST=0.0.0.0（默认）时同时报出局域网地址，
# 因为"手机怎么打开"是实际使用中最常问的一句；绑 127.0.0.1 就只报本机。
_addr_report() {
  local port="$1" host="${VM_HOST:-0.0.0.0}" lan=""
  echo "  地址  http://127.0.0.1:$port/"
  if [ "$host" = "0.0.0.0" ]; then
    lan="$(python3 - <<'EOF' 2>/dev/null || true
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("8.8.8.8", 80)); print(s.getsockname()[0])
except OSError:
    pass
finally:
    s.close()
EOF
)"
    [ -n "$lan" ] && echo "         http://$lan:$port/   （局域网/手机）"
  fi
}
LOG="${VM_LOG:-$REPO/vm-web.log}"
PY="${VM_PYTHON:-python3}"
READY_TIMEOUT="${VM_READY_TIMEOUT:-40}"
STOP_TIMEOUT="${VM_STOP_TIMEOUT:-15}"
COMFY_PORT=8188

die() { echo "✗ $*" >&2; exit 1; }

listener_pid() {
  # -H 去表头；只看 LISTEN。拿不到 pid（权限/别的工具集）就交回空串，由调用方拒绝动手。
  ss -ltnpH "sport = :$PORT" 2>/dev/null \
    | grep -oP 'pid=\K[0-9]+' | sort -u | head -1 || true
}

cmdline_of() { tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null || true; }

# 是不是"我们这个"服务进程：pipeline.py + --serve + 本端口，且不是自己/父进程。
# 夹具服务器（`python3 -m vm.tests.e2e_fixture --serve --port N`）也算 —— 它同样是本仓库
# 起的 web 服务（跑的是 vm/web.py 同一套 handler），今天手动重启过两次，没必要另写一个脚本。
is_ours() {
  local pid="$1" cmd
  [ -n "$pid" ] || return 1
  [ "$pid" = "$$" ] && return 1
  [ "$pid" = "$PPID" ] && return 1
  cmd="$(cmdline_of "$pid")"
  case "$cmd" in
    *--serve*)
      case "$cmd" in *"--port $PORT"*) ;; *) return 1;; esac
      case "$cmd" in
        *pipeline.py*|*vm.tests.e2e_fixture*) return 0;;
        *) return 1;;
      esac;;
    *) return 1;;
  esac
}

looks_like_comfyui() { case "$(cmdline_of "${1:-}")" in *main.py*--listen*) return 0;; *) return 1;; esac; }

http_status() {
  # curl 失败时自己就会打一个 000，再 `|| echo 000` 就成了 "000000"。
  # 用 -f 之外的方式判断：统一只取前 3 位。
  local s
  s="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT$1" 2>/dev/null || true)"
  echo "${s:0:3}"
}

pid_age() { ps -o etime= -p "$1" 2>/dev/null | tr -d ' ' || true; }

require_port_guard() {
  [ "$PORT" = "$COMFY_PORT" ] && die "$PORT 是 ComfyUI 的端口，本脚本绝不启停它（契约第 1 条）。要管它请用你自己的 ComfyUI 启动方式。"
  return 0
}

wait_ready() {
  local i
  for ((i = 0; i < READY_TIMEOUT; i += 2)); do
    [ "$(http_status /api/projects)" = "200" ] && return 0
    sleep 2
  done
  return 1
}

do_start() {
  local proj="${1:-}" pid cmd
  require_port_guard
  pid="$(listener_pid)"
  if [ -n "$pid" ]; then
    cmd="$(cmdline_of "$pid")"
    if is_ours "$pid"; then
      echo "已经在跑，不双开：pid=$pid 已运行 $(pid_age "$pid")  端口=$PORT"
      echo "  要换配置就 ./serve.sh restart${proj:+ $proj}"
      return 0
    fi
    if looks_like_comfyui "$pid"; then
      die "端口 $PORT 上是 **ComfyUI**（pid=$pid），不是控制台。拒绝动手。"
    fi
    die "端口 $PORT 已被别的进程占着（pid=$pid cmd=[$cmd]）。先腾开，或 VM_PORT=别的端口 ./serve.sh start"
  fi

  cd "$REPO"
  local -a argv=("$PY" -u pipeline.py)
  [ -n "$proj" ] && argv+=("$proj")
  argv+=(--serve --port "$PORT" --host "${VM_HOST:-0.0.0.0}")
  # setsid：脱离本脚本的会话，脚本退出后服务不被连带带走；nohup + 追加日志：可查。
  setsid nohup "${argv[@]}" >> "$LOG" 2>&1 &

  if ! wait_ready; then
    echo "✗ 起了 ${READY_TIMEOUT}s 还没就绪。日志尾部：" >&2
    tail -n 25 "$LOG" >&2 || true
    pid="$(listener_pid)"
    [ -n "$pid" ] && echo "  进程还在（pid=$pid），可能只是慢；确认后可 ./serve.sh stop" >&2
    exit 1
  fi
  pid="$(listener_pid)"
  echo "✓ 已启动：pid=${pid:-?}  端口=$PORT  默认项目=${proj:-（由服务自选）}"
  _addr_report "$PORT"

  echo "  日志  $LOG"
}

do_stop() {
  local force="${1:-}" pid cmd i
  require_port_guard
  pid="$(listener_pid)"
  if [ -z "$pid" ]; then echo "没在跑（端口 $PORT 无监听），无需停止"; return 0; fi
  cmd="$(cmdline_of "$pid")"
  if ! is_ours "$pid"; then
    if looks_like_comfyui "$pid"; then
      die "端口 $PORT 上是 **ComfyUI**（pid=$pid）。契约第 1 条：绝不启停它 —— 拒绝执行。"
    fi
    die "端口 $PORT 的进程不是本控制台（pid=$pid cmd=[$cmd]）。拒绝杀不认识的进程，请人工确认。"
  fi
  echo "停止 pid=$pid（已运行 $(pid_age "$pid")）：$cmd"
  kill -TERM "$pid"
  for ((i = 0; i < STOP_TIMEOUT; i++)); do
    kill -0 "$pid" 2>/dev/null || { echo "✓ 已退出（${i}s）"; return 0; }
    sleep 1
  done
  if [ "$force" = "--force" ]; then
    echo "⚠ ${STOP_TIMEOUT}s 没退，按 --force 发 KILL" >&2
    kill -KILL "$pid" 2>/dev/null || true
    sleep 1
    kill -0 "$pid" 2>/dev/null && die "连 KILL 都没能停掉 pid=$pid，请人工处理" || echo "✓ 已强制停止"
    return 0
  fi
  die "${STOP_TIMEOUT}s 还没退出，**没有**动用 KILL（怕它正写到一半）。人工看看：./serve.sh logs"
}

do_status() {
  local pid cmd up def
  pid="$(listener_pid)"
  printf "端口 %-6s " "$PORT"
  if [ -z "$pid" ]; then echo "● 未运行"; else
    cmd="$(cmdline_of "$pid")"
    if is_ours "$pid"; then echo "● 运行中  pid=$pid  已运行 $(pid_age "$pid")"
    elif looks_like_comfyui "$pid"; then echo "● 被 ComfyUI 占用  pid=$pid  ← 本脚本不会动它"
    else echo "● 被别的进程占用  pid=$pid  cmd=[$cmd]"; fi
    [ -n "$cmd" ] && echo "  命令行 $cmd"
  fi
  up="$(http_status /api/projects)"
  echo "  HTTP /api/projects → $up"
  if [ "$up" = "200" ]; then
    _addr_report "$PORT"
    # 默认选中哪个项目：由启动时的 project 参数决定，没传则由服务自选
    def="$(curl -s --max-time 5 "http://127.0.0.1:$PORT/api/projects" \
      | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("default") or "（未设）")' 2>/dev/null || true)"
    echo "  默认项目 ${def:-（取不到）}"
  fi
  echo "  日志 $LOG（$(wc -l < "$LOG" 2>/dev/null || echo 0) 行）"
}

do_logs() {
  [ -f "$LOG" ] || die "还没有日志文件：$LOG"
  case "${1:-}" in
    -f) tail -f "$LOG";;
    '') tail -n 40 "$LOG";;
    *)  tail -n "$1" "$LOG";;
  esac
}

case "${1:-status}" in
  start)   shift; do_start "${1:-}";;
  stop)    shift; do_stop "${1:-}";;
  restart) shift; do_stop; do_start "${1:-}";;
  status)  do_status;;
  logs)    shift; do_logs "${1:-}";;
  -h|--help|help) sed -n '2,16p' "$0";;
  *) die "未知动作：$1（可用 start|stop|restart|status|logs）";;
esac
