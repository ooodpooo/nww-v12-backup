"""NWW-V12 用户端 Web 后端：验证层。

镜像 nww-v12 现有验证体系（runtime/core/license.py + runtime/api/auth.py + runtime/core/database.py）：
  - JWT(HS256)，默认 24h 过期；create_token / verify_token
  - 用户态以 JSON 文件（users.json）持久化
  - 受保护接口使用 Bearer 依赖 get_current_user，并在每次调用时回查用户态
    （与 runtime/api/runtime.py:37-39 一致：token 不是最终信任源，用户态才是唯一一致性来源）

相对 runtime 原版的两处硬化（不改变"验证流程/架构"，只堵住 v3 审查指出的明文/硬编码密钥坑）：
  1) 密码以 pbkdf2_sha256 哈希存储，不再明文（runtime/data/users.json 为明文 123456×14）
  2) JWT 密钥可由环境变量 NWW_WEB_SECRET 覆盖；默认仍与 runtime 同值，便于本地联调

⚠️ 任何对外暴露前：必须注入强密钥(NWW_WEB_SECRET) + 前置 TLS 反向代理(nginx)，否则等同裸奔。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

_ALGORITHM = "HS256"
_ACCESS_TOKEN_EXPIRE_HOURS = 24
# 安全硬化：密钥必须外部注入。缺省/仍为代码内默认值 → 直接拒绝启动，
# 避免「默认密钥 = 任何人可自签 admin token 接管全站」的 P0 事故（2026-09-06 已实测可接管）。
_DEFAULT_SECRET = "nww-runtime-secret-key-change-in-production"
_SECRET_KEY = os.environ.get("NWW_WEB_SECRET", "").strip()
if (not _SECRET_KEY or _SECRET_KEY == _DEFAULT_SECRET) and os.environ.get("NWW_DEV_WEAK_SECRET", "0") != "1":
    raise RuntimeError(
        "NWW_WEB_SECRET 未设置或仍为代码内默认值，拒绝启动。"
        "生产必须用机器级环境变量注入 32 字符以上随机密钥："
        "[Environment]::SetEnvironmentVariable('NWW_WEB_SECRET', <random>, 'Machine') 后重启服务。"
        "仅本地联调可设 NWW_DEV_WEAK_SECRET=1 绕过。"
    )
if not _SECRET_KEY:
    _SECRET_KEY = _DEFAULT_SECRET

# ---------- 简易 IP 限流 ----------
# 单进程 uvicorn 场景下的内存滑动窗口；多 worker/多实例需换 Redis 等共享存储。
_RATE_WINDOW = 60.0
_RATE_MAX = {"login": 10, "register": 5, "redeem": 10}
_rate_bucket: dict[tuple[str, str], list[float]] = {}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MIN_PASSWORD_LEN = 8

_USERS_FILE = Path(__file__).resolve().parent / "users.json"
_CODES_FILE = Path(__file__).resolve().parent / "codes.json"
_PBKDF2_ITERS = 100_000

# 序列号字母表：去掉 0/O/1/I 等歧义字符，方便手抄手输
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
# 权限档位排名（高者覆盖低者）；None/未知档位视为 0
_TIER_RANK = {"basic": 1, "pro": 2}

router = APIRouter()


# ---------- 密码哈希 ----------
def hash_password(password: str, salt: bytes | None = None) -> str:
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS)
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
    except Exception:
        # 兼容老式明文（仅迁移用，新用户不应出现）
        return stored == password
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return dk.hex() == hash_hex


# ---------- 用户存储 ----------
def _load_users() -> dict:
    if not _USERS_FILE.exists():
        _seed()
    with open(_USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_users(data: dict) -> None:
    with open(_USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _seed() -> None:
    """首次启动无 users.json 时，写入默认 admin/admin123（哈希）。"""
    default = {
        "users": {
            "admin": {
                "email": "admin",
                "password": hash_password("admin123"),
                "nickname": "admin",
                "role": "admin",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
        }
    }
    _USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _save_users(default)


def get_user(email: str) -> dict | None:
    return _load_users().get("users", {}).get(email)


def authenticate_user(email: str, password: str) -> dict | None:
    u = get_user(email)
    if not u:
        return None
    if not verify_password(password, u.get("password", "")):
        return None
    return u


def register_user(email: str, password: str, nickname: str = "", role: str = "user") -> dict | None:
    data = _load_users()
    if email in data.get("users", {}):
        return None
    data.setdefault("users", {})[email] = {
        "email": email,
        "password": hash_password(password),
        "nickname": nickname or email,
        "role": role,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save_users(data)
    return data["users"][email]


def delete_user(email: str) -> bool:
    data = _load_users()
    users = data.get("users", {})
    if email not in users:
        return False
    del users[email]
    _save_users(data)
    return True


# ---------- 序列号 / 邀请码存储 ----------
def _load_codes() -> dict:
    if not _CODES_FILE.exists():
        return {"codes": {}}
    with open(_CODES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_codes(data: dict) -> None:
    _CODES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_CODES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _new_code(secrets_mod) -> str:
    body = "".join(secrets_mod.choice(_CODE_ALPHABET) for _ in range(12))
    return "NWW-" + "-".join(body[i:i + 4] for i in range(0, 12, 4))


def generate_codes(tier: str, days: int, count: int, usage: str = "redeem") -> list[str]:
    """管理员批量生成码。usage=redeem（充值续费）或 invite（邀请注册）。days=0 表示永久。"""
    import secrets as secrets_mod
    if tier not in _TIER_RANK or days < 0 or not 1 <= count <= 100:
        raise ValueError("bad tier/days/count")
    if usage not in ("redeem", "invite"):
        raise ValueError("usage must be redeem or invite")
    data = _load_codes()
    made = []
    while len(made) < count:
        code = _new_code(secrets_mod)
        if code in data["codes"]:
            continue
        data["codes"][code] = {
            "tier": tier,
            "days": days,
            "usage": usage,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "used_by": None,
            "used_at": None,
        }
        made.append(code)
    _save_codes(data)
    return made


def redeem_code(email: str, code: str) -> dict:
    """核销充值码：升级权限档位 + 叠加时长。仅 usage=redeem 的码可用。"""
    code = (code or "").strip().upper()
    data = _load_codes()
    entry = data.get("codes", {}).get(code)
    if not entry:
        raise ValueError("序列号不存在")
    if entry.get("usage", "redeem") != "redeem":
        raise ValueError("该码为邀请注册码，不能用于充值续费")
    if entry.get("used_by"):
        raise ValueError("序列号已被使用")
    users = _load_users().get("users", {})
    u = users.get(email)
    if not u:
        raise ValueError("用户不存在")
    # 档位：只升不降
    new_tier = entry["tier"]
    if _TIER_RANK.get(new_tier, 0) < _TIER_RANK.get(u.get("tier"), 0):
        new_tier = u.get("tier")
    # 时长：days=0 → 永久；否则从 max(now, 现有到期) 顺延
    now = datetime.now()
    days = int(entry["days"])
    if days == 0:
        expires = None
    else:
        cur = u.get("expires_at")
        base = now
        if cur:
            try:
                base = max(now, datetime.fromisoformat(cur))
            except ValueError:
                base = now
        expires = (base + timedelta(days=days)).isoformat(timespec="seconds")
    u["tier"] = new_tier
    u["expires_at"] = expires
    _save_users({"users": users})
    entry["used_by"] = email
    entry["used_at"] = now.isoformat(timespec="seconds")
    _save_codes(data)
    return {"tier": new_tier, "expires_at": expires, "code": code}


def consume_invite_code(email: str, code: str) -> dict:
    """消费邀请注册码：校验有效且未使用 → 标记已用 → 返回附带的 tier/days（注册后激活套餐）。"""
    code = (code or "").strip().upper()
    data = _load_codes()
    entry = data.get("codes", {}).get(code)
    if not entry:
        raise ValueError("邀请码不存在")
    if entry.get("usage") != "invite":
        raise ValueError("该码不是邀请注册码")
    if entry.get("used_by"):
        raise ValueError("邀请码已被使用")
    entry["used_by"] = email
    entry["used_at"] = datetime.now().isoformat(timespec="seconds")
    _save_codes(data)
    return {"tier": entry.get("tier"), "days": entry.get("days", 0), "code": code}


def user_active(u: dict) -> bool:
    """权限/时长检查：admin 永远通过；无 expires_at 视为永久；过期则 False。"""
    if u.get("role") == "admin":
        return True
    exp = u.get("expires_at")
    if not exp:
        return True
    try:
        return datetime.fromisoformat(exp) >= datetime.now()
    except ValueError:
        return True  # 坏数据不误伤，宁可放行


# ---------- JWT ----------
def create_token(user_id: str, role: str = "user") -> str:
    expire = datetime.utcnow() + timedelta(hours=_ACCESS_TOKEN_EXPIRE_HOURS)
    payload = {"user_id": user_id, "role": role, "exp": expire}
    return jwt.encode(payload, _SECRET_KEY, algorithm=_ALGORITHM)


def verify_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, _SECRET_KEY, algorithms=[_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


# ---------- API Key（长期凭证，用于 MCP 等服务间调用） ----------
def _load_api_keys() -> dict:
    """加载所有用户的 API Key。存储在 users.json 的 users[email].api_keys 字段。"""
    users = _load_users()
    result = {}
    for email, u in users.get("users", {}).items():
        for k in u.get("api_keys", []):
            result[k["key"]] = {"user_id": email, "role": u.get("role", "user"), **k}
    return result


def create_api_key(user_id: str, name: str = "default") -> dict:
    """为用户生成新的 API Key。返回完整 Key（仅此时可见完整值）。"""
    import secrets
    key = "nww_" + secrets.token_hex(16)
    users = _load_users()
    if user_id not in users.get("users", {}):
        raise ValueError(f"用户不存在: {user_id}")
    if "api_keys" not in users["users"][user_id]:
        users["users"][user_id]["api_keys"] = []
    entry = {
        "key": key,
        "prefix": key[:12],
        "name": name,
        "created_at": datetime.utcnow().isoformat(),
        "last_used_at": None,
    }
    users["users"][user_id]["api_keys"].append(entry)
    _save_users(users)
    return entry


def list_api_keys(user_id: str) -> list[dict]:
    """列出用户的所有 API Key（不返回完整 key，只返回前缀）。"""
    users = _load_users()
    u = users.get("users", {}).get(user_id)
    if not u:
        return []
    return [{k: v for k, v in entry.items() if k != "key"} for entry in u.get("api_keys", [])]


def revoke_api_key(user_id: str, prefix: str) -> bool:
    """撤销用户的指定 API Key（按前缀匹配）。"""
    users = _load_users()
    u = users.get("users", {}).get(user_id)
    if not u:
        return False
    before = len(u.get("api_keys", []))
    u["api_keys"] = [k for k in u.get("api_keys", []) if k["prefix"] != prefix]
    if len(u["api_keys"]) == before:
        return False
    _save_users(users)
    return True


def verify_api_key(key: str) -> dict | None:
    """验证 API Key，返回 {"user_id", "role"}。验证成功会更新 last_used_at。"""
    if not key or not key.startswith("nww_"):
        return None
    users = _load_users()
    for email, u in users.get("users", {}).items():
        for entry in u.get("api_keys", []):
            if entry["key"] == key:
                # 更新 last_used_at
                entry["last_used_at"] = datetime.utcnow().isoformat()
                _save_users(users)
                return {"user_id": email, "role": u.get("role", "user")}
    return None


# ---------- 依赖 ----------
def get_current_user(request: Request) -> dict:
    """解析凭证（JWT Bearer 或 API Key）+ 每次调用回查用户态。"""
    # 优先 JWT（Authorization: Bearer）
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        payload = verify_token(token)
        if payload:
            user_id = payload.get("user_id")
            user = get_user(user_id)
            if user:
                return {"user_id": user_id, "role": user.get("role", "user")}
    # 回退 API Key（X-API-Key header）
    api_key = request.headers.get("X-API-Key", "")
    if api_key:
        payload = verify_api_key(api_key)
        if payload:
            user_id = payload.get("user_id")
            user = get_user(user_id)
            if user:
                return {"user_id": user_id, "role": user.get("role", "user")}
    raise HTTPException(status_code=401, detail="Missing or invalid token/api_key")


# 供 main.py 的路由依赖 / 中间件复用
require_auth = Depends(get_current_user)


# ---------- 限流工具 ----------
def _client_ip(request: Request) -> str:
    """取客户端 IP：优先 X-Forwarded-For（nginx 反代场景），回落到直连地址。"""
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit(request: Request, action: str) -> None:
    """同一 IP + 动作的滑动窗口限流，超出返回 429。"""
    key = (_client_ip(request), action)
    now = time.monotonic()
    bucket = [t for t in _rate_bucket.get(key, []) if now - t < _RATE_WINDOW]
    if len(bucket) >= _RATE_MAX.get(action, 10):
        _rate_bucket[key] = bucket
        raise HTTPException(status_code=429, detail=f"操作过于频繁，请 {int(_RATE_WINDOW)} 秒后再试")
    bucket.append(now)
    _rate_bucket[key] = bucket


# ---------- 请求/响应模型 ----------
class LoginRequest(BaseModel):
    user: str
    password: str


class LoginResponse(BaseModel):
    token: str
    expires: int
    role: str
    tier: str | None = None
    expires_at: str | None = None


class RegisterRequest(BaseModel):
    email: str
    password: str
    nickname: str = ""
    invite_code: str = ""


class ProfileResponse(BaseModel):
    email: str
    nickname: str
    role: str
    created_at: str | None = None
    tier: str | None = None
    expires_at: str | None = None


# ---------- 路由 ----------
@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest, request: Request):
    """登录：限流 → 校验用户态 → 签发 JWT。失败同样计入限流（防暴力破解）。"""
    _rate_limit(request, "login")
    user = authenticate_user(req.user, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token(user_id=req.user, role=user.get("role", "user"))
    return LoginResponse(
        token=token,
        expires=_ACCESS_TOKEN_EXPIRE_HOURS * 3600,
        role=user.get("role", "user"),
        tier=user.get("tier"),
        expires_at=user.get("expires_at"),
    )


@router.get("/register/config")
def register_config() -> dict:
    """公开注册配置：前端据此决定是否显示邀请码输入框及是否必填。"""
    allow_register = os.environ.get("NWW_ALLOW_REGISTER", "0") == "1"
    require_invite = os.environ.get("NWW_ALLOW_INVITE_REGISTER", "0") == "1"
    return {
        "allow_register": allow_register or require_invite,
        "require_invite": require_invite,
    }


@router.post("/register", response_model=ProfileResponse)
def register(req: RegisterRequest, request: Request):
    """注册。
    - NWW_ALLOW_REGISTER=1：开放注册，无需邀请码。
    - NWW_ALLOW_INVITE_REGISTER=1：邀请注册，必须提供有效邀请码（usage=invite），注册成功后按码的 tier/days 激活套餐。
    - 两者都未设：注册关闭。
    """
    _rate_limit(request, "register")
    allow_open = os.environ.get("NWW_ALLOW_REGISTER", "0") == "1"
    require_invite = os.environ.get("NWW_ALLOW_INVITE_REGISTER", "0") == "1"
    if not allow_open and not require_invite:
        raise HTTPException(status_code=403, detail="注册已关闭，请联系管理员开通账号")
    email = (req.email or "").strip()
    password = req.password or ""
    nickname = (req.nickname or "").strip()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    if len(password) < _MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400, detail=f"密码长度不能少于 {_MIN_PASSWORD_LEN} 位")
    if len(nickname) > 32:
        raise HTTPException(status_code=400, detail="昵称过长（最多 32 字）")

    # 邀请码校验（邀请注册模式下必填）
    invite_info = None
    if require_invite:
        if not req.invite_code:
            raise HTTPException(status_code=400, detail="请输入邀请码")
        try:
            invite_info = consume_invite_code(email, req.invite_code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    user = register_user(email, password, nickname)
    if not user:
        # 注册失败时回滚邀请码（如果已消耗）
        if invite_info:
            data = _load_codes()
            entry = data.get("codes", {}).get(invite_info["code"])
            if entry:
                entry["used_by"] = None
                entry["used_at"] = None
                _save_codes(data)
        raise HTTPException(status_code=400, detail="User already exists")

    # 注册成功：按邀请码激活套餐
    if invite_info and invite_info.get("tier"):
        users = _load_users().get("users", {})
        u = users.get(email)
        if u:
            tier = invite_info["tier"]
            days = int(invite_info.get("days") or 0)
            now = datetime.now()
            if days == 0:
                expires = None
            else:
                expires = (now + timedelta(days=days)).isoformat(timespec="seconds")
            u["tier"] = tier
            u["expires_at"] = expires
            _save_users({"users": users})
            user = u  # 同步到返回值

    return ProfileResponse(
        email=user["email"],
        nickname=user["nickname"],
        role=user["role"],
        created_at=user.get("created_at"),
        tier=user.get("tier"),
        expires_at=user.get("expires_at"),
    )


@router.get("/profile", response_model=ProfileResponse)
def profile(user=Depends(get_current_user)):
    """获取当前用户资料（需 Bearer）。"""
    u = get_user(user["user_id"])
    return ProfileResponse(
        email=u["email"],
        nickname=u.get("nickname", u["email"]),
        role=u.get("role", "user"),
        created_at=u.get("created_at"),
        tier=u.get("tier"),
        expires_at=u.get("expires_at"),
    )


class SelfChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@router.put("/password")
def self_change_password(
    req: SelfChangePasswordRequest, user=Depends(get_current_user), request: Request = None
):
    """用户自助改密：校验旧密码 → 写入新密码哈希。用户独立性的基础能力之一。"""
    _rate_limit(request, "login")
    u = get_user(user["user_id"])
    if not u or not verify_password(req.old_password, u.get("password", "")):
        raise HTTPException(status_code=400, detail="原密码不正确")
    if len(req.new_password or "") < _MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400, detail=f"新密码长度不能少于 {_MIN_PASSWORD_LEN} 位")
    data = _load_users()
    data.setdefault("users", {})[user["user_id"]]["password"] = hash_password(req.new_password)
    _save_users(data)
    return {"status": "password_changed"}


class RedeemRequest(BaseModel):
    code: str


@router.post("/redeem")
def redeem(req: RedeemRequest, user=Depends(get_current_user), request: Request = None):
    """核销序列号：升级档位 + 叠加时长（充值/续费）。限流防序列号爆破。"""
    _rate_limit(request, "redeem")
    try:
        result = redeem_code(user["user_id"], req.code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "redeemed", **result}


@router.get("/check")
def check(user=Depends(get_current_user)):
    """检查当前 token 对应用户状态。"""
    return {"status": "active", "user_id": user["user_id"], "role": user["role"]}


# ---------- API Key 管理（用户自助） ----------
class CreateApiKeyRequest(BaseModel):
    name: str = "default"


@router.get("/api-keys")
def list_my_api_keys(user=Depends(get_current_user)):
    """列出当前用户的 API Key（只返回前缀，不返回完整 key）。"""
    return {"api_keys": list_api_keys(user["user_id"])}


@router.post("/api-keys")
def create_my_api_key(req: CreateApiKeyRequest, user=Depends(get_current_user)):
    """生成新的 API Key。**仅此次返回完整 key，之后不可见**。"""
    entry = create_api_key(user["user_id"], req.name)
    return {"status": "created", **entry}


@router.delete("/api-keys/{prefix}")
def revoke_my_api_key(prefix: str, user=Depends(get_current_user)):
    """撤销指定 API Key（按前缀匹配）。"""
    ok = revoke_api_key(user["user_id"], prefix)
    if not ok:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    return {"status": "revoked", "prefix": prefix}


# ---------- 管理员：用户管理 ----------
def require_admin(user=Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ---------- 管理员：序列号 ----------
class GenerateCodesRequest(BaseModel):
    tier: str = "basic"
    days: int = 30
    count: int = 10
    usage: str = "redeem"


@router.get("/codes")
def admin_list_codes(admin=Depends(require_admin)):
    """列出全部码（充值码 + 邀请码）及核销状态。"""
    codes = _load_codes().get("codes", {})
    return {
        "codes": [
            {"code": k, **v}
            for k, v in sorted(codes.items(), key=lambda kv: kv[1].get("created_at") or "")
        ]
    }


@router.post("/codes")
def admin_generate_codes(req: GenerateCodesRequest, admin=Depends(require_admin)):
    """批量生成码：usage=redeem（充值续费）或 invite（邀请注册）。days=0 表示永久。"""
    try:
        made = generate_codes(req.tier, req.days, req.count, usage=req.usage)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"codes": made, "tier": req.tier, "days": req.days, "usage": req.usage}


class AdminCreateUserRequest(BaseModel):
    email: str
    password: str
    nickname: str = ""
    role: str = "user"


@router.get("/users")
def admin_list_users(admin=Depends(require_admin)):
    """列出全部用户（不含密码哈希）。"""
    users = _load_users().get("users", {})
    return {
        "users": [
            {
                "email": u.get("email", k),
                "nickname": u.get("nickname", ""),
                "role": u.get("role", "user"),
                "created_at": u.get("created_at"),
                "tier": u.get("tier"),
                "expires_at": u.get("expires_at"),
            }
            for k, u in users.items()
        ]
    }


@router.post("/users", response_model=ProfileResponse)
def admin_create_user(req: AdminCreateUserRequest, admin=Depends(require_admin)):
    """管理员建号 —— 注册关闭时的唯一开户通道。"""
    if not req.email or not req.password:
        raise HTTPException(status_code=400, detail="email and password required")
    if req.role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="role must be user or admin")
    user = register_user(req.email, req.password, req.nickname, role=req.role)
    if not user:
        raise HTTPException(status_code=400, detail="User already exists")
    return ProfileResponse(
        email=user["email"],
        nickname=user["nickname"],
        role=user["role"],
        created_at=user.get("created_at"),
    )


@router.delete("/users/{email}")
def admin_delete_user(email: str, admin=Depends(require_admin)):
    """删除用户。不能删自己（防止误删唯一 admin 后被锁死）。"""
    if email == admin["user_id"]:
        raise HTTPException(status_code=400, detail="不能删除当前登录的管理员账号")
    if not delete_user(email):
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "deleted", "email": email}


class AdminResetPasswordRequest(BaseModel):
    password: str


@router.put("/users/{email}/password")
def admin_reset_password(email: str, req: AdminResetPasswordRequest, admin=Depends(require_admin)):
    """管理员重置任意用户密码。"""
    data = _load_users()
    users = data.get("users", {})
    if email not in users:
        raise HTTPException(status_code=404, detail="User not found")
    if not req.password:
        raise HTTPException(status_code=400, detail="password required")
    users[email]["password"] = hash_password(req.password)
    _save_users(data)
    return {"status": "password_reset", "email": email}
