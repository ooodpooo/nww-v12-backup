"""本地数据面路由（/api/books/*）：书稿文件读写、追踪、搜索。

仅本地开发/单机模式挂载（NWW_DATA_PLANE=1）；远程无状态执行服务不含本模块
（远程不存放书稿，见 main.py 架构说明）。鉴权由 main.py 的 AuthMiddleware 统一负责。

2026-09-07 多用户隔离改造：projects_root 按 user_id 分路径（projects/{user_id}/），
每个登录用户只能看到/操作自己 workspace 下的书籍。
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from . import books
from . import chapter_lock
from . import llm_client, llm_config
from . import live_feed
from .auth import get_current_user
from .fs import book_root, is_writable, read_file, rename_file, resolve_path

router = APIRouter()

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PROJECTS_ROOT = _REPO_ROOT / "projects"

# user_id（即 email）中可能出现 @/. 等字符，直接作为目录名在 macOS 上合法，
# 但禁止路径分隔符与空值，防止穿越。
_SAFE_USER_RE = re.compile(r"^[^\s/\\]+$")


def _safe_user_dir(user_id: str) -> str:
    if not user_id or not _SAFE_USER_RE.match(user_id):
        raise HTTPException(status_code=400, detail="invalid user id")
    return user_id


def projects_root(user_id: str) -> Path:
    """按用户隔离的 workspace 根路径：projects/{user_id}/"""
    base = Path(os.environ.get("NWW_PROJECTS_ROOT", DEFAULT_PROJECTS_ROOT))
    root = base / _safe_user_dir(user_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _user_workspace(user: dict) -> Path:
    return projects_root(user["user_id"])


@router.get("/books")
def api_books(user=Depends(get_current_user)) -> dict:
    """书架列表 + 副本诊断：books 为正常书籍，ignored 为疑似复制副本。"""
    listed, ignored = books.list_books_with_diagnostics(_user_workspace(user))
    return {"books": listed, "ignored": ignored}


@router.post("/books")
def api_create_book(payload: dict, user=Depends(get_current_user)) -> dict:
    """创建书籍：payload 需含 title；id 可选，缺省时用 title 做安全 slug。"""
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    book_id = (payload.get("id") or "").strip() or _slugify(title)
    if not book_id:
        raise HTTPException(status_code=400, detail="invalid book id")
    genre = (payload.get("genre") or "").strip()
    synopsis = (payload.get("synopsis") or "").strip()
    draft = payload.get("draft")
    if draft is not None and not isinstance(draft, dict):
        raise HTTPException(status_code=400, detail="draft must be an object")
    try:
        result = books.create_book(_user_workspace(user), book_id, title, genre, synopsis, draft)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result


# ---------------------------------------------------------------------------
# 建书 bootstrap（2026-10-23 建书补全方案 Step2，纯扩展零耦合）：
# chatbox 建书只落目录+bible，大纲/设定文件缺失。本路由读 bible.json 的
# 建书草案八项，调 book-coach 端点链生成 卷纲/世界观/主角 三文件。
# 幂等：已存在同名文件跳过不覆盖；失败只回 {ok:false,error}，不回滚已建目录。
# 2026-10-23 修复「部分文件静默成功」：单次调用输出三文件易截断/格式漂移，
# 改为逐文件生成（短输出+无标记协议），单文件失败自动重试一次，
# 三件齐全才 ok:true；成功一个落盘一个，重试只补缺失文件。
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 建书三件套（2026-10-30 起为「建书资产集」，见 api_bootstrap_book 的 ordered 列表）
#
# ⚠ 本元组现仅用于**资产体检**（check_foundation_assets）的三件核心判定
#   （卷纲 / 世界观 / 主角卡）；实际生成清单以 api_bootstrap_book 内的 `ordered` 为准
#   （含势力卡、商品档案、文风）。两处不一致时以 `ordered` 为生成真源。
# ---------------------------------------------------------------------------
_BOOTSTRAP_FILES = (
    "大纲/卷纲_第1卷.md",
    "设定/世界观/世界观.md",
)
_CARD_RELPREFIX = "设定/角色/"


def _bootstrap_card_rel(bible: dict) -> str:
    """主角卡路径：按 bible 主角名命名，与 chatbox 角色卡惯例（设定/角色/<姓名>.md，
    见 agent.py APPLY 协议）及存量各书一致；主角名缺失/不可解析时退回 主角.md。"""
    prota = bible.get("protagonist")
    raw = ""
    if isinstance(prota, dict):
        raw = str(prota.get("name") or "").strip()
    elif isinstance(prota, str):
        raw = prota.strip()
    m = re.match(r"^([\u4e00-\u9fa5·A-Za-z]{1,12})", raw)
    name = m.group(1) if m else ""
    return _CARD_RELPREFIX + (name or "主角") + ".md"
_BOOTSTRAP_FILE_MARK_RE = re.compile(r"^===FILE:(.+?)===\s*$")
# chrome 桥接端点给回复附加的整理模板起始标记（gemini 网页桥回包时拼在正文之后）
_BRIDGE_RESULT_MARK = "════ 结果分区"

_BOOTSTRAP_COMMON_PROMPT = """你是网文建书助手（book-coach）。根据用户提供的建书草案资料，产出一份开书必备文件。
输出规则（硬性）：
- 只输出该文件的 Markdown 正文本身，从一级标题开始；
- 不要任何解释、前言、结语；不要代码围栏；不要输出 ===FILE: 标记。
内容要求：
- 与草案资料一致，不得自创与资料矛盾的核心设定；资料未覆盖处可合理补全但须自洽。
- 面向番茄平台网文读者：节奏快、冲突前置、爽点明确；主角视角优先账房式冷幽默（若草案含此类设定）。
- 全部使用中文 Markdown，标题层级清晰。

