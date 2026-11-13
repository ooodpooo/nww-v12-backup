import { useEffect, useState, type FormEvent } from 'react'
import { fetchProfile, changePassword, redeemCode, getStoredUser, getStoredRole, logout, type Profile, listApiKeys, createApiKey, revokeApiKey, type ApiKeyInfo, type ApiKeyCreated } from '../auth'

const input =
  'w-full bg-[#1a1a1a] border border-[#333] rounded px-3 py-2 text-[14px] text-[#ccc] placeholder:text-[#555] outline-none focus:border-[#f59e0b] transition-colors'
const primaryBtn =
  'py-2 px-4 rounded text-[14px] font-medium bg-[#f59e0b] text-black hover:bg-[#fbbf24] disabled:opacity-50 transition-colors'

function fmtDate(s: string | null | undefined): string {
  if (!s) return '—'
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString('zh-CN', { hour12: false })
}

/** 个人中心：账号资料 + 自助改密 + 序列号核销。用户只能看到并操作自己的账号。 */
export default function AccountCenter({ onOpenAdmin }: { onOpenAdmin: () => void }) {
  const [profile, setProfile] = useState<Profile | null>(null)
  const [loadErr, setLoadErr] = useState<string | null>(null)

  // 改密
  const [oldPwd, setOldPwd] = useState('')
  const [newPwd, setNewPwd] = useState('')
  const [newPwd2, setNewPwd2] = useState('')
  const [pwdMsg, setPwdMsg] = useState<string | null>(null)
  const [pwdErr, setPwdErr] = useState<string | null>(null)
  const [pwdBusy, setPwdBusy] = useState(false)

  // 核销
  const [code, setCode] = useState('')
  const [redeemMsg, setRedeemMsg] = useState<string | null>(null)
  const [redeemErr, setRedeemErr] = useState<string | null>(null)
  const [redeemBusy, setRedeemBusy] = useState(false)

  // API Key 管理
  const [apiKeys, setApiKeys] = useState<ApiKeyInfo[]>([])
  const [newKeyName, setNewKeyName] = useState('')
  const [newKeyCreated, setNewKeyCreated] = useState<ApiKeyCreated | null>(null)
  const [keyBusy, setKeyBusy] = useState(false)
  const [keyErr, setKeyErr] = useState<string | null>(null)

  const loadApiKeys = () => {
    listApiKeys().then(setApiKeys).catch((e) => setKeyErr(String(e instanceof Error ? e.message : e)))
  }

  const role = getStoredRole()
  const fallbackEmail = getStoredUser()

  const load = () => {
    fetchProfile()
      .then(setProfile)
      .catch((e) => setLoadErr(String(e instanceof Error ? e.message : e)))
    loadApiKeys()
  }

  useEffect(load, [])

  const submitCreateKey = async (e: FormEvent) => {
    e.preventDefault()
    setKeyErr(null)
    setNewKeyCreated(null)
    if (!newKeyName.trim()) return
    setKeyBusy(true)
    try {
      const k = await createApiKey(newKeyName.trim())
      setNewKeyCreated(k)
      setNewKeyName('')
      loadApiKeys()
    } catch (err) {
      setKeyErr(String(err instanceof Error ? err.message : err))
    } finally {
      setKeyBusy(false)
    }
  }

  const submitRevokeKey = async (prefix: string) => {
    if (!confirm(`确定撤销 API Key ${prefix}？撤销后该 Key 立即失效，无法恢复。`)) return
    try {
      await revokeApiKey(prefix)
      setNewKeyCreated(null)
      loadApiKeys()
    } catch (err) {
      setKeyErr(String(err instanceof Error ? err.message : err))
    }
  }

  const submitPassword = async (e: FormEvent) => {
    e.preventDefault()
    setPwdMsg(null)
    setPwdErr(null)
    if (newPwd.length < 8) {
      setPwdErr('新密码长度不能少于 8 位')
      return
    }
    if (newPwd !== newPwd2) {
      setPwdErr('两次输入的新密码不一致')
      return
    }
    setPwdBusy(true)
    try {
      await changePassword(oldPwd, newPwd)
      setPwdMsg('密码已更新')
      setOldPwd('')
      setNewPwd('')
      setNewPwd2('')
    } catch (err) {
      setPwdErr(String(err instanceof Error ? err.message : err))
    } finally {
      setPwdBusy(false)
    }
  }

  const submitRedeem = async (e: FormEvent) => {
    e.preventDefault()
    setRedeemMsg(null)
    setRedeemErr(null)
    if (!code.trim()) return
    setRedeemBusy(true)
    try {
      const r = await redeemCode(code.trim())
      setRedeemMsg(`核销成功，当前档位：${r.tier ?? '—'}，到期：${fmtDate(r.expires_at)}`)
      setCode('')
      load()
    } catch (err) {
      setRedeemErr(String(err instanceof Error ? err.message : err))
    } finally {
      setRedeemBusy(false)
    }
  }

  return (
    <div className="h-full w-full overflow-auto bg-[#0d0d0d] p-6 box-border">
      <div className="max-w-2xl mx-auto flex flex-col gap-5">
        <header className="flex items-center justify-between">
          <div>
            <div className="text-[14px] font-semibold text-white tracking-wide">NWW-novel 账号中心</div>
            <div className="text-[14px] text-[#777] mt-0.5">账号管理与套餐</div>
          </div>
          <div className="flex items-center gap-2">
            {role === 'admin' && (
              <button onClick={onOpenAdmin} className={primaryBtn}>
                用户管理
              </button>
            )}
            <button
              onClick={logout}
              className="py-2 px-4 rounded text-[14px] text-[#aaa] border border-[#333] hover:border-[#f59e0b] hover:text-[#f59e0b] transition-colors"
            >
              退出登录
            </button>
          </div>
        </header>

        {loadErr && <div className="text-[14px] text-red-400">{loadErr}</div>}

        {/* 账号资料 */}
        <section className="rounded-lg border border-[#2a2a2a] bg-[#181818] p-5">
          <div className="text-[14px] text-white mb-3">账号资料</div>
          <dl className="grid grid-cols-[88px_1fr] gap-y-2 text-[14px]">
            <dt className="text-[#777]">邮箱</dt>
            <dd className="text-[#ccc]">{profile?.email ?? fallbackEmail ?? '—'}</dd>
            <dt className="text-[#777]">昵称</dt>
            <dd className="text-[#ccc]">{profile?.nickname ?? '—'}</dd>
            <dt className="text-[#777]">角色</dt>
            <dd className="text-[#ccc]">{profile?.role ?? role ?? '—'}</dd>
            <dt className="text-[#777]">注册时间</dt>
            <dd className="text-[#ccc]">{fmtDate(profile?.created_at)}</dd>
            <dt className="text-[#777]">套餐档位</dt>
            <dd className="text-[#ccc]">{profile?.tier ?? '未开通'}</dd>
            <dt className="text-[#777]">到期时间</dt>
            <dd className="text-[#ccc]">{profile?.expires_at ? fmtDate(profile.expires_at) : '长期有效'}</dd>
          </dl>
        </section>

        {/* 序列号核销 */}
        <section className="rounded-lg border border-[#2a2a2a] bg-[#181818] p-5">
          <div className="text-[14px] text-white mb-3">序列号核销</div>
          <form onSubmit={submitRedeem} className="flex gap-2">
            <input
              className={input}
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="NWW-XXXX-XXXX-XXXX"
              autoComplete="off"
            />
            <button type="submit" disabled={redeemBusy} className={primaryBtn}>
              {redeemBusy ? '核销中…' : '核销'}
            </button>
          </form>
          {redeemMsg && <div className="mt-2 text-[14px] text-emerald-400">{redeemMsg}</div>}
          {redeemErr && <div className="mt-2 text-[14px] text-red-400">{redeemErr}</div>}
        </section>

        {/* API Key 管理 */}
        <section className="rounded-lg border border-[#2a2a2a] bg-[#181818] p-5">
          <div className="text-[14px] text-white mb-1">API Key（MCP 接入凭证）</div>
          <div className="text-[14px] text-[#777] mb-3">长期有效，用于 Cursor / Trae / WorkBuddy 等 IDE 的 MCP 连接。Key 仅创建时可见完整值，请妥善保存。</div>

          {/* 刚创建的 Key 显示 */}
          {newKeyCreated && (
            <div className="mb-4 p-3 rounded border border-emerald-700 bg-emerald-950/30">
              <div className="text-[14px] text-emerald-400 mb-1">新 Key 已创建（仅此次可见，请复制保存）</div>
              <div className="flex items-center gap-2">
                <code className="flex-1 bg-black/40 px-3 py-2 rounded text-[14px] text-emerald-300 font-mono break-all">{newKeyCreated.key}</code>
                <button
                  onClick={() => navigator.clipboard.writeText(newKeyCreated.key)}
                  className="py-1.5 px-3 rounded text-[14px] bg-emerald-700 text-white hover:bg-emerald-600 transition-colors"
                >
                  复制
                </button>
              </div>
              <div className="text-[14px] text-[#666] mt-1">MCP 配置示例：{window.location.origin}/sse?api_key={newKeyCreated.key}</div>
            </div>
          )}

          {/* 创建新 Key */}
          <form onSubmit={submitCreateKey} className="flex gap-2 mb-4">
            <input
              className={input}
              value={newKeyName}
              onChange={(e) => setNewKeyName(e.target.value)}
              placeholder="Key 名称（如：workbuddy-mcp、cursor-home）"
              autoComplete="off"
            />
            <button type="submit" disabled={keyBusy} className={primaryBtn}>
              {keyBusy ? '生成中…' : '生成新 Key'}
            </button>
          </form>
          {keyErr && <div className="mb-3 text-[14px] text-red-400">{keyErr}</div>}

          {/* Key 列表 */}
          {apiKeys.length === 0 ? (
            <div className="text-[14px] text-[#555] py-2">暂无 API Key</div>
          ) : (
            <div className="flex flex-col gap-2">
              {apiKeys.map((k) => (
                <div key={k.prefix} className="flex items-center justify-between py-2 px-3 rounded bg-black/20 border border-[#252525]">
                  <div className="flex flex-col">
                    <div className="flex items-center gap-2">
                      <span className="text-[14px] text-[#ccc] font-mono">{k.prefix}…</span>
                      <span className="text-[14px] text-[#888]">{k.name}</span>
                    </div>
                    <div className="text-[14px] text-[#555] mt-0.5">
                      创建: {fmtDate(k.created_at)} · 最近使用: {k.last_used_at ? fmtDate(k.last_used_at) : '从未'}
                    </div>
                  </div>
                  <button
                    onClick={() => submitRevokeKey(k.prefix)}
                    className="py-1 px-3 rounded text-[14px] text-red-400 border border-red-900/50 hover:bg-red-950/30 transition-colors"
                  >
                    撤销
                  </button>
                </div>
              ))}
            </div>
          )}
        </section>

        {/* 修改密码 */}
        <section className="rounded-lg border border-[#2a2a2a] bg-[#181818] p-5">
          <div className="text-[14px] text-white mb-3">修改密码</div>
          <form onSubmit={submitPassword} className="flex flex-col gap-3 max-w-sm">
            <input
              className={input}
              type="password"
              value={oldPwd}
              onChange={(e) => setOldPwd(e.target.value)}
              placeholder="当前密码"
              autoComplete="current-password"
              required
            />
            <input
              className={input}
              type="password"
              value={newPwd}
              onChange={(e) => setNewPwd(e.target.value)}
              placeholder="新密码（至少 8 位）"
              autoComplete="new-password"
              required
            />
            <input
              className={input}
              type="password"
              value={newPwd2}
              onChange={(e) => setNewPwd2(e.target.value)}
              placeholder="确认新密码"
              autoComplete="new-password"
              required
            />
            <button type="submit" disabled={pwdBusy} className={primaryBtn}>
              {pwdBusy ? '提交中…' : '确认修改'}
            </button>
          </form>
          {pwdMsg && <div className="mt-2 text-[14px] text-emerald-400">{pwdMsg}</div>}
          {pwdErr && <div className="mt-2 text-[14px] text-red-400">{pwdErr}</div>}
        </section>
      </div>
    </div>
  )
}
