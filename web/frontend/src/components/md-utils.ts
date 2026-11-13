
/* ─────────────────────  Markdown 解析与渲染（EditorPane 与 PhonePreview 共用）  ───────────────────── */

type BlockType = 'h1' | 'h2' | 'h3' | 'p' | 'quote' | 'hr'
interface Block { type: BlockType; text: string }

function parseBlocks(md: string): Block[] {
  // 统一行级扫描（不依赖空行，兼容紧凑正文与标准 markdown）：
  //  标题行（# / ## / ###）独立成 h1/h2/h3；其余每行独立成段落 p——
  //  中文网文「一行一段」下每段首行缩进都生效，标题字号与正文明确区分。
  const out: Block[] = []
  for (const raw of md.split('\n')) {
    const t = raw.trim()
    if (!t) continue
    if (t.startsWith('### ')) { out.push({ type: 'h3' as const, text: t.slice(4) }); continue }
    if (t.startsWith('## ')) { out.push({ type: 'h2' as const, text: t.slice(3) }); continue }
    if (t.startsWith('# ')) { out.push({ type: 'h1' as const, text: t.slice(2) }); continue }
    if (t.startsWith('>')) { out.push({ type: 'quote' as const, text: t }); continue }
    if (/^-{3,}$/.test(t)) { out.push({ type: 'hr' as const, text: t }); continue }
    out.push({ type: 'p' as const, text: raw.replace(/^\s+/, '').replace(/\s+$/, '') })
  }
  return out
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}
function inlineHtml(text: string): string {
  return escapeHtml(text).split('\n').map((l) => l || '&nbsp;').join('<br/>')
}
function blockToHtml(b: Block): string {
  switch (b.type) {
    case 'h1': return `<h1>${inlineHtml(b.text)}</h1>`
    case 'h2': return `<h2>${inlineHtml(b.text)}</h2>`
    case 'h3': return `<h3>${inlineHtml(b.text)}</h3>`
    case 'hr': return '<hr/>'
    case 'quote': return `<blockquote>${inlineHtml(b.text.replace(/^>\s?/gm, ''))}</blockquote>`
    default: return `<p>${inlineHtml(b.text)}</p>`
  }
}
export function mdToHtml(md: string): string { return parseBlocks(md).map(blockToHtml).join('') }

/* DOM → Markdown */
function inlineToMd(el: Node): string {
  let out = ''
  el.childNodes.forEach((c) => {
    if (c.nodeType === Node.TEXT_NODE) out += (c.textContent ?? '').replace(/\u00a0/g, ' ')
    else if (c.nodeType === Node.ELEMENT_NODE) {
      const e = c as HTMLElement
      const tag = e.tagName.toLowerCase()
      const inner = inlineToMd(e)
      if (tag === 'br') out += '\n'
      else if (tag === 'strong' || tag === 'b') out += '**' + inner + '**'
      else if (tag === 'em' || tag === 'i') out += '*' + inner + '*'
      else if (tag === 'code') out += '`' + inner + '`'
      else out += inner
    }
  })
  return out
}
export function htmlToMd(root: HTMLElement): string {
  const lines: string[] = []
  root.childNodes.forEach((el) => {
    if (el.nodeType === Node.ELEMENT_NODE) {
      const e = el as HTMLElement
      const tag = e.tagName.toLowerCase()
      if (tag === 'hr') { lines.push('---'); return }
      const inner = inlineToMd(e)
      if (tag === 'blockquote') lines.push(inner.split('\n').map((l) => '> ' + l).join('\n'))
      else { const prefix = tag === 'h1' ? '# ' : tag === 'h2' ? '## ' : tag === 'h3' ? '### ' : ''; lines.push(prefix + inner) }
    } else if (el.nodeType === Node.TEXT_NODE) { const t = (el.textContent ?? '').trim(); if (t) lines.push(t) }
  })
  return lines.join('\n\n')
}

/* 排版渲染 —— 字号全部继承父级，不硬编码 */
// 统一内容样式：阅读/编辑模式共用，确保格式完全一致
export const PROSE_STYLE = 'space-y-3 ' + [
  '[&_h1]:text-[1.5em] [&_h1]:font-semibold [&_h1]:text-[#e0e0e0] [&_h1]:mt-8 [&_h1]:mb-4 [&_h1:first-child]:mt-0',
  '[&_h2]:text-[1.3em] [&_h2]:font-semibold [&_h2]:text-[#e0e0e0] [&_h2]:mt-7 [&_h2]:mb-3 [&_h2:first-child]:mt-0',
  '[&_h3]:text-[1.15em] [&_h3]:font-semibold [&_h3]:text-[#e0e0e0] [&_h3]:mt-6 [&_h3]:mb-2 [&_h3:first-child]:mt-0',
  '[&_p]:leading-[1.9] [&_p]:text-[#a8a8a8] [&_p]:whitespace-pre-wrap [&_p]:[text-indent:2em]',
  '[&_blockquote]:border-l-2 [&_blockquote]:border-[#f59e0b]/60 [&_blockquote]:pl-4 [&_blockquote]:py-1 [&_blockquote]:text-[#888] [&_blockquote]:whitespace-pre-wrap [&_blockquote]:leading-7',
  '[&_hr]:border-[#222] [&_hr]:my-6',
  '[&_strong]:font-bold [&_strong]:text-[#d0d0d0]',
  '[&_em]:italic',
  '[&_u]:underline',
].join(' ')
