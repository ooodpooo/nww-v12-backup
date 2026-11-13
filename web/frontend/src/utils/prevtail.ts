/**
 * 上一章「尾勾」抽取逻辑（纯函数，不碰 DOM / 网络，便于独立测试）
 *
 * 定义：章节末尾用于勾住读者的那几段（悬念、未竟动作、压轴对白）。
 * 策略：从最后一个非空段落往前累积到字数上限，至少保留最后一段。
 */

/** 剥离行内 markdown 标记，保留纯文本（标题符/强调符/引用符/行内代码/图片链接） */
function stripInline(line: string): string {
  return line
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')          // 图片
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')       // 链接保留文字
    .replace(/\*\*([^*]+)\*\*/g, '$1')             // 加粗
    .replace(/__([^_]+)__/g, '$1')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1$2')     // 斜体
    .replace(/`([^`]+)`/g, '$1')                   // 行内代码
    .replace(/^>\s?/gm, '')                        // 引用符
    .replace(/^#{1,6}\s+/gm, '')                   // 行内残留标题符
    .trim()
}

/** 章尾元信息行（如「天道感应值：1」）：独立成块、形如「标签：数值」且很短 —— 不是正文，不进尾勾 */
const META_LINE = /^[^\s：:]{2,12}[：:]\s*[\dA-Za-z]+\s*$/

/** 去掉 YAML front-matter（存在且闭合时） */
function dropFrontMatter(text: string): string {
  if (!text.startsWith('---')) return text
  const lines = text.split('\n')
  for (let i = 1; i < Math.min(lines.length, 201); i++) {
    if (lines[i] === '---' || lines[i] === '...') return lines.slice(i + 1).join('\n')
  }
  return text
}

/**
 * 从整章 markdown 中提取结尾钩子。
 * @param md 章节全文
 * @param maxChars 摘录上限（默认 300 字）
 * @returns 结尾摘录；无有效正文时返回空串（调用方据此不渲染）
 */
export function extractTail(md: string, maxChars = 300): string {
  if (!md) return ''
  const text = dropFrontMatter(md.replace(/\r\n/g, '\n').replace(/\r/g, '\n').replace(/^\uFEFF/, ''))

  const blocks = text
    .split(/\n{2,}/)
    .map((b) => b.trim())
    .filter(Boolean)
    .filter((b) => !/^#{1,6}\s/.test(b))                    // 标题块
    .filter((b) => !/^(-{3,}|\*{3,}|_{3,})$/.test(b))       // 分隔线
    .filter((b) => !(b.length <= 24 && !b.includes('\n') && META_LINE.test(b))) // 章尾元信息行
    .map(stripInline)
    .filter(Boolean)
  if (!blocks.length) return ''

  const picked: string[] = []
  let size = 0
  for (let i = blocks.length - 1; i >= 0; i--) {
    const b = blocks[i]
    if (picked.length > 0 && size + b.length > maxChars) break
    picked.unshift(b)
    size += b.length
    if (size >= maxChars) break
  }

  const out = picked.join('\n\n')
  // 单段即超限：保尾部（尾勾的价值在最后几句），头部截断并加省略号
  return out.length > maxChars ? '…' + out.slice(out.length - maxChars) : out
}
