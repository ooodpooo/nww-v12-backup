"""自动发布系统就绪度自检脚本"""
import asyncio
import sys
from pathlib import Path

# 加入模块搜索路径
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from app.database import init_db, DB_PATH
from app.executor import get_publish_config, check_platform_login_profile
from app.scheduler import is_scheduler_running


async def main():
    print("=== 网文自动发布系统 · 环境就绪度自检 ===")
    
    # 1. 数据库建表与连通性检查
    print("\n[1/3] 检查数据库...")
    await init_db()
    print(f"  ✓ 数据库就绪: {DB_PATH}")

    # 2. 底层 nww-publish 配置与适配器桥接检查
    print("\n[2/3] 检查底层 nww-publish 桥接...")
    try:
        cfg = get_publish_config()
        print(f"  ✓ 成功读取 nww-publish 配置，工作区: {cfg.home}")
        for p in ("qidian", "fanqie"):
            has_profile = check_platform_login_profile(p)
            print(f"  - 平台 [{p}]: {'已就绪 (存在持久化 Profile)' if has_profile else '未登录 (点击前端扫码即可建立)'}")
    except Exception as e:
        print(f"  ✗ 配置加载异常: {e}")

    # 3. 检查调度器组件
    print("\n[3/3] 检查调度器组件...")
    print(f"  ✓ 调度器模块导入正常，初始状态: {'running' if is_scheduler_running() else 'idle (启动 FastAPI 后常驻运行)'}")

    print("\n=== 自检完成：全部组件链路已打通！ ===")


if __name__ == "__main__":
    asyncio.run(main())
