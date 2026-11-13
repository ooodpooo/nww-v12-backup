import { useEffect, useMemo, useState } from 'react'
import { api, type Tree, type TreeFile } from '../api'
import EditorPane from './EditorPane'

interface Props {
  bookId: string
  activePath: string | null
  onNavigate: (p: string | null) => void
}

const SECTION_ORDER = ['正文', '大纲', '设定', '追踪']

function chapterNo(name: string): number | null {
  const m = name.match(/^第0*(\d+)章/)
  return m ? parseInt(m[1], 10) : null
}

function nameCmp(a: string, b: string): number {
  const na = chapterNo(a)
  const nb = chapterNo(b)
  if (na != null && nb != null && na !== nb) return na - nb
  return a.localeCompare(b, 'zh-CN')
}

// —— VS Code 风格目录树：把扁平文件列表按 path 重建为 目录/文件 层级 ——
interface TreeNode {
  name: string
  path: string // 文件=完整相对路径；目录=目录路径
  type: 'dir' | 'file'
  writable?: boolean
  children?: TreeNode[]
  /** 卷目录专用：该卷规划的章节总数（来自卷纲） */
  plannedChapters?: number
}

function buildTree(files: TreeFile[], section: string, volumePlans?: Record<string, number>): TreeNode[] {
  const prefix = section ? `${section}/` : '' // 后端 path 相对书目录，含分区前缀，建树前剥掉
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
    // 节点保留完整 path 用于打开文件，层级按去前缀后的相对路径重建
    const node: TreeNode = { name: f.name, path: f.path, type: 'file', writable: f.writable }
    if (parts.length > 1) ensureDir(parts.slice(0, -1).join('/')).children!.push(node)
    else root.push(node)
  }

  // 目录在前、文件在后；同类型按章节号/拼音排序
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

// —— VS Code 风格图标 ——
function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      className={`w-3 h-3 shrink-0 text-zinc-500 transition-transform duration-150 ${open ? 'rotate-90' : ''}`}
      viewBox="0 0 16 16"
      fill="currentColor"
      aria-hidden
    >
      <path d="M6 4l4 4-4 4V4z" />
    </svg>
  )
}

function FolderIcon({ open }: { open: boolean }) {
  return (
    <svg className="w-4 h-4 shrink-0" viewBox="0 0 16 16" aria-hidden>
      <path
        d="M1.5 3A1.5 1.5 0 0 1 3 1.5h3l1.5 1.5H13A1.5 1.5 0 0 1 14.5 4.5v1H2.2A1.5 1.5 0 0 1 1.5 7.7V3z"
        fill={open ? '#dcb67a' : '#7f8b99'}
      />
      <path
        d="M1.5 4.5h13A1.5 1.5 0 0 1 16 6v8.5A1.5 1.5 0 0 1 14.5 16h-13A1.5 1.5 0 0 1 0 14.5v-10A1.5 1.5 0 0 1 1.5 3v1.5z"
        fill={open ? '#e8c179' : '#8a97a5'}
      />
    </svg>
  )
}

function FileIcon({ readonly }: { readonly: boolean }) {
  return (
    <svg className="w-4 h-4 shrink-0" viewBox="0 0 16 16" aria-hidden>
      <path d="M4.5 1h5L13 4.5V15H4.5V1z" fill={readonly ? '#6b7280' : '#9aa4b0'} />
      <path d="M9.5 1v3.5H13l-3.5-3.5z" fill={readonly ? '#4b5563' : '#7f8b99'} />
      {readonly && (
        <g fill="#3f3f46">
          <rect x="6.2" y="9" width="3.6" height="3.2" rx="0.6" />
          <path d="M7.6 9V7.4a.9.9 0 0 1 1.8 0V9" stroke="#e4e4e7" strokeWidth="1.1" fill="none" />
        </g>
      )}
    </svg>
  )
}

// —— VS Code 风格目录树渲染 ——
// 缩进规范：每级 16px；行内固定 16px 图标列（目录=箭头，文件=占位），图标列对齐同级
const INDENT = 16

/** 缩进引导线：子节点沿祖先目录的箭头中线画竖线（VS Code 缩进参考线） */
function guideShadow(depth: number): string | undefined {
  if (depth <= 0) return undefined
  const lines: string[] = []
  for (let i = 0; i < depth; i++) lines.push(`${i * INDENT + 8}px 0 0 0 rgba(148,163,184,0.10)`)
  return lines.join(', ')
}

interface TreeNodeProps {
  node: TreeNode
  depth: number
  activePath: string | null
  onNavigate: (p: string | null) => void
}

