/** 鉴权：token 存取 + 带 Bearer 的 fetch 包装 + 登录/注册/登出（同源 /auth/*）。 */

const TOKEN_KEY = 'nww_web_token'
const USER_KEY = 'nww_web_user'
const ROLE_KEY = 'nww_web_role'
const TIER_KEY = 'nww_web_tier'
const EXPIRES_KEY = 'nww_web_expires'

export interface LoginResult {
  token: string
  expires: number
  role: string
  tier?: string | null
  expires_at?: string | null
}

/** token 变化/失效时广播，App 据此切登录页 */
export const AUTH_CHANGED = 'nww-auth-changed'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function getStoredUser(): string | null {
  return localStorage.getItem(USER_KEY)
}

export function getStoredRole(): string | null {
  return localStorage.getItem(ROLE_KEY)
}

function setAuth(token: string, user: string, role: string = 'user', tier?: string | null, expiresAt?: string | null) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, user)
  localStorage.setItem(ROLE_KEY, role)
  if (tier) localStorage.setItem(TIER_KEY, tier)
  else localStorage.removeItem(TIER_KEY)
  if (expiresAt) localStorage.setItem(EXPIRES_KEY, expiresAt)
  else localStorage.removeItem(EXPIRES_KEY)
}

export function logout() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
  localStorage.removeItem(ROLE_KEY)
  localStorage.removeItem(TIER_KEY)
  localStorage.removeItem(EXPIRES_KEY)
  window.dispatchEvent(new Event(AUTH_CHANGED))
}

function onUnauthorized() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
  localStorage.removeItem(ROLE_KEY)
  localStorage.removeItem(TIER_KEY)
  localStorage.removeItem(EXPIRES_KEY)
  window.dispatchEvent(new Event(AUTH_CHANGED))
}

/** 带 Bearer 的 fetch：401 时清 token 并广播，让 App 回到登录页 */
export async function authFetch(input: string, init?: RequestInit): Promise<Response> {
  const token = getToken()
  const headers = new Headers(init?.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const r = await fetch(input, { ...init, headers })
  if (r.status === 401) onUnauthorized()
  return r
}

async function j<T>(rp: Promise<Response>): Promise<T> {
  const r = await rp
  if (!r.ok) {
    let detail: unknown = r.statusText
    try {
      const body = await r.json()
      if (body && body.detail) detail = body.detail
    } catch {
      /* ignore */
    }
    // 结构化 detail（FastAPI 422 数组等）序列化展示，防 "[object Object]" 吞错
    const msg = typeof detail === 'string' ? detail : JSON.stringify(detail)
    throw new Error(msg)
  }
  return r.json()
}

export async function login(user: string, password: string): Promise<LoginResult> {
  const res = await j<LoginResult>(
    fetch('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user, password }),
    }),
  )
  setAuth(res.token, user, res.role, res.tier, res.expires_at)
  return res
}

/** 核销后刷新本地缓存的套餐信息 */
export function storeTier(tier: string | null | undefined, expiresAt: string | null | undefined) {
  if (tier) localStorage.setItem(TIER_KEY, tier)
  else localStorage.removeItem(TIER_KEY)
  if (expiresAt) localStorage.setItem(EXPIRES_KEY, expiresAt)
  else localStorage.removeItem(EXPIRES_KEY)
}

export async function register(email: string, password: string, nickname: string, inviteCode?: string): Promise<void> {
  await j(
    fetch('/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password, nickname, invite_code: inviteCode }),
    }),
  )
}

export interface RegisterConfig {
  allow_register: boolean
  require_invite: boolean
}

export async function fetchRegisterConfig(): Promise<RegisterConfig> {
  return j<RegisterConfig>(fetch('/auth/register/config'))
}

/* ─────────────  管理员：用户管理  ───────────── */

export interface AdminUser {
  email: string
  nickname: string
  role: string
  created_at: string | null
  tier?: string | null
  expires_at?: string | null
}

/** 核销序列号（充值/续费/升级） */
export async function redeemCode(code: string): Promise<{ tier: string | null; expires_at: string | null }> {
  return j(
    authFetch('/auth/redeem', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    }),
  )
}

// ---------- API Key 管理 ----------
export interface ApiKeyInfo {
  prefix: string
  name: string
  created_at: string
  last_used_at: string | null
}

export interface ApiKeyCreated extends ApiKeyInfo {
  key: string  // 仅创建时返回完整值
}

export async function listApiKeys(): Promise<ApiKeyInfo[]> {
  const res = await j<{ api_keys: ApiKeyInfo[] }>(authFetch('/auth/api-keys'))
  return res.api_keys
}

export async function createApiKey(name: string): Promise<ApiKeyCreated> {
  return j(
    authFetch('/auth/api-keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    }),
  )
}

export async function revokeApiKey(prefix: string): Promise<void> {
  await j(authFetch(`/auth/api-keys/${prefix}`, { method: 'DELETE' }))
}

export interface SerialCode {
  code: string
  tier: string
  days: number
  usage?: string
  created_at: string
  used_by: string | null
  used_at: string | null
}

export async function adminListCodes(): Promise<SerialCode[]> {
  const res = await j<{ codes: SerialCode[] }>(authFetch('/auth/codes'))
  return res.codes
}

export async function adminGenerateCodes(tier: string, days: number, count: number, usage: string = 'redeem'): Promise<string[]> {
  const res = await j<{ codes: string[] }>(
    authFetch('/auth/codes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tier, days, count, usage }),
    }),
  )
  return res.codes
}

export async function adminListUsers(): Promise<AdminUser[]> {
  const res = await j<{ users: AdminUser[] }>(authFetch('/auth/users'))
  return res.users
}

export async function adminCreateUser(email: string, password: string, nickname: string, role: string): Promise<AdminUser> {
  return j<AdminUser>(
    authFetch('/auth/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password, nickname, role }),
    }),
  )
}

export async function adminDeleteUser(email: string): Promise<void> {
  await j(authFetch(`/auth/users/${encodeURIComponent(email)}`, { method: 'DELETE' }))
}

export async function adminResetPassword(email: string, password: string): Promise<void> {
  await j(
    authFetch(`/auth/users/${encodeURIComponent(email)}/password`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    }),
  )
}

/* ─────────────  个人中心  ───────────── */

export interface Profile {
  email: string
  nickname: string
  role: string
  created_at: string | null
  tier?: string | null
  expires_at?: string | null
}

export async function fetchProfile(): Promise<Profile> {
  return j<Profile>(authFetch('/auth/profile'))
}

export async function changePassword(oldPassword: string, newPassword: string): Promise<void> {
  await j(
    authFetch('/auth/password', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    }),
  )
}
