import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { GripVertical } from 'lucide-react'

/* ─────────────────────────  三卡片面板容器（状态持久化）  ───────────────────────── */

export interface PanelDef {
  id: string
  title: string
  shortLabel: string
  content: ReactNode
  designWidth: number | null
  fixedWidth?: number
  resizable?: boolean
  fill?: boolean       // 始终作为 flex 填充面板，自动占满剩余空间，不持久化宽度
  bg?: string
  hideHeader?: boolean
  unframed?: boolean   // 去掉外框（边框+圆角），由内容自带边框（如右栏 VerticalSplit 的两张子卡片）
  minWidth?: number    // 硬性最小宽度（px）：拖拽/浏览器缩放都不得低于此值（如编辑器 = 文字块宽 + 左右边距）
  dragStripOverlay?: boolean // 顶部拖拽条改为悬浮层：不占布局高度，内容可顶到面板上沿（右栏用）
}

interface Props {
  panels: PanelDef[]
}

const GAP = 12
const COLLAPSED_WIDTH = 48
const HEADER_HEIGHT = 38
/** 主内容卡的 id：正文/常规布局恒为 main，写作台三列组合的工作台卡为 mvp-workbench，
 *  自动发布两列组合的工作台卡为 publish-workbench，千章守护两列组合的工作台卡为
 *  guard-workbench，建书三列组合的聊天卡为 create-chat，
 *  兼容旧持久化/旧布局里的 editor。六者都参与「分隔条相邻即可拖拽」判定。 */
const MAIN_PANEL_IDS = new Set(['main', 'editor', 'mvp-workbench', 'publish-workbench', 'guard-workbench', 'create-chat'])
const MIN_WIDTH = 160
const STORAGE_KEY = 'nww_v12_panels'

interface PersistedState {
  order: string[]
  widths: Record<string, number>
  widthsPx: Record<string, number>
  collapsed: Record<string, boolean>
  /** 按「面板集合签名」分组的顺序：不同布局（如 正文三栏 / 发布两栏）互不干扰 */
  orders?: Record<string, string[]>
}

function loadState(): PersistedState | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? JSON.parse(raw) as PersistedState : null
  } catch { return null }
}

function saveState(state: PersistedState) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)) } catch { /* ignore */ }
}

function PanelHeaderIcon() {
  return <GripVertical className="h-3.5 w-3.5" strokeWidth={1.8} aria-hidden />
}