标题规范（2026-10-30 硬性要求，直接决定设定能否被写章链读到的召回率）：
- 每条 ## / ### 小节的标题必须是**可被正文原样出现的实体名**，例：
  `## 青牛坊市`、`## 红尘满足值`、`## 店规领域`、`## 辟谷宗`；
- **禁止**把标题写成「一、」「2.2」「5.1 主场景：…」这类序号 + 长说明句式
  （长复合标题里的实体名会被写章链的召回器切不出来 → 设定写了等于没写）；
- 实体名出现在标题里时用**粗体**标出正文首次定义处，如「- **青牛坊市**：……」；
- 小节数量按需给足，不要把所有内容塞进一个超长小节。"""

_CARD_TASK = ("任务：产出主角人物卡。包含姓名、身份、欲望、金手指与互动方式、"
              "性格硬约束、致命弱点、人物弧光、语言风格与口头禅。"
              "必须与已给定的世界观/卷纲事实**完全一致**（灵根、修为、势力名、地名、"
              "金手指规则不得与之冲突）。")

# ★ 2026-10-30 建书资产扩容（对照 setup/SKILL.md Step 1/1.2/1.6）：
#   原 bootstrap 只产 3 个文件（卷纲 + 世界观.md + 主角卡），而 setup 规定的建书标配是
#   设定/角色/ + 设定/世界观/ + 设定/势力/ + 设定/商品|道具/ + 设定/文风.md
#   → 产出「只有屋顶没有墙」的临时房：势力无卡、商品无档、文风缺失。
#   本次按「一次多文件」补齐缺口，且每份都带明确任务说明。
_BOOTSTRAP_TASKS = {
    "大纲/卷纲_第1卷.md":
        "任务：产出第一卷卷纲。必须含单独一行「本卷章节区间：第 1 - 30 章」（章数按篇幅规划合理确定），"
        "以及本卷核心冲突、阶段划分、关键事件节拍、卷末高潮与钩子。"
        "地名、势力名、人物名必须与已给定的世界观完全一致。",
    "设定/世界观/世界观.md":
        "任务：产出世界观设定。包含世界格局、力量/规则体系、代价机制、主要势力、地域与时代氛围。"
        "按实体名分小节（如「## 青牛坊市」「## 红尘满足值」「## 辟谷宗」），"
        "每个实体独立成节并在正文首次出现处用粗体标注。"
        "末尾必须含「## 禁用条款」一节，列出本书**禁止出现**的器物/材质/品牌词"
        "（如现代工业材质、现实食品品牌），每条写明「禁用：词1、词2、词3」。",
    "设定/势力/主要势力.md":
        "任务：产出本书主要势力档案。至少覆盖 2 个势力，每个势力独立成节"
        "（标题即势力名，如「## 辟谷宗」），含：沿革时间线（谁建/为什么/几次大事+距今年限）、"
        "制度压顶（规矩 + 代价梯度写死）、经济生态（物价锚）、行话黑话、与其他势力的恩怨。",
    "设定/商品/商品档案.md":
        "任务：产出本书核心商品/道具档案。至少覆盖核心商品 1-2 件，每件独立成节"
        "（标题即商品名，如「## 泡面」），含：来历链、功能与**限制**（写清挡不住什么）、"
        "等级与解锁路径、每次重大使用的状态变化。",
    "设定/文风.md":
        "任务：产出本书文风档案（供写章链做风格锚）。必须含以下小节标题："
        "「## 文风档案」「## 基调与冷幽默」「## 句式与排版节奏」「## 文风禁忌」。"
        "文风档案写语言风格/叙事视角/句式节奏/对标参考/文风锚点片段（2-3 段示范文字）；"
        "句式与排版节奏写句长偏好与段落形态；文风禁忌写本书不能出现的腔调。"
        "【叙事视角硬约束】叙事视角必须与建书草案资料里的「叙事人称」完全一致（第一/第三人称），"
        "禁止自行判断或改成人称；文风锚点片段也必须用该人称写作，与叙事视角保持一致。",
}


def _bootstrap_material(bible: dict) -> str:
    """bible.json → 八项建书资料文本（缺项留空，由模型按已有信息合理补全）。"""
    def g(*keys: str, default: str = "") -> str:
        for k in keys:
            v = bible.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return default

    prota = bible.get("protagonist")
    if isinstance(prota, dict):
        prota_text = str(prota.get("description") or "")
    elif isinstance(prota, str):
        prota_text = prota
    else:
        prota_text = ""
    creative = bible.get("creative") if isinstance(bible.get("creative"), dict) else {}
    lines = [
        f"书名：{g('book_title', 'title')}",
        f"类型：{g('genre')}",
        f"一句话简介：{g('synopsis', 'world_summary')}",
        f"核心命题：{str(creative.get('core_question') or '')}",
        f"主角与金手指：{prota_text}",
        f"冲突结构：{g('conflict')}",
        f"差异化：{g('differentiation')}",
        f"篇幅规划：{g('scale')}",
        f"发布平台：{g('platform', default='番茄')}",
        f"叙事人称（视角）：{g('pov', default='第三人称')}",
        f"单章字数目标：{g('wordcount_target', default='2500-2750')}",
    ]
    return "以下是建书草案资料：\n" + "\n".join(lines)


def _clean_llm_doc(text: str) -> str:
    """规整单文件 LLM 输出，返回可落盘的文档正文；无正文返回空串。

    剥四层残留（2026-10-26 建书污染修复）：
    1. 首尾代码围栏；
    2. 残留 ===FILE: 标记行；
    3. 桥接整理模板块（「════ 结果分区…」8 分区或旧「【执行摘要…】」）——
       模板可能在正文后，也可能夹在对话与正文之间（多轮拼接实测）；
    4. 模板外的对话式残留（实测桥接模型会回「收到」「请告诉我目标文件
       路径」而不产出文档）——文档正文从第一个 Markdown 标题行起取。
    清洗后没有标题 = 纯对话回复，返回空串让上层按失败重试，杜绝对话
    残留落盘成开书文件。
    """
    s = (text or "").strip()
    if s.startswith("```"):
        parts = s.split("\n", 1)
        s = parts[1] if len(parts) == 2 else ""
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    kept: list[str] = []
    in_template = False
    for ln in s.splitlines():
        if not in_template and (_BRIDGE_RESULT_MARK in ln or ln.startswith("【执行摘要（桥接生成")):
            in_template = True
            continue
        if in_template:
            # 分区模板以「八、验证结果」（8 分区形态）或「- 完整记录：」（旧摘要形态）收尾
            if ln.startswith("八、") or ln.startswith("- 完整记录："):
                in_template = False
            continue
        if not _BOOTSTRAP_FILE_MARK_RE.match(ln.strip()):
            kept.append(ln)
    heading_idx = next((i for i, ln in enumerate(kept) if ln.lstrip().startswith("#")), None)
    if heading_idx is None:
        return ""
    return "\n".join(kept[heading_idx:]).strip()


def _bootstrap_one(rel: str, task: str, bible: dict, chain: list,
                   facts: str = "") -> tuple[str, str]:
    """生成单个开书文件。返回 (content, error)，content 非空即成功。失败自动重试一次。

    facts（2026-10-30 新增）：已生成文件的正文摘要，作为**事实约束**注入 prompt。
    根因：原实现每份文件独立调用 LLM、各自只看 bible 摘要 → 三份文件**互不知情**，
    于是同一本书出现三套名字（坊市：青牛 vs 青阳；宗门：辟谷宗 vs 青云宗）与
    两套主角设定（无灵根 vs 五灵根+炼气一层）——写章时必然打架。
    修法：按固定顺序生成，把已落盘文件的内容作为「已确定事实」喂给后续生成，
    使后续文件只能复用既有命名与设定，不得另起一套。
    """
    user_prompt = _bootstrap_material(bible)
    if facts.strip():
        user_prompt += (
            "\n\n【已确定事实（必须严格沿用，不得另起名字或改设定）】\n"
            "以下是本书已生成文件的正文节选。后续文件中的地名、势力名、人物名、"
            "主角设定（灵根/修为）、金手指规则、等级体系等，**必须与此处完全一致**；"
            "如需引用，直接使用这里的原词，不要改写或另造同义名。\n\n"
            + facts.strip()
        )
    user_prompt += "\n\n" + task
    last_err = ""
    for _ in range(2):
        try:
            # 桥接端点生成慢（网页版实测数分钟），预算给足；chat_completion 内部
            # 对 8788/8789 桥接还会自动抬到 NWW_BRIDGE_HTTP_TIMEOUT_S 下限。
            text = llm_client.chat_completion_robust(
                _BOOTSTRAP_COMMON_PROMPT, user_prompt, chain,
                temperature=0.6, max_tokens=6000, timeout=600,
            )
        except RuntimeError as exc:
            last_err = str(exc)
            continue
        content = _clean_llm_doc(text)
        if len(content) >= 80:
            return content, ""
        # 原始输出非空但清洗后无正文 → 模型只回了对话（无 Markdown 标题）
        last_err = ("模型输出非文档（对话式回复，无 Markdown 标题正文）"
                    if (text or "").strip() and not content else "模型输出过短或为空")
    return "", last_err


def _bootstrap_facts(prior: list[tuple[str, str]], limit: int = 6000) -> str:
    """已生成文件 → 事实约束文本（按相对路径标注来源，截断到 limit 字）。"""
    chunks: list[str] = []
    used = 0
    for rel, content in prior:
        if not content or not content.strip():
            continue
        piece = f"--- {rel} ---\n{content.strip()}\n"
        if used + len(piece) > limit:
            piece = piece[:max(0, limit - used)]
            if piece:
                chunks.append(piece)
            break
        chunks.append(piece)
        used += len(piece)
    return "\n".join(chunks)


@router.post("/books/{book_id}/bootstrap")
def api_bootstrap_book(book_id: str, user=Depends(get_current_user)) -> dict:
    """AI 实况入口（2026-11-08）：建书立项整体一个 run，逐文件生成各自一个环节。"""
    with live_feed.begin_run(
        "建书立项 · 开书资料包",
        mode="bootstrap",
        book_id=book_id,
        kind="flow",
        user_id=user.get("user_id"),
    ) as run:
        return _bootstrap_book_impl(book_id, user=user, run=run)


def _bootstrap_book_impl(book_id: str, user, run=None) -> dict:
    """生成开书三文件（卷纲/世界观/主角卡），逐文件生成。

    主角卡按 bible 主角名命名（设定/角色/<姓名>.md，无名退回 主角.md）。
    只写白名单路径，已存在则跳过（重试只补缺失文件）；
    三件齐全才 ok:true，任一缺失即 ok:false + missing（修复部分失败静默成功）。
    """
    root = book_root(_user_workspace(user), book_id)
    bible = books.load_json(root / "bible.json")
    if not bible:
        raise HTTPException(status_code=404, detail=f"书籍不存在或缺少 bible.json：{book_id}")
    # bootstrap 生成的是「纯文档」，对端点合规性敏感：优先走专用路由
    # book-bootstrap（指向直连/本地模型）；未配置时退回 book-coach。
    # 不直接改用 book-coach——那同时是 chatbox 对话路由（前端模型选择器的默认），
    # 网页桥接模型对话可用但对「只输出文档」指令合规率低（2026-10-26 污染事故根因）。
    chain = llm_config.agent_endpoint_chain("book-bootstrap") or llm_config.agent_endpoint_chain("book-coach")
    if not chain:
        return {"ok": False, "error": "LLM 未配置：模型中心无可用端点（book-coach 路由链为空）"}
    written: list[str] = []
    skipped: list[str] = []
    failed: list[dict] = []
    card_rel = _bootstrap_card_rel(bible)

    # ★ 生成顺序（2026-10-30）：世界观 → 势力/商品/文风 → 卷纲 → 主角卡。
    #   世界观先行（它是命名与规则的源头），其余文件把已生成的设定当事实约束，
    #   消除「三份文件各说各话」的漂移（坊市/宗门/灵根三处不一致）。
    #   顺序固定 = 结果可复现；幂等跳过已存在文件，但**已存在的文件也要读回来**
    #   作为事实约束（否则重跑补缺时新文件会与老文件打架）。
    ordered: list[str] = [
        "设定/世界观/世界观.md",
        "设定/势力/主要势力.md",
        "设定/商品/商品档案.md",
        "设定/文风.md",
        "大纲/卷纲_第1卷.md",
        card_rel,
    ]
    prior: list[tuple[str, str]] = []
    for rel in ordered:
        target = root / rel
        if target.exists():
            skipped.append(rel)
            try:
                prior.append((rel, target.read_text(encoding="utf-8", errors="ignore")))
            except Exception:  # noqa: BLE001 — 读回失败不阻断（仅少一份事实约束）
                pass
            continue
        task = _CARD_TASK if rel.startswith(_CARD_RELPREFIX) else _BOOTSTRAP_TASKS.get(rel, "")
        if not task:
            failed.append({"path": rel, "error": "无对应生成任务定义"})
            continue
        with live_feed.stage(rel, f"生成 {rel}", label=rel.split("/")[-1].replace(".md", "")) as _st:
            content, err = _bootstrap_one(rel, task, bible, chain,
                                           facts=_bootstrap_facts(prior))
            _st.detail_set(f"生成 {rel}")
            if content:
                _st.output(content[:4000])
            elif err:
                _st.fail(err)
        if content:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content + "\n", encoding="utf-8")
            written.append(rel)
            prior.append((rel, content))
        else:
            failed.append({"path": rel, "error": err})
    if failed:
        detail = "、".join(f"{f['path']}（{f['error']}）" for f in failed)
        return {"ok": False, "error": f"开书资料未全部生成，缺失：{detail}",
                "missing": [f["path"] for f in failed],
                "written": written, "skipped": skipped}
    return {"ok": True, "written": written, "skipped": skipped, "missing": []}


@router.post("/books/{book_id}/initialize")
def api_initialize_book(book_id: str, user=Depends(get_current_user)) -> dict:
    """界面内初始化 / 重建追踪状态（2026-11-07 根治 CLI 命令串反模式）。

    按当前登录用户目录解析书路径（多用户隔离），复用 books.initialize_book → 生产
    nww_track_commit.initialize；响应只回业务字段，不回传绝对路径或命令字符串；
    已存在非 stub 追踪状态时拒绝覆盖，返回 manual_required 由人工核对。
    """
    try:
        book = book_root(_user_workspace(user), book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        with live_feed.begin_run("初始化追踪状态", mode="initialize", book_id=book_id,
                                 kind="flow", user_id=user.get("user_id")):
            with live_feed.stage("initialize", "按当前用户目录复用 initialize_book", label="初始化追踪") as _st:
                res = books.initialize_book(book)
                _st.output(str((res or {}).get("detail") or (res or {}).get("ok"))[:2000])
                return res
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — 初始化失败如实上报，不静默
        raise HTTPException(status_code=500, detail=f"初始化失败：{exc}") from exc


# ---------------------------------------------------------------------------
# §B 建书资产体检（2026-10-29 落地，2026-10-30 恢复）
#
# 背景：本函数曾于 2026-10-29 落地并有 tests/test_foundation_preflight.py 守护，
#   但工作区 dataplane.py 回退到 10-26 状态后函数丢失 → 该测试 ImportError →
#   全量 pytest 在 collection 阶段直接中断（681 基线尽失）。此处按既有测试规格原样恢复。
#
# 判定分层（severity）：
#   - bible.json 缺失 → MISSING_BIBLE（致命）：bootstrap 硬依赖 bible.json，
#     系统不替作者发明设定，必须作者补原始构思。
#   - 世界观/卷纲/主角卡 缺失或过薄 → MISSING_FOUNDATION（可补）：bootstrap 可补齐。
# ---------------------------------------------------------------------------

# 建书三件套的相对路径真源 = _BOOTSTRAP_FILES（卷纲 + 世界观）+ 主角卡。
# 这里只做别名，避免同一事实源出现两份字面量。
_FOUNDATION_VOLUME_REL, _FOUNDATION_WORLD_REL = _BOOTSTRAP_FILES

# 过薄阈值（字节）。低于此值视为「空壳/占位」——有路径但没实质内容。
# 标定依据（按 tests/test_foundation_preflight.py 既有规格）：
#   空壳卡 "# 林小凡\n主角。" = 19 字节 → 判 thin
#   存量薄卡 "# 林小凡\n- 身份：…- 金手指：…- 弱点：…" = 108 字节 → 判 ok
# ⇒ 阈值取 60，落在两者之间。
# 注意：这是「空壳检测下限」，不是「厚度达标线」——真正的建书厚度由
# skills/nww-novel-skill/skills/setup/SKILL.md Step 1.6 的 checkpoint 负责
# （主角卡 ≥2000 字 / 配角 ≥800 字 / 势力卡 / 商品卡），两者是不同关卡。
_FOUNDATION_MIN_BYTES = 60

# 建书标配的**补充资产**（对照 skills/nww-novel-skill/skills/setup/SKILL.md
# Step 1.2 文风 / Step 1.6 底蕴资产）。三件核心齐备但这些缺失 = 「临时房」。
# 判定按「同目录任一 .md 实质存在即算有」（存量书命名不统一）。
_FOUNDATION_EXTRA_ASSETS = {
    "faction_cards": "设定/势力/主要势力.md",
    "item_cards": "设定/商品/商品档案.md",
    "style": "设定/文风.md",
}


def _asset_body_status(path: Path) -> str:
    """资产实体状态：missing / thin / ok。

    thin 判定：文件存在但正文过短（< _FOUNDATION_MIN_BYTES 字节），
    即「路径在、内容是空壳」——此时补全开书资料必须能识别并补写，否则幂等跳过会
    让空壳永久留在生产书里。
    """
    try:
        if not path.is_file():
            return "missing"
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:  # noqa: BLE001 — 体检失败按 missing 处理，不抛
        return "missing"
    if not text:
        return "thin"
    # 去掉 markdown 标记后再量长度，避免「只有标题+一行标签」被长标题撑过阈值
    body = re.sub(r"^#{1,6}\s*", "", text, flags=re.M).strip()
    if len(body.encode("utf-8")) < _FOUNDATION_MIN_BYTES:
        return "thin"
    return "ok"


def _foundation_card_paths(root: Path, bible: dict) -> list[Path]:
    """主角卡候选路径：bootstrap 命名 <名>.md；存量命名 角色卡_<名>.md。"""
    prota = bible.get("protagonist")
    raw = ""
    if isinstance(prota, dict):
        raw = str(prota.get("name") or "").strip()
    elif isinstance(prota, str):
        raw = prota.strip()
    if not raw:
        return []
    m = re.match(r"^([\u4e00-\u9fa5·A-Za-z]{1,12})", raw)
    name = m.group(1) if m else ""
    if not name:
        return []
    card_dir = root / "设定" / "角色"
    return [card_dir / f"{name}.md", card_dir / f"角色卡_{name}.md"]


def _foundation_world_paths(root: Path) -> list[Path]:
    """世界观候选路径：bootstrap 命名 设定/世界观/世界观.md；存量书用分文件
    （背景设定.md / 力量体系.md 等）——只要该目录下任一文件实质存在即算「有世界观」。

    依据：生产链 nww_packet_builder_v2._world_setting_files 按 设定/ 目录动态发现，
    并不要求单文件名为 世界观.md；只认单文件名会把存量书误判为「完全没设定」。
    """
    world_dir = root / "设定" / "世界观"
    cands = [root / _FOUNDATION_WORLD_REL]
    if world_dir.is_dir():
        for p in sorted(world_dir.glob("*.md")):
            if p not in cands:
                cands.append(p)
    return cands


def _foundation_volume_paths(root: Path) -> list[Path]:
    """卷纲候选路径：bootstrap 命名 大纲/卷纲_第1卷.md；存量命名 卷纲_第一卷.md
    （中文数字卷号）也认——生产链 _current_volume_no 的 glob 只匹配「卷纲_第*卷.md」，
    故中文数字卷号属于已知不识别形态，但文件本身仍是有效卷纲资产。"""
    cands = [root / _FOUNDATION_VOLUME_REL]
    outline = root / "大纲"
    if outline.is_dir():
        for p in sorted(outline.glob("卷纲_*.md")):
            if p not in cands:
                cands.append(p)
    return cands


def _best_status(cands: list[Path]) -> tuple[str, Path | None]:
    """多候选资产的状态：取最「厚」的一份（ok > thin > missing）。"""
    best = "missing"
    best_path: Path | None = None
    for c in cands:
        st = _asset_body_status(c)
        if st == "ok":
            return "ok", c
        if st == "thin" and best != "ok":
            best, best_path = "thin", c
    return best, best_path


def check_foundation_assets(book: Path) -> dict:
    """建书资产体检：返回 {ok, code, summary, bible_present, bootstrap_available,
    missing_for_bootstrap, assets}。

    code 语义：
      None              → 资产齐备
      MISSING_BIBLE     → 缺 bible.json（致命，bootstrap 不可用）
      MISSING_FOUNDATION→ 世界观/卷纲/主角卡 缺失或过薄（bootstrap 可补）
    """
    root = Path(book)
    bible_path = root / "bible.json"
    bible = {}
    bible_present = False
    try:
        if bible_path.is_file():
            raw = bible_path.read_text(encoding="utf-8", errors="ignore").strip()
            if raw:
                import json as _json
                parsed = _json.loads(raw)
                if isinstance(parsed, dict):
                    bible = parsed
                    bible_present = True
    except Exception:  # noqa: BLE001 — 坏 bible 等同缺失，不抛
        bible_present = False

    world_cands = _foundation_world_paths(root)
    volume_cands = _foundation_volume_paths(root)
    card_paths = _foundation_card_paths(root, bible)

    world_status, world_hit = _best_status(world_cands)
    volume_status, volume_hit = _best_status(volume_cands)
    # 主角卡：任一候选路径存在即视为「在」（存量/新命名兼容），状态取最厚的一份
    card_status = "missing"
    card_hit: Path | None = None
    for cp in card_paths:
        st = _asset_body_status(cp)
        if st == "ok":
            card_status, card_hit = "ok", cp
            break
        if st == "thin" and card_status != "ok":
            card_status, card_hit = "thin", cp
    if not card_paths:
        card_status = "missing"

    assets = {
        "bible": {"path": "bible.json", "status": "ok" if bible_present else "missing"},
        "world": {
            "path": _FOUNDATION_WORLD_REL,
            "status": world_status,
            "hit": str(world_hit.relative_to(root)) if world_hit else None,
        },
        "volume": {
            "path": _FOUNDATION_VOLUME_REL,
            "status": volume_status,
            "hit": str(volume_hit.relative_to(root)) if volume_hit else None,
        },
        "character_cards": {
            "path": "设定/角色/<主角名>.md",
            "status": card_status,
            "hit": str(card_hit.relative_to(root)) if card_hit else None,
            "candidates": [str(p.relative_to(root)) for p in card_paths],
        },
    }

    # —— 建书资产完整度（2026-10-30 新增）——
    # 三件核心（世界观/卷纲/主角卡）齐了 ≠ 书建好了。
    # 用户报障原文：「第一次性建书，世界设定文件不完整，就像搭建了一个屋顶漏雨的临时房」
    #   —— 屋顶 = 世界观文件在；漏雨 = 势力无卡、商品无档、文风缺失、配角无卡。
    # 对照 setup/SKILL.md Step 1/1.2/1.6 的建书标配，缺这些就是「临时房」。
    # 这里只做**报告**（不阻断），因为老书存量形态不一，强行阻断会误伤。
    extras: dict[str, dict] = {}
    for key, rel in _FOUNDATION_EXTRA_ASSETS.items():
        cands = [root / rel]
        folder = (root / rel).parent
        if folder.is_dir():
            for p in sorted(folder.glob("*.md")):
                if p not in cands:
                    cands.append(p)
        st, hit = _best_status(cands)
        extras[key] = {
            "path": rel,
            "status": st,
            "hit": str(hit.relative_to(root)) if hit else None,
        }
    assets["extras"] = extras
    missing_extras = [k for k, v in extras.items() if v["status"] != "ok"]

    missing: list[str] = []
    if world_status != "ok":
        missing.append(_FOUNDATION_WORLD_REL)
    if volume_status != "ok":
        missing.append(_FOUNDATION_VOLUME_REL)
    if card_status != "ok":
        rel = (str(card_paths[0].relative_to(root)) if card_paths
               else "设定/角色/<主角名>.md")
        missing.append(rel)

    if not bible_present:
        return {
            "ok": False,
            "code": "MISSING_BIBLE",
            "summary": ("缺少 bible.json（本书基础设定档案）：系统不会替你发明设定，"
                        "请先由作者补齐 bible.json 后再补全开书资料。"),
            "bible_present": False,
            "bootstrap_available": False,
            "missing_for_bootstrap": missing,
            "missing_extras": missing_extras,
            "assets": assets,
        }

    if missing:
        thin_bits = []
        if world_status == "thin":
            thin_bits.append(f"{_FOUNDATION_WORLD_REL}（空壳）")
        if volume_status == "thin":
            thin_bits.append(f"{_FOUNDATION_VOLUME_REL}（空壳）")
        if card_status == "thin":
            thin_bits.append("主角卡（空壳）")
        detail = "、".join(thin_bits) if thin_bits else "、".join(missing)
        return {
            "ok": False,
            "code": "MISSING_FOUNDATION",
            "summary": (f"开书资料缺失或过薄：{detail}。"
                        "可点「补全开书资料」由 bootstrap 补齐（幂等，只补缺失/过薄项）。"),
            "bible_present": True,
            "bootstrap_available": True,
            "missing_for_bootstrap": missing,
            "missing_extras": missing_extras,
            "assets": assets,
        }

    # 三件核心齐备。补充资产缺失**不阻断**（老书存量形态不一），
    # 但必须在摘要里点名——「屋顶已盖但还漏雨」，作者需要知道缺什么。
    extra_hint = ""
    if missing_extras:
        names = {"faction_cards": "势力卡", "item_cards": "商品/道具档案",
                 "style": "文风档案"}
        extra_hint = ("（补充资产缺失：" +
                      "、".join(names.get(k, k) for k in missing_extras) +
                      " —— 可重新生成补齐）")
    return {
        "ok": True,
        "code": None,
        "summary": f"开书资料齐备。{extra_hint}",
        "bible_present": True,
        "bootstrap_available": True,
        "missing_for_bootstrap": [],
        "missing_extras": missing_extras,
        "assets": assets,
    }


@router.get("/books/{book_id}/foundation")
def api_foundation(book_id: str, user=Depends(get_current_user)) -> dict:
    """建书资产体检（只读）：返回 check_foundation_assets 结果。"""
    root = book_root(_user_workspace(user), book_id)
    return check_foundation_assets(root)


def _slugify(text: str) -> str:
    """极简 slug：去空白，保留中日韩与字母数字，截断 64 字符。"""
    s = re.sub(r"\s+", "", text)
    s = re.sub(r'[\\/:*?"<>|]', "_", s)
    return s[:64]


@router.get("/books/{book_id}/chapters")
def api_chapters(book_id: str, user=Depends(get_current_user)) -> dict:
    root = book_root(_user_workspace(user), book_id)
    # 附带权威游标（2026-11-08）：前端据此区分「已提交章」/「未提交章」，对验收提交按钮做
    # 提示与引导，避免作者误以为已提交章的手动修改会被验收提交重新落账。
    tracking = books.load_json(books.tracking_path(root))
    last = tracking.get("last_committed_chapter") if isinstance(tracking, dict) else None
    return {"book_id": book_id, "chapters": books.list_chapters(root),
            "last_committed_chapter": last}


@router.get("/books/{book_id}/tree")
def api_tree(book_id: str, user=Depends(get_current_user)) -> dict:
    root = book_root(_user_workspace(user), book_id)
    return {"book_id": book_id, "tree": books.build_tree(root)}


@router.get("/books/{book_id}/tracking")
def api_tracking(book_id: str, user=Depends(get_current_user)) -> dict:
    root = book_root(_user_workspace(user), book_id)
    return {"book_id": book_id, **books.get_tracking(root)}


@router.get("/books/{book_id}/file")
def api_read_file(book_id: str, path: str = Query(..., min_length=1), user=Depends(get_current_user)) -> dict:
    root = book_root(_user_workspace(user), book_id)
    try:
        target, data = read_file(root, path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        content = None  # 二进制文件不支持编辑
    payload = {
        "book_id": book_id,
        "path": path,
        "name": target.name,
        "writable": is_writable(root, target),
        "bytes": len(data),
        "content": content,
    }
    # V13 第二阶段（2026-11-13）：事实卡常驻章节详情。与
    # /Users/oodpoo/NWW_V13/src/novel_app/main.py:279-296 同一纪律——只读附加字段，
    # 任何异常都静默省略，绝不让章节详情接口报错，也绝不让闸反过来拦住阅读。
    # 事实卡只陈述事实（禁 ok/block/score/pass 等判决字段），裁决状态单独挂，
    # 两者都来自同一个策略层，不在这里复制路径/格式规则。
    try:
        chapter = books.chapter_number_from_name(target.name)
        if chapter and isinstance(content, str):
            from . import gate_facts
            payload["gate_report"] = gate_facts.report(root, chapter)
            payload["gate_adjudication"] = gate_facts.decision(root, chapter)
    except Exception:  # noqa: BLE001 — fail-open
        payload.pop("gate_report", None)
        payload.pop("gate_adjudication", None)
    return payload


def _is_chapter_body_path(rel: str) -> bool:
    """正文路径判定（契约 C-ATOMIC-WRITE@1）：归一化分隔符后以 `正文/` 开头。"""
    norm = str(rel).replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm.startswith("正文/")


def _atomic_write_text(target: Path, content: str) -> None:
    """正文覆写：临时文件 + `os.replace`，读者只会看到旧稿或新稿。

    临时文件名带 pid 与 uuid4，多 Worker 并发覆写同一章时各写各的临时文件，
    不会互相截断；`os.replace` 在同一目录内是原子的。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


