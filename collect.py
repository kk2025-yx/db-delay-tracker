"""
DB Timetables API 采集脚本 (v2)
对 stations.py 里配置的每个站点，调用 fchg (/fchg/{evaNo})，拿到当前所有已知的
时刻偏差（延误、取消、月台变更等），解析后写入本地 SQLite。

跟 v1 相比的调整（根据实际抓到的数据结构修正）：
- 有些 <ar>/<dp> 只有 ct（变更后时间），没有 pt（计划时间）——这种情况下延误分钟数算不出来，
  先存成 NULL，以后可以拿同一个 stop_id 去 plan 接口把 pt 补上
- 新增：抓取延误原因码（<m t="d" c="..."> 里的 c 属性）
- 新增：单独一张表存"公告类"消息（<m t="h">，比如 Bauarbeiten/施工、Störung/故障），
  这类消息有 from/to 有效期，是判断"是不是系统性问题"的关键线索
- 新增：抓取取消标记（<m t="q"> 品质变更 / 实际 <ar>、<dp> 缺失可能代表取消，这里先记录原始 XML 供后续判断）

用法：
    python collect.py

需要的环境变量：
    DB_CLIENT_ID
    DB_API_KEY
"""

import os
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

from stations import STATIONS

API_BASE = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1"
DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")

