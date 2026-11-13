import { useState, useEffect } from 'react'
import { CheckCircle2 } from 'lucide-react'

interface Plan {
  id: string
  name: string
  price: number
  days: number
  books: number | null
  chapters_per_book: number | null
  unlimited: boolean
  once_only: boolean
}

interface PurchaseButtonProps {
  onPurchaseComplete?: () => void
  /**
   * 'text' = 顶部栏文字按钮（原形态）；'icon' = 图标按钮。
   * 2026-10-02 顶部栏取消后，会员入口下沉到左侧活动栏底部，窄条只放得下图标。
   */
  variant?: 'text' | 'icon'
}

export default function PurchaseButton({ onPurchaseComplete, variant = 'text' }: PurchaseButtonProps) {
  const [showModal, setShowModal] = useState(false)
  const [plans, setPlans] = useState<Plan[]>([])
  const [loading, setLoading] = useState(false)
  const [email, setEmail] = useState('')
  const [selectedPlan, setSelectedPlan] = useState<string | null>(null)
  const [step, setStep] = useState<'select' | 'confirm' | 'success'>('select')
  const [licenseInfo, setLicenseInfo] = useState<{ license: string; plan: string; expire: string } | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    if (showModal) {
      setError('')
      fetchPlans()
    }
  }, [showModal])

  const fetchPlans = async () => {
    try {
      const res = await fetch('/plans')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setPlans(data.plans || [])
    } catch (e) {
      console.error('Failed to fetch plans:', e)
      setError('套餐服务暂不可用')
    }
  }

  const handlePurchase = async () => {
    if (!email || !selectedPlan) return

    setLoading(true)
    setError('')
    try {
      // 创建订单
      const createRes = await fetch('/order/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, plan: selectedPlan }),
      })
      if (!createRes.ok) throw new Error(`HTTP ${createRes.status}`)
      const orderData = await createRes.json()

      // 模拟支付
      const payRes = await fetch('/order/pay', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ order_id: orderData.order_id }),
      })
      if (!payRes.ok) throw new Error(`HTTP ${payRes.status}`)
      const payData = await payRes.json()

      setLicenseInfo(payData)
      setStep('success')
      onPurchaseComplete?.()
    } catch (e) {
      console.error('Purchase failed:', e)
      setError('购买服务暂不可用，请稍后再试')
    } finally {
      setLoading(false)
    }
  }

  const resetModal = () => {
    setShowModal(false)
    setStep('select')
    setEmail('')
    setSelectedPlan(null)
    setLicenseInfo(null)
  }

  const formatPrice = (price: number) => `¥${price}`

  const formatLimit = (plan: Plan) => {
    if (plan.unlimited) return '不限'
    return `${plan.books}本 / 每本${plan.chapters_per_book}章`
  }

  return (
    <>
      {variant === 'icon' ? (
        <button
          onClick={() => setShowModal(true)}
          title="NWW会员"
          className="w-10 h-10 flex items-center justify-center rounded-lg border border-[#2a2a2a] bg-transparent text-[#7a7a7a] hover:border-[#3a3a3a] hover:text-[#b0b0b0] transition-all duration-150"
        >
          <svg className="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <path d="M3 8l3.5 3L12 5l5.5 6L21 8l-2 11H5L3 8z" />
            <path d="M12 5l1.6 1.6" />
          </svg>
        </button>
      ) : (
        <button
          onClick={() => setShowModal(true)}
          className="px-3 py-1 text-[14px] font-medium text-[#999] hover:text-white border border-[#444] hover:border-[#666] rounded transition-colors"
        >
          NWW会员
        </button>
      )}

      {showModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={resetModal}>
          <div
            className="bg-[#1e1e1e] rounded-lg border border-[#333] w-[520px] max-h-[80vh] overflow-hidden"
            onClick={(e) => e.stopPropagation()}
          >
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-[#333]">
              <h3 className="text-[14px] font-semibold text-white">购买 NWW 使用权限</h3>
              <button onClick={resetModal} className="text-[#777] hover:text-white">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M18 6L6 18M6 6l12 12" />
                </svg>
              </button>
            </div>

            {/* Content */}
            <div className="p-4 overflow-y-auto max-h-[60vh]">
              {step === 'select' && (
                <>
                  {error && (
                    <div className="mb-4 px-3 py-2 rounded text-[13px] text-[#e8a070] bg-[rgba(180,60,30,0.15)] border border-[rgba(200,80,40,0.5)]">
                      {error}
                    </div>
                  )}
                  {/* Email Input */}
                  <div className="mb-4">
                    <label className="block text-[14px] text-[#999] mb-1">邮箱账号</label>
                    <input
                      type="email"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      placeholder="your@email.com"
                      className="w-full bg-[#2a2a2a] border border-[#444] rounded px-3 py-2 text-[14px] text-white placeholder:text-[#666] outline-none focus:border-[#f59e0b]"
                    />
                  </div>

                  {/* Plans Grid */}
                  <div className="grid grid-cols-1 gap-3">
                    {plans.map((plan) => (
                      <div
                        key={plan.id}
                        onClick={() => setSelectedPlan(plan.id)}
                        className={`p-3 rounded-lg border cursor-pointer transition-all ${
                          selectedPlan === plan.id
                            ? 'border-[#f59e0b] bg-[#f59e0b]/10'
                            : 'border-[#333] hover:border-[#555] bg-[#252525]'
                        }`}
                      >
                        <div className="flex items-center justify-between">
                          <div>
                            <div className="text-[14px] font-medium text-white">{plan.name}</div>
                            <div className="text-[14px] text-[#999] mt-0.5">{formatLimit(plan)}</div>
                          </div>
                          <div className="text-right">
                            <div className="text-[16px] font-bold text-[#f59e0b]">{formatPrice(plan.price)}</div>
                            <div className="text-[14px] text-[#777]">
                              {plan.days >= 365 ? `${Math.floor(plan.days / 365)}年` : `${plan.days}天`}
                            </div>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </>
              )}

              {step === 'confirm' && (
                <div className="text-center py-6">
                  <div className="text-[14px] text-white mb-2">确认购买 {plans.find(p => p.id === selectedPlan)?.name}</div>
                  <div className="text-[14px] text-[#999] mb-4">账号: {email}</div>
                  <div className="text-[24px] font-bold text-[#f59e0b] mb-4">
                    ¥{plans.find(p => p.id === selectedPlan)?.price}
                  </div>
                </div>
              )}

              {step === 'success' && licenseInfo && (
                <div className="text-center py-6">
                  <div className="mb-3 text-[#5dbb63]"><CheckCircle2 className="h-10 w-10" strokeWidth={1.6} aria-hidden /></div>
                  <div className="text-[14px] text-white mb-2">购买成功!</div>
                  <div className="bg-[#2a2a2a] rounded p-3 mb-4">
                    <div className="text-[14px] text-[#999] mb-1">License 授权码</div>
                    <div className="text-[14px] font-mono text-[#f59e0b]">{licenseInfo.license}</div>
                  </div>
                  <div className="text-[14px] text-[#999]">
                    套餐: {plans.find(p => p.id === licenseInfo.plan)?.name}<br/>
                    有效期至: {new Date(licenseInfo.expire).toLocaleDateString('zh-CN')}
                  </div>
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="px-4 py-3 border-t border-[#333] flex justify-end gap-2">
              <button
                onClick={resetModal}
                className="px-4 py-2 text-[14px] text-[#999] hover:text-white transition-colors"
              >
                取消
              </button>
              {step === 'select' && (
                <button
                  onClick={() => email && selectedPlan && setStep('confirm')}
                  disabled={!email || !selectedPlan}
                  className="px-4 py-2 text-[14px] font-medium text-[#181818] bg-[#f59e0b] hover:bg-[#d97706] rounded disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                >
                  下一步
                </button>
              )}
              {step === 'confirm' && (
                <button
                  onClick={handlePurchase}
                  disabled={loading}
                  className="px-4 py-2 text-[14px] font-medium text-[#181818] bg-[#f59e0b] hover:bg-[#d97706] rounded disabled:opacity-50 transition-colors"
                >
                  {loading ? '处理中...' : '模拟支付成功'}
                </button>
              )}
              {step === 'success' && (
                <button
                  onClick={resetModal}
                  className="px-4 py-2 text-[14px] font-medium text-[#181818] bg-[#f59e0b] hover:bg-[#d97706] rounded transition-colors"
                >
                  完成
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  )
}
