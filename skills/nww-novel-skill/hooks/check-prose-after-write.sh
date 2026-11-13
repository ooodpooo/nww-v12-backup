#!/bin/bash
# check-prose-after-write.sh — PostToolUse(Write|Edit|MultiEdit) 正文兜底
# 正文落盘后自动跑「轻量确定性网」，把发现注入提醒——模型无关的兜底层：
# 即使主会话漏跑「确定性收尾」步骤（压缩/弱模型/分心），这些硬信号也保证被抓。
#
# 只兜「硬信号」（漏跑最伤、退化模型自己发现不了的）：截断、生成拒绝语 / AI 自指、
# 工程词漏进正文、紧邻整行复读、毒句式（确定性 AI 句式指纹）、落盘失败/截断。
# 碎句号/长段落/破折号这类 advisory，以及复读全量 / tier2 歧义词，仍由 workflow 收尾
# 步骤的 check-ai-patterns / check-degeneration 全量跑——本 hook 不部署也不依赖那两个
# 检测器，是独立的轻量网（毒句式规则与 check-ai-patterns.js 的同名规则统一规格）。
#
# 覆盖范围：只在 PostToolUse 的 Write|Edit|MultiEdit 上触发。cat>/tee/cp/mv 等用 Bash
# 写正文的路径绕过本 hook（Claude/OpenCode 侧 Bash 只做 pre-guard，无 post-write 兜底）；
# 这类路径由 Codex 的 Stop 回合末 git 改动集扫描兜全。已知边界，非缺陷。
#
# 内容网走 node 共享核 nww_hook_core.js（和 OpenCode/ZCode 同一份），只留 bash
# 做事件路由与文件类型判定。node 天生按 UTF-8 写 stdout，免掉旧内嵌 python 的 cp936 体操。
#
# 硬信号 exit 2；仅风格建议 exit 0。无发现时完全静默（不污染 context）。
# 硬信号：paragraph_gate rc=2、截断、工程词、毒句式、落盘过短。Advisory（段落 WARN 等）不挡。
# 测试回退：NWW_HOOK_ADVISORY_ONLY=1 时硬信号也 exit 0（生产禁止依赖；policy 扫描报警）。
# node 不可用时静默放行（兜底不能反过来卡流程）。
set -euo pipefail

source "$(dirname "$0")/lib/common.sh"

# 中文路径上做 bash 通配/basename/case。Windows 中文系统的 GBK 区域会把 UTF-8 字面量按
# 多字节误解码、让每个比较恒假而静默失效（issue #164）。强制 C 区域走字节匹配才稳定。
# node 单独进程按 UTF-8 处理，不受 LC_ALL=C 影响。
export LC_ALL=C

HOOK_INPUT="${CLAUDE_TOOL_INPUT:-}"
if [ -z "$HOOK_INPUT" ] && [ ! -t 0 ]; then
  HOOK_INPUT="$(cat)"
fi
# 故意不 export：Write/Edit 负载里带整章正文，export 会把它塞进本脚本每个子进程的 envp，
# 负载一大 execve 就 E2BIG（Linux 单个环境变量上限 128 KiB，macOS 整体 1 MiB），
# dirname/basename/node 全报「Argument list too long」，兜底网静默停用。改为只在需要负载的
# node 调用处用管道喂 stdin（nww_hook_cli.js extract-target 在 HOOK_INPUT 缺省时读 stdin）。

# 探测 node（官方现在推荐原生二进制装 Claude Code，只有 npm 装法才带 Node——native 安装
# 可能无 node。探测不到就静默放行：兜底网降级停用，session-start.sh 会在会话起点提示一次）。
node -e "" >/dev/null 2>&1 || exit 0
CLI="$(dirname "$0")/nww_hook_cli.js"
[ -f "$CLI" ] || exit 0

# 抽取目标文件路径（负载走管道喂 node 的 stdin，按 UTF-8 写回路径）。
TARGET="$(printf '%s' "$HOOK_INPUT" | node "$CLI" extract-target 2>/dev/null || true)"
[ -z "$TARGET" ] && exit 0