CLIENT_ID = os.environ.get("DB_CLIENT_ID")
API_KEY = os.environ.get("DB_API_KEY")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS stops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            eva TEXT NOT NULL,
            station_name TEXT,
            stop_id TEXT NOT NULL,       -- <s id="...">，同一趟车在这一站的唯一标识
            train_category TEXT,         -- RE / RB / ECE 等，来自 <tl c="...">，可能为空（部分记录没有tl）
            train_number TEXT,           -- 车次号，来自 <tl n="...">
            line_label TEXT,             -- 线路名，比如 RE96 / ECE 199，来自 <ar>/<dp> 的 l 属性
            kind TEXT NOT NULL,          -- 'arrival' 或 'departure'
            planned_time TEXT,           -- pt，原始格式 YYMMDDHHmm，可能为空
            changed_time TEXT,           -- ct，可能为空（代表这一项没有变化播报，通常不会出现在fchg里）
            delay_minutes INTEGER,       -- changed_time - planned_time，两者有一个缺失就是 NULL
            delay_reason_code TEXT,      -- 从 <m t="d"> 里取最新的 c 属性（延误原因分类码）
            planned_platform TEXT,
            changed_platform TEXT,
            route_path TEXT,             -- ppth，经停路径
            fetched_at TEXT NOT NULL,    -- 这条记录本次抓取的时间（UTC，ISO格式）
            UNIQUE(eva, stop_id, kind, fetched_at)
        );

        CREATE TABLE IF NOT EXISTS notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            eva TEXT NOT NULL,
            stop_id TEXT,                -- 这条公告挂在哪个 <s> 元素下面抓到的（同一公告常常挂在很多趟车上，会重复，靠UNIQUE去重）
            notice_id TEXT NOT NULL,     -- <m id="...">
            category TEXT,               -- Bauarbeiten / Störung / Information 等
            valid_from TEXT,
            valid_to TEXT,
            priority TEXT,
            fetched_at TEXT NOT NULL,
            UNIQUE(eva, notice_id)
        );
        """)
    conn.commit()


def parse_time(raw: str | None) -> datetime | None:
    """DB 的时间格式是 YYMMDDHHmm，比如 2609221530 = 2026-09-22 15:30"""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%y%m%d%H%M")
    except ValueError:
        return None


def compute_delay(planned_raw: str | None, changed_raw: str | None) -> int | None:
    planned = parse_time(planned_raw)
    changed = parse_time(changed_raw)
    if planned is None or changed is None:
        return None
    return int((changed - planned).total_seconds() // 60)


def fetch_full_changes(eva: str) -> ET.Element | None:
    url = f"{API_BASE}/fchg/{eva}"
    headers = {
        "DB-Client-Id": CLIENT_ID,
        "DB-Api-Key": API_KEY,
        "accept": "application/xml",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
    except requests.RequestException as e:
        print(f"[{eva}] 请求失败: {e}")
        return None

    if resp.status_code != 200:
        print(f"[{eva}] HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    try:
        return ET.fromstring(resp.content)
    except ET.ParseError as e:
        print(f"[{eva}] XML解析失败: {e}")
        return None


def latest_delay_reason(elem: ET.Element) -> str | None:
    """在一个 <ar> 或 <dp> 元素下面找所有 <m t="d">（延误原因），取时间戳最新的那个的 c 属性"""
    reasons = [m for m in elem.findall("m") if m.get("t") == "d"]
    if not reasons:
        return None
    reasons.sort(key=lambda m: m.get("ts") or "")
    return reasons[-1].get("c")


def extract_stop_records(
    root: ET.Element, eva: str, station_name: str, fetched_at: str
) -> list[dict]:
    records = []

    for s in root.findall(".//s"):
        stop_id = s.get("id")

        tl = s.find("tl")
        train_category = tl.get("c") if tl is not None else None
        train_number = tl.get("n") if tl is not None else None

        for kind, tag in (("arrival", "ar"), ("departure", "dp")):
            elem = s.find(tag)
            if elem is None:
                continue

            planned_raw = elem.get("pt")
            changed_raw = elem.get("ct")

            records.append(
                {
                    "eva": eva,
                    "station_name": station_name,
                    "stop_id": stop_id,
                    "train_category": train_category,
                    "train_number": train_number,
                    "line_label": elem.get("l"),
                    "kind": kind,
                    "planned_time": planned_raw,
                    "changed_time": changed_raw,
                    "delay_minutes": compute_delay(planned_raw, changed_raw),
                    "delay_reason_code": latest_delay_reason(elem),
                    "planned_platform": elem.get("pp"),
                    "changed_platform": elem.get("cp"),
                    "route_path": elem.get("ppth"),
                    "fetched_at": fetched_at,
                }
            )

    return records


def extract_notice_records(root: ET.Element, eva: str, fetched_at: str) -> list[dict]:
    """抓 <m t="h"> 这类公告消息（施工/故障/提示），挂在哪个 <s> 下面不重要，
    用 notice_id 去重，同一条公告即使出现在几十趟车下面，也只会存一条。"""
    records = []
    for s in root.findall(".//s"):
        stop_id = s.get("id")
        for m in s.findall("m"):
            if m.get("t") != "h":
                continue
            records.append(
                {
                    "eva": eva,
                    "stop_id": stop_id,
                    "notice_id": m.get("id"),
                    "category": m.get("cat"),
                    "valid_from": m.get("from"),
                    "valid_to": m.get("to"),
                    "priority": m.get("pr"),
                    "fetched_at": fetched_at,
                }
            )
    return records


def insert_stops(conn: sqlite3.Connection, records: list[dict]) -> int:
    if not records:
        return 0
    cur = conn.executemany(
        """
        INSERT OR IGNORE INTO stops
        (eva, station_name, stop_id, train_category, train_number, line_label, kind,
         planned_time, changed_time, delay_minutes, delay_reason_code,
         planned_platform, changed_platform, route_path, fetched_at)
        VALUES
        (:eva, :station_name, :stop_id, :train_category, :train_number, :line_label, :kind,
         :planned_time, :changed_time, :delay_minutes, :delay_reason_code,
         :planned_platform, :changed_platform, :route_path, :fetched_at)
        """,
        records,
    )
    conn.commit()
    return cur.rowcount


def insert_notices(conn: sqlite3.Connection, records: list[dict]) -> int:
    if not records:
        return 0
    cur = conn.executemany(
        """
        INSERT OR IGNORE INTO notices
        (eva, stop_id, notice_id, category, valid_from, valid_to, priority, fetched_at)
        VALUES
        (:eva, :stop_id, :notice_id, :category, :valid_from, :valid_to, :priority, :fetched_at)
        """,
        records,
    )
    conn.commit()
    return cur.rowcount


def main() -> None:
    if not CLIENT_ID or not API_KEY:
        raise SystemExit(
            "缺少 DB_CLIENT_ID / DB_API_KEY 环境变量。\n"
            "本地测试：export DB_CLIENT_ID=xxx && export DB_API_KEY=xxx\n"
            "GitHub Actions：在仓库 Settings -> Secrets 里配置同名的两个 secret"
        )

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    fetched_at = datetime.now(timezone.utc).isoformat()
    total_stops, total_notices = 0, 0

    for name, eva in STATIONS.items():
        if eva == "REPLACE_ME":
            print(f"[跳过] {name} 还没填 EVA number，去 stations.py 里补上")
            continue

        root = fetch_full_changes(eva)
        if root is None:
            continue

        stop_records = extract_stop_records(root, eva, name, fetched_at)
        notice_records = extract_notice_records(root, eva, fetched_at)

        inserted_stops = insert_stops(conn, stop_records)
        inserted_notices = insert_notices(conn, notice_records)

        total_stops += inserted_stops
        total_notices += inserted_notices

        print(
            f"[{name} / {eva}] 抓到 {len(stop_records)} 条停靠记录（新增 {inserted_stops}），"
            f"{len(notice_records)} 条公告（新增 {inserted_notices}）"
        )

    conn.close()
    print(f"完成。本次共新增 {total_stops} 条停靠记录，{total_notices} 条公告。")


if __name__ == "__main__":
    main()
