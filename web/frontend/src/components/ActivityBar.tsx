import {
  Activity,
  BookOpen,
  Cpu,
  LayoutDashboard,
  PenLine,
  Plus,
  Radio,
  ShieldCheck,
  UserRound,
  type LucideIcon,
} from 'lucide-react'
import PurchaseButton from './PurchaseButton'

/* ─────────────────────  主导航：纯图标栏（V14 图标 + 分组，V12 色板）  ─────────────────────
   用户 2026-11-05 裁决：左侧列**只放 icon，不要文字标注**；分组标题保留。
   故固定为 72px 图标栏（对齐 V14 collapsed 的 w-[72px]），去掉折叠/展开双态。
   2026-11-07 裁决：去掉「查找」按钮与「资产」分组（原组只剩空壳），
   「模型」按钮移入「系统」组 → 导航仅剩「创作 / 系统」两组。
   2026-11-07 图标裁决：「新建书籍」= Plus（纯 + 号），「写作台」= LayoutDashboard
   （多面板工作台；与「编辑」的 PenLine 区分，不再复用 SquarePen）。
   图标统一 lucide-react 线性（与 V14 同版本），选中态浅底 + 左侧琥珀竖条，
   行样式统一走 index.css 的 .v14-nav-row，色值全部沿用 V12。
────────────────────────────────────────────────────────────────────── */

export type ActivityId = 'shelf' | 'edit' | 'publish' | 'mvp' | 'model' | 'guard' | 'account'

interface NavItem {
  id: ActivityId | 'create' | 'live' | 'account'
  label: string
  icon: LucideIcon
  badge?: 'beta'
  /** 渲染时由外部回调决定有无（新建书籍 / AI 实况 / 账号） */
  optional?: boolean
}

interface NavGroup {
  title: string
  items: NavItem[]
}

const groups: NavGroup[] = [
  {
    title: '创作',
    items: [
      { id: 'create', label: '新建书籍', icon: Plus, optional: true },
      { id: 'edit', label: '编辑', icon: PenLine },
      { id: 'mvp', label: '写作台', icon: LayoutDashboard, badge: 'beta' },
      { id: 'publish', label: '自动发布', icon: Radio },
    ],
  },
  {
    title: '系统',
    items: [
      { id: 'model', label: '模型', icon: Cpu },
      { id: 'live', label: 'AI 实况', icon: Activity, optional: true },
      { id: 'guard', label: '千章守护', icon: ShieldCheck },
      { id: 'account', label: '账号', icon: UserRound },
    ],
  },
]

interface Props {
  active: ActivityId
  onChange: (id: ActivityId) => void
  /** 后端不可用等全局错误，以红色警示图标呈现（悬浮看全文） */
  error?: string | null
  /** 顶部「新建书籍」：进入纯聊天建书模式（2026-10-23 用户拍板，唯一建书入口） */
  onCreateBook?: () => void
  /** 建书模式激活态（按钮高亮） */
  creating?: boolean
  /** AI 实况面板开关（最左侧工具列内的独立工具按钮，点击切换浮动面板） */
  liveOpen?: boolean
  /** 有 AI 任务正在跑：icon 脉冲提示，不自动弹窗 */
  liveActive?: boolean
  onToggleLive?: () => void
}

export default function ActivityBar({ active, onChange, error, onCreateBook, creating, liveOpen, liveActive, onToggleLive }: Props) {
  const available = (item: NavItem): boolean => {
    if (item.id === 'create') return !!onCreateBook
    if (item.id === 'live') return !!onToggleLive
    return true
  }

  const handler = (item: NavItem): (() => void) => {
    if (item.id === 'create') return () => onCreateBook?.()
    if (item.id === 'live') return () => onToggleLive?.()
    return () => onChange(item.id as ActivityId)
  }

  const isActive = (item: NavItem): boolean => {
    if (item.id === 'create') return !!creating
    if (item.id === 'live') return !!liveOpen
    // 2026-11-07 裁决：建书态是独立布局，不与第 2 个「编辑」按钮关联。
    // 此时除建书按钮外一律不高亮，避免 activity 残留造成两个按钮同时选中。
    if (creating) return false
    return active === item.id
  }

  return (
    <aside className="w-[72px] shrink-0 h-full flex flex-col rounded-lg border border-[#2a2a2a] bg-[#141414] select-none">
      {/* 顶部品牌标记：纯图标，无文字标题 */}
      <div className="shrink-0 h-12 flex items-center justify-center">
        <BookOpen className="h-[18px] w-[18px] text-[#f59e0b]" strokeWidth={1.8} aria-hidden />
      </div>

      <nav className="flex-1 min-h-0 overflow-y-auto overscroll-contain px-2 scroll-y">
        {groups.map((group) => {
          const items = group.items.filter(available)
          if (items.length === 0) return null
          return (
            <div key={group.title} className="space-y-1">
              {/* 分组标题保留（用户裁决）；窄栏下居中显示，不用细线替代 */}
              <div className="v14-group-title is-compact">{group.title}</div>
              {items.map((item) => {
                const Icon = item.icon
                const activeItem = isActive(item)
                return (
                  <button
                    key={item.id}
                    onClick={handler(item)}
                    title={`${item.label}${item.badge === 'beta' ? ' · Beta' : ''}`}
                    aria-label={item.label}
                    className={`v14-nav-row is-collapsed ${activeItem ? 'is-active' : ''}`}
                  >
                    <Icon
                      className={`v14-nav-icon ${item.id === 'live' && liveActive && !liveOpen ? 'live-breathe' : ''}`}
                      strokeWidth={1.8}
                      aria-hidden
                    />
                  </button>
                )
              })}
            </div>
          )
        })}
        {error && (
          <div className="px-2 py-3 flex justify-center">
            <span className="v14-badge v14-badge-danger cursor-help" title={`后端不可用：${error}`}>!</span>
          </div>
        )}
      </nav>

      {/* 底部：会员入口（原顶部栏下沉） */}
      <div className="shrink-0 flex flex-col items-center gap-2 pb-3 pt-2 border-t border-[#232323]">
        <PurchaseButton variant="icon" />
      </div>
    </aside>
  )
}
