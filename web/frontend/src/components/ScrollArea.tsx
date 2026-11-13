import { useRef, useState, type ReactNode, type UIEvent } from 'react'

/* ─────────────────────  滚动区域：默认隐藏滚动条，滚动时显示  ───────────────────── */

interface Props {
  children: ReactNode
  className?: string
  style?: React.CSSProperties
}

export default function ScrollArea({ children, className = '', style }: Props) {
  const [active, setActive] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const handleScroll = (_e: UIEvent<HTMLDivElement>) => {
    setActive(true)
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => setActive(false), 800)
  }

  return (
    <div
      onScroll={handleScroll}
      className={`scroll-reveal ${active ? 'active' : ''} scroll-y h-full ${className}`}
      style={style}
    >
      {children}
    </div>
  )
}
