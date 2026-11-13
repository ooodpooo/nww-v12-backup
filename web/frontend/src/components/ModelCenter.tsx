import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { api, type AgentInfo, type ModelConfig, type ModelProvider } from '../api'

/**
 * 模型聚合中心（2026-09-11 新增，纯增量；嵌入左侧资产栏 320px 自适应窄版）：
 *   - 供应商（OpenAI 兼容服务）接入 + 连接测试 + 模型自动发现
 *   - 模型池 = 各供应商可用模型合集（NWW 可选可用模型）
 *   - 全局默认模型 + 按 agent 定制不同模型（agent_routes 覆盖）
 * 保存即生效（后端每次调用动态读取 model_config.json）。
 */

const inputCls =
  'bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 text-[14px] text-[#ccc] placeholder:text-[#555] outline-none focus:border-[#f59e0b] transition-colors w-full'
const btnSm =
  'px-2 py-1 text-[13px] rounded border transition-colors'

/** Agent 职能环节分组顺序（须与后端 registry.STAGE_ORDER 一致） */
const AGENT_STAGE_ORDER = ['创意与规划', '写作与出稿', '审查与修订', '素材与分析', '未接入']
/** 「未接入」= 生产链无调用点或路由键为死键，改了也不生效 → 灰显且禁止选择 */
const UNWIRED_STAGE = '未接入'

/** 生成唯一池模型 id：provider:model */
function poolId(provider: string, model: string) {
  return (provider + ':' + model).replace(/[^\w.:-]+/g, '_').replace(/^:+|:+$/g, '')
}

/** 移除图标（X） */
function XIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M18 6 6 18" />
      <path d="m6 6 12 12" />
    </svg>
  )
}

