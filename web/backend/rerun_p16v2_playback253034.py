# -*- coding: utf-8 -*-
"""Phase 1.6 第二阶段扩样回放（外脑 2026-09-17 裁决：ch25→ch30→ch34）。

每章只做 step_consistency_check + P6-1 + P6-2 + 人工裁决材料整理，不执行 repair executor。
已有审查记录会先备份、回放后恢复（保护既有闭环产物）。"""
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/oodpoo/NWW-V12/web/backend")
from app.agents_runtime import pipeline  # noqa: E402

BOOK = Path("/Users/oodpoo/NWW-V12/projects/admin/用泡面征服修仙界")
REVIEW_DIR = BOOK / "追踪" / "一致性审查"
VOL = BOOK / "正文" / "第一卷"

SAMPLES = [
    (25, VOL / "第025章_历练队的订单.md", "中期设定增长/金手指扩展/角色状态同步"),
    (30, VOL / "第030章_焦匾.md", "F006/F005伏笔链/行李箱闭环/跨卷节点"),
    (34, VOL / "第034章_另一种活法.md", "ch36/37前置规则/温故状态/凡气defer稳定"),
]

BACKUP_DIR = Path("/tmp/p16v2_backup")
BACKUP_DIR.mkdir(exist_ok=True)
# 备份既有审查记录
for ch, _, _ in SAMPLES:
    p = REVIEW_DIR / f"第{ch}章.json"
    if p.is_file():
        shutil.copy2(p, BACKUP_DIR / f"第{ch}章.json")
        print(f"已备份 第{ch}章 既有审查记录", flush=True)

results = {}
for ch, prose, purpose in SAMPLES:
    print(f"\n===== 回放 第{ch}章 [{purpose}] =====", flush=True)
    t0 = time.time()
    res = pipeline.step_consistency_check(BOOK, ch, prose)
    print(f"[耗时 {time.time()-t0:.0f}s] ok={res.get('ok')} has_s1_s2={res.get('has_s1_s2')} "
          f"s1s2={res.get('open_s1_s2_count')} s3s4={res.get('s3_s4_count')}", flush=True)
    if not res.get("ok"):
        results[ch] = {"ok": False, "detail": res.get("detail"), "purpose": purpose,
                       "raw_report": res.get("raw_report", "")[:500]}
        print(res.get("detail"), flush=True)
        continue
    rec = json.loads((BOOK / res["record_path"]).read_text(encoding="utf-8"))
    results[ch] = {
        "ok": True, "purpose": purpose,
        "open_s1_s2": rec.get("open_s1_s2", []),
        "s3_s4": rec.get("s3_s4", []),
        "stability_overlay": rec.get("stability_overlay", []),
        "evidence_validation": rec.get("evidence_validation", {}),
        "raw_report_head": res.get("raw_report", "")[:1000],
    }
    print(f"open_s1_s2={len(results[ch]['open_s1_s2'])} s3_s4={len(results[ch]['s3_s4'])} "
          f"overlay={len(results[ch]['stability_overlay'])}", flush=True)
    ev = results[ch]["evidence_validation"]
    print(f"ev_status={ev.get('status')} gate={ev.get('evidence_gate')}", flush=True)

out = Path("/tmp/p16v2_playback_253034.json")
out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n结果已存 {out}", flush=True)

# 恢复既有审查记录（回放产物不保留，避免覆盖生产闭环态）
for ch, _, _ in SAMPLES:
    p = REVIEW_DIR / f"第{ch}章.json"
    bk = BACKUP_DIR / f"第{ch}章.json"
    if bk.is_file():
        shutil.copy2(bk, p)
        print(f"已恢复 第{ch}章 既有审查记录", flush=True)
    elif p.is_file():
        p.unlink()
print("回放产物清理完成")