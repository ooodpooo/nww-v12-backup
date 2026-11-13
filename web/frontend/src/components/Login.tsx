import { useEffect, useState, type FormEvent } from 'react'
import { login, register, redeemCode, storeTier, fetchRegisterConfig, type RegisterConfig } from '../auth'

export default function Login({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [user, setUser] = useState('')
  const [password, setPassword] = useState('')
  const [nickname, setNickname] = useState('')
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [regConfig, setRegConfig] = useState<RegisterConfig | null>(null)

  useEffect(() => {
    fetchRegisterConfig().then(setRegConfig).catch(() => setRegConfig({ allow_register: false, require_invite: false }))
  }, [])

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      if (mode === 'login') {
        await login(user.trim(), password)
        // 序列号选填：登录成功后自动核销充值；核销失败不阻断登录
        if (code.trim()) {
          try {
            const r = await redeemCode(code.trim())
            storeTier(r.tier, r.expires_at)
          } catch (codeErr) {
            setError(`已登录，但序列号核销失败：${String(codeErr instanceof Error ? codeErr.message : codeErr)}`)
            setBusy(false)
            return
          }
        }
      } else {
        const inviteCode = regConfig?.require_invite ? code.trim() : undefined
        await register(user.trim(), password, nickname.trim(), inviteCode)
        await login(user.trim(), password)
        // 开放注册模式下，code 是充值序列号，注册后核销；邀请注册模式下邀请码已在后端消费
        if (!regConfig?.require_invite && code.trim()) {
          try {
            await redeemCode(code.trim())
          } catch { /* 注册通道核销失败不阻断 */ }
        }
      }
      onLoggedIn()
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err))
    } finally {
      setBusy(false)
    }
  }

  const input =
    'w-full bg-[#1a1a1a] border border-[#333] rounded px-3 py-2 text-[14px] text-[#ccc] placeholder:text-[#555] outline-none focus:border-[#f59e0b] transition-colors'

  return (
    <div className="h-full w-full flex items-center justify-center bg-[#0d0d0d] p-4 box-border">
      <div className="w-full max-w-sm rounded-lg border border-[#2a2a2a] bg-[#181818] p-6">
        <div className="text-[14px] font-semibold text-white tracking-wide mb-1">NWW-novel</div>
        <div className="text-[14px] text-[#777] mb-5">
          {mode === 'login' ? '登录以继续' : '注册新账号'}
        </div>

        <form onSubmit={submit} className="flex flex-col gap-3">
          <input
            className={input}
            value={user}
            onChange={(e) => setUser(e.target.value)}
            placeholder={mode === 'login' ? '账号（邮箱或用户名）' : '邮箱'}
            autoComplete="username"
            required
          />
          {mode === 'register' && (
            <input
              className={input}
              value={nickname}
              onChange={(e) => setNickname(e.target.value)}
              placeholder="昵称（可选）"
            />
          )}
          <input
            className={input}
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="密码"
            autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            required
          />
          <input
            className={input}
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder={
              mode === 'register' && regConfig?.require_invite
                ? '邀请码（必填）'
                : mode === 'register'
                  ? '序列号（选填，充值续费用）'
                  : '邀请码 / 序列号（选填，充值续费用）'
            }
            autoComplete="off"
            required={mode === 'register' && regConfig?.require_invite}
          />
          {error && <div className="text-[14px] text-red-400">{error}</div>}
          <button
            type="submit"
            disabled={busy}
            className="mt-1 w-full py-2 rounded text-[14px] font-medium bg-[#f59e0b] text-black hover:bg-[#fbbf24] disabled:opacity-50 transition-colors"
          >
            {busy ? '请稍候…' : mode === 'login' ? '登录' : '注册并登录'}
          </button>
        </form>

        <button
          onClick={() => {
            setMode(mode === 'login' ? 'register' : 'login')
            setError(null)
          }}
          className="mt-4 text-[14px] text-[#777] hover:text-[#f59e0b] transition-colors"
        >
          {mode === 'login' ? '没有账号？注册' : '已有账号？登录'}
        </button>
      </div>
    </div>
  )
}
