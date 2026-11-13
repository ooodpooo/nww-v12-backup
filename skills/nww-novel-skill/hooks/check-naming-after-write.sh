#!/bin/bash
# check-naming-after-write.sh — PostToolUse(Write|Edit|MultiEdit) 命名兜底
# 正文落盘后自动跑命名轻检查（nww_name_check.py --mode chapter），把 WARNING 注入提醒——
# 模型无关的兜底层：新角色漏登记 / 改名 / bible 名字变体不一致被抓到。
#
# 定位（与 check-prose-after-write.sh 同款模式）：
#   - 只做 chapter 轻检查（非阻塞 advisory，exit 0），绝不阻断写作；
#   - build（建书强检查）/ commit（发布前完整检查）由 setup / 发布流程跑，本 hook 不重复；
#   - 无发现时完全静默（不污染 context）；python 不可用时静默放行（兜底不能反过来卡流程）。
#
# 覆盖范围：只在 PostToolUse 的 Write|Edit|MultiEdit 上触发，且仅当目标是「小说正文」。
set -euo pipefail

source "$(dirname "$0")/lib/common.sh"

# 中文路径上做 bash 通配/basename/case，强制 C 区域走字节匹配才稳定（issue #164 同类）。
export LC_ALL=C

HOOK_INPUT="${CLAUDE_TOOL_INPUT:-}"
if [ -z "$HOOK_INPUT" ] && [ ! -t 0 ]; then
  HOOK_INPUT="$(cat)"
fi
# 故意不 export：Write/Edit 负载带整章正文，export 会把负载塞进本脚本每个子进程的 envp，
# 负载一大 execve 就 E2BIG，python 全报「Argument list too long」。改为只在本脚本内用变量。

# 探测 python（macOS/Linux 通用，python3 优先，python 兜底；探测不到就静默放行）。
PY=""
command -v python3 >/dev/null 2>&1 && PY=python3
[ -z "$PY" ] && command -v python >/dev/null 2>&1 && PY=python
[ -z "$PY" ] && exit 0
CHECK="$(dirname "$0")/nww_name_check.py"
[ -f "$CHECK" ] || exit 0

# 抽取目标文件路径（HOOK_INPUT 是 Write/Edit 的 JSON 负载；兼容 file_path 顶层或 tool_input 内嵌）。
TARGET="$(printf '%s' "$HOOK_INPUT" | "$PY" -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not isinstance(d, dict):
    sys.exit(0)
fp = d.get("file_path")
if not fp:
    ti = d.get("tool_input")
    if isinstance(ti, dict):
        fp = ti.get("file_path")
if fp:
    print(fp)
' 2>/dev/null || true)"
[ -z "$TARGET" ] && exit 0

ROOT=$(project_root)
case "$TARGET" in
  /*) ABS="$TARGET" ;;
  [A-Za-z]:[/\\]*) ABS="${TARGET//\\//}" ;;
  *)  ABS="$ROOT/$TARGET" ;;
esac

BASE="$(basename "$ABS")"

# 只对「正文」文件兜底，绝不碰代码/细纲/设定/大纲等非正文文件（与 check-prose-after-write.sh 同口径）：
#   - 短篇：{书}/正文.md，且同目录有 设定.md
#   - 长篇：正文树内的 第N章*.md，且 {书} 有 大纲/追踪/设定（真书结构）
IS_PROSE=false
case "$BASE" in
  正文.md)
    [ -f "$(dirname "$ABS")/设定.md" ] && IS_PROSE=true
    ;;
  第*章*.md)
    case "$ABS" in
      */正文/*) ;;
      *) exit 0 ;;
    esac
    _vol_dir="$ABS"
    while [ "$(basename "$_vol_dir")" != "正文" ] && [ "$_vol_dir" != "/" ]; do
      _vol_dir="$(dirname "$_vol_dir")"
    done
    [ "$(basename "$_vol_dir")" = "正文" ] || exit 0
    BOOK="$(dirname "$_vol_dir")"
    unset _vol_dir
    if [ -d "$BOOK/大纲" ] || [ -d "$BOOK/追踪" ] || [ -d "$BOOK/设定" ] || [ -f "$BOOK/设定.md" ]; then
      IS_PROSE=true
    fi
    ;;
esac
[ "$IS_PROSE" = true ] || exit 0
[ -f "$ABS" ] || exit 0

# 运行 chapter 轻检查；输出为 "[WARNING] ..." 行。命中即注入提醒，无命中完全静默。
OUT="$(LC_ALL=C "$PY" "$CHECK" --mode chapter --book "$BOOK" --file "$ABS" 2>/dev/null || true)"
[ -n "$OUT" ] || exit 0

# 必须 %s 不能 %b：$OUT 里可能含作者原文切片，%b 会把 `\n`/`\b`/`\t` 当转义展开改写原文。
printf '%s\n' "=== 命名检查（${BASE}）===" "新角色/改名/名字一致性（WARNING，需人工确认；bible 登记更新走 /nww-setup 或角色状态）"
printf '%s\n' "$OUT"
exit 0