@router.put("/books/{book_id}/file")
def api_write_file(book_id: str, payload: dict, path: str = Query(..., min_length=1),
                   source: str = Query("unknown"), user=Depends(get_current_user)) -> dict:
    root = book_root(_user_workspace(user), book_id)
    target = resolve_path(root, path)
    if not is_writable(root, target):
        raise HTTPException(status_code=403, detail=f"只读路径，禁止网页端修改：{path}")
    content = payload.get("content")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content must be a string")
    # P0 改稿规律：覆写前回读旧稿，只做旁路入队（不调模型、不抛错）。
    # source=editor 才学习；chatbox APPLY / 建书草稿不算作者口味。
    old = ""
    try:
        if target.is_file() and target.stat().st_size <= 2_000_000:
            old = target.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        old = ""
    # chatbox APPLY 常提案新子目录（如 设定/角色/主角.md），create_book 只建四区顶层目录——
    # 不建父目录会 FileNotFoundError→500（2026-10-23 角色设定失败根因）。
    # is_writable 已挡隐藏目录/追踪/只读契约，mkdir 无越权风险。
    if _is_chapter_body_path(path):
        chapter_id = books.chapter_number_from_name(target.name) or 0
        task_id = f"web-edit-{uuid.uuid4().hex[:12]}"
        handle = chapter_lock.acquire(root, chapter_id, "edit", task_id)
        if handle is None:
            holder = chapter_lock.probe(root, chapter_id) or {}
            raise HTTPException(
                status_code=409,
                detail=(f"同章操作进行中：mode={holder.get('mode') or 'unknown'} "
                        f"task_id={holder.get('task_id') or 'unknown'}"),
            )
        try:
            _atomic_write_text(target, content)
        finally:
            handle.release()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    out = {"ok": True, "book_id": book_id, "path": path, "bytes": len(content.encode("utf-8"))}
    try:
        from .style_feedback import enqueue_diff
        queued = enqueue_diff(root, path, old, content, source)
    except Exception:
        queued = False
    if path.replace("\\", "/").startswith("正文/"):
        out["style_feedback"] = {"queued": queued}
    return out


