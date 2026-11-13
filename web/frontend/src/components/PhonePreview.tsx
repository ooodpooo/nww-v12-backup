import { useCallback, useEffect, useRef, useState } from 'react'
import { X } from 'lucide-react'
import { MdView } from './md'
import { parseChapterName } from './AssetTree'

/**
 * 手机预览模式（手机版模式）：
 * 切换后桌面正文隐藏，正文只显示在编辑器正文区中央的「手机造型容器」里，
 * 模拟读者手机阅读排版。支持：
 *  - 机型三档：iPhone 14 Pro 393×852 / iPhone SE 375×667 / Android 360×800
 *  - 默认按舞台（编辑器正文区）等比缩放适配
 *  - 右下角手柄拖动等比缩放（0.3× 起，上限为充满舞台、不超出页面高度），文字随框同步缩放
 *  - 双击手柄恢复自动适配；切机型保留当前缩放比例
 *  - 正文复用 MdView 只读渲染，字号/字体跟随桌面工具栏
 */

const DEVICES = [
  { id: 'pro', label: 'iPhone 14 Pro', w: 393, h: 852 },
  { id: 'se', label: 'iPhone SE', w: 375, h: 667 },
  { id: 'android', label: 'Android', w: 360, h: 800 },
] as const
type DeviceId = (typeof DEVICES)[number]['id']

const RESIZE_MIN = 0.3
const RESIZE_MAX = 1.5

interface Props {
  /** 当前书籍名称（头栏展示，读取真实书名） */
  bookTitle: string
  /** 章节标题（头栏展示） */
  title: string
  /** 章号部分，如「第3章」；编辑态固定显示，只有标题部分可编辑 */
  titleNum?: string
  /** 标题部分（不含章号） */
  titleText?: string
  /** 标题提交回调（编辑态 + 标准章节命名时传入） */
  onTitleChange?: (title: string) => void | Promise<void>
  /** Markdown 正文 */
  content: string
  /** 是否可编辑（跟随工具栏 阅读/编辑 切换；手机容器即编辑器本身） */
  editable: boolean
  /** 编辑输入回调（编辑时更新正文内容） */
  onInput: (md: string) => void
  /** 跟随桌面工具栏字号（px） */
  fontSize: number
  /** 跟随桌面工具栏字体 */
  fontFamily: string
  /** 章节位置标签，如「第 1 / 3 章」 */
  posLabel: string
  /** 章节目录（头栏菜单查看大纲，点击跳转章节） */
  chapters: { path: string; name: string }[]
  /** 当前章节路径（目录高亮） */
  currentPath: string
  onNavigate: (path: string) => void
  hasPrev: boolean
  hasNext: boolean
  onPrev: () => void
  onNext: () => void
  onExit: () => void
}

