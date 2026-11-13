/**
 * 与 skills/nww-novel-skill/scripts/nww_wordcount.py 的 visible_chars_v1 同口径的轻量前端实现，
 * 仅用于 Web 端即时展示字数，权威口径仍以流水线为准。
 */

const WHITE_SPACE_CODEPOINTS = new Set<number>([
  0x0009, 0x000a, 0x000b, 0x000c, 0x000d,
  0x0020, 0x0085, 0x00a0, 0x1680, ...range(0x2000, 0x200b),
  0x2028, 0x2029, 0x202f, 0x205f, 0x3000,
])

function range(from: number, to: number): number[] {
  const out: number[] = []
  for (let i = from; i < to; i++) out.push(i)
  return out
}

// 可识别 frontmatter 键行：首个字符为 Latin/CJK 键名 + 冒号（与后端 _FRONTMATTER_KEY_RE 同义）
const FRONTMATTER_KEY_RE = /^[A-Za-z_一-鿿][^:\n]{0,80}:[ \t]*(?:.*)$/

function isBlankLine(line: string): boolean {
  return /^[\u0009\u0020\u3000]*$/.test(line)
}

export function countVisibleChars(value: string): number {
  let text = value.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
  if (text.startsWith('\ufeff')) text = text.slice(1)

  // 前导 frontmatter 仅在「闭块前存在可识别 key: 行」时才剥离——与后端
  // nww_wordcount.strip_recognizable_frontmatter 同义，避免前端即时计数与
  // derived.chars 不一致（frontmatter 无规范键时旧实现会误剥正文）。
  const lines = text.split('\n')
  let bodyLines: string[]
  if (lines[0] !== '---') {
    bodyLines = lines
  } else {
    let closing = -1
    for (let i = 1; i < Math.min(lines.length, 201); i++) {
      if (lines[i] === '---' || lines[i] === '...') {
        closing = i
        break
      }
    }
    const hasKey = closing >= 2 && lines.slice(1, closing).some((l) => FRONTMATTER_KEY_RE.test(l))
    bodyLines = hasKey ? lines.slice(closing + 1) : lines
  }
  while (bodyLines.length && isBlankLine(bodyLines[0])) bodyLines.shift()
  if (bodyLines.length && /^[\u0009\u0020]{0,3}#{1,6}[\u0009\u0020]+\S/.test(bodyLines[0])) bodyLines.shift()
  text = bodyLines.join('\n')

  let count = 0
  for (const ch of text) {
    if (!WHITE_SPACE_CODEPOINTS.has(ch.codePointAt(0) ?? 0)) count++
  }
  return count
}

export function formatChars(n: number): string {
  if (n < 10000) return `${n} 字`
  return `${(n / 10000).toFixed(2)} 万字`
}