ROOT=$(project_root)
# 盘符绝对路径归一（对齐 guard-outline-before-prose.sh / plugin.ts，issue #184）。
case "$TARGET" in
  /*) ABS="$TARGET" ;;
  [A-Za-z]:[/\\]*) ABS="${TARGET//\\//}" ;;
  *)  ABS="$ROOT/$TARGET" ;;
esac

BASE="$(basename "$ABS")"

# 只对「正文」文件兜底，绝不碰代码/细纲/设定/大纲等非正文文件：
#   - 短篇：{书}/正文.md，且同目录有 设定.md（真短篇工程信号，排除 docs/正文.md 之类）
#   - 长篇：正文树内的 第N章*.md——兼容平铺（{书}/正文/第N章*.md）与卷目录
#     （{书}/正文/第一卷/第N章*.md），且 {书} 有 大纲/追踪/设定（真书结构）
# case 模式锚定首字：细纲_第N章.md（首字「细」）、卷纲_第1卷.md、check-ai-patterns.js、
# 设定.md、大纲.md 等天然都不匹配 `正文.md`/`第*章*.md`，不会被捕获。
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
    # 定位「正文」目录的上一级作为 BOOK（兼容平铺与卷目录）
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

# 报告用真实换行拼接（NL），不用字面 `\n` 占位：末尾必须 printf '%s' 输出，见文末注释。
NL=$'\n'
OUT=""
HARD=0  # 硬信号累计；末尾非 advisory-only 时 exit 2

# 落盘检测：正文极短（<200 字节）多半是没写完或落盘失败（quota/timeout 中断）。
# 用字节（wc -c）而非字数：LC_ALL=C 下无法按码点数中文，字节阈值已足够判「几乎空」。
BYTES=$(wc -c < "$ABS" 2>/dev/null | tr -d ' ' || echo 0)
case "$BYTES" in ''|*[!0-9]*) BYTES=0 ;; esac
if [ "$BYTES" -lt 200 ]; then
  OUT+="【落盘】正文仅 ${BYTES} 字节，疑似未写完/落盘失败（quota/超时中断？），请核对并补写。${NL}"
  HARD=1
fi

# ── paragraph_gate（移植 detect_internal I-J 并校准：J1 >220 BLOCK / >160 WARN，J2/J3 BLOCK）──
# 只对正文章节文件跑（文件名匹配 /第N章*.md 且有标题行）。
# Writer 分多次 Write 时可能中间检测半成品（误报），但最终完整版一定会被检测到。
# 检测到 BLOCK 时输出醒目的 ASCII 警告框 + 三步动作指令，然后 exit 2（真阻断）。
if echo "$ABS" | grep -qE '/第[0-9]+章[^/]*\.md$'; then
  FIRST_LINE=$(head -1 "$ABS" 2>/dev/null || echo "")
  if echo "$FIRST_LINE" | grep -qE '^#?\s*第\s*[0-9]+\s*章'; then
    # 脚本与 hook 同目录：部署时 DEPLOY_CLI_FILES 把 nww_paragraph_gate.py
    # （及其依赖 nww_text.py）拷进 {BOOK}/.claude/hooks/，与 hook 平级。
    # 旧路径写成 "$(dirname "$0")/../scripts/..." = {BOOK}/.claude/scripts/...，
    # 该目录在部署树里不存在 → -f 恒假 → 段落门静默跳过。
    PG_SCRIPT="$(cd "$(dirname "$0")" && pwd)/nww_paragraph_gate.py"
    if command -v python3 >/dev/null 2>&1 && [ -f "$PG_SCRIPT" ]; then
      # 用 if 形式捕获退出码：`VAR="$(cmd)"` 在 set -e 下会因 cmd 非 0 直接中止
      # 整个 hook —— 那样 rc=2（BLOCK）时下面的提示框永远打不出来（死代码），
      # rc=1（脚本异常）更是整个兜底网静默消失。if 条件里的赋值豁免于 set -e，
      # 且 else 分支的 $? 就是 cmd 的真实退出码。
      if PG_OUT="$(python3 "$PG_SCRIPT" --prose "$ABS" 2>/dev/null)"; then
        PG_RC=0
      else
        PG_RC=$?
      fi
      if [ $PG_RC -eq 2 ]; then
        HARD=1
        # 硬信号：拼框 → 打印 → exit 2（advisory-only 除外，继续收网）。
        BOX="${NL}╔══════════════════════════════════════════════════════════╗${NL}"
        BOX+="║  BLOCK: 段落节奏硬坏形态（I-J fail）—— 必须修复            ║${NL}"
        BOX+="╠══════════════════════════════════════════════════════════╣${NL}"
        BOX+="║  本 hook 独立检测，不管主会话有没有跑 post_write。          ║${NL}"
        BOX+="║  你必须执行以下三步后再继续后续章节：                      ║${NL}"
        BOX+="║    1. 重写本章正文（解决 J1/J2/J3 问题）                  ║${NL}"
        BOX+="║    2. 重新调用 nww_post_write.py                          ║${NL}"
        BOX+="║    3. 确认 paragraph_gate 返回 PASS 后再继续               ║${NL}"
        BOX+="╠══════════════════════════════════════════════════════════╣${NL}"
        while IFS= read -r line; do
          BOX+="║  ${line:0:56}${NL}"
        done < <(printf '%s\n' "$PG_OUT" | grep -E 'BLOCK|violations|J[123]' | head -5)
        BOX+="╚══════════════════════════════════════════════════════════╝${NL}"
        OUT+="$BOX"
        if [ "${NWW_HOOK_ADVISORY_ONLY:-}" != "1" ]; then
          printf '%s\n' "=== 正文兜底检测（${BASE}）===" "轻量确定性网自动复扫（模型无关，防主会话漏跑收尾）。按类型处理后复扫到净："
          printf '%s' "$OUT"
          exit 2
        fi
      elif [ $PG_RC -eq 0 ] && echo "$PG_OUT" | grep -q "WARN"; then
        OUT+="【段落节奏提醒】${NL}${PG_OUT}${NL}"
      fi
    fi
  fi
fi


# 内容网走 node 共享核，抓截断/拒绝语/AI 自指/工程词 tier1/紧邻复读/毒句式。
# 字数只由 nww_ctl 的公开命令测量，不在 Adapter 内复制，也不受 Node 降级影响。
NET_MSG="$(node "$CLI" prose-net "$ABS" 2>/dev/null || true)"
if [ -n "$NET_MSG" ]; then
  OUT+="【退化/工程词/毒句式】（硬信号：截断/拒绝语/工程词/毒句式→重写；命中即处理，别留给下一章）${NL}${NET_MSG}${NL}"
  HARD=1
fi

[ -z "$OUT" ] && exit 0

# 必须 %s 不能 %b：${OUT} 里嵌的是作者原文切片（截断/复读/工程词摘录）。%b 会把正文里的
# `\n`、`\b`、`\t` 当转义展开，把摘录改写成文件里不存在的内容；`\c`（Windows 路径 C:\code
# 就带）更会直接终止整条 printf，把它后面所有硬信号静默丢掉（exit 0、stderr 空）。
# 本 hook 自己的分隔换行由上面的 ${NL} 真实换行承担，不再依赖 %b 展开。
printf '%s\n' "=== 正文兜底检测（${BASE}）===" "轻量确定性网自动复扫（模型无关，防主会话漏跑收尾）。按类型处理后复扫到净："
printf '%s' "$OUT"
# 测试回退：仅当显式打开时把硬信号降为 advisory（生产禁止依赖）。
if [ "${NWW_HOOK_ADVISORY_ONLY:-}" = "1" ]; then
  exit 0
fi
if [ "$HARD" -eq 1 ]; then
  exit 2
fi
exit 0