export default function PhonePreview({ bookTitle, title, titleNum, titleText, onTitleChange, content, editable, onInput, fontSize, fontFamily, posLabel, chapters, currentPath, onNavigate, hasPrev, hasNext, onPrev, onNext, onExit }: Props) {
  const [deviceId, setDeviceId] = useState<DeviceId>('pro')
  // 手机预览内标题编辑：章号固定，仅标题部分可编辑（与桌面章头一致）
  const [titleDraft, setTitleDraft] = useState(titleText ?? '')
  useEffect(() => { setTitleDraft(titleText ?? '') }, [titleText])
  const commitTitle = useCallback(() => {
    const next = titleDraft.trim()
    if (!onTitleChange || !next || next === titleText) { setTitleDraft(titleText ?? ''); return }
    void onTitleChange(next)
  }, [onTitleChange, titleDraft, titleText])
  /** 头栏菜单 → 章节目录浮层 */
  const [menuOpen, setMenuOpen] = useState(false)
  const stageRef = useRef<HTMLDivElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const phoneRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<{ cx: number; cy: number; halfDiag: number } | null>(null)

  const device = DEVICES.find((d) => d.id === deviceId)!

  /* 应用缩放：手机框（布局尺寸 w×h）以左上角为原点 transform scale，
     包裹层尺寸 = 缩放后尺寸，包裹层居中于舞台 → 手机视觉始终居中，文字随 scale 同步缩放 */
  const applyScale = useCallback((s: number) => {
    const el = phoneRef.current
    const wrap = wrapRef.current
    if (!el || !wrap) return
    el.style.width = device.w + 'px'
    el.style.height = device.h + 'px'
    el.style.transform = 'scale(' + s + ')'
    wrap.style.width = Math.round(device.w * s) + 'px'
    wrap.style.height = Math.round(device.h * s) + 'px'
  }, [device])

  /* 自动适配：按舞台可用空间等比缩放（留边距容纳手柄） */
  const fit = useCallback(() => {
    const stage = stageRef.current
    if (!stage || stage.clientWidth === 0) return
    const s = Math.min((stage.clientWidth - 24) / device.w, (stage.clientHeight - 24) / device.h, 1)
    applyScale(Math.max(0.1, s))
  }, [device, applyScale])

  /* 放大上限：手机框（含手柄）不超出舞台（编辑器页面高度），且不超过 1.5× */
  const maxFitScale = useCallback(() => {
    const stage = stageRef.current
    if (!stage || stage.clientWidth === 0) return RESIZE_MAX
    return Math.min(RESIZE_MAX, (stage.clientWidth - 20) / device.w, (stage.clientHeight - 20) / device.h)
  }, [device])

  // 挂载后首次适配
  useEffect(() => { fit() }, [fit])

  // Esc 退出预览
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onExit() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onExit])

  // 舞台尺寸变化（窗口/面板拖拽）时重新适配
  useEffect(() => {
    const stage = stageRef.current
    if (!stage) return
    const ro = new ResizeObserver(() => fit())
    ro.observe(stage)
    return () => ro.disconnect()
  }, [fit])

  // 切换机型：保留当前缩放比例
  const switchDevice = (id: DeviceId) => {
    setDeviceId(id)
    const s = phoneRef.current ? parseFloat(phoneRef.current.style.transform.replace(/[^0-9.]/g, '') || '1') : 1
    requestAnimationFrame(() => {
      const el = phoneRef.current
      const wrap = wrapRef.current
      const d = DEVICES.find((x) => x.id === id)!
      if (!el || !wrap) return
      el.style.width = d.w + 'px'
      el.style.height = d.h + 'px'
      el.style.transform = 'scale(' + s + ')'
      wrap.style.width = Math.round(d.w * s) + 'px'
      wrap.style.height = Math.round(d.h * s) + 'px'
    })
  }

  /* 右下角拖拽等比缩放：以手机中心为锚，按指针到中心距离换算比例 */
  const onResizePointerDown = (e: React.PointerEvent) => {
    e.preventDefault()
    const el = phoneRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    dragRef.current = {
      cx: r.left + r.width / 2,
      cy: r.top + r.height / 2,
      halfDiag: Math.hypot(device.w, device.h) / 2,
    }
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
    document.body.style.cursor = 'se-resize'
    const move = (ev: PointerEvent) => {
      const d = dragRef.current
      if (!d) return
      const dist = Math.hypot(ev.clientX - d.cx, ev.clientY - d.cy)
      const s = Math.min(maxFitScale(), Math.max(RESIZE_MIN, dist / d.halfDiag))
      applyScale(s)
    }
    const up = () => {
      dragRef.current = null
      document.body.style.cursor = ''
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  const isAndroid = deviceId === 'android'
  const isSE = deviceId === 'se'

  /* 章节目录项显示名：与章节头一致（第N章 标题），解析不了时显示原名 */
  const chapterLabel = (name: string) => {
    const parsed = parseChapterName(name)
    return parsed ? `第${parseInt(parsed.num, 10)}章 ${parsed.title}` : name.replace(/\.md$/, '')
  }

  return (
    <div className="flex-1 min-h-0 flex flex-col bg-[#0d0d0d]">
      {/* 顶部：机型切换 + 退出 */}
      <div className="shrink-0 flex items-center justify-between gap-2.5 px-4 pt-2 pb-2 select-none">
        <div className="flex gap-1 bg-[#1a1a1a] border border-[#2a2a2a] rounded-full p-0.5">
          {DEVICES.map((d) => (
            <button
              key={d.id}
              onClick={() => switchDevice(d.id)}
              className={`px-2.5 py-1 text-[11px] rounded-full transition-colors ${
                d.id === deviceId ? 'bg-[#2a2a2a] text-white' : 'text-[#777] hover:text-[#ccc]'
              }`}
            >
              {d.label}
            </button>
          ))}
        </div>
        <button
          onClick={onExit}
          title="退出预览（ESC）"
          className="w-7 h-7 rounded-full border border-[#3a3a3a] text-[#777] hover:text-white hover:border-[#666] flex items-center justify-center text-[12px] transition-colors"
        >
          <X className="h-3.5 w-3.5" strokeWidth={1.8} aria-hidden />
        </button>
      </div>

      {/* 舞台：手机框居中，可滚动（放大超出时仍可触及手柄） */}
      <div ref={stageRef} className="flex-1 min-h-0 w-full flex items-center justify-center overflow-auto">
        <div ref={wrapRef} className="relative shrink-0">
          <div
            ref={phoneRef}
            className="absolute top-0 left-0 flex flex-col overflow-hidden bg-black"
            style={{
              transformOrigin: '0 0',
              borderRadius: isAndroid ? 34 : isSE ? 38 : 44,
              padding: isAndroid ? 8 : 10,
              boxShadow: '0 20px 60px rgba(0,0,0,.7), 0 0 0 1px #2a2a2a',
            }}
          >
            <div
              className="flex-1 min-h-0 flex flex-col overflow-hidden bg-[#0f0f0f]"
              style={{ borderRadius: isAndroid ? 26 : isSE ? 30 : 36 }}
            >
              {/* 状态栏 */}
              <div className="shrink-0 flex items-center justify-between h-[30px] px-5 pt-1 text-[11px] font-semibold text-[#e8e8e8]">
                <span className="tabular-nums">9:41</span>
                <span className="flex items-center gap-1">
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M1 8h2v8H1zM5 5h2v14H5zM9 2h2v20H9zM13 5h2v14h-2zM17 8h2v8h-2zM21 10.5h2v3h-2z" /></svg>
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M5 12.55a11 11 0 0 1 14.08 0" /><path d="M1.42 9a16 16 0 0 1 21.16 0" /><path d="M8.53 16.11a6 6 0 0 1 6.95 0" /><circle cx="12" cy="20" r="1" fill="currentColor" stroke="none" /></svg>
                  <svg width="19" height="10" viewBox="0 0 24 12" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"><rect x="1" y="1" width="19" height="10" rx="2.5" /><rect x="3" y="3" width="12" height="6" rx="1" fill="currentColor" stroke="none" /><path d="M22 4v4" /></svg>
                </span>
              </div>

              {/* 阅读 App 头栏 */}
              <div className="shrink-0 flex items-center justify-between px-3.5 pt-0.5 pb-1.5 relative">
                <span className="flex items-center gap-0.5 text-[12px] text-[#999]">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><polyline points="15 18 9 12 15 6" /></svg>
                  返回
                </span>
                <span className="text-[12px] text-[#bbb] whitespace-nowrap overflow-hidden text-ellipsis max-w-[180px]">{bookTitle}</span>
                <button
                  onClick={() => setMenuOpen((v) => !v)}
                  title={menuOpen ? '关闭目录' : '目录'}
                  className={`w-6 h-6 flex items-center justify-center rounded-md transition-colors ${menuOpen ? 'text-[#f59e0b] bg-[rgba(245,158,11,0.12)]' : 'text-[#999] hover:text-white hover:bg-[rgba(128,128,128,0.12)]'}`}
                >
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M4 6h16M4 12h16M4 18h16" /></svg>
                </button>
              </div>

              {/* 正文区：复用 MdView，阅读/编辑跟随工具栏切换（手机容器即编辑器本身）。
                  隐藏正文 markdown 自带的第一个 h1（与桌面章头同规则，手机头栏已显示章节标题） */}
              <div
                className="flex-1 min-h-0 overflow-y-auto px-4 py-3.5"
                style={{ fontSize: fontSize + 'px', fontFamily: fontFamily || undefined }}
              >
                {editable && onTitleChange ? (
                  <div className="flex items-baseline gap-1.5 mb-3.5 leading-snug">
                    {titleNum && (
                      <span className="text-[19px] font-bold text-[#e8e8e8] select-none shrink-0">{titleNum}</span>
                    )}
                    <input
                      value={titleDraft}
                      onChange={(e) => setTitleDraft(e.target.value)}
                      onBlur={commitTitle}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') { e.preventDefault(); (e.target as HTMLInputElement).blur() }
                        else if (e.key === 'Escape') { setTitleDraft(titleText ?? ''); (e.target as HTMLInputElement).blur() }
                      }}
                      placeholder="章节标题"
                      title="回车或失焦保存；Esc 取消"
                      className="flex-1 min-w-0 bg-transparent border-b border-dashed border-[#3a3a3a] focus:border-[#f59e0b] outline-none text-[19px] font-bold text-[#e8e8e8] pb-0.5 transition-colors"
                    />
                  </div>
                ) : (
                  <div className="text-[19px] font-bold text-[#e8e8e8] mb-3.5 leading-snug select-none">{title}</div>
                )}
                <div className="[&>div.space-y-3>h1:first-child]:hidden">
                  <MdView text={content} editable={editable} onInput={onInput} />
                </div>
              </div>

              {/* 底部翻章条 */}
              <div className="shrink-0 border-t border-[rgba(128,128,128,0.15)]">
                <div className="flex items-center justify-between px-3 py-2">
                  <button
                    onClick={onPrev}
                    disabled={!hasPrev}
                    className="text-[12px] text-[#999] px-2.5 py-1 rounded-full border border-[rgba(128,128,128,0.25)] hover:text-white hover:border-[rgba(245,158,11,.5)] transition-colors disabled:opacity-35 disabled:cursor-not-allowed disabled:hover:text-[#999] disabled:hover:border-[rgba(128,128,128,0.25)]"
                  >
                    上一章
                  </button>
                  <span className="text-[11px] text-[#888] tabular-nums">{posLabel}</span>
                  <button
                    onClick={onNext}
                    disabled={!hasNext}
                    className="text-[12px] text-[#999] px-2.5 py-1 rounded-full border border-[rgba(128,128,128,0.25)] hover:text-white hover:border-[rgba(245,158,11,.5)] transition-colors disabled:opacity-35 disabled:cursor-not-allowed disabled:hover:text-[#999] disabled:hover:border-[rgba(128,128,128,0.25)]"
                  >
                    下一章
                  </button>
                </div>
              </div>
            </div>
          </div>

          {/* 目录浮层：头栏菜单打开，覆盖手机屏幕，点击章节跳转 */}
          {menuOpen && (
            <div
              className="absolute inset-0 z-40 flex flex-col bg-[#0f0f0f]/97"
              style={{ borderRadius: isAndroid ? 26 : isSE ? 30 : 36, overflow: 'hidden' }}
            >
              <div className="shrink-0 flex items-center justify-between px-3.5 py-2.5 border-b border-[rgba(128,128,128,0.15)]">
                <span className="text-[13px] font-semibold text-[#e8e8e8]">目录</span>
                <button
                  onClick={() => setMenuOpen(false)}
                  className="text-[11px] text-[#999] px-2 py-0.5 rounded-full border border-[rgba(128,128,128,0.25)] hover:text-white hover:border-[#666] transition-colors"
                >
                  关闭
                </button>
              </div>
              <div className="flex-1 min-h-0 overflow-y-auto py-1">
                {chapters.map((c) => {
                  const active = c.path === currentPath
                  return (
                    <button
                      key={c.path}
                      onClick={() => { onNavigate(c.path); setMenuOpen(false) }}
                      className={`w-full text-left px-3.5 py-2 text-[12.5px] leading-snug transition-colors ${
                        active
                          ? 'text-[#f59e0b] font-medium bg-[rgba(245,158,11,0.08)]'
                          : 'text-[#aaa] hover:bg-[rgba(128,128,128,0.08)] hover:text-white'
                      }`}
                    >
                      {chapterLabel(c.name)}
                    </button>
                  )
                })}
              </div>
            </div>
          )}

          {/* 右下角缩放手柄：拖动等比缩放 · 双击恢复适配 */}
          <div
            onPointerDown={onResizePointerDown}
            onDoubleClick={fit}
            title="拖动等比缩放 · 双击恢复适配"
            className="absolute -right-[13px] -bottom-[13px] w-[30px] h-[30px] flex items-center justify-center cursor-se-resize text-[#999] bg-[rgba(26,26,26,0.92)] border border-[#3a3a3a] rounded-[9px] shadow-[0_4px_14px_rgba(0,0,0,0.5)] hover:text-[#f59e0b] hover:border-[rgba(245,158,11,0.55)] transition-colors"
            style={{ touchAction: 'none', zIndex: 30 }}
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ pointerEvents: 'none' }}>
              <polyline points="15 3 21 3 21 9" />
              <polyline points="9 21 3 21 3 15" />
              <line x1="21" y1="3" x2="14" y2="10" />
              <line x1="3" y1="21" x2="10" y2="14" />
            </svg>
          </div>
        </div>
      </div>
    </div>
  )
}
