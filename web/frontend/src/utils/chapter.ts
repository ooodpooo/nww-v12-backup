/** 从编辑器当前打开路径解析章节号（NWW 正文命名约定：正文/第N章*.md，可含分卷目录）。
 *  仅识别「正文」下的第N章文件；大纲/设定/追踪/卷纲等一律视为未定位章节。
 *  口径与后端 nww_track_commit / AssetTree.parseChapterName 一致：/^第(0*\d+)章/。 */
export function chapterFromPath(path: string | null | undefined): { num: number; path: string } | null {
  if (!path) return null
  if (!path.startsWith('正文/')) return null
  const name = path.split('/').pop() ?? ''
  const m = /^第(0*\d+)章/.exec(name)
  if (!m) return null
  const num = parseInt(m[1], 10)
  if (!Number.isFinite(num) || num < 1) return null
  return { num, path }
}

/** 重写等级（2026-09-20 裁决命名，避免「表达/结构/剧情」歧义） */
export const REWRITE_LEVELS = [
  { id: 'L1', name: '保持剧情重写', desc: '原事件不变，重新表达（文笔差 / AI味 / 节奏差）' },
  { id: 'L2', name: '章节结构重写', desc: '保留章节目标，重排叙事结构（写法错 / 结构松散）' },
  { id: 'L3', name: '章节再生产', desc: '按合同（细纲+事实+前情）全量重新生产（严重漂移 / 事实错误）' },
] as const

export type RewriteLevel = (typeof REWRITE_LEVELS)[number]['id']
