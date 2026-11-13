const BASE = '/api'

import { authFetch } from './auth'

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
    // FastAPI 422 的 detail 是数组/对象；直接 new Error(obj) 会变成 "[object Object]"，
    // 结构化错误必须序列化后展示（2026-10-25 实机：监督目标卡报错只剩一串 [object Object]）。
    const msg = typeof detail === 'string' ? detail : JSON.stringify(detail)
    throw new Error(msg)
  }
  return r.json()
}

/** 生产 --check 只读结果：某章**当前**是否仍被重写熔断锁住（2026-11-07）。
 *  available=false 表示状态读取失败，界面必须回退/标注，绝不据此假绿。 */
export interface GuardFuseChapterState {
  chapter: number
  available: boolean
  ok?: boolean
  locked?: boolean | null
  action?: string
  /** none=未锁 / receipts=回执计数超限 / outline=细纲前提变更 */
  lock_kind?: 'none' | 'receipts' | 'outline'
  reason?: string | null
  receipts?: number
  receipts_total?: number
  baseline?: number
  max_rewrites?: number
  note?: string
}

/* ─────────── V13 道闸事实卡 / 作者裁决（2026-11-13 第二阶段） ───────────
 * 事实卡只陈述事实：后端硬禁 ok/block/level/score/pass 等判决字段。
 * gate_status 是「作者裁决状态」不是质量判定；事实一变旧裁决自动 stale。 */
export interface GateFinding {
  code: string
  source: string
  detail: string
  at: string
}
export interface GateReport {
  chapter: number
  mode: string
  status: string
  generated_at?: string
  count: number
  findings: GateFinding[]
}
export interface GateDecision {
  chapter: number
  action: 'accept' | 'recheck' | 'rollback'
  at: string
  by: string
  note: string
  card_fingerprint: string
  card_count: number
  strict_would_block: string[]
  check_ok?: boolean
  check_detail?: string
  rollback_to?: number
  recheck_mode?: string
}
export type GateStatus = 'pending' | 'stale' | 'accept' | 'recheck' | 'rollback'
export interface GateChapterRow {
  chapter: number
  count: number
  codes: string[]
  generated_at?: string | null
  last_at?: string | null
  status: GateStatus
  decision: GateDecision | null
  /** 是否已进入权威游标；未提交章的「已接受」不代表已落账 */
  committed: boolean
}
export interface GateSnapshot {
  /** false = 读取失败，界面必须标注「未知」，禁止渲染成「全部通过」 */
  available: boolean
  mode: string
  chapters: GateChapterRow[]
  pending_count: number
  note?: string
}
export interface GateDecisionsResp extends GateSnapshot {
  ok: boolean
  book_id: string
}
export interface GateDecideResp {
  ok: boolean
  action: string
  book_id: string
  chapter: number
  decision: GateDecision | null
  after: GateSnapshot
  recheck_mode?: string
  strict_would_block?: string[]
  check_ok?: boolean
  check_detail?: string
  rollback_to?: number
  note?: string
}

export interface Book {
  id: string
  title: string
  /** 书在用户工作区内的目录名（projects/{user}/{root}/） */
  root?: string
  genre: string
  synopsis?: string
  chapter_count: number
  total_chars: number
  last_committed_chapter: number | null
  state_revision: number | null
  schema_version: number | null
  /** 是否已初始化追踪状态（换 IDE 工作时 init 中断的书为 false） */
  initialized?: boolean
  /** 未初始化时的修复提示 */
  init_hint?: string
  /** 界面内可执行的初始化动作类型：'initialize' | 'repair_stub'（其余情况为 undefined，需人工处理） */
  init_action?: string
}

/** 疑似复制副本（2026-11-07 后端副本识别闸）：无 .nww-book.json 身份标记且标题重复的目录，
 *  后端判定为「疑似复制副本」并从书架排除。id 为目录名，可直接 deleteBook（移入 .trash）。 */
export interface IgnoredBook {
  id: string
  title: string
  /** 判定原因（后端给出，如「标题重复且无身份标记」） */
  reason: string
}

/** GET /api/books 的诊断响应：books=正常书架；ignored=被排除的疑似副本。
 *  旧后端（未重启/已回滚）不带 ignored 字段，由 booksWithDiagnostics 回落为 []。 */
export interface BooksDiagnostics {
  books: Book[]
  ignored: IgnoredBook[]
}

export interface Chapter {
  chapter: number
  file: string
  title: string
  target: number | null
  actual: number
  status: string | null
  summary: string
}

export interface TreeFile {
  path: string
  name: string
  writable: boolean
  chars?: number
  /** 正文分区专用：**章节健康分**（追踪/评分/第NNN章.json 的 best_score，单章审稿闭环 I-A~I-I，阈值 85）。
   *  注意口径：这是「生产健康检查」，只回答有没有明显坏点/契约违反/细纲未兑现，**不回答「好不好看」**；
   *  商业可读性评审是另一套（卷级 nww-score90，E0+E1-E5），两者不可互相引用。未审稿时不返回。 */
  score?: number
  /** 正文分区专用：正文章节的落盘时间（Unix 秒，正文文件 mtime） */
  mtime?: number
}

export interface TreeSection {
  section: string
  files: TreeFile[]
  /** 正文分区专用：卷模式 — "split" 分卷 / "flat" 不分卷 */
  volume_mode?: 'split' | 'flat'
  /** 正文分区专用：卷的总数（来自大纲卷纲文件，split模式使用） */
  volume_count?: number
  /** 正文分区专用：卷目录名 → 该卷规划章节数（split模式使用） */
  volume_plans?: Record<string, number>
  /** 正文分区专用：总规划章数（所有卷之和，flat模式下显示"总X章"） */
  total_planned_chapters?: number
}

export interface Tree {
  title: string
  sections: TreeSection[]
}

export interface FileMeta {
  book_id: string
  path: string
  name: string
  writable: boolean
  bytes: number
  content: string | null
  /** 章节详情常驻的只读事实卡（非章正文文件时后端省略这两个字段） */
  gate_report?: GateReport
  gate_adjudication?: GateDecision | null
}

export interface DerivedFile {
  path: string
  name: string
  section: string
  chars: number
  writable: boolean
}

export interface TrackingPayload {
  book_id: string
  tracking: Record<string, unknown>
  derived: DerivedFile[]
}

export interface SearchHit {
  book: string
  path: string
  name: string
}

export interface MvpPacketScene {
  scene_id?: string
  act?: string
  setting?: string
  driver_goal?: { actor?: string; goal?: string; constraint?: string; failure?: string }
  conflict?: { source?: string; pressure?: string; forces?: string }
  decision?: { choice?: string; cost?: string; benefit?: string }
  final_state?: { from?: string; to?: string; new_issue?: string }
}

export interface MvpPacketChar {
  name?: string
  role?: string
  function?: string
  state?: string
}

export interface MvpPacketCanon {
  must_keep?: string[]
  must_not_invent?: string[]
  must_release?: string[]
  must_avoid?: string[]
}

/** v2 Writing Packet 契约（与 skills/nww-novel-skill/scripts/nww_packet_builder_v2.py 对齐）。
 *  旧 8 字段（beat/logic_line/must_keep/must_avoid/active_characters/hard_facts/
 *  preceding_context/wordcount_target）已于 CCv2 冻结契约移除，前端预览据此重构。 */
