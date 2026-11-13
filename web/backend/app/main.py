"""NWW-V12 账号面板后端（纯账号管理 + 无状态脚本执行，不含书架）。

职责边界：
  1. 登录 / 注册 / 个人资料 / 自助改密 / 序列号核销 + 管理员用户与序列号管理 —— /auth/*
  2. nww-novel-skill 确定性脚本的无状态执行通道 —— POST /api/skill/run
     本地上传书项目 zip → 远程临时目录跑白名单脚本 → 回传 stdout/产物 → 删除现场。
     远程不落盘任何书稿（跑完即删），无任何 LLM 调用；正文由本地 LLM/IDE 撰写。

2026-09-06 下线数据面（/api/books/* 、/api/search），原因：
  1) 隔离缺失：projects_root() 为全局单路径，不接收用户参数，任何登录用户都看到同一批书籍，
     无法满足「用户独立性」；
  2) 泄露面：任何注册用户均可读取 C:\\nww\\projects 下全部书稿。
书稿读写改由本地 nww-local 客户端负责，Web 面板不再承载数据面。
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .auth import router as auth_router, verify_token, verify_api_key, get_user, user_active
from .skillrun import run_skill_payload

# 数据面开关：本地单机模式（NWW_DATA_PLANE=1）挂载 /api/books/* 书稿端点；
# 远程无状态执行服务默认关闭（远程不存放书稿）。
_DATA_PLANE = os.environ.get("NWW_DATA_PLANE", "0") == "1"
if _DATA_PLANE:
    from .dataplane import router as dataplane_router

# NWW-MVP WebUI 接入开关：NWW_MVP_ENABLED=1 时挂载 /api/books/*/mvp/* 写作流水线端点
# （2026-09-10 新增，独立模块，与现有路由零耦合；关闭即整体下线）
_MVP_ENABLED = os.environ.get("NWW_MVP_ENABLED", "0") == "1"
if _MVP_ENABLED:
    from .nwwmvp import router as nwwmvp_router

# Agent 建书对话开关：NWW_AGENT_ENABLED=1 时挂载 /api/agent/chat
# （2026-09-10 新增，独立模块，走 NWW_AGENT_* 本地模型命名空间；关闭即整体下线）
_AGENT_ENABLED = os.environ.get("NWW_AGENT_ENABLED", "0") == "1"
if _AGENT_ENABLED:
    from .agent import router as agent_router

# 多环节独立 Agent 全流程开关：NWW_AGENTS_ENABLED=1 时启用
# /api/books/*/mvp/agents/write 全流程写章与 /api/agents/registry、/api/agents/routing 诊断
# （2026-09-10 新增，独立模块，依赖 nwwmvp 路由；关闭即整体下线，灰度/快速回滚）
_AGENTS_ENABLED = os.environ.get("NWW_AGENTS_ENABLED", "0") == "1"

# 模型聚合中心开关：NWW_MODEL_CONFIG_ENABLED=1 时挂载
# /api/models/config（读写）/ /api/models/test（连通性测试）/ /api/models/discover（模型发现）
# （2026-09-11 新增，独立模块，不依赖任何业务路由；关闭即整体下线）
_MODEL_CONFIG_ENABLED = os.environ.get("NWW_MODEL_CONFIG_ENABLED", "0") == "1"

# skill 契约执行宿主开关：NWW_SKILL_EXEC_ENABLED=1 时挂载 /api/skill/{name}/execute
# （2026-09-12 新增，外脑收敛方案：按 nww-novel-skill/execution-contract 锁步执行，默认关闭）
_SKILL_EXEC_ENABLED = os.environ.get("NWW_SKILL_EXEC_ENABLED", "0") == "1"

# 流水线监督器开关：NWW_SUPERVISOR_ENABLED=1 时挂载 /api/supervisor/* 监督端点，
# 并在 startup 起常驻监督线程（目标驱动自动派单 + 收件箱督促；2026-10-25 新增，
# 独立模块，依赖 dataplane/nwwmvp；关闭即整体下线）
_SUPERVISOR_ENABLED = os.environ.get("NWW_SUPERVISOR_ENABLED", "0") == "1"
if _SUPERVISOR_ENABLED:
    from .supervisor import router as supervisor_router

# 千章守护中心：/api/guard/* 只读聚合路由（2026-11-04 新增）。
# 只读消费既有落盘（registry / tracking / agent_tasks），不写状态、不建第二套事实、不耦合生产链。
# ★ 默认挂载（只读无副作用，不应因开关默认关而呈现「404/空树」的坏界面）；
#   NWW_GUARD_ENABLED=0 可整体下线做回滚。
_GUARD_ENABLED = os.environ.get("NWW_GUARD_ENABLED", "1") != "0"
if _GUARD_ENABLED:
    from .guard import router as guard_router

_REPO_ROOT = Path(__file__).resolve().parents[3]

# 生产默认关闭 /docs、/redoc、/openapi.json：原公网可直接拉到完整接口清单。
# 需要排障时临时设 NWW_ENABLE_DOCS=1 并重启，用完关掉。
_DOCS_ENABLED = os.environ.get("NWW_ENABLE_DOCS", "0") == "1"

app = FastAPI(
    title="NWW-V12 账号面板",
    version="1.0.0",
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
)


# ---- 鉴权中间件：数据 API(/api/*，health 除外) 与 MCP(/sse) 需 Bearer，并每次回查用户态 ----
# /messages 不鉴权：MCP SSE 协议中 session_id 是随机不可猜测的凭证，仅通过已鉴权的 GET /sse 下发
class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        is_protected_api = path.startswith("/api/") and path != "/api/health"
        is_protected_mcp = path.startswith("/sse")
        if not is_protected_api and not is_protected_mcp:
            return await call_next(request)
        # 提取凭证：支持 JWT（Authorization header / ?token=）和 API Key（X-API-Key header / ?api_key=）
        auth = request.headers.get("Authorization", "")
        jwt_token = auth[7:] if auth.startswith("Bearer ") else None
        api_key = request.headers.get("X-API-Key", "") or None
        if is_protected_mcp:
            # /sse 路径额外接受 URL query（Trae/Qoder/WorkBuddy 不支持自定义 header）
            if not jwt_token:
                jwt_token = request.query_params.get("token")
            if not api_key:
                api_key = request.query_params.get("api_key")

        # 验证：优先 JWT，失败后尝试 API Key
        user_id = None
        role = None
        if jwt_token:
            payload = verify_token(jwt_token)
            if payload:
                user_id = payload.get("user_id")
                role = payload.get("role", "user")
        if not user_id and api_key:
            payload = verify_api_key(api_key)
            if payload:
                user_id = payload.get("user_id")
                role = payload.get("role", "user")

        if not user_id:
            return JSONResponse(status_code=401, content={"detail": "Missing or invalid token/api_key"})
        if not get_user(user_id):
            return JSONResponse(status_code=401, content={"detail": "User no longer exists"})
        # 套餐时长门控：/api/* 过期即拦截；/sse 只做身份认证，套餐校验放到工具调用时优雅报错
        if is_protected_api and not user_active(get_user(user_id)):
            return JSONResponse(status_code=403, content={"detail": "套餐已过期，请充值续费"})
        return await call_next(request)


# 注意顺序：AuthMiddleware 必须先于 CORS 注册，使 401 响应也能带上 CORS 头
app.add_middleware(AuthMiddleware)

# 同源部署（nginx 443 反代）无需跨域；仅保留本地 dev 端口，公网来源不放行。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/auth", tags=["auth"])

if _DATA_PLANE:
    app.include_router(dataplane_router, prefix="/api", tags=["dataplane"])
else:
    @app.get("/api/books")
    def api_books() -> dict:
        """兼容端点：远程不存放书籍，恒为空列表（旧前端书架显示「暂无书」而非报错）。"""
        return {"books": []}

if _MVP_ENABLED:
    app.include_router(nwwmvp_router, prefix="/api", tags=["nwwmvp"])

if _AGENT_ENABLED:
    app.include_router(agent_router, prefix="/api", tags=["agent"])

if _AGENTS_ENABLED:
    from .nwwmvp import agents_router
    app.include_router(agents_router, prefix="/api", tags=["nww-agents"])

if _MODEL_CONFIG_ENABLED:
    from .llm_config import router as model_config_router
    app.include_router(model_config_router, prefix="/api", tags=["model-config"])

if _SKILL_EXEC_ENABLED:
    from .skillrun_route import router as skillrun_router
    app.include_router(skillrun_router, prefix="/api", tags=["skill-exec"])

if _SUPERVISOR_ENABLED:
    app.include_router(supervisor_router, prefix="/api", tags=["supervisor"])

    @app.on_event("startup")
    def _start_supervisor_thread() -> None:
        """startup 起常驻监督线程（幂等；uvicorn --reload 重载也只会保留一个活线程）。"""
        from .supervisor import start_supervisor
        start_supervisor()

if _GUARD_ENABLED:
    app.include_router(guard_router, prefix="/api", tags=["guard"])

# 编辑器选段 AI 改写：POST /api/editor/rewrite（默认开启，编辑器核心功能）
from .editor_rewrite import router as editor_rewrite_router
app.include_router(editor_rewrite_router, prefix="/api", tags=["editor-rewrite"])

# 编辑器 AI 统一网关（V14 工具条能力层）：走模型中心端点链，不再是 NWW_LLM_API_KEY。
# NWW_EDITOR_AI=0 整体下线做回滚；前端探测到 404 自动回落旧 /api/editor/rewrite。
if os.environ.get("NWW_EDITOR_AI", "1") != "0":
    from .editor_ai import router as editor_ai_router
    app.include_router(editor_ai_router, prefix="/api", tags=["editor-ai"])

# AI 实况统一采集面：GET /api/live/feed（只读，默认开启）
# 覆盖所有执行流程的模型调用与环节进度；NWW_LIVE_FEED=0 可整体回滚。
if os.environ.get("NWW_LIVE_FEED", "1") != "0":
    from .live_routes import router as live_router
    app.include_router(live_router, prefix="/api", tags=["live"])


@app.get("/api/health")
def health() -> dict:
    """健康检查。不回显任何服务器路径，避免泄露部署结构。"""
    return {"ok": True}


@app.post("/api/skill/run")
def api_skill_run(payload: dict) -> dict:
    """无状态脚本执行：上传书项目 zip → 临时目录跑白名单脚本 → 回传 stdout/产物 → 删现场。"""
    return run_skill_payload(payload)


# MCP Server：NWW_MCP_ENABLED=1 时注册 /sse 与 /messages 端点，供 Cursor/Claude Desktop 远程调用确定性脚本
# 注意：sse_app() 内部已含 /sse 和 /messages 路由，直接扩展到主 app，避免 mount 导致路径重复
_MCP_ENABLED = os.environ.get("NWW_MCP_ENABLED", "0") == "1"
if _MCP_ENABLED:
    from .mcp_server import build_mcp_server
    from mcp.server.transport_security import TransportSecuritySettings

    # DNS rebinding 保护：默认 sse_app(host="127.0.0.1") 只允许 localhost Host，
    # 经 nginx 反代后 Host 为公网 IP/域名，会被 421 拒绝。
    # 从 NWW_MCP_ALLOWED_HOSTS 环境变量读取额外允许的 Host（逗号分隔，支持 :* 通配端口），
    # 未设置时仅允许 localhost（本地直连场景）。
    _mcp_allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    _mcp_allowed_origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    _extra_hosts = os.environ.get("NWW_MCP_ALLOWED_HOSTS", "")
    if _extra_hosts:
        for _h in _extra_hosts.split(","):
            _h = _h.strip()
            if _h:
                # 同时添加精确匹配（不带端口，nginx 443 标准端口场景）和通配符匹配（带 :*）
                if ":" not in _h:
                    _mcp_allowed_hosts.append(_h)          # 精确：106.54.1.162
                    _mcp_allowed_hosts.append(f"{_h}:*")   # 通配：106.54.1.162:*
                else:
                    _mcp_allowed_hosts.append(_h)
                for _scheme in ("https", "http"):
                    _origin = f"{_scheme}://{_h}" if ":" not in _h else f"{_scheme}://{_h.rsplit(':', 1)[0]}"
                    _mcp_allowed_origins.append(_origin)
                    _mcp_allowed_origins.append(f"{_origin}:*")
    _mcp_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_mcp_allowed_hosts,
        allowed_origins=list(set(_mcp_allowed_origins)),
    )
    _mcp_app = build_mcp_server().sse_app(transport_security=_mcp_security)
    for _route in _mcp_app.routes:
        app.routes.append(_route)


# 生产模式：若前端已构建，则托管静态产物
_dist = _REPO_ROOT / "web" / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="frontend")