function TreeNodeView({ node, depth, activePath, onNavigate }: TreeNodeProps) {
  const indent = depth * INDENT
  const shadow = guideShadow(depth)

  if (node.type === 'file') {
    const active = activePath === node.path
    return (
      <button
        onClick={() => onNavigate(node.path)}
        title={node.path}
        className={`group/file w-full flex items-center gap-0 h-[22px] pr-2 text-[14px] transition-colors duration-100 ${
          active ? 'bg-zinc-800/80 text-zinc-50' : 'text-zinc-300 hover:bg-zinc-800/40'
        }`}
        style={{
          paddingLeft: indent, // 与同级目录一致：内容区同起点，图标列对齐
          boxShadow: active ? `inset 2px 0 0 0 #f59e0b${shadow ? ', ' + shadow : ''}` : shadow,
        }}
      >
        <span className="w-4 shrink-0" aria-hidden /> {/* 箭头占位，与同级目录图标列对齐 */}
        <FileIcon readonly={!node.writable} />
        <span className="truncate flex-1 text-left ml-1.5">{node.name}</span>
      </button>
    )
  }

  const [open, setOpen] = useState(depth < 1) // 一级子目录默认展开，更深层折叠
  return (
    <div>
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-0 h-[22px] pr-2 text-[14px] text-zinc-300 hover:bg-zinc-800/40 transition-colors duration-100"
        style={{ paddingLeft: indent, boxShadow: shadow }}
      >
        <span className="w-4 shrink-0 flex items-center justify-center">
          <Chevron open={open} />
        </span>
        <FolderIcon open={open} />
        <span className="truncate flex-1 text-left ml-1.5">{node.name}</span>
        {node.plannedChapters != null && (
          <span className="ml-2 shrink-0 text-[11px] text-zinc-500 tabular-nums">{countFiles(node.children!)}/{node.plannedChapters}</span>
        )}
      </button>
      {open &&
        node.children!.map((c) => (
          <TreeNodeView key={c.path} node={c} depth={depth + 1} activePath={activePath} onNavigate={onNavigate} />
        ))}
    </div>
  )
}

function SectionView({
  section,
  nodes,
  activePath,
  onNavigate,
  volumeMode,
  volumeCount,
  totalPlannedChapters,
}: {
  section: string
  nodes: TreeNode[]
  activePath: string | null
  onNavigate: (p: string | null) => void
  volumeMode?: 'split' | 'flat'
  volumeCount?: number
  totalPlannedChapters?: number
}) {
  const [open, setOpen] = useState(true)
  // 正文分区显示「卷纲」；split分卷模式显示"总X卷"，flat不分卷模式显示"总X章"
  const displayName = section === '正文' ? '卷纲' : section
  const badge = section === '正文'
    ? volumeMode === 'split' && volumeCount != null
      ? `总${volumeCount}卷`
      : volumeMode === 'flat' && totalPlannedChapters != null && totalPlannedChapters > 0
        ? `总${totalPlannedChapters}章`
        : countFiles(nodes)
    : countFiles(nodes)
  return (
    <div>
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-0 pr-2 h-[26px] text-[14px] font-semibold uppercase tracking-wider text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/40 transition-colors duration-100"
      >
        <span className="w-4 shrink-0 flex items-center justify-center">
          <Chevron open={open} />
        </span>
        <FolderIcon open={open} />
        <span className="flex-1 text-left ml-1.5">{displayName}</span>
        <span className="text-[10px] font-medium text-zinc-500 tabular-nums">{badge}</span>
      </button>
      {open &&
        nodes.map((n) => (
          <TreeNodeView key={n.path} node={n} depth={0} activePath={activePath} onNavigate={onNavigate} />
        ))}
    </div>
  )
}

export default function BookWorkspace({ bookId, activePath, onNavigate }: Props) {
  const [tree, setTree] = useState<Tree | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .tree(bookId)
      .then(setTree)
      .catch((e) => setError(String(e)))
  }, [bookId])

  const sections = useMemo(() => {
    if (!tree) return []
    return [...SECTION_ORDER]
      .filter((s) => tree.sections.some((x) => x.section === s))
      .map((s) => {
        const sec = tree.sections.find((x) => x.section === s)!
        return { ...sec, nodes: buildTree(sec.files, s, sec.volume_plans) }
      })
  }, [tree])

  // 未指定文件时默认打开正文最新一章
  useEffect(() => {
    if (!activePath && tree) {
      const body = tree.sections.find((s) => s.section === '正文')
      if (body && body.files.length > 0) {
        const sorted = [...body.files].sort((a, b) => nameCmp(a.name, b.name))
        onNavigate(sorted[sorted.length - 1].path)
      }
    }
  }, [activePath, tree, onNavigate])

  if (error) return <div className="h-full flex items-center justify-center text-sm text-red-400">{error}</div>
  if (!tree) return <div className="h-full flex items-center justify-center text-sm text-zinc-500">加载中…</div>

  const activeIsBody = activePath ? activePath.startsWith('正文/') : false

  return (
    <div className="h-full flex">
      {/* 侧栏文件树（VS Code 风格） */}
      <aside className="w-64 shrink-0 h-full overflow-auto border-r border-zinc-800 bg-zinc-900/40">
        <div className="px-3 h-9 flex items-center gap-1.5 text-[14px] font-medium text-zinc-500 sticky top-0 bg-zinc-900/95 backdrop-blur border-b border-zinc-800/60">
          <FolderIcon open />
          <span className="truncate">{tree.title}</span>
        </div>
        {sections.map((sec) => (
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
      </aside>

      {/* 内容区 */}
      <div className="flex-1 min-w-0 h-full">
        {activePath ? (
          <EditorPane key={activePath} bookId={bookId} path={activePath} readFirst={activeIsBody} />
        ) : (
          <div className="h-full flex items-center justify-center text-sm text-zinc-500">从左侧选择文件</div>
        )}
      </div>
    </div>
  )
}
