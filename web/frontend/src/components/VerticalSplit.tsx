import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { ChevronDown, ChevronUp } from 'lucide-react'

/* ─────────────────────────  右栏垂直二分容器（上/下两栏，可拖拽分割）  ───────────────────────── */

interface VerticalSplitProps {
  top: ReactNode
  bottom: ReactNode
  topTitle: string
  bottomTitle: string
  /** localStorage 持久化 key（分割比例 + 折叠状态） */
  storageKey: string
  /** 上栏初始高度比例，默认 0.45 */
  initialRatio?: number
}

const MIN_RATIO = 0.15
const MAX_RATIO = 0.85
const HEADER_H = 30

function loadSplit(key: string): { ratio: number; collapsed: 'top' | 'bottom' | null } | null {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return null
    const s = JSON.parse(raw)
    if (typeof s.ratio === 'number' && s.ratio >= MIN_RATIO && s.ratio <= MAX_RATIO) {
      return { ratio: s.ratio, collapsed: s.collapsed === 'top' || s.collapsed === 'bottom' ? s.collapsed : null }
    }
  } catch { /* ignore */ }
  return null
}

/** 折叠箭头：V14 线性 chevron（原实心三角已统一替换） */
function CollapseArrow({ up }: { up: boolean }) {
  return up
    ? <ChevronUp className="h-3.5 w-3.5" strokeWidth={1.8} aria-hidden />
    : <ChevronDown className="h-3.5 w-3.5" strokeWidth={1.8} aria-hidden />
}

/** 单栏细头部：标题 + 折叠箭头，双击折叠（对应草图的「双击隐藏」） */
function PaneHeader({ title, collapsed, onToggle }: { title: string; collapsed: boolean; onToggle: () => void }) {
  return (
    <div
      className="shrink-0 flex items-center justify-between gap-2 px-3 select-none cursor-pointer hover:bg-[#2a2a2a]/60 transition-colors"
      style={{ height: HEADER_H, borderBottom: '1px solid #1e1e1e' }}
      onDoubleClick={onToggle}
      title="双击折叠 / 展开"
    >
      <span className="min-w-0 truncate text-[14px] font-semibold tracking-wide text-[#a8a8a8]" title={title}>{title}</span>
      <button
        onClick={onToggle}
        className="text-[#6a6a6a] hover:text-[#f59e0b] transition-colors p-1"
        title={collapsed ? '展开' : '折叠'}
      >
        <CollapseArrow up={collapsed} />
      </button>
    </div>
  )
}

