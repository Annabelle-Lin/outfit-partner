# -*- coding: utf-8 -*-
"""清空衣橱脚本（直接操作本地 SQLite，不依赖后端服务）。

用法：
    # 1) 先看看有哪些用户、各有多少件衣物（只看不删）
    python scripts/clear_clothes.py

    # 2) 确认后真正删除「全部用户」的所有衣物
    python scripts/clear_clothes.py --yes

    # 3) 只删某个用户的衣物
    python scripts/clear_clothes.py --user u_xxxxxx --yes

注意：
- 只删除衣物（clothing_items），不动用户、反馈、推荐历史
- 历史方案用衣物快照展示，不受影响
- 不可撤销，务必先不带 --yes 看一眼
"""
import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import app.config as config  # noqa: E402
from app.db import SessionLocal, ClothingItem, User  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="清空衣橱（删除衣物记录）")
    ap.add_argument("--user", default=None, help="只删除指定 user_id 的衣物；不填则删全部用户")
    ap.add_argument("--yes", action="store_true", help="真正执行删除；不加则只预览")
    args = ap.parse_args()

    db_path = Path(config.DB_PATH)
    print(f"数据库：{db_path}")
    if not db_path.exists():
        print("数据库文件不存在，无需清理。")
        return

    db = SessionLocal()
    try:
        q = db.query(ClothingItem)
        if args.user:
            q = q.filter(ClothingItem.user_id == args.user)

        total = q.count()
        print(f"\n待删除衣物：{total} 件" + (f"（user_id={args.user}）" if args.user else "（全部用户）"))

        # 按用户列出明细，方便确认
        rows = db.query(ClothingItem.user_id).distinct().all()
        if rows:
            print("\n各用户衣物数量：")
            for (uid,) in rows:
                cnt = db.query(ClothingItem).filter(ClothingItem.user_id == uid).count()
                uname = ""
                u = db.query(User).filter(User.user_id == uid).first()
                if u:
                    uname = f"  ({u.nickname or u.user_id})"
                print(f"  {uid}{uname}: {cnt} 件")

        if total == 0:
            print("\n没有需要删除的衣物。")
            return

        if not args.yes:
            print("\n这是预览模式，未做任何修改。")
            print("确认无误后，追加 --yes 参数重新运行：")
            cmd = "python scripts/clear_clothes.py" + (f" --user {args.user}" if args.user else "") + " --yes"
            print(f"  {cmd}")
            return

        deleted = q.delete(synchronize_session=False)
        db.commit()
        print(f"\n已删除 {deleted} 件衣物。")
    finally:
        db.close()


if __name__ == "__main__":
    main()