export interface MvpPacket {
  schema_version?: string
  chapter_id?: number
  intent?: {
    hook?: string
    goal?: { value?: string; source?: string }
    ending?: string
    memory_point?: string
    emotion_curve?: string
    emotion_target?: string
    selling_points?: string
  }
  scene_chain?: MvpPacketScene[]
  character_contract?: MvpPacketChar[]
  canon_constraints?: MvpPacketCanon
  context?: { preceding?: string; preceding_state?: string; note?: string }
  wordcount?: { declared?: string; target?: number; min?: number; max?: number; metric?: string }
  meta?: { content_load?: { beats?: number; formal_points?: number; wc_target?: number; estimated_min_chars?: number; overload?: boolean } }
}

/** 写章提交状态（契约 C-WRITE-RESULT@1，2026-11-13）：
 *  committed=已进权威游标 / pending_author_review=正文已落盘但未提交（待作者裁决）/
 *  SKIPPED_DRY_RUN=仅演练未写盘 / failed=未提交。 */
export type MvpCommitStatus = 'committed' | 'pending_author_review' | 'SKIPPED_DRY_RUN' | 'failed'

export interface MvpWriteResult {
  book_id: string
  chapter_id: number
  packet: MvpPacket
  model: string
  path: string
  chars: number
  /** 契约字段（C-WRITE-RESULT@1）；旧后端缺省时按「已提交」历史语义处理 */
  ok?: boolean
  commit_status?: MvpCommitStatus
  /** v13 道闸把失败动作翻正后为 true：正文在盘上，但机械层未过 */
  degraded_pass?: boolean
  /** 链内告警（如「有正文但未进权威游标」），逐条原文展示 */
  warnings?: string[]
  /** 写链逐环节结果（既有多环节载荷，成功/失败都可能挂） */
  stages?: AgentStage[]
}

/** 篇幅平衡方案（契约 C-BALANCE-ROUTE@1，2026-11-13）：
 *  applied=false 时只是方案预览，正文未被改写。 */
export interface LengthBalanceResult {
  ok: boolean
  chapter_id: number
  applied: boolean
  rounds: number
  selected_strategy: string
  quality_score: number | null
  net_delta: number
  residual_gap: number
  stop_reason: string
  detail: string
}

/** 流式写章 SSE 事件 */
export type MvpWriteStreamEvent =
  | { type: 'delta'; text: string }
  | { type: 'done'; result: MvpWriteResult }
  | { type: 'error'; detail: string }

/** 多环节 Agent 全流程：单环节结果（name/ok/detail + 环节附加字段） */
export interface AgentStage {
  name: string
  ok: boolean
  detail: string
  [k: string]: unknown
}

// ---- 异步任务状态与续跑（契约 C-TASK-STATE@1 / C-RESUME@1）----

/** 任务状态：interrupted = 进程中断（非逻辑失败），可按章续跑 */
export type AgentTaskStatus = 'running' | 'interrupted' | 'done' | 'error' | 'cancelled'

/** 任务时间线单个环节（live_stages 元素；逐环节累积，不被覆盖） */
export interface AgentTaskStage {
  name: string
  ok: boolean
  detail: string
  output?: string
  /** 环节完成时间（Unix 秒）；后端异常路径可能缺省 */
  ts?: number
}

/** 异步任务结果载荷（status=done 时存在；语义与原 getAgentTask 返回保持一致） */
export interface AgentTaskResult {
  ok: boolean
  chapter_id?: number
  mode?: string
  level?: string
  stages?: AgentStage[]
  /** audit 链：发现总数 / 报告相对路径 / 逐章结果（只读诊断产物） */
  total_findings?: number
  report_md?: string
  chapters?: AuditChapterResult[]
}

/** 续跑所需的最小参数快照（与任务记录同源，仅 resumable=true 时提供） */
export interface AgentTaskResumeParams {
  mode: string
  book_id: string
  chapter_id: number
  level?: string
  note?: string
  dry_run?: boolean
}

/** 异步任务记录（GET /agents/task/{id}；后端只增字段，既有字段语义不变） */
export interface AgentTaskRecord {
  task_id: string
  status: AgentTaskStatus
  phase?: string
  phase_ok?: boolean | null
  phase_detail?: string
  mode: string
  chapter_id?: number
  book_id?: string
  /** 发起任务时的执行要点（rewrite/revise 携带，≤500 字）；用于 AI 实况任务标题的副行 */
  note?: string
  /** audit 链的章节范围 [起, 止]；无 chapter_id 时用于拼任务标题 */
  range?: number[] | null
  /** 进程中断：可按章重跑恢复（需含 book_id+chapter_id+受支持 mode） */
  resumable?: boolean
  /** 僵尸判定：运行线程已丢失且长时间无更新（此时 status 通常为 interrupted） */
  stale?: boolean
  /** 仅 interrupted 存在：process_restarted / thread_lost_timeout 等 */
  interrupt_reason?: string
  /** 仅 interrupted 存在（Unix 秒） */
  interrupted_at?: number
  /** 续跑参数快照（resumable=true 时提供） */
  resume?: AgentTaskResumeParams
  /** 主笔流式输出缓存（writer 阶段实时预览） */
  stream_text?: string
  /** AI 实况时间线（逐环节累积，不被覆盖） */
  live_stages?: AgentTaskStage[]
  result?: AgentTaskResult
  error?: string
  ok?: boolean
  /** 任务启动时间（Unix 秒） */
  started_at?: number
  /** 续跑产生的新记录会带上旧 task_id，便于审计 */
  resumed_from?: string
}

/** ── AI 实况统一采集（2026-11-08）：所有执行流程的 run → stage → call 三级流 ── */
export interface LiveStage {
  id: string
  name: string
  label: string
  status: 'running' | 'done' | 'failed' | 'cancelled'
  detail?: string
  output?: string
  started_at?: number
  ended_at?: number | null
  duration_ms?: number | null
  ok?: boolean | null
  error?: string
}

export interface LiveCall {
  id: string
  run_id: string | null
  stage: string | null
  stage_name?: string | null
  agent?: string
  model?: string
  provider?: string
  stream?: boolean
  prompt?: string
  prompt_chars?: number
  output?: string
  reasoning?: string
  chars?: number
  reasoning_chars?: number
  status: 'running' | 'done' | 'error'
  error?: string | null
  started_at?: number
  ended_at?: number | null
  duration_ms?: number | null
  tokens?: { prompt?: number; completion?: number; total?: number } | null
}

export interface LiveRun {
  run_id: string
  kind: 'task' | 'flow'
  label: string
  mode?: string | null
  book_id?: string | null
  chapter_id?: number | null
  task_id?: string | null
  status: 'running' | 'done' | 'error' | 'cancelled'
  started_at: number
  ended_at?: number | null
  error?: string | null
  stages: LiveStage[]
  calls: LiveCall[]
  call_count?: number
}

export type LiveEvent = {
  seq: number
  type: 'run' | 'stage' | 'call' | 'call_delta'
  run?: LiveRun
  run_id?: string | null
  stage?: LiveStage
  call?: LiveCall
  call_id?: string
  preview?: string
  chars?: number
  reasoning_chars?: number
}

export interface LiveFeedPayload {
  seq: number
  reset: boolean
  runs?: LiveRun[]
  events?: LiveEvent[]
  running?: Array<{ run_id: string; label: string; status: string }>
}

/** 章级续跑响应（POST /agents/task/{id}/resume） */
export interface AgentTaskResumeResult {
  task_id: string
  resumed: true
  from_task_id: string
  mode: string
  book_id: string
  chapter_id: number
  /** 续跑是章级重跑（不是断点续传）；重写链自带事务回滚 */
  resume_scope: 'chapter'
}

