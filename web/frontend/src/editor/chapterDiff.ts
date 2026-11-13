/**
 * 编辑器候选的逐字 diff 消费层（V14 document.ts 同构移植）。
 *
 * 分工（DRY）：LCS 分词与 chunk 生成由后端 editor_ai.build_diff_chunks 单点计算，
 * 这里只负责「把相邻增删改并为一条改动」以及「按用户勾选写回」。
 * 禁止在前端再造一份 diff parser。
 */
export interface DiffChunk {
  id: string
  type: 'equal' | 'insert' | 'delete'
  text: string
}

export interface DiffChange {
  id: string
  kind: 'insert' | 'delete' | 'replace'
  originalText: string
  candidateText: string
  chunkIds: string[]
}

/** 相邻的 insert/delete 合并为一条替换，避免采纳后留下重复原文。 */
export function groupChanges(chunks: readonly DiffChunk[]): DiffChange[] {
  const changes: DiffChange[] = []
  let pending: DiffChunk[] = []
  const flush = () => {
    if (!pending.length) return
    const originalText = pending.filter((c) => c.type === 'delete').map((c) => c.text).join('')
    const candidateText = pending.filter((c) => c.type === 'insert').map((c) => c.text).join('')
    changes.push({
      id: `change-${changes.length + 1}`,
      kind: originalText && candidateText ? 'replace' : originalText ? 'delete' : 'insert',
      originalText,
      candidateText,
      chunkIds: pending.map((c) => c.id),
    })
    pending = []
  }
  for (const chunk of chunks) {
    if (chunk.type === 'equal') {
      flush()
      continue
    }
    pending.push(chunk)
  }
  flush()
  return changes
}

export function defaultSelectedChangeIds(chunks: readonly DiffChunk[]): Set<string> {
  return new Set(groupChanges(chunks).map((c) => c.id))
}

/**
 * 按勾选写回：未勾选的改动保留原文，勾选的改动采用候选文本。
 * 返回的是「替换选区用的最终文本」，不是整章正文。
 */
export function applySelectedDiff(
  originalText: string,
  candidateText: string,
  chunks: readonly DiffChunk[],
  selectedChangeIds: ReadonlySet<string>,
): string {
  if (!chunks.length) return candidateText
  const valid = new Set(['equal', 'insert', 'delete'])
  if (chunks.some((c) => !valid.has(c.type))) {
    throw new Error('候选对比数据不完整，请重新生成。')
  }
  const fromOriginal = chunks.filter((c) => c.type !== 'insert').map((c) => c.text).join('')
  const fromCandidate = chunks.filter((c) => c.type !== 'delete').map((c) => c.text).join('')
  if (fromOriginal !== originalText || fromCandidate !== candidateText) {
    throw new Error('正文或候选已变化，请重新生成后再采纳。')
  }
  const changes = groupChanges(chunks)
  const byChunk = new Map<string, DiffChange>()
  for (const change of changes) for (const id of change.chunkIds) byChunk.set(id, change)
  const rendered = new Set<string>()
  return chunks.map((chunk) => {
    if (chunk.type === 'equal') return chunk.text
    const change = byChunk.get(chunk.id)
    if (!change || rendered.has(change.id)) return ''
    rendered.add(change.id)
    return selectedChangeIds.has(change.id) ? change.candidateText : change.originalText
  }).join('')
}
