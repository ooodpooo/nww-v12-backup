"""registry.py — 扫描 skills/nww-novel-skill/agents/*.md，注册各环节独立 agent。

每个 agent 从 .md frontmatter 读取：name / description / model / tools / disallowedTools，
并读取正文（方法论）作为 system prompt 底座。模型档位映射（本地+云端混合）由
pipeline 按 agent 分类决定，不在此处写死。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_AGENTS_DIR = _REPO_ROOT / "skills" / "nww-novel-skill" / "agents"

# consistency-checker.md 等文件头以 `***` 分隔，frontmatter 不在第一行——容忍开头分隔行
_FRONTMATTER_RE = re.compile(r"^(?:\*\*\*\s*\n)?---\n(.*?)\n---\n?", re.S)
_FIELD_RE = re.compile(r"^([a-zA-Z_]+):\s*(.*)$")

# agent 分类 → 模型路由档位（本地/云端混合）
# write 类走本地大模型（直连 omlx）；analyze 类走云端（DeepSeek，未配置回退本地小模型）
LOCAL_WRITE = "local-write"   # 本地 27B
LOCAL_SMALL = "local-small"   # 本地 9B
CLOUD = "cloud"               # 云端 DeepSeek


@dataclass
class AgentDef:
    name: str
    description: str = ""
    model: str = ""
    tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    method: str = ""          # .md 正文（方法论，剥掉 frontmatter）
    path: Path = None         # noqa: F821（实际赋值）

    @property
    def routing(self) -> str:
        """按 agent 职责分类 → 路由档位。"""
        if self.name in {"narrative-writer", "nww-character-designer"}:
            return LOCAL_WRITE
        # 独立审稿（pipeline 作 delta 语义提取/审稿打分/审稿修订的 system 底座）：
        # 与主笔同池本地 27B（保证 delta JSON 契约与打分质量不回退 9B），但人格/方法论独立。
        # 若要「模型级」独立 → 把 nww-reviewer 移到 CLOUD 集合即可（一行）。
        if self.name == "nww-reviewer":
            return LOCAL_WRITE
        if self.name in {"consistency-checker", "nww-researcher", "nww-architect", "nww-chapter-extractor", "nww-explorer"}:
            return CLOUD
        return LOCAL_SMALL


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 .md frontmatter，返回 (字段 dict, 正文)。解析失败 → 全空。

    支持 YAML 块标量（`description: |` 多行缩进）——agent .md 的 description
    大多用 `|` 写法，单行键值会解析成孤立的 "|"。
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        # 兜底：无 `---` 围栏的文件（consistency-checker.md 等手写 YAML 段，
        # 文件头以 *** 分隔）：按 frontmatter 特征 name: ↔ model: 切片再走有围栏解析。
        if text.startswith(("*" * 3)):
            body = text.splitlines()
            start = next((k for k, ln in enumerate(body) if re.match(r"^name\s*:", ln.strip())), None)
            end = next(
                (k for k, ln in enumerate(body) if start is not None and k > start and re.match(r"^model\s*:", ln.strip())),
                None,
            )
            if start is not None and end is not None:
                fields, _ = _parse_frontmatter("---\n" + "\n".join(body[start : end + 1]) + "\n---\n")
        return fields, text.strip()
    fields: dict = {}
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        fm = _FIELD_RE.match(lines[i].strip())
        if not fm:
            i += 1
            continue
        key, val = fm.group(1), fm.group(2).strip()
        if val in {"|", ">-", ">", "|-"}:  # YAML 块标量：后续行都属于该字段，直到下一个 ASCII 键值行
            block: list[str] = []
            i += 1
            while i < len(lines) and not _FIELD_RE.match(lines[i].strip()):
                block.append(lines[i].strip())
                i += 1
            fields[key] = "\n".join(block).strip()
            continue
        if val.startswith("[") and val.endswith("]"):
            fields[key] = [x.strip() for x in val[1:-1].split(",") if x.strip()]
        else:
            fields[key] = val
        i += 1
    return fields, text[m.end():].strip()


def _load_agents() -> dict[str, AgentDef]:
    """从 skill 包扫描全部 agent .md，返回 name → AgentDef。"""
    agents: dict[str, AgentDef] = {}
    if not _AGENTS_DIR.is_dir():
        return agents
    for md in sorted(_AGENTS_DIR.glob("*.md")):
        fields, method = _parse_frontmatter(md.read_text(encoding="utf-8"))
        name = fields.get("name", md.stem)
        agents[name] = AgentDef(
            name=name,
            description=fields.get("description", ""),
            model=fields.get("model", ""),
            tools=fields.get("tools", []),
            disallowed_tools=fields.get("disallowedTools", []),
            method=method,
            path=md,
        )
    return agents


def get_agent(name: str) -> AgentDef:
    """按名字取 agent；未注册 → ValueError（由调用方转 404）。"""
    agents = _load_agents()
    if name not in agents:
        raise ValueError(f"agent not found: {name}（skill 包 agents/ 下无此定义）")
    return agents[name]


# agent → 职能环节分组（模型中心按环节分组展示；STAGE_ORDER 即 UI 展示顺序）。
# 说明：分组依据是「该 agent 在 web 生产链里实际负责的环节」，不是 agent 自述的人设。
# 「未接入」= 在 web 生产链无调用点（或路由键为死键），改这里选了也不生效。
STAGE_ORDER = ("创意与规划", "写作与出稿", "审查与修订", "素材与分析", "未接入")

_STAGE_BY_AGENT = {
    "nww-architect": "创意与规划",
    "nww-character-designer": "创意与规划",
    "narrative-writer": "写作与出稿",
    "nww-reviewer": "审查与修订",
    "consistency-checker": "审查与修订",
    "nww-researcher": "素材与分析",
    "nww-chapter-extractor": "素材与分析",
    "nww-explorer": "素材与分析",
    "nww-contract-extractor": "素材与分析",  # 2026-10-31：L1/L2 合同提取专用路由（重写链 analyze 环节）
    # 2026-11-01 改名（原 contract-extractor）：与 agent_routes / model_config 的键统一，
    #   否则「模型中心里配的路由」永远不命中（实测 agent_routes 只有 nww-contract-extractor）。
    "narrative-writer-v2": "未接入",
}


# ── 接线事实扫描（2026-10-30）────────────────────────────────────────────
# 「这个 agent 在生产链里到底会不会被调」不能靠手写清单（必然漂移）——
# 事实源是代码本身：写章主链里的 get_agent("x") 调用 + 拆书执行契约的 executor.agent。
# 扫描结果供模型中心区分「可配」与「配了也不生效」，避免用户改错对象还蒙在鼓里。
_RUNTIME_DIR = Path(__file__).resolve().parent
_CONTRACT_DIR = _REPO_ROOT / "skills" / "nww-novel-skill" / "execution-contract"
_WIRING_CACHE: dict = {"key": None, "value": {}}
_CALL_RE = re.compile(r"get_agent\(\s*[\"']([A-Za-z0-9_-]+)[\"']")
# 2026-11-01：`_writer_stream(agent_name="x")` 也是**生产链真实调用点**（合同提取专用路由走这条），
#   此前只扫 get_agent() → 该 agent 在模型中心被误标「未接入（配了不生效）」，与事实相反。
_AGENT_NAME_RE = re.compile(r"agent_name\s*=\s*[\"']([A-Za-z0-9_-]+)[\"']")
_CONTRACT_RE = re.compile(r"\"agent\"\s*:\s*\"([A-Za-z0-9_-]+)\"")


def _wiring_files() -> tuple[list[Path], list[Path]]:
    return sorted(_RUNTIME_DIR.glob("*.py")), sorted(_CONTRACT_DIR.glob("*.json"))


def scan_wiring() -> dict[str, dict]:
    """扫代码得出 agent 接线事实：{name: {"level": "main"|"contract"|"none", "refs": [...]}}。

    level=main     写章主链（agents_runtime/*.py）里真的 get_agent 调用 → 配了必生效
    level=contract 拆书执行契约 executor.agent（需 NWW_SKILL_EXEC_ENABLED=1，默认关）
    level=none     生产链无任何调用点 → 配了也不会被用到

    按文件 mtime 缓存：代码不变则不重复扫盘。
    """
    pys, jsons = _wiring_files()
    key = (max((p.stat().st_mtime for p in pys + jsons), default=0.0), len(pys), len(jsons))
    if _WIRING_CACHE["key"] == key:
        return _WIRING_CACHE["value"]
    main_refs: dict[str, list[str]] = {}
    for py in pys:
        if py.name == "registry.py":
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in _CALL_RE.finditer(text):
            name = m.group(1)
            line = text.count("\n", 0, m.start()) + 1
            main_refs.setdefault(name, []).append(f"{py.name}:{line}")
        for m in _AGENT_NAME_RE.finditer(text):
            name = m.group(1)
            line = text.count("\n", 0, m.start()) + 1
            main_refs.setdefault(name, []).append(f"{py.name}:{line}(agent_name)")
    contract_refs: dict[str, list[str]] = {}
    for js in jsons:
        try:
            text = js.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in _CONTRACT_RE.finditer(text):
            name = m.group(1)
            line = text.count("\n", 0, m.start()) + 1
            contract_refs.setdefault(name, []).append(f"{js.name}:{line}")
    out: dict[str, dict] = {}
    for name in set(main_refs) | set(contract_refs):
        if name in main_refs:
            out[name] = {"level": "main", "refs": main_refs[name]}
        else:
            out[name] = {"level": "contract", "refs": contract_refs[name]}
    _WIRING_CACHE["key"] = key
    _WIRING_CACHE["value"] = out
    return out


def list_agents() -> list[dict]:
    """返回注册表（供诊断/前端展示 agent 分工）。"""
    wiring = scan_wiring()
    return [
        {
            "name": a.name,
            "model": a.model,
            "routing": a.routing,
            "tools": a.tools,
            "disallowed_tools": a.disallowed_tools,
            "description": a.description[:100],
            "stage": _STAGE_BY_AGENT.get(a.name, "其它"),
            "wired": wiring.get(a.name, {}).get("level", "none"),
            "wired_refs": wiring.get(a.name, {}).get("refs", []),
        }
        for a in sorted(_load_agents().values(), key=lambda a: a.name)
    ]