export interface AgentsWriteResult {
  book_id: string
  chapter_id: number
  status: number
  /** 写章=append（新章）；重写=revision（已提交章节回炉） */
  mode?: 'append' | 'revision'
  result: {
    ok: boolean
    chapter_id: number
    stages: AgentStage[]
    /** commit-only / 若干链路可能有汇总详情（stdout+stderr 拼接） */
    detail?: string
    /** 失败决策载荷（后端 _with_decision 附加，仅失败时存在） */
    needs_decision?: boolean
    failure?: { stage: string; detail: string; gate: string; rolled_back?: boolean }
    facts?: Record<string, unknown>
    /** 后端 _with_decision 生成失败回执的路径（书内相对路径） */
    receipt_path?: string
    decision_options?: Array<{ id: string; label: string; hint: string; risk: string; action: string }>
    /** V13 道闸事实卡：成功与失败都挂（2026-11-13），只读、不含任何判决字段 */
    gate_report?: GateReport
    gate_adjudication?: GateDecision | null
    /** 作者裁决状态，不是质量判定：pending/stale/accept/recheck/rollback */
    gate_status?: GateStatus
    gate_pending?: boolean
    /** 「有正文但未进权威游标」的 V13 收口标记 */
    commit_status?: string
    warnings?: string[]
  }
}

export interface AgentsRoutingInfo {
  cloud_configured: boolean
  local_write: string
  local_small: string
  cloud_model: string
  config_configured?: boolean
  default?: string
  providers?: ModelProvider[]
  pool?: PoolModel[]
  agent_routes?: Record<string, string>
}

/** 模型聚合中心：供应商（OpenAI 兼容服务） */
export interface ModelProvider {
  id: string
  base_url: string
  /** 已保存的 Key 后端只回显掩码（sk-****abcd） */
  api_key: string
  enabled: boolean
}

/** 模型聚合中心：模型池条目（nww 可选可用模型） */
export interface PoolModel {
  id: string
  provider: string
  model: string
  label?: string
  fallback_pool_ids?: string[]
}

/** 模型聚合中心：完整配置（providers 已掩码） */
/** 路由键体检：把「配了但不会生效」的情况全部算出来（后端 route_diagnostics） */
export interface RouteDiagnostics {
  /** 生产链不认识的键（名字拼错/存量遗留）→ 永不生效 */
  orphan_keys: { key: string; pool_id: string; hint?: string }[]
  /** 键名合法但该 agent 生产链不调用 → 配了也不生效 */
  dead_keys: { key: string; pool_id: string; level: string }[]
  /** 每个 agent 实际会用哪个池模型（键命中 or 回落全局默认） */
  effective: Record<string, string>
  default: string
  known_non_agent_keys: string[]
}

export interface ModelConfig {
  configured: boolean
  providers: ModelProvider[]
  pool: PoolModel[]
  default: string
  agent_routes: Record<string, string>
  route_diagnostics?: RouteDiagnostics
}

/** 模型聚合中心：供应商连接测试 / 模型发现结果 */
export interface ModelTestResult {
  ok: boolean
  models?: string[]
  detail?: string
}

/** 模型聚合中心：agent 注册表条目 */
export interface AgentInfo {
  name: string
  routing: string
  description: string
  /** 职能环节分组（后端 registry 按生产链真实职责给出）：创意与规划 / 写作与出稿 / 审查与修订 / 素材与分析 / 未接入 */
  stage?: string
  /** 接线事实（后端扫代码得出）：main=写章主链真会调 / contract=仅拆书契约（默认关） / none=生产链无调用点 */
  wired?: 'main' | 'contract' | 'none'
  /** 接线证据（文件:行号） */
  wired_refs?: string[]
  tools?: string[]
  disallowed_tools?: string[]
}

export interface BookContext {
  bound: boolean
  title: string | null
  root: string | null
  book_id?: string
  rel_dir?: string
  chapter_count?: number
}

/** 建书草案（未绑书态 DRAFT_JSON 协议，后端校验 8 字段全部非空才下发） */
export interface BookDraft {
  title: string
  genre: string
  synopsis: string
  core_theme: string
  protagonist: string
  conflict: string
  differentiation: string
  scale: string
}

export interface AgentChatReply {
  reply: string
  book_context: BookContext
  /** 建书草案卡（仅未绑书态；后端 fail-closed：无效草案不下发） */
  book_draft?: BookDraft | null
  /** 模型思考内容（<think> 剥离），前端用「思考内容」折叠块区分展示 */
  thinking?: string | null
  /** agent 提案的设定/大纲文件增补（经 <apply> 协议解析校验），前端逐条提供「应用」按钮 */
  proposed_updates?: ProposedUpdate[] | null
  /** agent 派单的 pipeline 执行指令（经 <execute> 协议解析校验），前端提供「一键执行」按钮 */
  proposed_actions?: ProposedAction[] | null
  /** 监督目标提案（仅绑书态 GOAL_JSON 协议；后端 fail-closed：非法目标不下发），前端渲染「开始监督」确认卡 */
  proposed_goal?: SupervisorGoal | null
  /** 结构化执行事件（P0-C：动作归一化 / 指令被拒），与 reply 正文隔离，前端独立提示位展示 */
  execution_notice?: ExecNotice[] | null
  /** 本轮实际使用的模型名（后端回传，便于溯源；模型选择器指定时为所选池模型） */
  model?: string | null
}

/** 对话 SSE 流事件（/agent/chat/stream）：progress=思考进程实时行；done=完整回复；error=失败 */
export type AgentChatStreamEvent =
  | { type: 'progress'; stage: 'llm' | 'tools' | 'retry' | 'repair' | string; detail: string }
  | { type: 'done'; payload: AgentChatReply }
  | { type: 'error'; detail: string }

/**
 * 后端结构化执行事件（agent.py `_build_exec_actions` / `_fail_exec` / `_bind_chapter_target` 产出）。
 * - action_normalized：write/rewrite 依目标章正文存在性被确定性命中归一化（不是 AI 猜测）；
 * - action_rejected：指令被 fail-closed 拒绝（本轮未下发任何可执行动作）；
 * - action_chapter_rebound：用户未提章号且当前打开为正文章时，执行目标章被确定性校正为当前打开章。
 */
export interface ExecNotice {
  type: 'action_normalized' | 'action_rejected' | 'action_chapter_rebound' | string
  /** 归一化：模型原始请求动作；拒绝：被拒动作 */
  requested_action?: string
  /** 归一化后实际下发的动作 */
  normalized_action?: string
  corrected?: boolean
  /** 拒绝原因（后端 reason 原文） */
  reason?: string
  /** 校正后/归一化后的目标章号 */
  chapter?: number
  /** 章号校正专用：模型原报章号（action_chapter_rebound 时返回） */
  from_chapter?: number
}

