"""
IHG 价格监控 - SQLite 数据库封装

表结构:
    hotels  - 酒店基本信息 (从 hotel_list_fetcher 写入)
    prices  - 每日价格记录 (从 batch_monitor 写入, 保留历史)

用法:
    from ihg_db import IHGDatabase

    db = IHGDatabase()  # 默认 ./ihg_data/ihg_prices.db
    db.upsert_hotel({...})
    db.save_prices("HPHHL", prices_list, "2026-05-15")
    baseline = db.load_latest_prices("HPHHL")
"""

import sqlite3
from datetime import date
from pathlib import Path


# 默认数据库路径
DEFAULT_DB_PATH = "./ihg_data/ihg_prices.db"


class IHGDatabase:
    """IHG 价格监控数据库"""

    def __init__(self, db_path=DEFAULT_DB_PATH):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row  # 返回字典式访问
        self.conn.execute("PRAGMA journal_mode=WAL")  # 提升并发写入性能
        self._create_tables()

    def _create_tables(self):
        """创建表 (如果不存在) + 自动 migration"""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS hotels (
                mnemonic     TEXT PRIMARY KEY,
                name         TEXT,
                brand_code   TEXT,
                city         TEXT,
                country      TEXT,
                address      TEXT,
                rating       REAL,
                review_count INTEGER,
                url          TEXT,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS prices (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                hotel_code           TEXT NOT NULL,
                date                 TEXT NOT NULL,
                cash_price           REAL,
                cash_price_after_tax REAL,
                cash_price_usd       REAL,
                currency             TEXT,
                points               INTEGER,
                cpp                  REAL,
                fetch_date           TEXT NOT NULL,
                UNIQUE(hotel_code, date, fetch_date)
            );

            CREATE INDEX IF NOT EXISTS idx_prices_hotel_date
                ON prices(hotel_code, date);
            CREATE INDEX IF NOT EXISTS idx_prices_fetch_date
                ON prices(fetch_date);
        """)
        # Migration: 老库可能没有 cash_price_usd 字段, 加上
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(prices)").fetchall()}
        if "cash_price_usd" not in cols:
            self.conn.execute("ALTER TABLE prices ADD COLUMN cash_price_usd REAL")
        self.conn.commit()

    # ============ 酒店操作 ============

    def upsert_hotel(self, hotel):
        """插入或更新酒店信息"""
        now = date.today().isoformat()
        self.conn.execute("""
            INSERT INTO hotels (mnemonic, name, brand_code, city, country, address, rating, review_count, url, created_at, updated_at)
            VALUES (:mnemonic, :name, :brand_code, :city, :country, :address, :rating, :review_count, :url, :now, :now)
            ON CONFLICT(mnemonic) DO UPDATE SET
                name = excluded.name,
                brand_code = excluded.brand_code,
                city = excluded.city,
                country = excluded.country,
                address = excluded.address,
                rating = excluded.rating,
                review_count = excluded.review_count,
                url = excluded.url,
                updated_at = excluded.updated_at
        """, {**hotel, "now": now})

    def upsert_hotels(self, hotels):
        """批量插入或更新酒店"""
        for h in hotels:
            self.upsert_hotel(h)
        self.conn.commit()

    def get_hotel(self, mnemonic):
        """获取单个酒店信息"""
        row = self.conn.execute(
            "SELECT * FROM hotels WHERE mnemonic = ?", (mnemonic,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_hotels(self, country=None):
        """获取所有酒店 (可按国家筛选)"""
        if country:
            rows = self.conn.execute(
                "SELECT * FROM hotels WHERE country = ? ORDER BY rating DESC", (country,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM hotels ORDER BY country, rating DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_hotel_codes(self, country=None):
        """获取酒店代码列表 (用于批量监控)"""
        hotels = self.get_all_hotels(country)
        return [h["mnemonic"] for h in hotels]

    def get_hotel_count(self):
        """获取酒店总数"""
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM hotels").fetchone()
        return row["cnt"]

    # ============ 价格操作 ============

    def save_prices(self, hotel_code, prices, fetch_date=None):
        """保存价格数据 (批量插入, 忽略重复)
        注: cpp 字段含义为 USD 美分/积分 (新版), 旧版数据可能是本地币*100/积分, 统计时需注意
        """
        if not fetch_date:
            fetch_date = date.today().isoformat()

        rows = []
        for p in prices:
            rows.append((
                hotel_code,
                p.get("date"),
                p.get("cash_price"),
                p.get("cash_price_after_tax"),
                p.get("cash_price_usd"),
                p.get("currency"),
                p.get("points"),
                p.get("cpp"),
                fetch_date,
            ))

        self.conn.executemany("""
            INSERT OR IGNORE INTO prices
                (hotel_code, date, cash_price, cash_price_after_tax, cash_price_usd, currency, points, cpp, fetch_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        self.conn.commit()

    def load_latest_prices(self, hotel_code):
        """加载最近一次采集的价格 (作为基线对比)"""
        # 先找到最近的 fetch_date
        row = self.conn.execute("""
            SELECT fetch_date FROM prices
            WHERE hotel_code = ?
            ORDER BY fetch_date DESC
            LIMIT 1
        """, (hotel_code,)).fetchone()

        if not row:
            return [], None

        fetch_date = row["fetch_date"]

        # 取该次采集的所有价格
        rows = self.conn.execute("""
            SELECT date, cash_price, cash_price_after_tax, cash_price_usd,
                   currency, points, cpp
            FROM prices
            WHERE hotel_code = ? AND fetch_date = ?
            ORDER BY date
        """, (hotel_code, fetch_date)).fetchall()

        prices = [dict(r) for r in rows]
        return prices, fetch_date

    def get_price_history(self, hotel_code, target_date):
        """获取某酒店某天的历史价格变化"""
        rows = self.conn.execute("""
            SELECT fetch_date, cash_price, cash_price_after_tax, cash_price_usd,
                   currency, points, cpp
            FROM prices
            WHERE hotel_code = ? AND date = ?
            ORDER BY fetch_date
        """, (hotel_code, target_date)).fetchall()
        return [dict(r) for r in rows]

    def get_fetch_dates(self, hotel_code):
        """获取某酒店的所有采集日期"""
        rows = self.conn.execute("""
            SELECT DISTINCT fetch_date FROM prices
            WHERE hotel_code = ?
            ORDER BY fetch_date DESC
        """, (hotel_code,)).fetchall()
        return [r["fetch_date"] for r in rows]

    def get_all_cash_prices(self, hotel_code):
        """获取该酒店所有历史快照中的非空现金价 (优先含税)
        Returns: [(date, value), ...] 同一日期可能多条 (来自不同 fetch_date)
        """
        rows = self.conn.execute("""
            SELECT date, cash_price_after_tax, cash_price
            FROM prices
            WHERE hotel_code = ?
              AND (cash_price_after_tax IS NOT NULL OR cash_price IS NOT NULL)
        """, (hotel_code,)).fetchall()
        return [(r["date"], r["cash_price_after_tax"] or r["cash_price"]) for r in rows]

    def get_all_points_prices(self, hotel_code):
        """获取该酒店所有历史快照中的非空积分价
        Returns: [(date, value), ...]
        """
        rows = self.conn.execute("""
            SELECT date, points
            FROM prices
            WHERE hotel_code = ? AND points IS NOT NULL
        """, (hotel_code,)).fetchall()
        return [(r["date"], r["points"]) for r in rows]

    def get_stats(self):
        """获取数据库统计信息"""
        hotel_count = self.conn.execute("SELECT COUNT(*) as cnt FROM hotels").fetchone()["cnt"]
        price_count = self.conn.execute("SELECT COUNT(*) as cnt FROM prices").fetchone()["cnt"]
        fetch_dates = self.conn.execute(
            "SELECT COUNT(DISTINCT fetch_date) as cnt FROM prices"
        ).fetchone()["cnt"]
        hotels_with_prices = self.conn.execute(
            "SELECT COUNT(DISTINCT hotel_code) as cnt FROM prices"
        ).fetchone()["cnt"]

        return {
            "hotel_count": hotel_count,
            "price_records": price_count,
            "fetch_dates": fetch_dates,
            "hotels_with_prices": hotels_with_prices,
        }

    # ============ 清理 ============

    def close(self):
        """关闭数据库连接"""
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ============ 命令行工具: 查看数据库状态 ============

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="IHG 数据库工具")
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH, help="数据库路径")
    parser.add_argument("--stats", action="store_true", help="显示统计信息")
    parser.add_argument("--hotels", action="store_true", help="列出所有酒店")
    parser.add_argument("--country", type=str, default=None, help="按国家筛选")
    parser.add_argument("--history", type=str, default=None,
                        help="查看价格历史 (格式: HOTEL_CODE:DATE, 如 HPHHL:2026-08-01)")
    args = parser.parse_args()

    db = IHGDatabase(args.db)

    if args.stats:
        stats = db.get_stats()
        print(f"数据库: {args.db}")
        print(f"  酒店数: {stats['hotel_count']}")
        print(f"  价格记录: {stats['price_records']}")
        print(f"  采集次数: {stats['fetch_dates']}")
        print(f"  有价格的酒店: {stats['hotels_with_prices']}")

    elif args.hotels:
        hotels = db.get_all_hotels(args.country)
        print(f"酒店列表 ({len(hotels)} 个):")
        for h in hotels:
            print(f"  {h['mnemonic']:8s} {h['name'] or '':40s} {h['country'] or '':15s} {h['rating'] or 0:.1f}")

    elif args.history:
        parts = args.history.split(":")
        if len(parts) == 2:
            code, target_date = parts
            history = db.get_price_history(code.upper(), target_date)
            if history:
                print(f"{code.upper()} {target_date} 价格历史:")
                print(f"  {'采集日期':12s} | {'含税价':>8s} | {'积分':>8s} | {'CPP':>6s}")
                print(f"  {'-'*50}")
                for h in history:
                    tax = f"{h['cash_price_after_tax']:.0f}" if h['cash_price_after_tax'] else ""
                    pts = f"{h['points']}" if h['points'] else ""
                    cpp = f"{h['cpp']:.2f}" if h['cpp'] else ""
                    print(f"  {h['fetch_date']:12s} | {tax:>8s} | {pts:>8s} | {cpp:>6s}")
            else:
                print(f"无记录: {code.upper()} {target_date}")
        else:
            print("格式错误, 请使用: --history HPHHL:2026-08-01")

    else:
        parser.print_help()

    db.close()
