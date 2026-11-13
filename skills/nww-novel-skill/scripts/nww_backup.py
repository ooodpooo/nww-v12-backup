#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nww_backup.py — 写前原稿自动备份（借鉴 sumeru 修改前备份机制）。

在任何确定性改写（nww_enforce / deslop 落盘）前，把目标正文文件的首份
原稿备份到 {BOOK}/.backup/{basename}.orig.md。幂等：首份优先，已有不覆盖。
用法：python nww_backup.py --book <书根> --file <正文.md>
"""
import argparse
import json
import os
import sys


def backup(book, target_file, orig_content=None):
    target = os.path.abspath(target_file)
    backup_dir = os.path.join(book, ".backup")
    os.makedirs(backup_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(target))[0]
    dst = os.path.join(backup_dir, base + ".orig.md")
    if os.path.exists(dst):
        return {"status": "exists", "path": dst}
    if orig_content is None:
        with open(target, "r", encoding="utf-8") as f:
            orig_content = f.read()
    with open(dst, "w", encoding="utf-8") as f:
        f.write(orig_content)
    os.chmod(dst, 0o444)  # 只读防误改
    return {"status": "created", "path": dst}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True, help="书项目根（生成 .backup/）")
    ap.add_argument("--file", required=True, help="待备份正文 md 路径")
    args = ap.parse_args()
    if not os.path.isfile(args.file):
        print(json.dumps({"status": "no_file", "file": args.file}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps(backup(args.book, args.file), ensure_ascii=False))


if __name__ == "__main__":
    main()
