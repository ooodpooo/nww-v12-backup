import { useEffect, useState, type FormEvent } from 'react'
import {
  adminListUsers, adminCreateUser, adminDeleteUser, adminResetPassword,
  adminListCodes, adminGenerateCodes,
  type AdminUser, type SerialCode,
} from '../auth'

/** 管理员面板：用户管理（建号/删号/重置密码） + 序列号发放（充值码） */
export default function UserMgmt({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<'users' | 'codes'>('users')
  const [users, setUsers] = useState<AdminUser[] | null>(null)
  const [codes, setCodes] = useState<SerialCode[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // 发码表单
  const [cTier, setCTier] = useState('basic')
  const [cDays, setCDays] = useState(30)
  const [cCount, setCCount] = useState(10)
  const [cUsage, setCUsage] = useState<'redeem' | 'invite'>('redeem')
  const [freshCodes, setFreshCodes] = useState<string[]>([])

  // 建号表单
  const [nEmail, setNEmail] = useState('')
  const [nPwd, setNPwd] = useState('')
  const [nNick, setNNick] = useState('')
  const [nRole, setNRole] = useState('user')

  // 重置密码
  const [resetTarget, setResetTarget] = useState<string | null>(null)
  const [resetPwd, setResetPwd] = useState('')

  const reload = () => {
    adminListUsers().then(setUsers).catch((e) => setError(String(e)))
    adminListCodes().then(setCodes).catch(() => { /* 码列表加载失败不阻塞用户表 */ })
  }

  useEffect(reload, [])

  const createUser = (e: FormEvent) => {
    e.preventDefault()
    if (!nEmail.trim() || !nPwd || busy) return
    setBusy(true)
    setError(null)
    setMsg(null)
    adminCreateUser(nEmail.trim(), nPwd, nNick.trim(), nRole)
      .then(() => {
        setMsg(`已创建账号 ${nEmail.trim()}`)
        setNEmail('')
        setNPwd('')
        setNNick('')
        setNRole('user')
        reload()
      })
      .catch((err) => setError(String(err)))
      .finally(() => setBusy(false))
  }

  const delUser = (email: string) => {
    if (busy) return
    if (!window.confirm(`确定删除账号 ${email}？`)) return
    setBusy(true)
    setError(null)
    setMsg(null)
    adminDeleteUser(email)
      .then(() => {
        setMsg(`已删除 ${email}`)
        reload()
      })
      .catch((err) => setError(String(err)))
      .finally(() => setBusy(false))
  }

  /** 点击复制序列号，成功后在顶部提示 */
  const copyCode = (c: string) => {
    const done = () => { setMsg(null); setError(null); setTimeout(() => setMsg(`已复制：${c}`), 0) }
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(c).then(done).catch(() => {
        window.prompt('复制失败，请手动复制：', c)
      })
    } else {
      window.prompt('请手动复制：', c)
    }
  }

  const doReset = (email: string) => {    if (!resetPwd || busy) return
    setBusy(true)
    setError(null)
    setMsg(null)
    adminResetPassword(email, resetPwd)
      .then(() => {
        setMsg(`已重置 ${email} 的密码`)
        setResetTarget(null)
        setResetPwd('')
      })
      .catch((err) => setError(String(err)))
      .finally(() => setBusy(false))
  }

  return (
    <div className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center" onClick={onClose}>
      <div
        className="w-[520px] max-h-[80vh] flex flex-col bg-[#1e1e1e] border border-[#333] rounded-lg shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-[#2a2a2a]">
          <div className="flex items-center gap-3">
            <span className="text-[14px] font-semibold text-white">管理面板</span>
            <button
              onClick={() => setTab('users')}
              className={`text-[14px] px-2 py-0.5 rounded transition-colors ${tab === 'users' ? 'text-[#f59e0b] bg-[#f59e0b]/10' : 'text-[#777] hover:text-white'}`}
            >
              用户
            </button>
            <button
              onClick={() => setTab('codes')}
              className={`text-[14px] px-2 py-0.5 rounded transition-colors ${tab === 'codes' ? 'text-[#f59e0b] bg-[#f59e0b]/10' : 'text-[#777] hover:text-white'}`}
            >
              序列号
            </button>
          </div>
          <button onClick={onClose} className="text-[#777] hover:text-white text-[14px] leading-none px-1">
            ×
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-3">
          {error && <div className="mb-2 text-[14px] text-red-400">{error}</div>}
          {msg && <div className="mb-2 text-[14px] text-green-400">{msg}</div>}

          {tab === 'users' && (
            <>
          <table className="w-full text-[14px]">
            <thead>
              <tr className="text-left text-[#777] border-b border-[#2a2a2a]">
                <th className="py-1.5 font-normal">账号</th>
                <th className="py-1.5 font-normal">角色</th>
                <th className="py-1.5 font-normal">套餐/到期</th>
                <th className="py-1.5 font-normal text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {users?.map((u) => (
                <tr key={u.email} className="border-b border-[#252525]">
                  <td className="py-1.5 text-[#ccc]">
                    {u.email}
                    {u.nickname && u.nickname !== u.email && <span className="text-[#666] ml-1">（{u.nickname}）</span>}
                  </td>
                  <td className="py-1.5">
                    <span className={u.role === 'admin' ? 'text-[#f59e0b]' : 'text-[#777]'}>{u.role}</span>
                  </td>
                  <td className="py-1.5 text-[#666] tabular-nums">
                    {u.tier ? `${u.tier} · ` : ''}
                    {u.expires_at ? u.expires_at.slice(0, 10) : (u.role === 'admin' ? '永久' : '未开通')}
                  </td>
                  <td className="py-1.5 text-right whitespace-nowrap">
                    <button
                      onClick={() => { setResetTarget(u.email); setResetPwd('') }}
                      className="text-[#777] hover:text-[#f59e0b] mr-3 transition-colors"
                    >
                      重置密码
                    </button>
                    {u.role !== 'admin' && (
                      <button onClick={() => delUser(u.email)} className="text-[#777] hover:text-red-400 transition-colors">
                        删除
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {users && users.length === 0 && <div className="py-3 text-[14px] text-[#666]">暂无用户</div>}

          {resetTarget && (
            <div className="mt-3 p-2 bg-[#252526] rounded border border-[#333] flex items-center gap-2">
              <span className="text-[14px] text-[#999] shrink-0">重置 {resetTarget}：</span>
              <input
                type="password"
                value={resetPwd}
                onChange={(e) => setResetPwd(e.target.value)}
                placeholder="新密码"
                className="flex-1 bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 text-[14px] text-[#ccc] outline-none focus:border-[#f59e0b]"
              />
              <button onClick={() => doReset(resetTarget)} disabled={busy || !resetPwd} className="px-2 py-1 text-[14px] text-[#f59e0b] border border-[#f59e0b]/50 rounded hover:bg-[#f59e0b]/10 disabled:opacity-40">
                确定
              </button>
              <button onClick={() => setResetTarget(null)} className="px-2 py-1 text-[14px] text-[#777] hover:text-white">
                取消
              </button>
            </div>
          )}
            </>
          )}

          {tab === 'codes' && (
            <>
              <div className="flex items-center gap-2 mb-3">
                <select
                  value={cUsage}
                  onChange={(e) => setCUsage(e.target.value as 'redeem' | 'invite')}
                  className="bg-[#1a1a1a] border border-[#333] rounded px-1.5 py-1 text-[14px] text-[#ccc] outline-none focus:border-[#f59e0b]"
                >
                  <option value="redeem">充值码</option>
                  <option value="invite">邀请码</option>
                </select>
                <select
                  value={cTier}
                  onChange={(e) => setCTier(e.target.value)}
                  className="bg-[#1a1a1a] border border-[#333] rounded px-1.5 py-1 text-[14px] text-[#ccc] outline-none focus:border-[#f59e0b]"
                >
                  <option value="basic">basic</option>
                  <option value="pro">pro</option>
                </select>
                <select
                  value={cDays}
                  onChange={(e) => setCDays(Number(e.target.value))}
                  className="bg-[#1a1a1a] border border-[#333] rounded px-1.5 py-1 text-[14px] text-[#ccc] outline-none focus:border-[#f59e0b]"
                >
                  <option value={7}>7 天</option>
                  <option value={30}>1 个月</option>
                </select>
                <input
                  type="number"
                  min={1}
                  max={100}
                  value={cCount}
                  onChange={(e) => setCCount(Number(e.target.value))}
                  className="w-16 bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 text-[14px] text-[#ccc] outline-none focus:border-[#f59e0b]"
                />
                <button
                  onClick={() => {
                    if (busy) return
                    setBusy(true); setError(null); setMsg(null)
                    adminGenerateCodes(cTier, cDays, cCount, cUsage)
                      .then((made) => { setFreshCodes(made); setMsg(`已生成 ${made.length} 个${cUsage === 'invite' ? '邀请码' : '序列号'}`); reload() })
                      .catch((err) => setError(String(err)))
                      .finally(() => setBusy(false))
                  }}
                  disabled={busy}
                  className="ml-auto px-3 py-1 text-[14px] font-medium text-black bg-[#f59e0b] hover:bg-[#d97706] rounded transition-colors disabled:opacity-40"
                >
                  生成{cUsage === 'invite' ? '邀请码' : '序列号'}
                </button>
              </div>

              {freshCodes.length > 0 && (
                <div className="mb-3 p-2 bg-[#252526] rounded border border-[#f59e0b]/40">
                  <div className="text-[14px] text-[#f59e0b] mb-1">新生成（点单个复制）：</div>
                  <div className="flex flex-wrap gap-1.5">
                    {freshCodes.map((c) => (
                      <button
                        key={c}
                        onClick={() => copyCode(c)}
                        className="text-[14px] font-mono text-[#ccc] bg-[#1a1a1a] border border-[#333] rounded px-1.5 py-0.5 hover:border-[#f59e0b] transition-colors"
                        title="点击复制"
                      >
                        {c}
                      </button>
                    ))}
                  </div>
                </div>
              )}

              <table className="w-full text-[14px]">
                <thead>
                  <tr className="text-left text-[#777] border-b border-[#2a2a2a]">
                    <th className="py-1.5 font-normal">码</th>
                    <th className="py-1.5 font-normal">类型</th>
                    <th className="py-1.5 font-normal">套餐</th>
                    <th className="py-1.5 font-normal">状态</th>
                  </tr>
                </thead>
                <tbody>
                  {[...codes ?? []].reverse().map((c) => (
                    <tr key={c.code} className="border-b border-[#252525]">
                      <td className="py-1.5 font-mono">
                        <button
                          onClick={() => copyCode(c.code)}
                          className="text-[#ccc] hover:text-[#f59e0b] font-mono transition-colors"
                          title="点击复制"
                        >
                          {c.code}
                        </button>
                      </td>
                      <td className="py-1.5">
                        {c.usage === 'invite'
                          ? <span className="text-[#60a5fa]">邀请</span>
                          : <span className="text-[#999]">充值</span>}
                      </td>
                      <td className="py-1.5 text-[#999]">{c.tier} · {c.days === 0 ? '永久' : `${c.days}天`}</td>
                      <td className="py-1.5">
                        {c.used_by
                          ? <span className="text-[#666]">已用：{c.used_by}</span>
                          : <span className="text-green-400">未使用</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {codes && codes.length === 0 && <div className="py-3 text-[14px] text-[#666]">还没有生成过序列号</div>}
            </>
          )}
        </div>

        {tab === 'users' && (
        <form onSubmit={createUser} className="border-t border-[#2a2a2a] px-4 py-3">
          <div className="text-[14px] text-[#777] mb-2">新建账号（注册已关闭，这里是唯一开户通道）</div>
          <div className="flex items-center gap-2">
            <input
              value={nEmail}
              onChange={(e) => setNEmail(e.target.value)}
              placeholder="账号"
              className="w-32 bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 text-[14px] text-[#ccc] placeholder:text-[#555] outline-none focus:border-[#f59e0b]"
            />
            <input
              type="password"
              value={nPwd}
              onChange={(e) => setNPwd(e.target.value)}
              placeholder="密码"
              className="w-32 bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 text-[14px] text-[#ccc] placeholder:text-[#555] outline-none focus:border-[#f59e0b]"
            />
            <input
              value={nNick}
              onChange={(e) => setNNick(e.target.value)}
              placeholder="昵称（可选）"
              className="w-28 bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 text-[14px] text-[#ccc] placeholder:text-[#555] outline-none focus:border-[#f59e0b]"
            />
            <select
              value={nRole}
              onChange={(e) => setNRole(e.target.value)}
              className="bg-[#1a1a1a] border border-[#333] rounded px-1 py-1 text-[14px] text-[#ccc] outline-none focus:border-[#f59e0b]"
            >
              <option value="user">user</option>
              <option value="admin">admin</option>
            </select>
            <button
              type="submit"
              disabled={busy || !nEmail.trim() || !nPwd}
              className="ml-auto px-3 py-1 text-[14px] font-medium text-black bg-[#f59e0b] hover:bg-[#d97706] rounded transition-colors disabled:opacity-40"
            >
              创建
            </button>
          </div>
        </form>
        )}
      </div>
    </div>
  )
}