@router.post("/books/{book_id}/file/rename")
def api_rename_file(book_id: str, payload: dict, path: str = Query(..., min_length=1), user=Depends(get_current_user)) -> dict:
    """同目录内重命名单个文件（章节改标题用，不移动目录）。

    payload: {"new_name": "第001章_新标题.md"} —— 只接受纯文件名，后缀必须与原文件一致。
    """
    root = book_root(_user_workspace(user), book_id)
    new_name = payload.get("new_name")
    if not isinstance(new_name, str) or not new_name.strip():
        raise HTTPException(status_code=400, detail="new_name must be a non-empty string")
    try:
        target, new_rel = rename_file(root, path, new_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "book_id": book_id, "path": new_rel, "name": target.name}


@router.delete("/books/{book_id}")
def api_delete_book(book_id: str, user=Depends(get_current_user)) -> dict:
    """删除书：默认移入回收站 projects/.trash/，NWW_DELETE_PURGE=1 时物理删除。"""
    try:
        return books.delete_book(_user_workspace(user), book_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/books/{book_id}/file")
def api_delete_file(book_id: str, path: str = Query(..., min_length=1), user=Depends(get_current_user)) -> dict:
    """删除单篇文章/文件：默认移入回收站，NWW_DELETE_PURGE=1 时物理删除。"""
    try:
        return books.delete_file(_user_workspace(user), book_id, path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/search")
def api_search(q: str = Query("", min_length=1), user=Depends(get_current_user)) -> dict:
    hits = []
    root = _user_workspace(user)
    query = q.lower()
    for book_dir in sorted(root.iterdir()):
        if not book_dir.is_dir() or book_dir.name.startswith("."):
            continue
        for p in book_dir.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in {".md", ".json", ".yaml", ".yml", ".txt"}:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if query not in text.lower():
                continue
            rel = p.relative_to(book_dir).as_posix()
            hits.append({"book": book_dir.name, "path": rel, "name": p.name})
            if len(hits) >= 100:
                break
        if len(hits) >= 100:
            break
    return {"hits": hits}
