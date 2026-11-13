import { Fragment, useEffect, useMemo, useState } from 'react'
import {
  BookOpen,
  ChevronDown,
  ChevronRight,
  FileText,
  Folder,
  FolderOpen,
  Globe2,
  Layers,
  ListChecks,
  type LucideIcon,
} from 'lucide-react'
import { api, type Tree, type TreeFile } from '../api'
import ScrollArea from './ScrollArea'

/* ─────────────────────  资产树：V14 目录排版（V12 色板）  ─────────────────────
   结构：大分组（本书目录 / 小说资产）→ 分区（卷纲/大纲/设定/追踪）→ 目录 → 文件
   排版对齐 V14 `ResourceDirectory.tsx` / `AssetTreeNavigator.tsx`：
     · 分组标题小字 tracking-widest；行 min-h-9(36px) + rounded-md + 图标 mr-2
     · 每级 18px 缩进 + 绝对定位引导竖线；叶子用小圆点
     · 选中态浅底 + 左侧琥珀竖条；右侧字数/计数为 10px 状态文字
   图标统一 lucide-react 线性（与 V14 同版本），色值沿用 V12。
────────────────────────────────────────────────────────────────────── */

interface Props {
  bookId: string
  activePath: string | null
  treeVersion?: number
  onNavigate: (p: string) => void
}

const SECTION_ORDER = ['正文', '大纲', '设定', '追踪']
const GROUP_OF: Record<string, string> = { 正文: '本书目录', 大纲: '本书目录', 设定: '小说资产', 追踪: '小说资产' }
const GROUP_ORDER = ['本书目录', '小说资产']
/** V14 AssetTreeNavigator 的层级步长；行内 paddingLeft = depth * 18 + 6 */
const TREE_INDENT = 18
const SECTION_ICONS: Record<string, LucideIcon> = {
  正文: Layers, 大纲: BookOpen, 设定: Globe2, 追踪: ListChecks,
}

interface TreeNode {
  name: string
  path: string
  type: 'dir' | 'file'
  writable?: boolean
  children?: TreeNode[]
  displayName?: string
  chapterNum?: string
  chapterTitle?: string
  chars?: number
  /** 章节健康分：审稿闭环历史最高分（来自 追踪/评分/第NNN章.json）。
   *  命名口径见下方渲染处注释 —— 它是「生产健康检查」，不是「商业质量评分」。 */
  score?: number
  /** 章节副行：正文落盘时间（Unix 秒） */
  mtime?: number
  /** 卷目录专用：该卷规划的章节总数（来自卷纲） */
  plannedChapters?: number
}

function chapterNo(name: string): number | null {
  const m = name.match(/^第0*(\d+)章/)
  return m ? parseInt(m[1], 10) : null
}

/** 解析正文章节文件名「第001章_标题.md」→ { num: "001", title: "标题" }，自动剥离末尾 _N字 后缀 */
export function parseChapterName(name: string): { num: string; title: string } | null {
  const m = name.match(/^第(0*\d+)章[_\s]*(.+?)(?:[_\s-]*\d+\s*字)?\.md$/)
  return m ? { num: m[1], title: m[2] } : null
}

function nameCmp(a: string, b: string): number {
  const na = chapterNo(a)
  const nb = chapterNo(b)
  if (na != null && nb != null && na !== nb) return na - nb
  return a.localeCompare(b, 'zh-CN')
}

const pad2 = (n: number) => String(n).padStart(2, '0')

function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
}

