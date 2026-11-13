import { useState, useEffect } from 'react'

interface UserProfile {
  email: string
  nickname: string
  created_at: string | null
  plan: string | null
  plan_info: {
    id: string
    name: string
    price: number
    unlimited: boolean
    books_limit: number | null
    chapters_limit: number | null
  } | null
  license: string | null
  expire: string | null
  is_expired: boolean
}

interface Order {
  order_id: string
  plan: string
  status: string
  created_at: string
}

interface UserCenterProps {
  onClose: () => void
}

export default function UserCenter({ onClose }: UserCenterProps) {
  const [tab, setTab] = useState<'profile' | 'orders'>('profile')
  const [token, setToken] = useState<string | null>(null)
  const [profile, setProfile] = useState<UserProfile | null>(null)
  const [orders, setOrders] = useState<Order[]>([])
  const [loading, setLoading] = useState(false)

  // 登录表单
  const [isLogin, setIsLogin] = useState(true)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [nickname, setNickname] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    const savedToken = localStorage.getItem('nww_token')
    if (savedToken) {
      setToken(savedToken)
      fetchProfile(savedToken)
    }
  }, [])

  const fetchProfile = async (t: string) => {
    try {
      const res = await fetch('/auth/profile', {
        headers: { Authorization: `Bearer ${t}` },
      })
      if (res.ok) {
        const data = await res.json()
        setProfile(data)
        fetchOrders(t)
      } else {
        localStorage.removeItem('nww_token')
        setToken(null)
      }
    } catch (e) {
      console.error('Failed to fetch profile:', e)
    }
  }

  const fetchOrders = async (t: string) => {
    try {
      const res = await fetch('/auth/orders', {
        headers: { Authorization: `Bearer ${t}` },
      })
      if (res.ok) {
        const data = await res.json()
        setOrders(data.orders || [])
      }
    } catch (e) {
      console.error('Failed to fetch orders:', e)
    }
  }

  const handleAuth = async () => {
    setError('')
    setLoading(true)

    try {
      const endpoint = isLogin ? '/auth/login' : '/auth/register'
      const body = isLogin
        ? { user: email, password }
        : { email, password, nickname }

      const res = await fetch(`/auth${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })

      const data = await res.json()

      if (!res.ok) {
        setError(data.detail || 'Operation failed')
        return
      }

      if (isLogin) {
        localStorage.setItem('nww_token', data.token)
        setToken(data.token)
        fetchProfile(data.token)
      } else {
        // 注册成功，自动登录
        const loginRes = await fetch('/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ user: email, password }),
        })
        const loginData = await loginRes.json()
        if (loginRes.ok) {
          localStorage.setItem('nww_token', loginData.token)
          setToken(loginData.token)
          fetchProfile(loginData.token)
        }
      }
    } catch (e) {
      setError('Network error')
    } finally {
      setLoading(false)
    }
  }

  const handleLogout = () => {
    localStorage.removeItem('nww_token')
    setToken(null)
    setProfile(null)
    setOrders([])
  }

  const formatDate = (dateStr: string | null) => {
    if (!dateStr) return '-'
    return new Date(dateStr).toLocaleDateString('zh-CN')
  }

  // 未登录 - 显示登录/注册表单
  if (!token) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
        <div
          className="bg-[#1e1e1e] rounded-lg border border-[#333] w-[360px] overflow-hidden"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex items-center justify-between px-4 py-3 border-b border-[#333]">
            <h3 className="text-[14px] font-semibold text-white">
              {isLogin ? '登录' : '注册'}
            </h3>
            <button onClick={onClose} className="text-[#777] hover:text-white">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M18 6L6 18M6 6l12 12" />
              </svg>
            </button>
          </div>

          <div className="p-4">
            {!isLogin && (
              <div className="mb-3">
                <label className="block text-[14px] text-[#999] mb-1">昵称</label>
                <input
                  value={nickname}
                  onChange={(e) => setNickname(e.target.value)}
                  placeholder="可选"
                  className="w-full bg-[#2a2a2a] border border-[#444] rounded px-3 py-2 text-[14px] text-white placeholder:text-[#666] outline-none focus:border-[#f59e0b]"
                />
              </div>
            )}

            <div className="mb-3">
              <label className="block text-[14px] text-[#999] mb-1">邮箱</label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="your@email.com"
                className="w-full bg-[#2a2a2a] border border-[#444] rounded px-3 py-2 text-[14px] text-white placeholder:text-[#666] outline-none focus:border-[#f59e0b]"
              />
            </div>

            <div className="mb-4">
              <label className="block text-[14px] text-[#999] mb-1">密码</label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••"
                className="w-full bg-[#2a2a2a] border border-[#444] rounded px-3 py-2 text-[14px] text-white placeholder:text-[#666] outline-none focus:border-[#f59e0b]"
              />
            </div>

            {error && (
              <div className="mb-3 text-[14px] text-red-400">{error}</div>
            )}

            <button
              onClick={handleAuth}
              disabled={loading || !email || !password}
              className="w-full py-2 text-[14px] font-medium text-[#181818] bg-[#f59e0b] hover:bg-[#d97706] rounded disabled:opacity-50 transition-colors"
            >
              {loading ? '处理中...' : isLogin ? '登录' : '注册'}
            </button>

            <div className="mt-3 text-center">
              <button
                onClick={() => { setIsLogin(!isLogin); setError('') }}
                className="text-[14px] text-[#f59e0b] hover:underline"
              >
                {isLogin ? '没有账号？立即注册' : '已有账号？去登录'}
              </button>
            </div>
          </div>
        </div>
      </div>
    )
  }

  // 已登录 - 显示用户中心
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className="bg-[#1e1e1e] rounded-lg border border-[#333] w-[480px] max-h-[80vh] overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-[#333]">
          <h3 className="text-[14px] font-semibold text-white">用户中心</h3>
          <button onClick={onClose} className="text-[#777] hover:text-white">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M18 6L6 18M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Tabs */}
        <div className="flex border-b border-[#333]">
          <button
            onClick={() => setTab('profile')}
            className={`flex-1 py-2 text-[14px] font-medium transition-colors ${
              tab === 'profile' ? 'text-[#f59e0b] border-b-2 border-[#f59e0b]' : 'text-[#999] hover:text-white'
            }`}
          >
            我的资料
          </button>
          <button
            onClick={() => setTab('orders')}
            className={`flex-1 py-2 text-[14px] font-medium transition-colors ${
              tab === 'orders' ? 'text-[#f59e0b] border-b-2 border-[#f59e0b]' : 'text-[#999] hover:text-white'
            }`}
          >
            订单记录
          </button>
        </div>

        {/* Content */}
        <div className="p-4 overflow-y-auto max-h-[60vh]">
          {tab === 'profile' && profile && (
            <div className="space-y-4">
              {/* 基本信息 */}
              <div className="bg-[#252525] rounded-lg p-3">
                <div className="text-[14px] text-[#777] mb-2">基本信息</div>
                <div className="flex items-center gap-3">
                  <div className="w-10 h-10 rounded-full bg-[#f59e0b] flex items-center justify-center text-[14px] font-bold text-[#181818]">
                    {profile.nickname[0].toUpperCase()}
                  </div>
                  <div>
                    <div className="text-[14px] text-white font-medium">{profile.nickname}</div>
                    <div className="text-[14px] text-[#777]">{profile.email}</div>
                  </div>
                </div>
              </div>

              {/* 套餐信息 */}
              <div className="bg-[#252525] rounded-lg p-3">
                <div className="text-[14px] text-[#777] mb-2">当前套餐</div>
                {profile.plan && profile.plan_info ? (
                  <div>
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-[14px] text-[#f59e0b] font-medium">{profile.plan_info.name}</span>
                      {profile.is_expired ? (
                        <span className="text-[14px] text-red-400">已过期</span>
                      ) : (
                        <span className="text-[14px] text-green-400">有效</span>
                      )}
                    </div>
                    <div className="text-[14px] text-[#999] space-y-1">
                      <div>有效期至: {formatDate(profile.expire)}</div>
                      {!profile.plan_info.unlimited && (
                        <>
                          <div>书籍限制: {profile.plan_info.books_limit} 本</div>
                          <div>章节限制: 每本 {profile.plan_info.chapters_limit} 章</div>
                        </>
                      )}
                      {profile.plan_info.unlimited && (
                        <div>不限书籍和章节</div>
                      )}
                    </div>
                    {profile.license && (
                      <div className="mt-2 pt-2 border-t border-[#333]">
                        <div className="text-[14px] text-[#777]">License</div>
                        <div className="text-[14px] font-mono text-[#f59e0b]">{profile.license}</div>
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="text-[14px] text-[#777]">
                    暂无套餐，
                    <button className="text-[#f59e0b] hover:underline" onClick={onClose}>
                      去购买
                    </button>
                  </div>
                )}
              </div>

              {/* 账号信息 */}
              <div className="bg-[#252525] rounded-lg p-3">
                <div className="text-[14px] text-[#777] mb-2">账号信息</div>
                <div className="text-[14px] text-[#999] space-y-1">
                  <div>注册时间: {formatDate(profile.created_at)}</div>
                </div>
              </div>
            </div>
          )}

          {tab === 'orders' && (
            <div>
              {orders.length === 0 ? (
                <div className="text-center py-8 text-[14px] text-[#777]">暂无订单记录</div>
              ) : (
                <div className="space-y-2">
                  {orders.map((order) => (
                    <div key={order.order_id} className="bg-[#252525] rounded-lg p-3">
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-[14px] text-white">{order.order_id}</span>
                        <span className={`text-[14px] ${order.status === 'paid' ? 'text-green-400' : 'text-yellow-400'}`}>
                          {order.status === 'paid' ? '已支付' : '待支付'}
                        </span>
                      </div>
                      <div className="text-[14px] text-[#777]">
                        {order.plan} · {formatDate(order.created_at)}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-4 py-3 border-t border-[#333] flex justify-end">
          <button
            onClick={handleLogout}
            className="px-4 py-2 text-[14px] text-[#999] hover:text-red-400 transition-colors"
          >
            退出登录
          </button>
        </div>
      </div>
    </div>
  )
}
