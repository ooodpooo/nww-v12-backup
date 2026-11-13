#!/usr/bin/env python3
"""nww-publish 命令行入口。"""
import sys
from nww_publish.cli import main

if __name__ == "__main__":
    main(sys.argv[1:])