/** Unix 秒 → 侧边栏短时间：今天只给 HH:MM，其余给 MM-DD HH:MM（完整年份时间放 hover 提示） */
export function formatMtime(sec: number): string {
  const d = new Date(sec * 1000)
  const hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
  if (sameDay(d, new Date())) return hm
  return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${hm}`
}

/** 完整落盘时间，用于 hover 提示 */
export function fullMtime(sec: number): string {
  const d = new Date(sec * 1000)
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}

/** 章节健康分配色：≥90 干净 / 85-89 达标 / <85 待改（对齐流水线 PASS_SCORE = 85） */
export function scoreTone(score: number): string {
  if (score >= 90) return '#5dbb63'
  if (score >= 85) return '#d9a23b'
  return '#e0685f'
}

/** 章节健康分文本：整数不带小数点，半数保留一位（如 89.5） */
export function formatScore(score: number): string {
  return Number.isInteger(score) ? String(score) : score.toFixed(1)
}

function buildTree(files: TreeFile[], section: string, volumePlans?: Record<string, number>): TreeNode[] {
  const prefix = section ? `${section}/` : ''
  const root: TreeNode[] = []
  const dirIndex = new Map<string, TreeNode>()

  const ensureDir = (dirPath: string): TreeNode => {
    const cached = dirIndex.get(dirPath)
    if (cached) return cached
    const idx = dirPath.lastIndexOf('/')
    const parent = idx > 0 ? ensureDir(dirPath.slice(0, idx)) : null
    const dirName = dirPath.slice(idx + 1)
    const node: TreeNode = {
      name: dirName,
      path: dirPath,
      type: 'dir',
      children: [],
      // 正文分区下的一级目录（卷目录）附加规划章节数
      plannedChapters: section === '正文' && idx <= 0 ? volumePlans?.[dirName] : undefined,
    }
    dirIndex.set(dirPath, node)
    ;(parent ? parent.children! : root).push(node)
    return node
  }

  for (const f of files) {
    const rel = f.path.startsWith(prefix) ? f.path.slice(prefix.length) : f.path
    const parts = rel.split('/')
    const parsed = section === '正文' ? parseChapterName(f.name) : null
    const node: TreeNode = {
      name: f.name,
      path: f.path,
      type: 'file',
      writable: f.writable,
      chars: f.chars,
      score: f.score,
      mtime: f.mtime,
      displayName: parsed ? `${parsed.num}. ${parsed.title}` : undefined,
      chapterNum: parsed?.num,
      chapterTitle: parsed?.title,
    }
    if (parts.length > 1) ensureDir(parts.slice(0, -1).join('/')).children!.push(node)
    else root.push(node)
  }

  const sortTree = (nodes: TreeNode[]) => {
    nodes.forEach((n) => n.type === 'dir' && sortTree(n.children!))
    nodes.sort((a, b) => {
      if (a.type !== b.type) return a.type === 'dir' ? -1 : 1
      return nameCmp(a.name, b.name)
    })
  }
  sortTree(root)
  return root
}

function countFiles(nodes: TreeNode[]): number {
  let n = 0
  for (const node of nodes) n += node.type === 'file' ? 1 : countFiles(node.children!)
  return n
}

/* ── 图标：统一 lucide 线性，尺寸/颜色走 index.css 的 .v14-tree-icon / .v14-chevron ── */
function Expander({ open }: { open: boolean }) {
  return open
    ? <ChevronDown className="v14-chevron" strokeWidth={1.8} aria-hidden />
    : <ChevronRight className="v14-chevron" strokeWidth={1.8} aria-hidden />
}

/** 目录/文件节点前的 18px 箭头位（叶子用小圆点，对齐 V14 AssetTreeNavigator） */
function Lead({ open }: { open?: boolean }) {
  return (
    <span className="w-[18px] shrink-0 flex items-center justify-center">
      {open == null ? <span className="v14-leaf-dot" /> : <Expander open={open} />}
    </span>
  )
}

const indentOf = (depth: number) => depth * TREE_INDENT + 6

function TreeNodeView({ node, depth, activePath, onNavigate }: { node: TreeNode; depth: number; activePath: string | null; onNavigate: (p: string) => void }) {
  const indent = indentOf(depth)
  const label = node.displayName ?? node.name

  if (node.type === 'file') {
    const active = activePath === node.path
    const isChapter = !!node.chapterNum
    return (
      <button
        onClick={() => onNavigate(node.path)}
        title={node.path}
        className={`v14-tree-row ${active ? 'is-active' : ''}`}
        style={{ paddingLeft: indent }}
      >
        {isChapter ? (
          <>
            {/* V14 目录面板的章节行由编号起头，不重复放叶子圆点 */}
            <span className="w-[18px] shrink-0" aria-hidden />
            <span className="min-w-0 flex-1 truncate">
              <span
                className="mr-1.5 font-semibold tabular-nums text-[12px]"
                style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace' }}
              >{node.chapterNum}.</span>
              <span>{node.chapterTitle}</span>
            </span>
          </>
        ) : (
          <>
            <Lead />
            <FileText className="v14-tree-icon mr-2" strokeWidth={1.8} aria-hidden />
            <span className="min-w-0 flex-1 truncate">{label}</span>
          </>
        )}
        {node.chars != null && <span className="v14-meta ml-2">{node.chars.toLocaleString()}</span>}
      </button>
    )
  }

  const [open, setOpen] = useState(depth < 2)
  // activePath 落在本目录子树中时自动展开（翻页/点文件后高亮不丢）
  useEffect(() => {
    if (activePath && activePath.startsWith(node.path + '/') && !open) setOpen(true)
  }, [activePath, node.path, open])
  const FolderGlyph = open ? FolderOpen : Folder
  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        className="v14-tree-row"
        style={{ paddingLeft: indent }}
        title={node.path}
      >
        <Lead open={open} />
        <FolderGlyph className="v14-tree-icon mr-2" strokeWidth={1.8} aria-hidden />
        <span className="min-w-0 flex-1 truncate">{label}</span>
        {node.plannedChapters != null && (
          <span className="v14-meta ml-2">{countFiles(node.children!)}/{node.plannedChapters}</span>
        )}
      </button>
      {open && (
        <div className="relative space-y-0.5">
          <span className="v14-guide" style={{ left: indent + 9 }} aria-hidden />
          {node.children!.map((c) => (
            <TreeNodeView key={c.path} node={c} depth={depth + 1} activePath={activePath} onNavigate={onNavigate} />
          ))}
        </div>
      )}
    </div>
  )
}

function SectionView({ section, nodes, activePath, onNavigate, volumeMode, volumeCount, totalPlannedChapters }: { section: string; nodes: TreeNode[]; activePath: string | null; onNavigate: (p: string) => void; volumeMode?: 'split' | 'flat'; volumeCount?: number; totalPlannedChapters?: number }) {
  const [open, setOpen] = useState(section === '正文')
  // activePath 落在本 section 任何节点时自动展开（翻页/点文件后高亮所在 section 不丢）
  useEffect(() => {
    if (!activePath || open) return
    const hit = (ns: TreeNode[]): boolean => ns.some(n =>
      activePath === n.path || activePath.startsWith(n.path + '/') || (n.children && hit(n.children))
    )
    if (hit(nodes)) setOpen(true)
  }, [activePath, nodes, open])
  // 正文分区显示「卷纲」；split分卷模式显示"总X卷"，flat不分卷模式显示"总X章"
  const displayName = section === '正文' ? '卷纲' : section
  const badge = section === '正文'
    ? volumeMode === 'split' && volumeCount != null
      ? `总${volumeCount}卷`
      : volumeMode === 'flat' && totalPlannedChapters != null && totalPlannedChapters > 0
        ? `总${totalPlannedChapters}章`
        : countFiles(nodes)
    : countFiles(nodes)
  const SectionIcon = SECTION_ICONS[section] ?? Layers
  return (
    <div className="space-y-0.5">
      <button
        onClick={() => setOpen(!open)}
        className="v14-tree-row font-medium text-[#d0d0d0] hover:text-white"
        style={{ paddingLeft: 6 }}
        title={`${displayName}（点击折叠/展开）`}
      >
        <span className="w-[18px] shrink-0 flex items-center justify-center"><Expander open={open} /></span>
        <SectionIcon className="v14-tree-icon mr-2" strokeWidth={1.8} aria-hidden />
        <span className="min-w-0 flex-1 truncate">{displayName}</span>
        <span className="v14-meta ml-2">{badge}</span>
      </button>
      {open && (
        <div className="relative">
          <span className="v14-guide" style={{ left: 15 }} aria-hidden />
          {nodes.map((n) => <TreeNodeView key={n.path} node={n} depth={1} activePath={activePath} onNavigate={onNavigate} />)}
        </div>
      )}
    </div>
  )
}

interface SectionData {
  section: string
  nodes: TreeNode[]
  volume_mode?: 'split' | 'flat'
  volume_count?: number
  total_planned_chapters?: number
}

export default function AssetTree({ bookId, activePath, treeVersion, onNavigate }: Props) {
  const [tree, setTree] = useState<Tree | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.tree(bookId).then(setTree).catch((e) => setError(String(e)))
  }, [bookId, treeVersion])

  const sections = useMemo<SectionData[]>(() => {
    if (!tree) return []
    return SECTION_ORDER
      .filter((s) => tree.sections.some((x) => x.section === s))
      .map((s) => {
        const sec = tree.sections.find((x) => x.section === s)!
        return {
          section: s,
          volume_mode: sec.volume_mode,
          volume_count: sec.volume_count,
          total_planned_chapters: sec.total_planned_chapters,
          nodes: buildTree(sec.files, s, sec.volume_plans),
        }
      })
  }, [tree])

  if (error) return <div className="h-full flex items-center justify-center text-[14px] text-red-400 p-3">{error}</div>
  if (!tree) return <div className="h-full flex items-center justify-center text-[14px] text-[#6a6a6a]">加载中…</div>

  return (
    <ScrollArea className="pb-3">
      {GROUP_ORDER.map((group) => {
        const list = sections.filter((s) => GROUP_OF[s.section] === group)
        if (list.length === 0) return null
        return (
          <Fragment key={group}>
            <div className="v14-section-label">{group}</div>
            <div className="px-2 space-y-0.5">
              {list.map((sec) => (
                <SectionView
                  key={sec.section}
                  section={sec.section}
                  nodes={sec.nodes}
                  activePath={activePath}
                  onNavigate={onNavigate}
                  volumeMode={sec.volume_mode}
                  volumeCount={sec.volume_count}
                  totalPlannedChapters={sec.total_planned_chapters}
                />
              ))}
            </div>
          </Fragment>
        )
      })}
    </ScrollArea>
  )
}