/** agent 派单的一步 pipeline 执行：action 白名单 write/rewrite/deslop/revise/audit/set_register，对应 nwwmvp agents 端点 */
export interface ProposedAction {
  action: 'write' | 'rewrite' | 'deslop' | 'deslop_text' | 'revise' | 'audit' | 'set_register'
  chapter: number
  /** 执行要点（字数目标/衔接/保持原文/重点段；revise 必填：修订点与修订要求），执行时透传给用户确认用 */
  note: string
  /** deslop_text 专用：贴入的待润色正文（无章号、不落盘、引擎改写后返回改后全文） */
  text?: string
  /** audit 专用：审计区间（后端 _parse_execute_block 已把 chapter 展成 start/end） */
  start?: number
  end?: number
  /** audit 专用：只读诊断（auto_fix 已从审计职责移除，2026-09-20；字段保留仅为兼容旧会话展示） */
  auto_fix?: boolean
  /** rewrite 专用：重写等级 L1 保持剧情 / L2 结构重排 / L3 章节再生产（默认 L3） */
  level?: 'L1' | 'L2' | 'L3'
  /** set_register 专用：语域档位 strict 修真正典 / balanced 修主现辅 / modern 现代语感 */
  register?: 'strict' | 'balanced' | 'modern'
}

/** 一致性审计发现（只读诊断产物，2026-09-20 裁决）：issue_id + status 供接受偏移治理后续版本 */
export interface AuditFinding {
  issue_id: string
  status: 'open' | 'accepted' | 'resolved' | string
  level: 'P0' | 'P1' | 'P2' | string
  /** 后端实际产出字段（severity=P0/P1/P2，真实运行验证 2026-09-20）；level 仅为兼容层，展示统一 level ?? severity */
  severity?: 'P0' | 'P1' | 'P2' | string
  type: string
  entity?: string
  detail: string
  evidence?: string
  evidence_unverified?: boolean
  fix_hint?: string
  /** 建议动作（确定性路由）：rewrite_L1/L2/L3 / patch / accept */
  suggestion: string
}

export interface AuditChapterResult {
  chapter: number
  ok?: boolean
  error?: string
  outline_source?: string
  prose_path?: string
  findings: AuditFinding[]
  dropped_by_recheck?: unknown[]
}

/** agent 提案的一处文件增补：path 为书内相对路径；content 为应用后的完整文件内容（后端已拼好追加） */
export interface ProposedUpdate {
  path: string
  content: string
  /** 追加内容原文（用于预览与展示） */
  preview: string
}

// ---- 流水线监督器（2026-10-25）：目标驱动自动派单 + 收件箱督促 ----

/** 监督目标（GOAL_JSON 协议 / 监督面板设定；后端 supervisor.validate_goal 同一判据） */
export interface SupervisorGoal {
  type: 'write_to' | 'daily'
  /** write_to：连续写到提交至第 N 章 */
  target_chapter?: number
  /** daily：今日完成 N 章（1~12） */
  daily_chapters?: number
  /** 作者叮嘱 ≤200 字 */
  note?: string
}

/** 监督收件箱事件（events.jsonl；level: info/gate/fail/done） */
export interface SupervisorEvent {
  id: string
  ts: string
  kind: string
  level: 'info' | 'gate' | 'fail' | 'done' | string
  title: string
  detail?: string
  chapter?: number | null
  read?: boolean
}

/** 监督器书级总览行（GET /supervisor/overview） */
export interface SupervisorOverviewRow {
  book_id: string
  title: string
  goal: SupervisorGoal | null
  paused: boolean
  stop_reason: string | null
  /** 阶段机缓存（监督器最近一次观察；无目标且未观察时为 null） */
  phase: string | null
  target_chapter: number | null
  last_committed: number
  chapters_done_today: number
  consecutive_failures: number
  running: { task_id: string; mode: string; chapter_id: number; phase: string; origin: string; range?: number[] | null } | null
  unread: number
  last_event: SupervisorEvent | null
}

/** 监督器单书详情（GET /supervisor/books/{id}/state） */
export interface SupervisorState {
  book_id: string
  goal: SupervisorGoal | null
  policy: { paused: boolean; daily_chapter_cap: number; max_consecutive_failures: number; retry_per_chapter: number }
  runtime: {
    day?: string
    chapters_done_today?: number
    consecutive_failures?: number
    stop_reason?: string | null
    active_task_id?: string | null
    last_failure?: { chapter: number; error: string; ts: string } | null
    last_tick?: number
  }
  phase: string | null
  target_chapter: number | null
  next_action?: string | null
  running: Array<{ task_id: string; mode: string; chapter_id: number; phase: string; origin: string; range?: number[] | null }>
  events: SupervisorEvent[]
  unread: number
}

/* ─────────── 编辑器选区 / 光标 AI（V14 工具条能力，2026-11-13） ───────────
 * 统一走模型中心端点链；旧 /editor/rewrite 仅作为 NWW_EDITOR_AI=0 时的回落。 */
export interface EditorAiDiffChunk {
  id: string
  type: 'equal' | 'insert' | 'delete'
  text: string
}
export interface EditorAiCandidate {
  id: string
  label: string
  content: string
  summary?: string
  rationale?: string
  diff_chunks?: EditorAiDiffChunk[]
}
export interface EditorAiResult {
  ok: boolean
  status: string
  mode?: 'revision' | 'continuation'
  operation?: string
  operation_label?: string
  candidates: EditorAiCandidate[]
  model?: string
  base_url?: string
  degraded?: boolean
  error?: string
  original_text?: string
  diff_truncated?: boolean
}
export interface EditorAiRevisionBody {
  selected_text: string
  before_context?: string
  after_context?: string
  operation: string
  custom_instruction?: string
  chapter_goal?: string
}
export interface EditorAiContinuationBody {
  before_context?: string
  after_context?: string
  operation: string
  instruction?: string
  chapter_goal?: string
}

