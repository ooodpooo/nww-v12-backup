import { useEffect, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { api } from '../api'
import { countVisibleChars } from '../utils/wordcount'
import { extractTail } from '../utils/prevtail'
import { parseChapterName } from './AssetTree'

/**
 * 上一章「尾勾」提示条（默认折叠）
 *
 * 目的：写新章时作者最需要的上下文是上一章结尾留的那口气（悬念/动作/对白），
 * 但翻回去看完一整章太重。故只抽章节尾部若干段，折叠在编辑器正文开头。
 *
 * 边界：
 * - 只在「上一章存在」时渲染（第 1 章无上一章 → 返回 null，不占位）
 * - 抽不到有效文字（空章/纯标题）→ 返回 null，不显示空壳
 * - 只读一个 api.file，不做任何写操作
 */

/** 「第12章 标题」的可读标签（无法解析章号时退回文件名） */
function labelOf(name: string): string {
  const parsed = parseChapterName(name)
  return parsed ? `第${parseInt(parsed.num, 10)}章 ${parsed.title}` : name.replace(/\.(md|txt)$/, '')
}

interface Props {
  bookId: string
  /** 上一章的书内相对路径（正文/第NNN章.md） */
  prevPath: string
  /** 点击「打开上一章」时的跳转回调 */
  onOpenPrev?: () => void
}

export default function PrevTailHook({ bookId, prevPath, onOpenPrev }: Props) {
  const [open, setOpen] = useState(false)
  const [tail, setTail] = useState('')
  const [loading, setLoading] = useState(false)
  const [copied, setCopied] = useState(false)

  // 换章即回到默认折叠，避免展开态跨章残留占住正文顶部
  useEffect(() => {
    setOpen(false)
    setCopied(false)
  }, [prevPath])

  useEffect(() => {
    if (!bookId || !prevPath) { setTail(''); return }
    let cancelled = false
    setLoading(true)
    api.file(bookId, prevPath)
      .then((f) => { if (!cancelled) setTail(extractTail(f.content ?? '')) })
      .catch(() => { if (!cancelled) setTail('') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [bookId, prevPath])

  // 空内容不占位：第 1 章本就不渲染；这里兜住「上一章为空章」的情况
  if (!tail) return null

  const chars = countVisibleChars(tail)
  const prevName = prevPath.split('/').pop() ?? prevPath

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(tail)
    } catch {
      const ta = document.createElement('textarea')
      ta.value = tail
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <div className="mb-6">
      {/* 折叠条：常驻一行，默认折叠 */}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        title={open ? '收起上一章尾勾' : '展开上一章尾勾'}
        className="flex w-full items-center gap-2 rounded-md border border-[#332c22] bg-[#1b1815]/70 px-3 py-1.5 text-left transition-colors hover:border-[#5a452a] hover:bg-[#221d17]"
      >
        <span
          className="shrink-0 text-[#a8823f] transition-transform duration-150"
          style={{ transform: open ? 'rotate(0deg)' : 'rotate(-90deg)' }}
        >
          <ChevronDown className="h-3 w-3" strokeWidth={1.8} aria-hidden />
        </span>
        <span className="shrink-0 text-[13px] font-medium text-[#c79a52]">上一章尾勾</span>
        <span className="min-w-0 truncate text-[12px] text-[#7d6c53]">{labelOf(prevName)}</span>
        <span className="ml-auto shrink-0 pl-2 text-[12px] tabular-nums text-[#6b6558]">
          {loading ? '…' : `${chars} 字`}
        </span>
      </button>

      {/* 展开区 */}
      {open && (
        <div className="mt-2 rounded-md border border-[#332c22] bg-[#1a1714]/70">
          <div className="px-4 py-3">
            <div className="max-h-[240px] overflow-y-auto whitespace-pre-wrap break-words text-[14px] leading-[1.9] text-[#a8a094]">
              {tail}
            </div>
          </div>
          <div className="flex items-center gap-3 border-t border-[#2a251f] px-4 py-2">
            {onOpenPrev && (
              <button
                type="button"
                onClick={onOpenPrev}
                className="text-[12px] text-[#8d7a5c] transition-colors hover:text-[#c79a52]"
              >
                打开上一章
              </button>
            )}
            <button
              type="button"
              onClick={handleCopy}
              className="text-[12px] text-[#8d7a5c] transition-colors hover:text-[#c79a52]"
            >
              {copied ? '已复制' : '复制'}
            </button>
            <span className="ml-auto text-[11px] text-[#5c574d]">仅摘录结尾，非全文</span>
          </div>
        </div>
      )}
    </div>
  )
}