/** 可折叠分块：标题可点击，展开/折叠，默认折叠 */
function CollapseSection({ title, children }: { title: string; children: ReactNode }) {
  const [open, setOpen] = useState(false)
  return (
    <section className="space-y-1.5">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-1.5 text-left group"
      >
        <h3 className="text-[13px] font-semibold text-white group-hover:text-[#f59e0b] transition-colors">{title}</h3>
        <span className="flex-1" />
        <svg className={`w-3 h-3 shrink-0 text-[#666] transition-transform ${open ? 'rotate-180' : ''}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="m6 9 6 6 6-6" />
        </svg>
      </button>
      {open && children}
    </section>
  )
}

export default function ModelCenter() {
  const [cfg, setCfg] = useState<ModelConfig>(() => ({
    configured: false,
    providers: [],
    pool: [],
    default: '',
    agent_routes: {},
  }))
  const [agents, setAgents] = useState<AgentInfo[]>([])
  // 「未接入」组默认折叠（这些 agent 改了模型也不生效，避免干扰视线）
  const [showUnwired, setShowUnwired] = useState(false)
  // 按职能环节分组（顺序固定；后端未给 stage 的落到「其它」）
  const agentGroups = useMemo(() => {
    const map = new Map<string, AgentInfo[]>()
    for (const s of AGENT_STAGE_ORDER) map.set(s, [])
    for (const a of agents) {
      // 接线事实优先：生产链根本不调用的 agent 一律归「未接入」（配了也不生效）
      const s = a.wired === 'none' ? UNWIRED_STAGE : (a.stage || '其它')
      if (!map.has(s)) map.set(s, [])
      map.get(s)!.push(a)
    }
    return [...map.entries()].filter(([, l]) => l.length > 0)
  }, [agents])
  // 路由键体检结果（后端算好）：配了但不生效的键 → 显式暴露，不再静默
  // 本地再按当前 agent_routes 过滤一次：删除后警告条即时消失（不必等保存重载）
  const diag = cfg.route_diagnostics
  const orphanKeys = (diag?.orphan_keys ?? []).filter((o) => o.key in cfg.agent_routes)
  const deadKeys = (diag?.dead_keys ?? []).filter((d) => d.key in cfg.agent_routes)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  // 添加供应商表单
  const [npId, setNpId] = useState('')
  const [npUrl, setNpUrl] = useState('')
  const [npKey, setNpKey] = useState('')
  // 手动添加模型表单
  const [nmProvider, setNmProvider] = useState('')
  const [nmModel, setNmModel] = useState('')
  const [nmLabel, setNmLabel] = useState('')
  // 行级测试/发现结果 { providerId: { state, msg, models? } }
  const [rowTest, setRowTest] = useState<Record<string, { state: 'idle' | 'ok' | 'err' | 'busy'; msg: string; models?: string[] }>>({})
  // 展开中的供应商 id（null = 全部折叠）
  const [openProvider, setOpenProvider] = useState<string | null>(null)
  // 池内模型可用性检测结果（ok/detail）；不可用模型红色提示 + 下拉禁用
  const [poolHealth, setPoolHealth] = useState<Record<string, { ok: boolean; detail: string }>>({})
  const [poolValidating, setPoolValidating] = useState(false)

  const runValidate = useCallback(async () => {
    setPoolValidating(true)
    try {
      const h = await api.validatePool()
      setPoolHealth(h)
    } catch {
      // 检测失败不影响主流程
    } finally {
      setPoolValidating(false)
    }
  }, [])

  const load = useCallback(async () => {
    try {
      const [c, a] = await Promise.all([api.getModelConfig(), api.agentsRegistry()])
      setCfg(c)
      // book-coach 已提升为 ChatBox 对话模型的共享配置（③ 默认对话模型，绑定 agent_routes['book-coach']），
      // 不再混入「Agent 定制」列表，避免与其形成三层覆盖
      setAgents(Array.isArray(a) ? a : [])
      setError(null)
      runValidate()
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [runValidate])

  useEffect(() => { load() }, [load])

  // 与 ChatBox 模型选择器双向同步：ChatBox 侧保存后广播，这里仅同步 book-coach 键（不动其它未保存编辑）
  useEffect(() => {
    const onChanged = () => {
      api.getModelConfig()
        .then((c) => setCfg((prev) => {
          const routes = { ...prev.agent_routes }
          const bc = c.agent_routes?.['book-coach']
          if (bc) routes['book-coach'] = bc
          else delete routes['book-coach']
          return { ...prev, agent_routes: routes }
        }))
        .catch(() => { /* ignore */ })
    }
    window.addEventListener('nww-model-config-changed', onChanged)
    return () => window.removeEventListener('nww-model-config-changed', onChanged)
  }, [])

  const providerNames = useMemo(() => cfg.providers.map((p) => p.id), [cfg.providers])
  const poolOptions = useMemo(() => cfg.pool.map((m) => ({ id: m.id, label: m.label || m.id, model: m.model })), [cfg.pool])
  /** 「跟随全局默认」文案：解析全局默认池模型的显示名 */
  const defaultPoolLabel = useMemo(() => {
    const m = cfg.pool.find((x) => x.id === cfg.default)
    return m ? (m.label || m.model) : (cfg.default || '环境变量路由')
  }, [cfg.pool, cfg.default])
  /**
   * 全局默认指向的池条目是否已不存在（池模型/供应商被删、手改配置文件）。
   * 下拉值无匹配项时浏览器会显示空白 → 必须显式暴露，避免「看着是空的、其实配了个死 id」
   * （禁静默降级：后端 _endpoint_from_pool 解析失败会回落环境变量，用户无从察觉）。
   */
  const defaultMissing = useMemo(
    () => !!cfg.default && !cfg.pool.some((x) => x.id === cfg.default),
    [cfg.default, cfg.pool],
  )
  /** 模型池按供应商分组（组内保持池内顺序），供分组面板展示 */
  const poolGroups = useMemo(() => {
    const g = new Map<string, typeof cfg.pool>()
    for (const m of cfg.pool) g.set(m.provider, [...(g.get(m.provider) ?? []), m])
    return [...g.entries()].map(([provider, models]) => ({ provider, models }))
  }, [cfg.pool])

  const dirty = saved

  /** 保存到后端 */
  const save = async () => {
    setSaving(true)
    setSaved(null)
    try {
      const next = await api.saveModelConfig(cfg)
      setCfg(next)
      setSaved('已保存，即时生效')
      // 广播：ChatBox 模型选择器据此重取 agent_routes['book-coach']，实现双向同步
      window.dispatchEvent(new Event('nww-model-config-changed'))
    } catch (e) {
      setError(String(e))
    } finally {
      setSaving(false)
    }
  }

  /** 级联清理：删除供应商时移除其池模型、对它的降级引用与 agent 路由 */
  const removeProvider = (id: string) => {
    const poolIds = cfg.pool.filter((m) => m.provider === id).map((m) => m.id)
    const routes = { ...cfg.agent_routes }
    for (const k of Object.keys(routes)) if (poolIds.includes(routes[k])) delete routes[k]
    const ndefault = poolIds.includes(cfg.default) ? '' : cfg.default
    setCfg({
      ...cfg,
      providers: cfg.providers.filter((p) => p.id !== id),
      pool: cfg.pool.filter((m) => m.provider !== id).map((m) => ({ ...m, fallback_pool_ids: (m.fallback_pool_ids || []).filter((f) => !poolIds.includes(f)) })),
      default: ndefault,
      agent_routes: routes,
    })
    setRowTest((r) => { const n = { ...r }; delete n[id]; return n })
  }

  const updateProvider = (id: string, patch: Partial<ModelProvider>) => {
    setCfg({ ...cfg, providers: cfg.providers.map((p) => (p.id === id ? { ...p, ...patch } : p)) })
  }

  /** 一键导入模型的池条目（已存在跳过） */
  const importModels = (provider: string, models: string[]) => {
    if (!models.length) return
    setCfg((c) => {
      const existing = new Set(c.pool.map((m) => m.id))
      const add = models.filter((m) => m && !existing.has(poolId(provider, m)))
      return { ...c, pool: [...c.pool, ...add.map((m) => ({ id: poolId(provider, m), provider, model: m }))] }
    })
  }

  /** 行内测试连接 */
  const runTest = async (p: ModelProvider) => {
    setRowTest((r) => ({ ...r, [p.id]: { state: 'busy', msg: '测试中…' } }))
    try {
      const res = await api.testModelConnection(p.base_url, p.api_key)
      if (res.ok) {
        setRowTest((r) => ({ ...r, [p.id]: { state: 'ok', msg: `连接成功 · ${(res.models || []).length} 个模型`, models: res.models || [] } }))
      } else {
        setRowTest((r) => ({ ...r, [p.id]: { state: 'err', msg: res.detail || '连接失败' } }))
      }
    } catch (e) {
      setRowTest((r) => ({ ...r, [p.id]: { state: 'err', msg: String(e) } }))
    }
  }

  /** 发现模型清单 */
  const runDiscover = async (p: ModelProvider) => {
    setRowTest((r) => ({ ...r, [p.id]: { state: 'busy', msg: '拉取模型清单…' } }))
    try {
      const res = await api.discoverModels(p.base_url, p.api_key)
      if (res.ok) {
        setRowTest((r) => ({ ...r, [p.id]: { state: 'ok', msg: `发现 ${(res.models || []).length} 个模型，点下方「导入池」`, models: res.models || [] } }))
      } else {
        setRowTest((r) => ({ ...r, [p.id]: { state: 'err', msg: res.detail || '发现失败' } }))
      }
    } catch (e) {
      setRowTest((r) => ({ ...r, [p.id]: { state: 'err', msg: String(e) } }))
    }
  }

  /** 新增供应商 */
  const addProvider = () => {
    const id = npId.trim()
    const url = npUrl.trim()
    if (!id || !url) { setError('供应商 id 与 base_url 必填'); return }
    if (cfg.providers.some((p) => p.id === id)) { setError(`供应商 ${id} 已存在`); return }
    setCfg({ ...cfg, providers: [...cfg.providers, { id, base_url: url, api_key: npKey.trim(), enabled: true }] })
    setNpId(''); setNpUrl(''); setNpKey('')
    setError(null)
  }

  /** 手动添加池模型 */
  const addPoolModel = () => {
    const prov = nmProvider.trim()
    const model = nmModel.trim()
    if (!prov || !model) { setError('请选择供应商并填写模型名'); return }
    if (cfg.pool.some((m) => m.id === poolId(prov, model))) { setError(`模型 ${poolId(prov, model)} 已在池中`); return }
    setCfg({ ...cfg, pool: [...cfg.pool, { id: poolId(prov, model), provider: prov, model, label: nmLabel.trim() || model }] })
    setNmModel(''); setNmLabel('')
    setError(null)
  }

  const removePool = (id: string) => {
    const routes = { ...cfg.agent_routes }
    for (const k of Object.keys(routes)) if (routes[k] === id) delete routes[k]
    setCfg({
      ...cfg,
      // 同时清理其它模型指向它的降级引用
      pool: cfg.pool.filter((m) => m.id !== id).map((m) => ({ ...m, fallback_pool_ids: (m.fallback_pool_ids || []).filter((f) => f !== id) })),
      default: cfg.default === id ? '' : cfg.default,
      agent_routes: routes,
    })
  }

  /** agent 路由下拉：'' = 跟随全局默认；其它 = 池模型 id */
  const setAgentRoute = (name: string, val: string) => {
    const routes = { ...cfg.agent_routes }
    if (val === '') delete routes[name]
    else routes[name] = val
    setCfg({ ...cfg, agent_routes: routes })
  }

  /** 全局默认模型：'' = 未设置（回落 env NWW_MVP_LLM_MODEL）；其它 = 池模型 id */
  const setDefaultModel = (val: string) => {
    setCfg({ ...cfg, default: val })
  }

  /** 删除一条 agent 路由（清理无效键：配了也不会生效，留着只会误导） */
  const removeAgentRoute = (name: string) => {
    const routes = { ...cfg.agent_routes }
    delete routes[name]
    setCfg({ ...cfg, agent_routes: routes })
  }

  return (
    <div className="h-full flex flex-col overflow-hidden">
      {/* 头：标题 + 保存 */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-[#1e1e1e] shrink-0">
        <span className="text-[14px] font-semibold text-white">模型</span>
        <div className="flex-1" />
        {saved && <span className="text-[12px] text-emerald-400">{saved}</span>}
        <button onClick={save} disabled={saving}
          className="px-2.5 py-1 text-[13px] font-medium rounded bg-[#f59e0b] text-black hover:bg-[#fbbf24] disabled:opacity-50 transition-colors">
          {saving ? '保存中…' : '保存'}
        </button>
      </div>

      {loading ? (
        <div className="flex-1 flex items-center justify-center text-[14px] text-[#666]">加载中…</div>
      ) : error && !dirty ? (
        <div className="flex-1 flex items-center justify-center text-[14px] text-red-400 px-3 text-center">{error}</div>
      ) : (
        <div className="flex-1 overflow-y-auto px-3 py-2 space-y-4">
          {error && <div className="text-[13px] text-red-400 break-all">{error}</div>}

          {/* ── 供应商 ── */}
          <CollapseSection title="① 供应商（OpenAI 兼容）">
            {cfg.providers.length === 0 && <div className="text-[13px] text-[#666]">尚未接入。添加后先「测试连接」，通过后模型进入模型池。</div>}
            {cfg.providers.map((p) => {
              const r = rowTest[p.id] ?? { state: 'idle' as const, msg: '' }
              const open = openProvider === p.id
              return (
                <div key={p.id} className="rounded-lg border border-[#2a2a2a] bg-[#1e1e1e] overflow-hidden">
                  <button onClick={() => setOpenProvider(open ? null : p.id)}
                    className="w-full flex items-center gap-1.5 px-2 py-1.5 hover:bg-[#242424] transition-colors text-left">
                    <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${r.state === 'ok' ? 'bg-emerald-400' : r.state === 'err' ? 'bg-red-400' : 'bg-[#555]'}`} />
                    <span className="flex-1 min-w-0 truncate text-[14px] text-white">{p.id}</span>
                    <span className={`text-[11px] shrink-0 ${p.enabled ? 'text-emerald-400/90' : 'text-[#666]'}`}>{p.enabled ? '启用' : '停用'}</span>
                    <svg className={`w-3 h-3 shrink-0 text-[#666] transition-transform ${open ? 'rotate-180' : ''}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="m6 9 6 6 6-6" />
                    </svg>
                  </button>
                  {open && (
                    <div className="px-2 pb-2 pt-1.5 flex flex-col gap-1.5 border-t border-[#2a2a2a]">
                      <div className="flex items-center gap-1.5">
                        <label className="flex items-center gap-1 text-[13px] text-[#999] cursor-pointer shrink-0">
                          <input type="checkbox" checked={p.enabled} onChange={(e) => updateProvider(p.id, { enabled: e.target.checked })} className="accent-[#f59e0b]" />
                          启用
                        </label>
                        <div className="flex-1" />
                        <button onClick={() => removeProvider(p.id)} title="删除供应商（级联移除其池模型）" className={`${btnSm} border-red-900/60 text-red-400 hover:bg-red-900/20 shrink-0`}>删除</button>
                      </div>
                      <input className={inputCls} value={p.base_url} onChange={(e) => updateProvider(p.id, { base_url: e.target.value })} placeholder="Base URL" title="Base URL" />
                      <input className={inputCls} value={p.api_key} onChange={(e) => updateProvider(p.id, { api_key: e.target.value })} placeholder="API Key（掩码=不修改）" title="API Key" />
                      <div className="flex items-center gap-1.5">
                        <button onClick={() => runTest(p)} className={`${btnSm} flex-1 border-[#666]/60 text-[#ccc] hover:text-white hover:border-[#999]`}>测试连接</button>
                        <button onClick={() => runDiscover(p)} className={`${btnSm} flex-1 border-[#666]/60 text-[#ccc] hover:text-white hover:border-[#999]`}>发现模型</button>
                      </div>
                      {r.state !== 'idle' && (
                        <div className={`text-[12px] break-all ${r.state === 'ok' ? 'text-emerald-400' : r.state === 'err' ? 'text-red-400' : 'text-[#999]'}`}>
                          {r.msg}
                        </div>
                      )}
                      {r.models && r.models.length > 0 && r.state === 'ok' && (
                        <button onClick={() => { importModels(p.id, r.models || []); setRowTest((x) => ({ ...x, [p.id]: { state: 'idle', msg: '' } })) }}
                          className={`${btnSm} bg-[#2a2a2a] text-[#f59e0b] border border-[#f59e0b]/40 hover:bg-[#3a3a3a]`}>
                          导入 {r.models.length} 个模型到池
                        </button>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
            {/* 添加供应商卡片 */}
            <div className="rounded-lg border border-dashed border-[#333] bg-[#161616] p-2 flex flex-col gap-1.5">
              <input className={inputCls} value={npId} onChange={(e) => setNpId(e.target.value)} placeholder="id，如 deepseek" />
              <input className={inputCls} value={npUrl} onChange={(e) => setNpUrl(e.target.value)} placeholder="Base URL，如 https://api.deepseek.com" />
              <input className={inputCls} value={npKey} onChange={(e) => setNpKey(e.target.value)} placeholder="API Key（本地免鉴权可空）" />
              <button onClick={addProvider} className="px-3 py-1 text-[13px] font-medium rounded border border-[#f59e0b]/50 text-[#f59e0b] hover:bg-[#f59e0b]/10 transition-colors">添加供应商</button>
            </div>
          </CollapseSection>

          {/* ── 模型池 ── */}
          <CollapseSection title="② 模型池（NWW 可选可用模型）">
            <div className="flex items-center justify-between mb-1">
              <button onClick={runValidate} disabled={poolValidating}
                className={`${btnSm} border-[#666]/60 text-[#ccc] hover:text-white hover:border-[#999] ${poolValidating ? 'opacity-50 cursor-wait' : ''}`}>
                {poolValidating ? '检测中…' : '检测全部模型可用性'}
              </button>
              <span className="text-[11px] text-[#666]">检测会实际调用一次各模型；本地按需加载模型可能瞬时不可用</span>
            </div>
            {cfg.pool.length === 0 && <div className="text-[13px] text-[#666]">模型池为空：手动添加，或对供应商「发现模型」后一键导入。</div>}
            {/* 按供应商分组卡片：每组一个小卡片（不折叠），组头=供应商 + 模型数 + 不可用计数 */}
            {poolGroups.map(({ provider, models }) => {
              const bad = models.filter((mm) => poolHealth[mm.id] && !poolHealth[mm.id].ok).length
              return (
                <div key={provider} className="rounded-lg border border-[#2a2a2a] bg-[#1e1e1e] overflow-hidden">
                  <div className="px-2 py-1.5 flex items-center gap-1.5 border-b border-[#2a2a2a]">
                    <span className="flex-1 min-w-0 truncate text-[13px] font-semibold text-white">{provider}</span>
                    <span className={`text-[11px] shrink-0 ${bad ? 'text-red-400' : 'text-[#888]'}`}>{models.length} 个模型{bad ? ` · ${bad} 不可用` : ''}</span>
                  </div>
                  <div className="p-1.5 flex flex-col gap-1.5">
                    {models.map((m) => {
                      const ph = poolHealth[m.id]
                      return (
                        <div key={m.id} className="flex items-center gap-1.5 rounded-lg border border-[#2a2a2a] bg-[#161616] px-1.5 py-1">
                          {/* 名称（含可用性徽标） */}
                          <span className={`flex-1 min-w-0 truncate font-mono text-[12px] ${ph && !ph.ok ? 'text-red-400/90' : 'text-[#ccc]'}`} title={`${m.id}\n${ph ? ph.detail : ''}`}>{m.model}</span>
                          {ph && (
                            <span className={`text-[10px] px-1 py-0.5 rounded shrink-0 ${ph.ok ? 'text-emerald-400/90 bg-emerald-400/10' : 'text-red-400 bg-red-400/10'}`} title={ph.detail}>
                              {ph.ok ? '可用' : '不可用'}
                            </span>
                          )}
                          {/* 显示名 */}
                          <input className="w-20 shrink-0 bg-[#1a1a1a] border border-[#333] rounded px-1.5 py-0.5 text-[12px] text-[#ccc] placeholder:text-[#555] outline-none focus:border-[#f59e0b] transition-colors"
                            value={m.label || ''} placeholder="显示名" title="显示标签"
                            onChange={(e) => setCfg({ ...cfg, pool: cfg.pool.map((x) => x.id === m.id ? { ...x, label: e.target.value } : x) })} />
                          {/* 降级备选 */}
                          <select className="w-24 shrink-0 bg-[#1a1a1a] border border-[#333] rounded px-1 py-0.5 text-[12px] text-[#ccc] outline-none focus:border-[#f59e0b] transition-colors"
                            value={m.fallback_pool_ids?.[0] ?? ''} title="降级备选：主模型失败时自动切换的模型"
                            onChange={(e) => setCfg({ ...cfg, pool: cfg.pool.map((x) => x.id === m.id ? { ...x, fallback_pool_ids: e.target.value ? [e.target.value] : [] } : x) })}>
                            <option value="">降级</option>
                            {cfg.pool.filter((x) => x.id !== m.id).map((x) => (
                              <option key={x.id} value={x.id} disabled={!!(poolHealth[x.id] && !poolHealth[x.id].ok)}>
                                {x.label || x.id}{poolHealth[x.id] && !poolHealth[x.id].ok ? '（不可用）' : ''}
                              </option>
                            ))}
                          </select>
                          {/* 删除 */}
                          <button onClick={() => removePool(m.id)} title="移除该模型"
                            className="w-5 h-5 shrink-0 flex items-center justify-center rounded text-[#888] hover:text-red-400 hover:bg-red-900/20 transition-colors">
                            <XIcon />
                          </button>
                        </div>
                      )
                    })}
                  </div>
                </div>
              )
            })}
            {/* 手动添加模型 */}
            <div className="rounded-lg border border-dashed border-[#333] bg-[#161616] p-2 flex flex-col gap-1.5">
              <select className={inputCls} value={nmProvider} onChange={(e) => setNmProvider(e.target.value)}>
                <option value="">供应商…</option>
                {cfg.providers.map((p) => (<option key={p.id} value={p.id}>{p.id}</option>))}
              </select>
              <input className={inputCls} value={nmModel} onChange={(e) => setNmModel(e.target.value)} placeholder="模型名，如 deepseek-chat" />
              <input className={inputCls} value={nmLabel} onChange={(e) => setNmLabel(e.target.value)} placeholder="显示标签（可选）" />
              <button onClick={addPoolModel} disabled={!providerNames.length} className="px-3 py-1 text-[13px] font-medium rounded border border-[#f59e0b]/50 text-[#f59e0b] hover:bg-[#f59e0b]/10 disabled:opacity-40 transition-colors">加入模型池</button>
            </div>
          </CollapseSection>

          {/* ── 默认对话模型 + Agent 定制 ── */}
          <CollapseSection title="③ 默认模型与 Agent 定制">
            {/* 全局默认模型：所有 Agent 未单独定制时的兜底端点。
                此前 setDefaultModel 只定义无入口（UI 上无法设置），此处补齐（2026-11-01）。
                写法与「默认对话模型」一致：池模型 + 不可用禁选，'' = 回落环境变量路由。 */}
            <div className="flex flex-col gap-1">
              <span className="text-[13px] text-[#999]">全局默认模型（Agent 未单独定制时使用）</span>
              <select className={inputCls} value={cfg.default} onChange={(e) => setDefaultModel(e.target.value)}>
                <option value="">未设置（回落环境变量路由）</option>
                {defaultMissing && (
                  <option value={cfg.default}>{cfg.default}（不在模型池，不生效）</option>
                )}
                {poolOptions.map((m) => (
                  <option key={m.id} value={m.id} disabled={!!(poolHealth[m.id] && !poolHealth[m.id].ok)}>
                    {m.label} · {m.model}{poolHealth[m.id] && !poolHealth[m.id].ok ? '（不可用）' : ''}
                  </option>
                ))}
              </select>
              {defaultMissing && (
                <span className="text-[11px] text-[#e0a340] break-all">
                  当前全局默认「{cfg.default}」在模型池里找不到（模型或供应商可能已删除）→ 解析不到端点会回落环境变量。请重选，或先把该模型补回模型池。
                </span>
              )}
            </div>
            <div className="flex flex-col gap-1">
              <span className="text-[13px] text-[#999]">默认对话模型（与 ChatBox 同步）</span>
              <select className={inputCls} value={cfg.agent_routes['book-coach'] || ''} onChange={(e) => setAgentRoute('book-coach', e.target.value)}>
                <option value="">跟随全局默认（{defaultPoolLabel}）</option>
                {poolOptions.map((m) => (
                  <option key={m.id} value={m.id} disabled={!!(poolHealth[m.id] && !poolHealth[m.id].ok)}>
                    {m.label} · {m.model}{poolHealth[m.id] && !poolHealth[m.id].ok ? '（不可用）' : ''}
                  </option>
                ))}
              </select>
            </div>
            {agentGroups.length > 0 && (
              <div className="space-y-1.5">
                {orphanKeys.length + deadKeys.length > 0 && (
                  <div className="rounded border border-[#6b4a1a] bg-[#241a0c] px-2 py-1.5 space-y-1">
                    <p className="text-[12px] text-[#e0a340]">
                      有 {orphanKeys.length + deadKeys.length} 条模型路由不会生效
                    </p>
                    {orphanKeys.map((o) => (
                      <div key={o.key} className="flex items-start gap-1 text-[11px] text-[#c8a06b]">
                        <span className="flex-1 min-w-0 break-all">
                          「{o.key}」生产链不认识这个键 → 永不生效{o.hint ? `；${o.hint}` : ''}
                        </span>
                        <button onClick={() => removeAgentRoute(o.key)} className="shrink-0 underline hover:text-[#f0c070]">
                          删除
                        </button>
                      </div>
                    ))}
                    {deadKeys.map((d) => (
                      <div key={d.key} className="flex items-start gap-1 text-[11px] text-[#c8a06b]">
                        <span className="flex-1 min-w-0 break-all">
                          「{d.key}」生产链不调用（{d.level === 'contract' ? '仅拆书契约可达，默认关闭' : '无调用点'}）→ 配了也不生效
                        </span>
                        <button onClick={() => removeAgentRoute(d.key)} className="shrink-0 underline hover:text-[#f0c070]">
                          删除
                        </button>
                      </div>
                    ))}
                  </div>
                )}
                <span className="text-[13px] text-[#999]">Agent 定制（留「跟随全局」则用默认模型）</span>
                {agentGroups.map(([stage, list]) => {
                  const unwired = stage === UNWIRED_STAGE
                  return (
                    <div key={stage} className="space-y-1">
                      <button
                        onClick={() => unwired && setShowUnwired((v) => !v)}
                        disabled={!unwired}
                        className={`flex w-full items-center gap-1 text-left ${unwired ? 'cursor-pointer' : 'cursor-default'}`}
                      >
                        <span className={`text-[12px] font-medium ${unwired ? 'text-[#777]' : 'text-[#bbb]'}`}>{stage}</span>
                        {unwired && (
                          <span className="text-[11px] text-[#666]">
                            （生产链未接入，改了也不生效{showUnwired ? '·点击收起' : '·点击展开'}）
                          </span>
                        )}
                      </button>
                      {(unwired ? showUnwired : true) && list.map((a) => (
                        <div key={a.name} className="flex items-center gap-1.5">
                          <span
                            className={`flex-1 min-w-0 text-[12px] leading-snug break-all ${unwired ? 'text-[#777]' : 'text-[#ccc]'}`}
                            title={`${a.name}\n${a.description}${a.wired_refs?.length ? `\n接线：${a.wired_refs.join('、')}` : ''}`}
                          >
                            {a.description.split(/[。\n]/)[0]}
                          </span>
                          <select
                            disabled={unwired}
                            className="bg-[#1a1a1a] border border-[#333] rounded px-1.5 py-1 text-[13px] text-[#ccc] outline-none focus:border-[#f59e0b] transition-colors w-32 shrink-0 disabled:opacity-40 disabled:cursor-not-allowed"
                            value={cfg.agent_routes[a.name] || ''}
                            onChange={(e) => setAgentRoute(a.name, e.target.value)}
                          >
                            <option value="">跟随全局（{defaultPoolLabel}）</option>
                            {poolOptions.map((m) => (
                              <option key={m.id} value={m.id} disabled={!!(poolHealth[m.id] && !poolHealth[m.id].ok)}>
                                {m.label}{poolHealth[m.id] && !poolHealth[m.id].ok ? '（不可用）' : ''}
                              </option>
                            ))}
                          </select>
                        </div>
                      ))}
                    </div>
                  )
                })}
              </div>
            )}
          </CollapseSection>
        </div>
      )}
    </div>
  )
}