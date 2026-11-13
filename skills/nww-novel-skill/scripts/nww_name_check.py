#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nww_name_check.py — 命名质量校验（只读，不改任何文件）

独立新模块，与既有流水线零耦合：由 hook / setup / 手动调用，
按模式校验命名规则（重名 / 缺名 / 高危确定性谐音 / 正文↔bible 登记一致性 / 疑似新角色名）。

模式：
  build    建书强检查：重名 / 缺名 / 高危确定性谐音 → ERROR 阻断（exit 2）
  chapter  写章轻检查：正文↔bible 名字一致性 + 疑似新角色名（启发式）→ WARNING（exit 0，永不阻断）
  commit   发布前完整交叉检查：bible + 全部正文 + 追踪/角色状态 → WARNING/ERROR

分级：
  ERROR    确定性错误，须修复（build/commit 中 exit 2）
  WARNING  提示 / 需人工确认，不阻断（chapter 永远 exit 0）

用法：
  python nww_name_check.py --mode build --book <书根>
  python nww_name_check.py --mode chapter --book <书根> --file <章节文件>
  python nww_name_check.py --mode commit --book <书根>
  --json 时以 JSON 输出 findings（供脚本消费）。
"""
import argparse
import json
import os
import re
import sys

# ---------------------------------------------------------------------------
# 常用姓氏（疑似人名提取用；单姓 + 常见复姓，覆盖命名库高频项）
# ---------------------------------------------------------------------------
_SURNAMES = frozenset(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
    "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍"
    "虞万支柯管卢莫经房裘缪干解应宗丁宣邓郁单杭洪包诸左石崔吉龚程嵇邢"
    "滑裴陆荣翁荀羊惠甄曲封芮储靳汲邴糜松井段富巫乌焦巴弓牧隗车侯宓蓬"
    "全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄"
    "印宿白怀蒲台从鄂索咸籍赖卓蔺屠蒙池乔阴胥能苍双闻莘党翟谭贡劳逄姬"
    "申扶堵冉宰郦雍郤璩桑桂濮牛寿通边扈燕冀郏浦尚农温别庄晏柴瞿阎充慕"
    "连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东欧"
    "殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢"
    "关蒯相查后荆红游竺权逯盖益桓公"
)
_COMPOUND_SURNAMES = (
    "万俟司马上官欧阳夏侯诸葛闻人东方赫连皇甫尉迟公羊澹台公冶宗政濮阳"
    "淳于单于太叔申屠公孙仲孙轩辕令狐钟离宇文长孙慕容司徒司空南宫西门"
)
_SURNAME_SET = _SURNAMES | frozenset(
    _COMPOUND_SURNAMES[i:i + 2] for i in range(0, len(_COMPOUND_SURNAMES), 2)
)

# ---------------------------------------------------------------------------
# 高危确定性谐音（build 阻断项 3）
# 设计约束：只做「确定」匹配，宁漏不误报（避免建书体验污染）。
#   _LITERAL_RISK  字面组合（整名/子串包含即命中）
#   _PY_TABLE      拼音表（只收录高危词涉及的常用字，人工核验拼音）
#   _PY_RISK       高危拼音子串（名字全拼后做子串匹配）
# ---------------------------------------------------------------------------
_LITERAL_RISK = (
    ("史珍香", "谐音「屎真香」"),
    ("史一坨", "含不雅谐音"),
    ("王八蛋", "不雅称谓"),
    ("王八", "不雅称谓"),
    ("阴曹", "生僻/暗黑组词，非必要禁用"),
)

_PY_TABLE = {
    "史": "shi", "珍": "zhen", "香": "xiang", "一": "yi", "坨": "tuo",
    "王": "wang", "八": "ba", "蛋": "dan",
    "操": "cao", "艹": "cao", "草": "cao",
    "屎": "shi", "真": "zhen",
    "阴": "yin", "曹": "cao",
    "贱": "jian", "娘": "niang", "婊": "biao",
    "死": "si", "尸": "shi",
}

_PY_RISK = (
    ("shizhen", "谐音「屎真」"),
    ("wangba", "谐音「王八」"),
    ("cao", "谐音「操」"),
    ("jian", "谐音「贱」"),
    ("biao", "不雅"),
)

# ---------------------------------------------------------------------------
# 疑似人名提取（chapter 启发式）：称谓后缀表
# ---------------------------------------------------------------------------
_SUFFIX_TITLES = ("爷", "大人", "公子", "姑娘", "先生", "夫人", "小姐", "兄", "掌柜", "娘子", "员外")


def _iter_lines(text: str):
    yield from text.splitlines()


def _load_bible(book: str) -> dict | None:
    path = os.path.join(book, "bible.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _bible_characters(bible: dict | None) -> list[dict]:
    if not bible:
        return []
    chars = bible.get("characters")
    return [c for c in chars if isinstance(c, dict)] if isinstance(chars, list) else []


def _bible_names(bible: dict | None) -> list[str]:
    return [c.get("name", "") for c in _bible_characters(bible) if isinstance(c.get("name"), str) and c["name"].strip()]


def _name_pinyin(name: str) -> str:
    out = []
    for ch in name:
        py = _PY_TABLE.get(ch)
        if py:
            out.append(py)
    return "".join(out)


# ---------------------------------------------------------------------------
# build：建书强检查（重名 / 缺名 / 高危确定性谐音）
# ---------------------------------------------------------------------------
def check_build(bible: dict | None) -> list[dict]:
    findings: list[dict] = []
    if bible is None:
        return [{"level": "ERROR", "kind": "bible_missing", "message": "bible.json 不存在，无法执行命名 build 检查"}]

    names = _bible_names(bible)
    # 1) 重名
    seen: dict[str, int] = {}
    for n in names:
        seen[n] = seen.get(n, 0) + 1
    for n, cnt in seen.items():
        if cnt > 1:
            findings.append({"level": "ERROR", "kind": "duplicate_name",
                             "message": f"角色重名「{n}」出现 {cnt} 次"})
    # 2) 缺名
    for i, c in enumerate(_bible_characters(bible)):
        label = c.get("name") or f"characters[{i}]"
        if not (isinstance(c.get("name"), str) and c["name"].strip()):
            findings.append({"level": "ERROR", "kind": "missing_name",
                             "message": f"角色 {label} 缺必填字段 name"})
        for key in ("role", "identity"):
            if not (isinstance(c.get(key), str) and c[key].strip()):
                findings.append({"level": "WARNING", "kind": "missing_field",
                                 "message": f"角色 {label} 缺 {key}"})
    # 3) 高危确定性谐音
    for n in names:
        for lit, why in _LITERAL_RISK:
            if lit in n:
                findings.append({"level": "ERROR", "kind": "homophone",
                                 "message": f"角色名「{n}」命中高危词「{lit}」（{why}）"})
        pinyin = _name_pinyin(n)
        for risk, why in _PY_RISK:
            if risk in pinyin:
                findings.append({"level": "ERROR", "kind": "homophone",
                                 "message": f"角色名「{n}」（拼音 {pinyin}）谐音命中「{why}」"})
    return findings


# ---------------------------------------------------------------------------
# chapter：写章轻检查（正文↔bible 名字一致性 + 疑似新角色名，WARNING）
# ---------------------------------------------------------------------------
def _has_surname(names: list[str], surname: str) -> bool:
    """bible 已登记角色中是否存在姓 surname 的角色（单姓比首字，复姓比前两字）。"""
    for n in names:
        if n.startswith(surname):
            return True
    return False


def _candidate_is_bible_like(cand: str, names: list[str]) -> bool:
    """候选名与 bible 已登记名「同姓不同名」时不算新角色（视为已登记别称/确认），
    仅当与任何已登记名都无关时才判定为疑似新角色。"""
    for n in names:
        if cand == n or cand.startswith(n) or n.startswith(cand):
            return True
        # 共享前 1-2 个字符（复姓按前 2 字，单姓按前 1 字）
        if len(n) >= 2 and len(cand) >= 2:
            if cand[0] == n[0] and (len(n) > 2 and len(cand) > 2 and cand[1] == n[1]):
                return True
            if cand[0] == n[0] and len(n) == 2:
                return True
    return False


def extract_suspect_names(text: str, names: list[str]) -> list[str]:
    """启发式提取疑似人名，与 bible 比对后返回「未登记」的候选（去重、限量）。

    只保留强信号模式（低误报）：
      ① 姓氏 + 称谓后缀（沈大人 / 周爷 / 司马先生）—— n 必须在姓氏表内
      ② 老/小/阿 + 姓氏（老沈 / 小周）—— 后缀必须在姓氏表内
    普通动词/副词 + 称谓（回大人 / 给先生 / 老夫 / 小吏）自动被「非姓氏」过滤。
    与 bible 已登记名同姓/同名相关的候选（沈大人 vs 沈砚）视为已有角色别称，不报。

    不做 NER、不自动判定改名；仅提示人工确认。"""
    candidates: list[str] = []
    for line in _iter_lines(text):
        # ① 姓氏 + 称谓后缀（单姓 1 字 / 复姓 2 字）
        for m in re.finditer(r"(?P<n>[\u4e00-\u9fa5]{1,2})(?P<t>爷|大人|公子|姑娘|先生|夫人|小姐|兄|掌柜|娘子|员外)", line):
            n = m.group("n")
            if n in _SURNAME_SET:
                candidates.append(n + m.group("t"))
        # ② 前缀称谓 老/小/阿 + 姓氏（bible 已有同姓角色视为别称，不报）
        for m in re.finditer(r"(?P<t>[老小阿])(?P<n>[\u4e00-\u9fa5])", line):
            t, n = m.group("t"), m.group("n")
            if n in _SURNAME_SET and not _has_surname(names, n):
                candidates.append(t + n)
    # 去重（保持出现顺序）、过滤已登记相关、限量
    suspects: list[str] = []
    for c in candidates:
        if c in suspects:
            continue
        if _candidate_is_bible_like(c, names):
            continue
        suspects.append(c)
    return suspects[:5]


def check_chapter(bible: dict | None, filepath: str) -> list[dict]:
    findings: list[dict] = []
    if not os.path.exists(filepath):
        return [{"level": "WARNING", "kind": "file_missing",
                 "message": f"章节文件不存在：{filepath}"}]
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        return [{"level": "WARNING", "kind": "read_failed",
                 "message": f"读取章节失败：{exc}"}]

    names = _bible_names(bible)
    # 1) 疑似新角色名（启发式，需人工确认）——只报强信号（姓氏+称谓/老小阿+姓氏）
    for cand in extract_suspect_names(text, names):
        findings.append({"level": "WARNING", "kind": "suspect_name",
                         "message": f"发现疑似新角色名「{cand}」，当前 bible 未登记。"
                                    f"请确认：1) 新角色是否需要写入 bible；2) 是否为已有角色别称"})
    return findings


# ---------------------------------------------------------------------------
# commit：完整交叉检查（bible + 全部正文 + 追踪/角色状态）
# ---------------------------------------------------------------------------
def _walk_md(root: str) -> list[str]:
    out: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(".md"):
                out.append(os.path.join(dirpath, fn))
    return out


def check_commit(bible: dict | None, book: str) -> list[dict]:
    findings = check_build(bible)
    if bible is None:
        return findings

    # 全书正文汇总 chapter 检测（suspect 全局去重，只报一次避免刷屏）
    prose_dir = os.path.join(book, "正文")
    if os.path.isdir(prose_dir):
        seen_suspect: set[str] = set()
        for fp in sorted(_walk_md(prose_dir)):
            for f in check_chapter(bible, fp):
                if f.get("kind") == "suspect_name":
                    if f["message"] in seen_suspect:
                        continue
                    seen_suspect.add(f["message"])
                findings.append(f)

    # 追踪/角色状态 文件名 ↔ bible 一致性（确定性）
    state_dir = os.path.join(book, "追踪", "角色状态")
    names = _bible_names(bible)
    if os.path.isdir(state_dir):
        for fp in sorted(_walk_md(state_dir)):
            stem = os.path.splitext(os.path.basename(fp))[0]
            if stem and stem not in names:
                findings.append({"level": "WARNING", "kind": "state_not_in_bible",
                                 "message": f"追踪/角色状态存在文件「{stem}」但 bible 未登记对应角色"})
    for n in names:
        sheet = os.path.join(state_dir, f"{n}.md") if state_dir else None
        if sheet and not os.path.exists(sheet):
            findings.append({"level": "WARNING", "kind": "bible_no_state",
                             "message": f"bible 登记角色「{n}」无对应 追踪/角色状态/{n}.md"})
    return findings


# ---------------------------------------------------------------------------
# 输出与退出码
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", required=True, choices=("build", "chapter", "commit"))
    ap.add_argument("--book", required=True, help="书项目根（含 bible.json）")
    ap.add_argument("--file", help="chapter 模式：章节文件路径")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出 findings")
    args = ap.parse_args()

    bible = _load_bible(args.book)

    if args.mode == "build":
        findings = check_build(bible)
    elif args.mode == "chapter":
        if not args.file:
            print("ERROR: chapter 模式需要 --file", file=sys.stderr)
            return 2
        findings = check_chapter(bible, args.file)
    else:
        findings = check_commit(bible, args.book)

    errors = [x for x in findings if x["level"] == "ERROR"]
    warnings = [x for x in findings if x["level"] == "WARNING"]

    if args.json:
        print(json.dumps({"mode": args.mode, "errors": len(errors),
                          "warnings": len(warnings), "findings": findings},
                         ensure_ascii=False, indent=2))
    else:
        for x in findings:
            print(f"[{x['level']}] {x['message']}")
        if not findings:
            print("OK: 命名检查通过")
        else:
            print(f"共 {len(errors)} ERROR / {len(warnings)} WARNING")

    # build/commit：有 ERROR 阻断；chapter：永不阻断
    if args.mode in ("build", "commit"):
        return 2 if errors else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
