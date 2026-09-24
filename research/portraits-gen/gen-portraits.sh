#!/usr/bin/env bash
# H3 角色定妆照批量生成 —— 复刻用户原有流程（gen-chars.sh + PIPELINE-DESIGN.md §阶段2）
#
# 原理：H3 生成 5 秒 1:1 定妆视频（正面朝镜头），抽第 24 帧作为 R2V 参考图。
# 本机唯一可行的资产生成通路（NiliX 自身的 assets 阶段走 Krea-2，本机未装）。
#
# 用法：
#   ./gen-portraits.sh                 # 生成 prompts/ 下全部角色
#   ./gen-portraits.sh 孙悟空           # 只生成指定角色（验证用）
#
# 血的教训（2026-09-23）：本脚本初版用了 `SECONDS` 作变量名，而 SECONDS 是 bash 内置
# 特殊变量（记录 shell 已运行秒数），赋值后持续自增 → `--seconds 5` 变成 `--seconds 574`
# → 提交 13766 帧巨型任务把 ComfyUI worker 卡死。此后一律：
#   ① 变量名不得用 bash 保留名；② 批量前先跑参数断言。
set -uo pipefail

COMFY_PROJ=/home/max/comfyui
PROMPT_DIR="$(cd "$(dirname "$0")" && pwd)/prompts"
OUT_DIR="$(cd "$(dirname "$0")" && pwd)"
RAW_DIR="$OUT_DIR/_raw"
LOG="$OUT_DIR/gen.log"

MODEL=pruned      # 21GB；完整版 34GB 在 24GB 卡上冷启动 staging >60s，
                  # 会撞 h3-test-run.py 提交阶段硬编码的 timeout=60（实测 4/5 失败）
STEPS=8
DUR=5             # ★ 不要改名成 SECONDS（bash 保留变量，见文件头教训）
FRAME=24          # 抽第 24 帧（≈1s）

mkdir -p "$OUT_DIR" "$RAW_DIR"
: > "$LOG"
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

# ── 参数断言（防变量污染重演）────────────────────────────────────────────────
[[ "$DUR" =~ ^[0-9]+$ && "$DUR" -ge 3 && "$DUR" -le 15 ]] || { log "❌ DUR=$DUR 非法"; exit 2; }
[[ "$MODEL" == "pruned" || "$MODEL" == "full" ]]         || { log "❌ MODEL=$MODEL 非法"; exit 2; }

# ── ComfyUI 健康前置检查（避免把一个卡死的服务当正常用）─────────────────────
if ! curl -sS -m 8 -o /dev/null http://127.0.0.1:8188/system_stats; then
  log "❌ ComfyUI(8188) 无响应，拒绝提交任务。先重启：bash ~/comfyui/start-comfyui.sh"; exit 3
fi

# ── 角色清单 ────────────────────────────────────────────────────────────────
if [ $# -gt 0 ]; then
  NAMES=("$@")
else
  NAMES=()
  for f in "$PROMPT_DIR"/char_*.txt; do
    [ -e "$f" ] || continue
    b=$(basename "$f" .txt); NAMES+=("${b#char_}")
  done
fi
[ ${#NAMES[@]} -gt 0 ] || { log "❌ $PROMPT_DIR 下无 char_*.txt"; exit 2; }

log "===== 定妆生成：${#NAMES[@]} 个角色 | model=$MODEL steps=$STEPS ${DUR}s 抽帧=$FRAME ====="
log "参数断言通过：--seconds $DUR --model $MODEL"

ok=0; fail=0
for name in "${NAMES[@]}"; do
  pf="$PROMPT_DIR/char_${name}.txt"
  [ -f "$pf" ] || { log "❌ 缺提示词: $pf"; fail=$((fail+1)); continue; }

  seed=$(printf '%d' $(( 2000 + $(echo -n "$name" | cksum | cut -d' ' -f1) % 8000 )))
  prefix="PORTRAIT_${name}"
  log "▶ $name (seed=$seed) 生成中…"

  if ! (cd "$COMFY_PROJ" && timeout 900 python3 h3-test-run.py \
        --model "$MODEL" --steps "$STEPS" --size 1x1 --seconds "$DUR" \
        --prompt "$(cat "$pf")" --seed "$seed" --prefix "$prefix") >>"$LOG" 2>&1; then
    log "❌ $name 生成失败（见日志尾部）"
    tail -5 "$LOG" | sed 's/^/      /' >>"$LOG"
    fail=$((fail+1)); continue
  fi

  src="/home/max/ComfyUI/output/${prefix}_00001-audio.mp4"
  [ -f "$src" ] || { log "❌ $name 产物未找到: $src"; fail=$((fail+1)); continue; }

  if ffmpeg -v error -y -i "$src" -vf "select=eq(n\\,${FRAME})" -vframes 1 \
       "$OUT_DIR/char_${name}.png" 2>>"$LOG"; then
    sz=$(ffprobe -v error -select_streams v -show_entries stream=width,height -of csv=p=0 \
         "$OUT_DIR/char_${name}.png" 2>/dev/null)
    log "✅ $name → char_${name}.png ($sz)"
    ok=$((ok+1))
  else
    log "❌ $name 抽帧失败"; fail=$((fail+1))
  fi

  # ★ 绝不把产物移出 ComfyUI output 目录（2026-09-23 事故）：
  #   ComfyUI 的生成历史面板引用的是 output/ 下的文件，移走 → 界面上"看不到图"。
  #   需要留档就复制，不要移动。
done

log "===== 完成：成功 $ok / 失败 $fail ====="
ls -la "$OUT_DIR"/char_*.png 2>/dev/null | tee -a "$LOG"
[ "$fail" -eq 0 ]