export const api = {
  books: () => j<{ books: Book[] }>(authFetch(`${BASE}/books`)).then((r) => r.books),
  /** 书架 + 副本诊断（2026-11-07）：额外返回 ignored（疑似复制副本，后端已排除不进书架）。
   *  旧后端无 ignored 字段时回落 []，前端行为与改造前一致。 */
  booksWithDiagnostics: (): Promise<BooksDiagnostics> =>
    j<{ books?: Book[]; ignored?: IgnoredBook[] }>(authFetch(`${BASE}/books`)).then((r) => ({
      books: r.books ?? [],
      ignored: r.ignored ?? [],
    })),
  chapters: (id: string) =>
    j<{ book_id: string; chapters: Chapter[]; last_committed_chapter?: number | null }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/chapters`),
    ).then((r) => ({
      chapters: r.chapters,
      last_committed_chapter: r.last_committed_chapter ?? null,
    })),
  tree: (id: string) =>
    j<{ book_id: string; tree: Tree }>(authFetch(`${BASE}/books/${encodeURIComponent(id)}/tree`)).then((r) => r.tree),
  tracking: (id: string) =>
    j<TrackingPayload>(authFetch(`${BASE}/books/${encodeURIComponent(id)}/tracking`)),
  file: (id: string, path: string) =>
    j<FileMeta>(authFetch(`${BASE}/books/${encodeURIComponent(id)}/file?path=${encodeURIComponent(path)}`)),
  writeFile: (id: string, path: string, content: string, source?: string) =>
    j<{ ok: boolean; style_feedback?: { queued: boolean } }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/file?path=${encodeURIComponent(path)}`
        + (source ? `&source=${encodeURIComponent(source)}` : ''), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
      }),
    ),
  /** 编辑器 AI 网关自检：chain_size=0 表示模型中心无可用端点，界面必须显式标注。 */
  editorAiHealth: () =>
    j<{ ok: boolean; status: string; chain_size: number; error?: string }>(
      authFetch(`${BASE}/editor/ai/health`),
    ),
  editorAiRevision: (body: EditorAiRevisionBody) =>
    j<EditorAiResult>(authFetch(`${BASE}/editor/ai/revision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })),
  editorAiContinuation: (body: EditorAiContinuationBody) =>
    j<EditorAiResult>(authFetch(`${BASE}/editor/ai/continuation`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })),
  /** 回滚通道：NWW_EDITOR_AI=0 时新端点 404，回落到旧单候选端点（带明确降级标注）。 */
  editorRewriteLegacy: (body: EditorAiRevisionBody) =>
    j<{ candidate?: string; candidates?: EditorAiCandidate[]; error?: string }>(
      authFetch(`${BASE}/editor/rewrite`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          selected_text: body.selected_text,
          before_context: body.before_context ?? '',
          after_context: body.after_context ?? '',
          operation: body.operation,
          custom_instruction: body.custom_instruction ?? '',
        }),
      }),
    ),
  search: (q: string) =>
    j<{ hits: SearchHit[] }>(authFetch(`${BASE}/search?q=${encodeURIComponent(q)}`)).then((r) => r.hits),
  deleteBook: (id: string) =>
    j<{ ok: boolean; mode: 'trash' | 'purge'; book_id: string; trash_path?: string }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    ),
  createBook: (title: string, genre?: string, synopsis?: string, draft?: BookDraft) =>
    j<{ id: string; title: string; path: string }>(
      authFetch(`${BASE}/books`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, genre, synopsis, draft }),
      }),
    ),
  /** 建书 bootstrap：LLM 生成 卷纲/世界观/主角 三文件（幂等，已存在跳过；慢，可达数分钟） */
  bootstrapBook: (id: string) =>
    j<{ ok: boolean; written?: string[]; skipped?: string[]; missing?: string[]; error?: string }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/bootstrap`, { method: 'POST' }),
    ),
  /** 界面内初始化 / 重建追踪状态（根治 CLI 命令串反模式）：按当前登录用户执行，不回传路径/命令 */
  initializeBook: (id: string) =>
    j<{ ok: boolean; action?: string; book_id?: string; state_revision?: number; last_committed_chapter?: number | null; detail?: string }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/initialize`, { method: 'POST' }),
    ),
  deleteFile: (id: string, path: string) =>
    j<{ ok: boolean; mode: 'trash' | 'purge'; book_id: string; path: string; trash_path?: string }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/file?path=${encodeURIComponent(path)}`, { method: 'DELETE' }),
    ),
  /** 同目录内重命名文件（章节改标题用），返回书内新的相对路径 */
  renameFile: (id: string, path: string, newName: string) =>
    j<{ ok: boolean; book_id: string; path: string; name: string }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/file/rename?path=${encodeURIComponent(path)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_name: newName }),
      }),
    ),
  /** NWW-MVP：构建 Writing Packet */
  mvpPacket: (id: string, chapter: number) =>
    j<{ book_id: string; chapter_id: number; packet: MvpPacket }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/mvp/packet`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chapter }),
      }),
    ),
  /** NWW-MVP：打包 → LLM 生成正文 → 落盘 正文/第N章.md */
  mvpWrite: (id: string, chapter: number) =>
    j<MvpWriteResult>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/mvp/write`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chapter }),
      }),
    ),
  /** NWW-MVP：流式写章（SSE 打字机）— delta/done/error 事件，完成后 resolve 结果 */
  mvpWriteStream: (id: string, chapter: number, onEvent: (ev: MvpWriteStreamEvent) => void) =>
    new Promise<MvpWriteResult>((resolve, reject) => {
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/mvp/write-stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chapter }),
      })
        .then(async (resp) => {
          if (!resp.ok) {
            let detail = `HTTP ${resp.status}`
            try { detail = (await resp.json()).detail ?? detail } catch { /* ignore */ }
            reject(new Error(detail))
            return
          }
          const reader = resp.body!.getReader()
          const decoder = new TextDecoder()
          let buf = ''
          try {
            for (;;) {
              const { done, value } = await reader.read()
              if (done) break
              buf += decoder.decode(value, { stream: true })
              let idx: number
              while ((idx = buf.indexOf('\n\n')) >= 0) {
                const raw = buf.slice(0, idx)
                buf = buf.slice(idx + 2)
                if (!raw.startsWith('data:')) continue
                let ev: MvpWriteStreamEvent
                try { ev = JSON.parse(raw.slice(5).trim()) } catch { continue }
                if (ev.type === 'done') { onEvent(ev); resolve(ev.result); return }
                if (ev.type === 'error') { onEvent(ev); reject(new Error(ev.detail)); return }
                onEvent(ev)
              }
            }
            reject(new Error('流式响应意外结束'))
          } catch (e) {
            reject(e)
          }
        })
        .catch(reject)
    }),
  /** 多环节 Agent 全流程：registry 诊断（返回注册表 agent 条目） */
  agentsRegistry: () =>
    j<{ agents: AgentInfo[]; count: number }>(authFetch(`${BASE}/agents/registry`)).then((r) => r.agents),
  /** 模型聚合中心：读取配置（providers 掩码） */
  getModelConfig: () => j<ModelConfig>(authFetch(`${BASE}/models/config`)),
  /** 模型聚合中心：保存配置（body: {providers, pool, default, agent_routes}） */
  saveModelConfig: (cfg: ModelConfig) =>
    j<ModelConfig>(
      authFetch(`${BASE}/models/config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cfg),
      }),
    ),
  /** 模型聚合中心：供应商连通性测试 */
  testModelConnection: (baseUrl: string, apiKey: string, model?: string) =>
    j<ModelTestResult>(
      authFetch(`${BASE}/models/test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey, model }),
      }),
    ),
  /** 模型聚合中心：拉取供应商模型清单（一键导入模型池） */
  discoverModels: (baseUrl: string, apiKey: string) =>
    j<ModelTestResult>(
      authFetch(`${BASE}/models/discover`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey }),
      }),
    ),
  /** 模型聚合中心：池内全部模型可用性验证（红色提示/禁用依据） */
  validatePool: () =>
    j<Record<string, { ok: boolean; detail: string }>>(
      authFetch(`${BASE}/models/validate-pool`, { method: 'POST' }),
    ),
  /** 多环节 Agent 全流程：模型路由诊断 */
  agentsRouting: () => j<AgentsRoutingInfo>(authFetch(`${BASE}/agents/routing`)),
  /** 多环节 Agent 全流程同步写章：prealign→packet→writer→落盘→delta→post_write→deslop */
  agentsWrite: (id: string, chapter: number, prealign: boolean) =>
    j<AgentsWriteResult>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/mvp/agents/write`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chapter, prealign }),
      }),
    ),
  /** 多环节 Agent 重写已提交章节：备份旧稿 → 全流程重写 → revision 提交 */
  agentsRewrite: (id: string, chapter: number, note?: string) =>
    j<AgentsWriteResult>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/mvp/agents/rewrite`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chapter, ...(note ? { note } : {}) }),
      }),
    ),
  /** 异步重写（等级化章节重新生产）：L1 保持剧情重写 / L2 结构重排 / L3 章节再生产（默认 L3） */
  agentsRewriteAsync: (id: string, chapter: number, note?: string, level?: 'L1' | 'L2' | 'L3') =>
    j<{ task_id: string; book_id: string; chapter_id: number; mode: string; level?: string }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/mvp/agents/rewrite/async`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chapter, ...(note ? { note } : {}), level: level ?? 'L3' }),
      }),
    ),
  /** 异步写章：后台跑 pipeline，返回 task_id（配合 getAgentTask 轮询进度）。
   *  prealign 默认 true（写前对齐：检查细纲缺项→自动回补→重跑，第 1–3 章含三九 G1–G9）。 */
  agentsWriteAsync: (id: string, chapter: number, prealign = true) =>
    j<{ task_id: string; book_id: string; chapter_id: number; mode: string }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/mvp/agents/write/async`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chapter, prealign }),
      }),
    ),
  /** 语域档位设置（感知-确认型）：作者一句话改档的落地调用，同步小写入 */
  agentsSetRegister: (id: string, register: 'strict' | 'balanced' | 'modern') =>
    j<{ ok: boolean; register_mode: string; message: string }>(
      authFetch(`${BASE}/agent/book/${encodeURIComponent(id)}/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ register }),
      }),
    ),
  /** 去AI味润色：备份旧稿 → LLM 保守润色 → revision 提交 → 复扫（同步版，写作台用） */
  agentsDeslop: (id: string, chapter: number) =>
    j<AgentsWriteResult>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/mvp/agents/deslop`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chapter }),
      }),
    ),
  /** 异步润色：后台跑 pipeline，返回 task_id（配合 getAgentTask 轮询进度） */
  agentsDeslopAsync: (id: string, chapter: number) =>
    j<{ task_id: string; book_id: string; chapter_id: number; mode: string }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/mvp/agents/deslop/async`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chapter }),
      }),
    ),
  /** 异步润色（文本质量优化层，2026-09-20 裁决）：保剧情提表达 + 章节合同守卫
      analyze(诊断+合同快照) → rewrite_pass → deslop → normalize → contract_guard → revision
      note=作者润色意见（可选；最高优先级注入 rewrite+deslop，2026-11-02 全链打通） */
  agentsPolishAsync: (id: string, chapter: number, note?: string) =>
    j<{ task_id: string; book_id: string; chapter_id: number; mode: string }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/mvp/agents/polish/async`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chapter, ...(note && note.trim() ? { note: note.trim() } : {}) }),
      }),
    ),
  /** 贴文直接润色（聊天文本模式）：用户贴入一段无章号正文 → 引擎去AI味改写 → 返回改后全文；
      不落盘、不改文件（与 agentsDeslop 作用于已提交章文件相反）。同步返回 {result:{ok,polished,...}} */
  agentsDeslopText: (id: string, text: string, note = '') =>
    j<{ book_id: string; status: number; result: { ok: boolean; polished?: string; chars_before?: number; chars_after?: number; error?: string } }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/mvp/agents/deslop_text`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, note }),
      }),
    ),
  /** 局部修订：备份旧稿 → LLM 定点增补（只增不删）→ diff 核对 → revision 提交 → 复扫（同步版，写作台用） */
  agentsRevise: (id: string, chapter: number, note: string) =>
    j<AgentsWriteResult>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/mvp/agents/revise`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chapter, note }),
      }),
    ),
  /** 异步局部修订：后台跑 pipeline，返回 task_id（配合 getAgentTask 轮询进度） */
  agentsReviseAsync: (id: string, chapter: number, note: string) =>
    j<{ task_id: string; book_id: string; chapter_id: number; mode: string }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/mvp/agents/revise/async`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chapter, note }),
      }),
    ),
  /** 异步一致性审计（区间批量，只读诊断）：A 细纲/B 世界/C 角色/D 伏笔 → P0/P1/P2 报告+建议动作；绝不修改正文 */
  agentsAuditAsync: (id: string, start: number, end: number) =>
    j<{ task_id: string; book_id: string; range: number[]; mode: string; readonly: boolean;
        /** 后端单飞互斥：该书已有审计任务在跑 → 复用其 task_id，不再起第二个线程 */
        reused?: boolean }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/mvp/agents/audit/async`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start, end }),
      }),
    ),
  /** 仅提交已落盘的悬空章（append commit，不重写正文）：前置闭环绕已确认，否则 409 */
  agentsCommitOnly: (id: string, chapter: number) =>
    j<AgentsWriteResult>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/mvp/agents/commit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chapter }),
      }),
    ),
  /** 写前自动规划（去堵按钮）：细纲缺失/无效时由 LLM 生成该章细纲并落盘；成功后需再次点「新写」 */
  agentsPlanOutline: (id: string, chapter: number) =>
    j<{ book_id: string; chapter_id: number; status: number;
        result: { ok: boolean; detail: string; path?: string } }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/mvp/agents/plan-outline`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chapter }),
      }),
    ),
  /** 确认第 N 章一致性闭环：审查通过 + 作者确认 → 写 追踪/一致性核验/第N章.json（A-3 闸输入） */
  confirmConsistencyClosure: (id: string, chapter: number, note?: string) =>
    j<{
      ok: boolean
      chapter: number
      closure_path: string
      closed: boolean
      already_confirmed: boolean
    }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/consistency/closure/confirm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chapter, ...(note ? { note } : {}) }),
      }),
    ),
  /** 人工修改审核提交：系统补跑完整验收（enforce→审稿→一致性→原闸 commit）后正式落账 */
  acceptanceSettle: (id: string, chapter: number, reason?: string) =>
    j<{
      ok: boolean
      committed: boolean
      already_committed?: boolean
      chapter?: number
      last_before?: number
      last_after?: number
      /** already_committed 时后端回带的当前权威游标（章号） */
      last_committed_chapter?: number | null
      steps?: string[]
      detail: string
    }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/chapters/${chapter}/acceptance-settle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...(reason ? { reason } : {}) }),
      }),
    ),
  /** 篇幅平衡方案：apply=false 只出方案预览（不写正文）；apply=true 按方案定向执行并覆写正文（持章锁） */
  lengthBalance: (id: string, chapter: number, apply = false) =>
    j<LengthBalanceResult>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/chapters/${chapter}/length-balance`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ apply }),
      }),
    ),
  /** Consistency Repair Loop Phase 1：生成一致性修复方案（只读，产出 repair_plan.json，不改正文） */
  repairPlan: (id: string, chapter: number) =>
    j<{
      ok: boolean
      chapter: number
      already_generated: boolean
      plan_path: string
      repair_plan: {
        chapter: number
        plan_status: 'pending' | 'confirmed' | 'executed' | 'rejected'
        created_at: string
        updated_at: string
        issues: Array<{
          id: string
          type: string
          category: 'A' | 'B' | 'C'
          evidence: string[]
          summary: string
          suggested_fix: string
          risk_note: string
          fix_targets?: string[]
          evidence_refs?: Array<{ file: string; location: string; excerpt: string }>
        }>
      }
    }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/chapters/${chapter}/repair-plan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      }),
    ),
  /** Consistency Repair Loop Phase 1.5：批量人工授权执行本章修复（写入 repair_plan.json 的 confirmation 块）
   *  仅由 Web 端按钮触发，禁止后台伪造确认。 */
  confirmRepairPlan: (id: string, chapter: number) =>
    j<{
      ok: boolean
      chapter: number
      confirmed: boolean
      already_confirmed: boolean
      plan_path: string
    }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/chapters/${chapter}/repair-plan/confirm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      }),
    ),
  /** Consistency Repair Loop Phase 1.5：Repair Executor v1（仅 confirmed plan 可执行）
   *  A 类→自动 patch；B/C 类→proposal only；不接 closure 自动化。 */
  executeRepairPlan: (id: string, chapter: number) =>
    j<{
      ok: boolean
      chapter: number
      executor: string
      snapshot: string | null
      backup: string | null
      summary: { a_applied: number; proposal_only: number }
      results: Array<{
        issue_id: string
        mode: 'patch_applied' | 'proposal_only'
        reason?: string
        detail?: string
        old?: string
        new?: string
      }>
      audit_path: string
    }>(
      authFetch(`${BASE}/books/${encodeURIComponent(id)}/chapters/${chapter}/repair-plan/execute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      }),
    ),
  /** 查询异步任务进度：{status, phase, phase_ok, phase_detail, stream_text, result?, error?}
   *  status 含 interrupted（进程中断，可续跑）与 stale（僵尸判定）等契约字段 */
  getAgentTask: (taskId: string) =>
    j<AgentTaskRecord>(authFetch(`${BASE}/agents/task/${taskId}`)),
  /** AI 实况增量流：since=0 拿物化快照，之后按 seq 取增量事件（所有流程共用） */
  getLiveFeed: (since = 0) =>
    j<LiveFeedPayload>(authFetch(`${BASE}/live/feed?since=${since}`)),
  /** 终止异步任务（协作式）；对 interrupted 幂等：仅回 {status, cancelled:false, stale:true}，不改状态 */
  cancelAgentTask: (taskId: string) =>
    j<{ task_id: string; status: string; cancelled: boolean; stale?: boolean }>(
      authFetch(`${BASE}/agents/task/${taskId}/cancel`, { method: 'POST' }),
    ),
  /** 章级续跑已中断任务：复用原 mode/book/chapter 重跑，返回新 task_id（resume_scope='chapter'）。
   *  409 = 任务非 interrupted/不可续跑或同章已有在途操作；404 = 任务不存在 */
  resumeAgentTask: (taskId: string) =>
    j<AgentTaskResumeResult>(
      authFetch(`${BASE}/agents/task/${taskId}/resume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      }),
    ),
  /** 获取最近一个 agent 任务（刷新后恢复实况面板用）；中断记录也可恢复，前端据 status 显示续跑入口 */
  getRecentAgentTask: () =>
    j<{
      task_id: string
      mode: string
      status: AgentTaskStatus
      resumable?: boolean
      stale?: boolean
      interrupt_reason?: string
    }>(authFetch(`${BASE}/agents/tasks/recent`)),
  /** Agent 建书对话：多轮消息 → 助手回复（含当前绑定书籍上下文 + 当前打开文件）；signal 用于终止生成 */
  agentChat: (
    bookId: string | null,
    activePath: string | null,
    messages: Array<{ role: 'user' | 'assistant'; content: string }>,
    signal?: AbortSignal,
    /** 模型选择器选中的模型池条目 id；不传 = 后端 book-coach 路由链（原行为） */
    poolId?: string | null,
  ) =>
    j<AgentChatReply>(
      authFetch(`${BASE}/agent/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ book_id: bookId, active_path: activePath, messages, pool_id: poolId ?? null }),
        signal,
      }),
    ),
  /** 对话 SSE 流（/agent/chat/stream）：progress 事件实时上报思考进程（第几轮生成/取材/纠偏），
   *  done 携带与同步版一致的完整回复；心跳注释行（: keep）自动忽略 */
  agentChatStream: (
    bookId: string | null,
    activePath: string | null,
    messages: Array<{ role: 'user' | 'assistant'; content: string }>,
    handlers?: { onEvent?: (ev: AgentChatStreamEvent) => void },
    signal?: AbortSignal,
    poolId?: string | null,
  ) =>
    new Promise<AgentChatReply>((resolve, reject) => {
      authFetch(`${BASE}/agent/chat/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ book_id: bookId, active_path: activePath, messages, pool_id: poolId ?? null }),
        signal,
      })
        .then(async (resp) => {
          if (!resp.ok) {
            let detail = `HTTP ${resp.status}`
            try {
              const b = await resp.json()
              if (b && b.detail) detail = typeof b.detail === 'string' ? b.detail : JSON.stringify(b.detail)
            } catch { /* ignore */ }
            reject(new Error(detail))
            return
          }
          const reader = resp.body!.getReader()
          const decoder = new TextDecoder()
          let buf = ''
          try {
            for (;;) {
              const { done, value } = await reader.read()
              if (done) break
              buf += decoder.decode(value, { stream: true })
              let idx: number
              while ((idx = buf.indexOf('\n\n')) >= 0) {
                const raw = buf.slice(0, idx)
                buf = buf.slice(idx + 2)
                if (!raw.startsWith('data:')) continue
                let ev: AgentChatStreamEvent
                try { ev = JSON.parse(raw.slice(5).trim()) } catch { continue }
                if (ev.type === 'done') { handlers?.onEvent?.(ev); resolve(ev.payload); return }
                if (ev.type === 'error') { reject(new Error(ev.detail)); return }
                handlers?.onEvent?.(ev)
              }
            }
            reject(new Error('流式响应意外结束'))
          } catch (e) {
            reject(e)
          }
        })
        .catch(reject)
    }),
  // ---- 流水线监督器 ----
  /** 监督器书架总览（轮询友好：只读缓存与状态文件，不触发阶段解析） */
  supervisorOverview: () =>
    j<{ books: SupervisorOverviewRow[]; supervisor_running: boolean; interval_s: number }>(
      authFetch(`${BASE}/supervisor/overview`),
    ),
  /** 监督器单书详情：目标/策略/运行时 + 收件箱事件 */
  supervisorState: (bookId: string) =>
    j<SupervisorState>(authFetch(`${BASE}/supervisor/books/${encodeURIComponent(bookId)}/state`)),
  /** 设定/清除监督目标：goal=null 清除；设定即重置熔断与重试账本并开始推进 */
  supervisorSetGoal: (bookId: string, goal: SupervisorGoal | null) =>
    j<{ ok: boolean; goal: SupervisorGoal | null }>(
      authFetch(`${BASE}/supervisor/books/${encodeURIComponent(bookId)}/goal`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ goal }),
      }),
    ),
  /** 暂停/恢复监督派单（暂停会取消监督器派出的在跑任务；作者手动任务不动） */
  supervisorSetPaused: (bookId: string, paused: boolean) =>
    j<{ ok: boolean; paused: boolean }>(
      authFetch(`${BASE}/supervisor/books/${encodeURIComponent(bookId)}/policy`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paused }),
      }),
    ),
  /** 解除停止态/熔断账本，继续当前目标 */
  supervisorResume: (bookId: string) =>
    j<{ ok: boolean }>(authFetch(`${BASE}/supervisor/books/${encodeURIComponent(bookId)}/resume`, { method: 'POST' })),
  /** 标记收件箱事件已读 */
  supervisorEventRead: (bookId: string, eventId: string) =>
    j<{ ok: boolean }>(
      authFetch(`${BASE}/supervisor/books/${encodeURIComponent(bookId)}/events/${encodeURIComponent(eventId)}/read`, {
        method: 'POST',
      }),
    ),
  // ---- 千章守护中心（只读影子控制台，对标 V14 LIVE LOG + 运行记录 + 推进模式库）----
  /** 守护维度登记册：真实状态快照 + 运行时 env 覆盖 */
  guardRegistry: () =>
    j<{
      module?: string
      version?: string
      updated?: string
      layers: Record<string, string>
      dimensions: Array<{
        id: string
        layer: string
        name: string
        prevents: string
        stage: string
        mode: string
        evidence: string
        packet_key: string | null
        note: string
      }>
      counts: Record<string, Record<string, number>>
      env: Record<string, string>
      note?: string
    }>(authFetch(`${BASE}/guard/registry`)),
  /** 单书健康总览（tracking 存在性、章节数、相关 env 开关、维度计数） */
  guardOverview: (bookId: string) =>
    j<{
      book_id: string
      tracking_present: boolean
      chapter_count: number
      dimension_total: number
      dimension_mode_counts: Record<string, number>
      env: Record<string, string>
      guard_module_enabled: boolean
    }>(authFetch(`${BASE}/guard/overview?book_id=${encodeURIComponent(bookId)}`)),
  /** 运行记录：只读扫描 runtime/agent_tasks（按书过滤、按时间倒序） */
  guardRuns: (bookId: string | null, limit = 50) =>
    j<{
      book_id: string | null
      limit: number
      count: number
      runs: Array<{
        task_id: string
        mode: string | null
        book_id: string | null
        chapter_id: number | null
        status: string | null
        stage_count: number
        last_stage: string | null
        error: string | null
        mtime: number
      }>
    }>(authFetch(`${BASE}/guard/runs?limit=${limit}${bookId ? `&book_id=${encodeURIComponent(bookId)}` : ''}`)),
  /** 当前状态研判（工程层：能力/配置）。book_id 可省略：工程能力是系统级常规指标 */
  guardRisk: (bookId?: string | null) =>
    j<{
      book_id: string | null
      overall: { level: string; label: string; summary: string }
      counts: { high: number; medium: number; low: number; ok?: number }
      system: Array<{ level: string; title: string; detail: string; suggestion: string; action?: { code: string; label: string; risk?: string; confirm?: boolean; command?: string; note?: string; task_id?: string; task_ids?: string[]; mode?: string; path?: string; chapter_id?: number } | null }>
      system_metrics: Array<{
        id: string
        kind: 'dim' | 'env'
        layer?: string
        name: string
        level: string
        mode?: string
        detail: string
        suggestion: string
        action?: { code: string; label: string; risk?: string; confirm?: boolean; command?: string; note?: string; task_id?: string; task_ids?: string[]; mode?: string; path?: string; chapter_id?: number } | null
      }>
      book: Array<{ level: string; title: string; detail: string; suggestion: string; action?: { code: string; label: string; risk?: string; confirm?: boolean; command?: string; note?: string; task_id?: string; task_ids?: string[]; mode?: string; path?: string; chapter_id?: number } | null }>
      dimensions: Array<{
        id: string
        name: string
        layer: string
        level: string
        reason: string
        suggestion: string
        mode?: string
        action?: { code: string; label: string; risk?: string; confirm?: boolean; command?: string; note?: string; task_id?: string; task_ids?: string[]; mode?: string; path?: string; chapter_id?: number } | null
      }>
    }>(authFetch(`${BASE}/guard/risk${bookId ? `?book_id=${encodeURIComponent(bookId)}` : ''}`)),
  /** 故事层（内容）风险研判：读正文/追踪真实资产，判断这本书本身是否在崩（只读） */
  guardStoryRisk: (bookId: string) =>
    j<{
      overall: { level: string; label: string; summary: string }
      metrics: { chapter_count?: number; avg_chars?: number; min_chars?: number; max_chars?: number }
      checks: Array<{ level: string; title: string; detail: string; suggestion: string }>
      /** 2026-11-07：checks=[] 不等于「检查通过」。assessable=false 表示没有样本可查，
       *  前端必须显示「无法研判」，禁止渲染绿色「未发现崩塌信号」。 */
      assessable: boolean
      assess_reason: string
      missing_assets: Array<{ key: string; label: string; state: 'missing' | 'thin' | 'ok' }>
      foundation: {
        ok: boolean
        code: string | null
        summary: string
        bible_present: boolean
        bootstrap_available: boolean
        missing_for_bootstrap: string[]
        missing_extras: string[]
        status: Record<string, string>
      } | null
    }>(authFetch(`${BASE}/guard/story-risk?book_id=${encodeURIComponent(bookId)}`)),
  /** 只读：各章**当前**重写锁状态。2026-11-07 起界面判定只认这个，
   *  不再用「历史上有没有 fuse 失败回执」反推（解锁按设计不删回执）。 */
  guardFuseState: (bookId: string) =>
    j<{
      ok: boolean
      book_id: string
      available: boolean
      chapters: GuardFuseChapterState[]
      any_locked: boolean
      note?: string
    }>(authFetch(`${BASE}/guard/fuse-state?book_id=${encodeURIComponent(bookId)}`)),

  /** 界面内「解除重写锁」：只传 book_id + chapter；书路径由后端按登录用户解析，
   *  响应不含任何文件系统路径 / 用户目录 / 部署结构（多用户隔离，2026-11-07）。
   *  after = 动作后立刻用生产 --check 复核的当前锁态，供界面即时更新。 */
  guardFuseBaseline: (bookId: string, chapter: number) =>
    j<{
      ok: boolean
      action?: string
      book_id: string
      chapter: number
      receipts_total?: number
      previous_baseline?: number | null
      effective?: number
      after?: GuardFuseChapterState
      note?: string
    }>(authFetch(`${BASE}/guard/fix/fuse-baseline`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ book_id: bookId, chapter }),
    })),

  /** 细纲 outline_hash 变更造成的锁：登记计数基线无效，须同步前提基线。 */
  guardFuseSyncHash: (bookId: string, chapter: number) =>
    j<{
      ok: boolean
      action?: string
      book_id: string
      chapter: number
      invalidated_previous?: boolean
      after?: GuardFuseChapterState
      note?: string
    }>(authFetch(`${BASE}/guard/fix/fuse-sync-hash`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ book_id: bookId, chapter }),
    })),

  /** 界面内「一键补建全局状态基线」：只传 book_id，路径/章号由服务端按登录账号解析。
   *  响应只含业务字段，不含任何文件系统路径或命令。 */
  guardRebuildGlobalState: (bookId: string) =>
    j<{
      ok: boolean
      action?: string
      book_id: string
      chapter?: number
      fields?: string[]
      position?: { volume?: string; scene?: string; story_time?: string }
      revision_before?: number
      revision_after?: number
      views_written?: number
      backup_name?: string
      verified?: boolean
      precheck_ok?: boolean | null
      check_ok?: boolean | null
      note?: string
    }>(authFetch(`${BASE}/guard/fix/global-state`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ book_id: bookId }),
    })),

  /** 只读：本书哪些章产生了闸事实、其中哪些还等作者裁决。
   *  available=false 表示读取失败，必须标注「状态未知」，绝不假绿。 */
  guardGateDecisions: (bookId: string) =>
    j<GateDecisionsResp>(
      authFetch(`${BASE}/guard/gate-decisions?book_id=${encodeURIComponent(bookId)}`)),

  /** 作者裁决：accept / recheck / rollback。只传 book_id + chapter + action，
   *  路径由后端按登录账号解析；rollback 必须二次确认并给出 to_chapter。 */
  guardGateDecide: (
    bookId: string,
    chapter: number,
    action: 'accept' | 'recheck' | 'rollback',
    opts?: { note?: string; confirm?: boolean; to_chapter?: number },
  ) =>
    j<GateDecideResp>(authFetch(`${BASE}/guard/gate/decide`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        book_id: bookId, chapter, action,
        note: opts?.note ?? '',
        confirm: opts?.confirm ?? false,
        ...(opts?.to_chapter != null ? { to_chapter: opts.to_chapter } : {}),
      }),
    })),
}