export default function VerticalSplit({ top, bottom, topTitle, bottomTitle, storageKey, initialRatio = 0.45 }: VerticalSplitProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const saved = useRef(loadSplit(storageKey))
  const [ratio, setRatio] = useState(saved.current?.ratio ?? initialRatio)
  const [collapsed, setCollapsed] = useState<'top' | 'bottom' | null>(saved.current?.collapsed ?? null)
  const [containerH, setContainerH] = useState(0)

  useEffect(() => {
    if (!containerRef.current) return
    const el = containerRef.current
    const update = () => setContainerH(el.clientHeight)
    update()
    const ro = new ResizeObserver(update)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // 持久化
  useEffect(() => {
    try { localStorage.setItem(storageKey, JSON.stringify({ ratio, collapsed })) } catch { /* ignore */ }
  }, [storageKey, ratio, collapsed])

  const toggle = useCallback((pane: 'top' | 'bottom') => {
    setCollapsed((prev) => (prev === pane ? null : pane))
  }, [])

  // 分割线拖拽（可上下拖拽自由分割窗口尺寸）
  const startResize = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault()
      const startY = e.clientY
      const h = containerRef.current?.clientHeight ?? containerH
      const startRatio = ratio
      const onMove = (ev: MouseEvent) => {
        const dyPct = (ev.clientY - startY) / h
        setRatio(Math.max(MIN_RATIO, Math.min(MAX_RATIO, startRatio + dyPct)))
      }
      const onUp = () => {
        document.removeEventListener('mousemove', onMove)
        document.removeEventListener('mouseup', onUp)
      }
      document.addEventListener('mousemove', onMove)
      document.addEventListener('mouseup', onUp)
    },
    [ratio, containerH],
  )

  const topCollapsed = collapsed === 'top'
  const bottomCollapsed = collapsed === 'bottom'
  /** 两栏都展开时才允许拖拽分割 */
  const canDragSeam = !topCollapsed && !bottomCollapsed

  return (
    // 不留内边距：卡片直接吃满面板宽度，使「编辑器↔追踪卡」「追踪卡↔窗口右缘」的可见间距
    // 都等于面板主间隙 12px（全站统一 12px 节奏）。上/下同样为 0，首张顶到面板上沿、末张顶到下沿。
    <div ref={containerRef} className="h-full w-full flex flex-col min-h-0 bg-black">
      {/* 上栏：下栏折叠时自动占满，上栏折叠时缩为细条 */}
      <div
        className="flex flex-col min-h-0 overflow-hidden rounded-lg border border-[#333] bg-[#141414]"
        style={
          topCollapsed
            ? { flex: 'none' }
            : bottomCollapsed
              ? { flex: '1 1 0', height: 'auto' }
              : { height: `${ratio * 100}%`, flex: 'none' }
        }
      >
        {topCollapsed ? (
          <button
            onClick={() => toggle('top')}
            className="w-full flex items-center justify-between px-3 select-none hover:bg-[#2a2a2a]/60 transition-colors"
            style={{ height: HEADER_H }}
            title="展开上栏"
          >
            <span className="text-[14px] text-[#777] font-medium">{topTitle}</span>
            <span className="text-[#6a6a6a]"><CollapseArrow up={false} /></span>
          </button>
        ) : (
          <>
            <PaneHeader title={topTitle} collapsed={false} onToggle={() => toggle('top')} />
            <div className="flex-1 min-h-0 overflow-hidden relative">
              {top}
              {/* 上/下边缘渐隐：内容滚到可视区边沿时淡出，避免半行硬截断 */}
              <div className="edge-fade edge-fade-top" />
              <div className="edge-fade edge-fade-bottom" />
            </div>
          </>
        )}
      </div>

      {/* 分割带：始终保留 12px 间距（与全站面板间距一致），折叠任一栏也不塌陷；
          仅在两栏都展开时可拖拽，拖拽线默认隐藏、悬停时浅色提示 */}
      <div
        className={`shrink-0 relative h-3 group/div ${canDragSeam ? 'cursor-row-resize' : ''}`}
        onMouseDown={canDragSeam ? startResize : undefined}
        title={canDragSeam ? '上下拖拽分割窗口尺寸' : undefined}
      >
        {canDragSeam && (
          <div className="absolute inset-x-0 top-1/2 -translate-y-1/2 h-px bg-transparent group-hover/div:bg-[#f59e0b]/50 transition-colors" />
        )}
      </div>

      {/* 下栏：上栏折叠时占满剩余空间，下栏折叠时缩为细条 */}
      <div
        className="flex flex-col min-h-0 overflow-hidden rounded-lg border border-[#333] bg-[#141414]"
        style={{ flex: bottomCollapsed ? 'none' : '1 1 0' }}
      >
        {bottomCollapsed ? (
          <button
            onClick={() => toggle('bottom')}
            className="w-full flex items-center justify-between px-3 select-none hover:bg-[#2a2a2a]/60 transition-colors"
            style={{ height: HEADER_H }}
            title="展开下栏"
          >
            <span className="text-[14px] text-[#777] font-medium">{bottomTitle}</span>
            <span className="text-[#6a6a6a]"><CollapseArrow up /></span>
          </button>
        ) : (
          <>
            <PaneHeader title={bottomTitle} collapsed={false} onToggle={() => toggle('bottom')} />
            <div className="flex-1 min-h-0 overflow-hidden">{bottom}</div>
          </>
        )}
      </div>
    </div>
  )
}