export default function ResizablePanels({ panels }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [containerWidth, setContainerWidth] = useState(0)

  /** 面板集合签名：同一签名 = 同一套布局，状态各存各的（避免 正文三栏 / 发布两栏 互相覆盖） */
  const panelSignature = panels.map((p) => p.id).join('|')

  // 用面板 id 作为持久化的 key，内部仍用索引数组操作
  const [order, setOrder] = useState<number[]>(() => {
    const persisted = loadState()
    // 优先取本布局自己的顺序；老记录只有单一 order 字段，长度匹配时沿用
    const savedOrder = persisted?.orders?.[panelSignature]
      ?? (persisted && persisted.order?.length === panels.length ? persisted.order : undefined)
    if (savedOrder && savedOrder.length === panels.length) {
      const idToIdx = new Map(panels.map((p, i) => [p.id, i]))
      const mapped = savedOrder.map((id) => idToIdx.get(id)).filter((v): v is number => v !== undefined)
      // 确保无重复且长度匹配
      if (mapped.length === panels.length && new Set(mapped).size === panels.length) return mapped
    }
    return panels.map((_, i) => i)
  })

  const [widthPct, setWidthPct] = useState<(number | null)[]>(() => {
    const persisted = loadState()
    return panels.map((p) => {
      if (p.fill) return null // 填充面板始终用 flex，不设百分比
      if (p.fixedWidth != null) return null // 固定宽度面板不用百分比
      const saved = persisted?.widths[p.id]
      if (saved != null && saved > 1 && saved < 95) return saved
      return null
    })
  })

  // 固定像素宽度（不随容器变化）
  const [widthPx, setWidthPx] = useState<(number | null)[]>(() => {
    const persisted = loadState()
    return panels.map((p) => {
      if (p.fill) return null // 填充面板始终用 flex，不设固定像素
      if (p.fixedWidth == null) return null
      const saved = persisted?.widthsPx?.[p.id]
      if (saved != null && saved >= MIN_WIDTH && saved < 2000) return saved
      return p.fixedWidth
    })
  })

  const [collapsed, setCollapsed] = useState<boolean[]>(() => {
    const persisted = loadState()
    return panels.map((p) => persisted?.collapsed[p.id] ?? false)
  })

  const [resizingPair, setResizingPair] = useState<number | null>(null)
  const [dragIdx, setDragIdx] = useState<number | null>(null)
  const [dragOverIdx, setDragOverIdx] = useState<number | null>(null)
  const [widthsInitialized, setWidthsInitialized] = useState(false)

  // 面板集合变化时（例：切到「自动发布」的 资产卡+发布卡 两栏布局）按新面板重建内部状态。
  // 不做这一步，order/widthPct/widthPx/collapsed 仍是旧长度，渲染时会取到 undefined 面板而整页崩。
  useEffect(() => {
    const persisted = loadState()
    setOrder((prev) => {
      const idToIdx = new Map(panels.map((p, i) => [p.id, i]))
      const kept = prev
        .map((i) => panels[i]?.id)
        .filter((id): id is string => !!id)
        .map((id) => idToIdx.get(id))
        .filter((v): v is number => v !== undefined)
      for (let i = 0; i < panels.length; i++) if (!kept.includes(i)) kept.push(i)
      return kept
    })
    setWidthPct(panels.map((p) => {
      if (p.fill || p.fixedWidth != null) return null
      const saved = persisted?.widths[p.id]
      return saved != null && saved > 1 && saved < 95 ? saved : null
    }))
    setWidthPx(panels.map((p) => {
      if (p.fill || p.fixedWidth == null) return null
      const saved = persisted?.widthsPx?.[p.id]
      return saved != null && saved >= MIN_WIDTH && saved < 2000 ? saved : p.fixedWidth
    }))
    setCollapsed(panels.map((p) => persisted?.collapsed[p.id] ?? false))
    setWidthsInitialized(false) // 让设计宽度重新换算为百分比
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [panelSignature])

  // 监听容器宽度
  useEffect(() => {
    if (!containerRef.current) return
    const el = containerRef.current
    const update = () => setContainerWidth(el.clientWidth)
    update()
    const ro = new ResizeObserver(update)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // 首次容器宽度就绪时，将设计像素换算为百分比（仅未被持久化覆盖的列）
  // 同时做归一化：固定像素面板 + 百分比面板 + gaps 的总宽度不得超过容器
  useEffect(() => {
    if (containerWidth <= 0 || widthsInitialized) return
    setWidthPct((prev) => {
      // 第一步：算出每个面板的目标百分比
      const rawPct = prev.map((w, i) => {
        if (w != null) return w
        const p = panels[i]
        if (p.fill) return null // 填充面板保持 flex
        if (p.designWidth != null) return (p.designWidth / containerWidth) * 100
        return null
      })

      // 第二步：计算固定像素面板和 gaps 占用的百分比
      let fixedPx = 0
      panels.forEach((p, i) => {
        if (p.fixedWidth != null) fixedPx += widthPx[i] ?? p.fixedWidth
      })
      const gapsPx = (panels.length - 1) * GAP
      const occupiedPct = ((fixedPx + gapsPx) / containerWidth) * 100

      // 第三步：百分比面板总和
      let pctSum = 0
      rawPct.forEach((v) => { if (v != null) pctSum += v })

      // 第四步：如果超出可用空间，按比例缩减所有百分比面板
      // （flex 填充面板的硬性最小宽度也计入占用，防止恢复持久化宽度时把编辑器挤破）
      let flexMinPct = 0
      panels.forEach((p, i) => {
        if (p.minWidth && widthPx[i] == null && widthPct[i] == null) {
          flexMinPct += (p.minWidth / containerWidth) * 100
        }
      })
      const availablePct = Math.max(0, 100 - occupiedPct - flexMinPct)
      if (pctSum > availablePct && availablePct > 0) {
        const scale = availablePct / pctSum
        return rawPct.map((v) => (v != null ? v * scale : v))
      }
      return rawPct
    })
    setWidthsInitialized(true)
  }, [containerWidth, widthsInitialized, panels, widthPx])

  // 持久化：状态变化时保存。
  // 必须「合并」而非整体覆盖——否则切到发布两栏布局时会把正文三栏的右栏宽度抹掉，
  // 返回后右栏只能退回设计宽度并触发归一化缩放，资产卡宽度每次往返都会被吃掉一点。
  useEffect(() => {
    const prev = loadState()
    const idOrder = order.map((i) => panels[i]?.id).filter(Boolean) as string[]
    const widths: Record<string, number> = { ...(prev?.widths ?? {}) }
    const widthsPx: Record<string, number> = { ...(prev?.widthsPx ?? {}) }
    const collapsedMap: Record<string, boolean> = { ...(prev?.collapsed ?? {}) }
    // 切换布局的过渡帧里内部状态还停留在旧长度，此时不落盘，避免写入错位/残缺记录
    if (order.length !== panels.length || widthPct.length !== panels.length) return
    panels.forEach((p, i) => {
      if (p.fill) return // 填充面板不持久化宽度
      if (widthPct[i] != null) widths[p.id] = widthPct[i]!
      if (widthPx[i] != null) widthsPx[p.id] = widthPx[i]!
      if (collapsed[i]) collapsedMap[p.id] = true
      else delete collapsedMap[p.id]
    })
    saveState({
      order: prev?.order ?? idOrder,
      orders: { ...(prev?.orders ?? {}), [panelSignature]: idOrder },
      widths,
      widthsPx,
      collapsed: collapsedMap,
    })
  }, [order, widthPct, widthPx, collapsed, panels, panelSignature])

  const displayPanels = order.map((i) => panels[i])

  const toggleCollapse = useCallback((panelIdx: number) => {
    setCollapsed((prev) => { const next = [...prev]; next[panelIdx] = !next[panelIdx]; return next })
  }, [])

  // 判断分隔条是否可拖拽：凡涉及主内容卡的相邻面板对（侧栏↔主卡、主卡↔右栏）均可拖拽
  const canResize = useCallback((pairIdx: number): boolean => {
    const leftIdx = order[pairIdx]
    const rightIdx = order[pairIdx + 1]
    if (collapsed[leftIdx] || collapsed[rightIdx]) return false
    const leftId = panels[leftIdx]?.id
    const rightId = panels[rightIdx]?.id
    return MAIN_PANEL_IDS.has(leftId ?? '') || MAIN_PANEL_IDS.has(rightId ?? '')
  }, [order, collapsed, panels])

  // 分隔条拖拽改宽（支持固定像素宽度和百分比，不可调整面板只改对侧）
  const startResize = useCallback(
    (pairIdx: number, e: React.MouseEvent) => {
      e.preventDefault()
      e.stopPropagation()
      const leftIdx = order[pairIdx]
      const rightIdx = order[pairIdx + 1]
      if (!canResize(pairIdx)) return

      const leftIsFill = panels[leftIdx]?.fill === true
      const leftResizable = !leftIsFill && panels[leftIdx]?.resizable !== false
      const leftIsPx = widthPx[leftIdx] != null
      const rightIsPx = widthPx[rightIdx] != null
      const rightIsFlex = !rightIsPx && widthPct[rightIdx] == null

      setResizingPair(pairIdx)
      const startX = e.clientX
      const cw = containerRef.current?.clientWidth ?? containerWidth

      // 左侧面板不可调整大小：拖拽只改变右侧面板宽度
      if (!leftResizable) {
        if (rightIsPx) {
          // 右侧是固定像素，拖拽改变右侧像素值（向左拖增大右侧，向右拖减小右侧）
          const startRightPx = widthPx[rightIdx]!
          const onMove = (ev: MouseEvent) => {
            const dx = startX - ev.clientX // 反向：分隔条左移=右侧变宽
            const newRight = Math.max(MIN_WIDTH, startRightPx + dx)
            setWidthPx((prev) => { const next = [...prev]; next[rightIdx] = newRight; return next })
          }
          const onUp = () => {
            setResizingPair(null)
            document.removeEventListener('mousemove', onMove)
            document.removeEventListener('mouseup', onUp)
          }
          document.addEventListener('mousemove', onMove)
          document.addEventListener('mouseup', onUp)
          return
        }
        // 右侧是百分比或flex，拖拽改变右侧百分比
        const startRightPct = rightIsFlex ? null : widthPct[rightIdx]!
        const minPct = (MIN_WIDTH / cw) * 100

        // 左侧填充面板（编辑器）声明了硬性 minWidth → 反推右侧面板的百分比上限，
        // 使拖拽在编辑器触及最小宽度时立即「锁死」，不再继续压缩正文块。
        const leftMinPx = panels[leftIdx]?.minWidth ?? 0
        let maxRightPx = Infinity
        if (leftMinPx > 0) {
          let othersPx = 0
          order.forEach((idx) => {
            if (idx === leftIdx || idx === rightIdx) return
            if (collapsed[idx]) othersPx += COLLAPSED_WIDTH
            else if (widthPx[idx] != null) othersPx += widthPx[idx]!
            else if (widthPct[idx] != null) othersPx += (widthPct[idx]! / 100) * cw
          })
          maxRightPx = cw - othersPx - (order.length - 1) * GAP - leftMinPx
        }
        const maxPct = maxRightPx === Infinity ? Infinity : Math.max(minPct, (maxRightPx / cw) * 100)

        const onMove = (ev: MouseEvent) => {
          const dxPct = ((startX - ev.clientX) / cw) * 100 // 反向
          if (rightIsFlex) {
            // flex 面板：需要给它设置一个百分比初始值
            const currentRightPct = startRightPct ?? (100 - (widthPct[leftIdx] ?? 0))
            const newRight = Math.min(maxPct, Math.max(minPct, currentRightPct + dxPct))
            setWidthPct((prev) => { const next = [...prev]; next[rightIdx] = newRight; return next })
          } else {
            const newRight = Math.min(maxPct, Math.max(minPct, widthPct[rightIdx]! + dxPct))
            setWidthPct((prev) => { const next = [...prev]; next[rightIdx] = newRight; return next })
          }
        }
        const onUp = () => {
          setResizingPair(null)
          document.removeEventListener('mousemove', onMove)
          document.removeEventListener('mouseup', onUp)
        }
        document.addEventListener('mousemove', onMove)
        document.addEventListener('mouseup', onUp)
        return
      }

      // 左侧可调整，用原逻辑
      if (!leftIsPx && widthPct[leftIdx] == null) return

      if (leftIsPx) {
        const startLeftPx = widthPx[leftIdx]!
        const onMove = (ev: MouseEvent) => {
          const dx = ev.clientX - startX
          const newLeft = Math.max(MIN_WIDTH, startLeftPx + dx)
          setWidthPx((prev) => { const next = [...prev]; next[leftIdx] = newLeft; return next })
        }
        const onUp = () => {
          setResizingPair(null)
          document.removeEventListener('mousemove', onMove)
          document.removeEventListener('mouseup', onUp)
        }
        document.addEventListener('mousemove', onMove)
        document.addEventListener('mouseup', onUp)
        return
      }

      const startLeftPct = widthPct[leftIdx]!
      const startRightPct = rightIsFlex ? 0 : widthPct[rightIdx]!
      const totalPct = startLeftPct + startRightPct
      const minPct = (MIN_WIDTH / cw) * 100

      // 右侧是 flex 填充面板且声明了硬性最小宽度（如编辑器 800）：
      // 反推左侧面板的宽度上限，拖拽时右侧触及最小宽度即「锁死」，不可再压缩。
      let maxLeftPct = Infinity
      const rightMinPx = panels[rightIdx]?.minWidth ?? 0
      if (rightIsFlex && rightMinPx > 0) {
        let othersPx = 0
        order.forEach((idx) => {
          if (idx === leftIdx || idx === rightIdx) return
          if (collapsed[idx]) othersPx += COLLAPSED_WIDTH
          else if (widthPx[idx] != null) othersPx += widthPx[idx]!
          else if (widthPct[idx] != null) othersPx += (widthPct[idx]! / 100) * cw
        })
        const gapsPx = (order.length - 1) * GAP
        maxLeftPct = Math.max(minPct, ((cw - othersPx - gapsPx - rightMinPx) / cw) * 100)
      }

      const onMove = (ev: MouseEvent) => {
        const dxPct = ((ev.clientX - startX) / cw) * 100
        if (rightIsFlex) {
          const newLeft = Math.min(maxLeftPct, Math.max(minPct, startLeftPct + dxPct))
          setWidthPct((prev) => { const next = [...prev]; next[leftIdx] = newLeft; return next })
        } else {
          const newLeft = Math.max(minPct, Math.min(startLeftPct + dxPct, totalPct - minPct))
          const newRight = totalPct - newLeft
          setWidthPct((prev) => {
            const next = [...prev]
            next[leftIdx] = newLeft
            next[rightIdx] = newRight
            return next
          })
        }
      }

      const onUp = () => {
        setResizingPair(null)
        document.removeEventListener('mousemove', onMove)
        document.removeEventListener('mouseup', onUp)
      }

      document.addEventListener('mousemove', onMove)
      document.addEventListener('mouseup', onUp)
    },
    [order, collapsed, widthPct, widthPx, containerWidth, panels, canResize],
  )

  // 拖拽排序
  const handleDragStart = useCallback((e: React.DragEvent, panelIdx: number) => {
    setDragIdx(panelIdx)
    e.dataTransfer.effectAllowed = 'move'
    e.dataTransfer.setData('text/plain', String(panelIdx))
  }, [])

  const handleDragOver = useCallback((e: React.DragEvent, panelIdx: number) => {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
    if (dragOverIdx !== panelIdx) setDragOverIdx(panelIdx)
  }, [dragOverIdx])

  const handleDragLeave = useCallback(() => setDragOverIdx(null), [])

  const handleDrop = useCallback((e: React.DragEvent, targetIdx: number) => {
    e.preventDefault()
    if (dragIdx === null || dragIdx === targetIdx) { setDragIdx(null); setDragOverIdx(null); return }
    setOrder((prev) => {
      const next = [...prev]
      const from = next.indexOf(dragIdx)
      const to = next.indexOf(targetIdx)
      if (from === -1 || to === -1) return prev
      ;[next[from], next[to]] = [next[to], next[from]]
      return next
    })
    setDragIdx(null)
    setDragOverIdx(null)
  }, [dragIdx])

  const handleDragEnd = useCallback(() => { setDragIdx(null); setDragOverIdx(null) }, [])

  return (
    <div ref={containerRef} className="flex-1 min-w-0 h-full flex items-stretch relative overflow-hidden" style={{ gap: GAP }}>
      {displayPanels.map((panel, displayIdx) => {
        const panelIdx = order[displayIdx]
        // 防御：面板集合刚变化、内部状态尚未重算时跳过越界项，避免 undefined.bg 崩掉整个应用
        if (!panel) return null
        const isCollapsed = collapsed[panelIdx]
        const pct = widthPct[panelIdx]
        const px = widthPx[panelIdx]
        const bg = panel.bg ?? '#141414'
        const isDragging = dragIdx === panelIdx
        const isDragOver = dragOverIdx === panelIdx && dragIdx !== panelIdx
        const hideHeader = panel.hideHeader ?? false

        const widthStyle = isCollapsed
          ? `${COLLAPSED_WIDTH}px`
          : px != null
            ? `${px}px`
            : pct != null
              ? `${pct}%`
              : undefined

        const isFlex = px == null && pct == null && !isCollapsed
        // 固定像素宽度面板（如侧边栏）：刚性，不随容器收缩（避免编辑器 minWidth 挤压它）
        const isFixedPx = !isFlex && panel.fixedWidth != null && px != null

        return (
          <div
            key={panel.id}
            className={`h-full flex flex-col relative overflow-hidden transition-opacity ${panel.unframed ? '' : 'rounded-lg border'} ${isDragging ? 'opacity-40' : ''}`}
            style={{
              width: widthStyle,
              flex: isFlex ? '1 1 0' : isFixedPx ? '0 0 auto' : '0 1 auto',
              minWidth: isCollapsed ? COLLAPSED_WIDTH : (panel.minWidth ?? 0),
              background: bg,
              borderColor: isDragOver ? '#f59e0b' : '#2a2a2a',
              boxShadow: isDragOver ? '0 0 0 1px #f59e0b' : 'none',
            }}
            onDragOver={(e) => handleDragOver(e, panelIdx)}
            onDragLeave={handleDragLeave}
            onDrop={(e) => handleDrop(e, panelIdx)}
          >
            {!isCollapsed && (
              <div
                draggable
                onDragStart={(e) => handleDragStart(e, panelIdx)}
                onDragEnd={handleDragEnd}
                className={
                  panel.dragStripOverlay
                    ? 'absolute inset-x-0 top-0 z-30 h-1.5 select-none cursor-grab active:cursor-grabbing'
                    : `shrink-0 flex items-center justify-between px-3 select-none cursor-grab active:cursor-grabbing group ${hideHeader ? 'h-1.5 bg-transparent hover:bg-[#2a2a2a]/50' : ''}`
                }
                style={hideHeader || panel.dragStripOverlay ? undefined : {
                  height: HEADER_HEIGHT,
                  background: bg,
                  borderBottom: '1px solid #1e1e1e',
                  borderTopLeftRadius: 8,
                  borderTopRightRadius: 8,
                }}
                onDoubleClick={() => toggleCollapse(panelIdx)}
                title="拖拽移动位置 · 双击折叠"
              >
                {!hideHeader && (
                  <>
                    <span className="text-[14px] font-semibold tracking-wide text-[#a8a8a8] group-hover:text-[#d0d0d0] transition-colors">
                      {panel.title}
                    </span>
                    <button className="text-[#6a6a6a] hover:text-[#b0b0b0] transition-colors opacity-0 group-hover:opacity-100">
                      <PanelHeaderIcon />
                    </button>
                  </>
                )}
              </div>
            )}

            {isCollapsed && (
              <div className="flex-1 flex items-center justify-center" onDoubleClick={() => toggleCollapse(panelIdx)}>
                <span className="text-[14px] text-[#888] font-medium tracking-wider [writing-mode:vertical-rl] rotate-180 cursor-pointer">
                  {panel.shortLabel}
                </span>
              </div>
            )}

            {!isCollapsed && (
              <div className="flex-1 min-h-0 overflow-hidden" style={{ background: bg }}>
                {panel.content}
              </div>
            )}
          </div>
        )
      })}

      {/* 分隔条 */}
      {(() => {
        // 先计算每个面板的实际渲染宽度（正确处理 flex 填充面板）
        const actualWidths: number[] = []
        let fixedTotal = 0
        let flexCount = 0
        order.forEach((idx) => {
          if (collapsed[idx]) {
            actualWidths.push(COLLAPSED_WIDTH)
            fixedTotal += COLLAPSED_WIDTH
          } else if (widthPx[idx] != null) {
            actualWidths.push(widthPx[idx]!)
            fixedTotal += widthPx[idx]!
          } else if (widthPct[idx] != null) {
            const w = (widthPct[idx]! / 100) * containerWidth
            actualWidths.push(w)
            fixedTotal += w
          } else {
            actualWidths.push(-1) // 标记为 flex 填充面板
            flexCount++
          }
        })
        const gapsTotal = (order.length - 1) * GAP
        let flexWidth = flexCount > 0 ? Math.max(0, (containerWidth - fixedTotal - gapsTotal) / flexCount) : 0
        // 填充面板若声明了 minWidth（如编辑器文本块基线 800），其渲染宽度不得低于该值，
        // 否则分隔条会偏离面板真实右边缘（因为 flex 数学不受 CSS min-width 约束）。
        if (flexCount > 0) {
          let maxMin = 0
          order.forEach((idx) => {
            if (!collapsed[idx] && widthPx[idx] == null && widthPct[idx] == null) {
              maxMin = Math.max(maxMin, panels[idx]?.minWidth ?? 0)
            }
          })
          if (maxMin > flexWidth) flexWidth = maxMin
        }
        const finalWidths = actualWidths.map((w) => (w < 0 ? flexWidth : w))

        return displayPanels.slice(0, -1).map((_, pairIdx) => {
          const isResizing = resizingPair === pairIdx
          let leftPx = 0
          for (let i = 0; i <= pairIdx; i++) {
            leftPx += finalWidths[i]
          }
          leftPx += pairIdx * GAP
          const dividerLeft = leftPx + GAP / 2 - 2

          const resizable = canResize(pairIdx)
          return (
            <div
              key={`divider-${pairIdx}`}
              className={`absolute top-3 bottom-3 z-20 rounded transition-colors ${resizable ? 'cursor-col-resize' : 'cursor-default'} ${isResizing ? 'bg-[#f59e0b]' : resizable ? 'hover:bg-[#f59e0b]/50' : ''}`}
              style={{ left: dividerLeft, width: 4 }}
              onMouseDown={(e) => { if (resizable) startResize(pairIdx, e) }}
            />
          )
        })
      })()}
    </div>
  )
}
