import sqlite3
import os
import threading
from contextlib import closing, contextmanager
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4


DB_PATH = Path(__file__).resolve().with_name("home_inventory.db")
BACKUP_DIR = Path(__file__).resolve().with_name("data_backups")
BACKUP_LOCK = threading.RLock()
REQUIRED_COLUMNS = {
    "locations": {"id", "floor", "room", "spot"},
    "items": {
        "id",
        "name",
        "location_id",
        "quantity",
        "tags",
        "created_at",
        "updated_at",
    },
}


def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def database_connection():
    conn = get_connection()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _add_audit_log(
    conn,
    action,
    entity_type,
    entity_id,
    summary,
    actor,
):
    conn.execute("""
        INSERT INTO audit_log (
            action,
            entity_type,
            entity_id,
            summary,
            actor
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        action,
        entity_type,
        entity_id,
        str(summary)[:500],
        str(actor or "system")[:50],
    ))


def init_db():
    with database_connection() as conn:
        conn.execute("PRAGMA journal_mode = WAL")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                floor TEXT NOT NULL,
                room TEXT NOT NULL,
                spot TEXT NOT NULL,
                UNIQUE(floor, room, spot)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                location_id INTEGER NOT NULL,
                quantity TEXT,
                tags TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                deleted_at TEXT,
                deleted_by TEXT,
                FOREIGN KEY (location_id) REFERENCES locations(id)
            )
        """)

        item_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(items)").fetchall()
        }
        if "deleted_at" not in item_columns:
            conn.execute("ALTER TABLE items ADD COLUMN deleted_at TEXT")
        if "deleted_by" not in item_columns:
            conn.execute("ALTER TABLE items ADD COLUMN deleted_by TEXT")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id INTEGER,
                summary TEXT NOT NULL,
                actor TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_items_location_id
            ON items(location_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_items_created_at
            ON items(created_at DESC)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_items_deleted_at
            ON items(deleted_at)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_log_created_at
            ON audit_log(created_at DESC, id DESC)
        """)

        conn.execute("""
            INSERT OR IGNORE INTO locations (floor, room, spot)
            VALUES
                ('一楼', '客厅', '茶几'),
                ('一楼', '客厅', '电视柜'),
                ('一楼', '客厅', '边几'),
                ('一楼', '客厅', '展示柜'),
                ('一楼', '厨房', '左侧顶橱柜'),
                ('一楼', '厨房', '左侧底橱柜'),
                ('一楼', '厨房', '右侧顶橱柜'),
                ('一楼', '厨房', '右侧底橱柜'),
                ('一楼', '厨房', '烤箱抽屉'),
                ('一楼', '卫生间', '浴室柜'),
                ('一楼', '玄关', '鞋柜'),
                ('一楼', '卧室', '衣柜'),
                ('一楼', '卧室', '床底'),
                ('一楼', '卧室', '床头柜'),

                ('二楼', '小客厅', '茶几'),
                ('二楼', '主卧', '衣柜'),
                ('二楼', '主卧', '床头柜'),
                ('二楼', '主卧', '床头柜边收纳箱'),
                ('二楼', '主卧', '电视柜'),
                ('二楼', '主卧', '床底'),
                ('二楼', '衣帽间', '左衣柜'),
                ('二楼', '衣帽间', '右衣柜'),
                ('二楼', '主卫', '浴室柜'),
                ('二楼', '主卫', '收纳箱'),
                ('二楼', '靠楼梯客卧', '衣柜'),
                ('二楼', '带阳台客卧', '床底'),
                ('二楼', '带阳台客卧', '收纳箱'),
                ('二楼', '带阳台客卧', '床头柜'),

                ('三楼', '书房', '书柜'),
                ('三楼', '书房', '书桌柜'),
                ('三楼', '大卧室', '衣柜'),
                ('三楼', '大卧室', '书桌柜'),
                ('三楼', '大卧室', '床底'),
                ('三楼', '大卧室', '床头柜'),
                ('三楼', '卫生间', '浴室柜'),

                ('地下室', '储藏间', '金属架'),
                ('地下室', '小卧室', '金属架'),
                ('地下室', '大厅', '酒柜')
        """)

        conn.commit()


def get_all_locations():
    with database_connection() as conn:
        return conn.execute("""
            SELECT * FROM locations
            ORDER BY
                CASE floor
                    WHEN '一楼' THEN 1
                    WHEN '二楼' THEN 2
                    WHEN '三楼' THEN 3
                    WHEN '地下室' THEN 4
                    ELSE 5
                END,
                room,
                spot
        """).fetchall()


def get_locations_with_item_counts():
    with database_connection() as conn:
        return conn.execute("""
            SELECT
                locations.id,
                locations.floor,
                locations.room,
                locations.spot,
                COUNT(
                    CASE WHEN items.deleted_at IS NULL THEN items.id END
                ) AS item_count,
                COUNT(
                    CASE WHEN items.deleted_at IS NOT NULL THEN items.id END
                ) AS deleted_item_count
            FROM locations
            LEFT JOIN items ON items.location_id = locations.id
            GROUP BY
                locations.id,
                locations.floor,
                locations.room,
                locations.spot
            ORDER BY
                CASE locations.floor
                    WHEN '一楼' THEN 1
                    WHEN '二楼' THEN 2
                    WHEN '三楼' THEN 3
                    WHEN '地下室' THEN 4
                    ELSE 5
                END,
                locations.room,
                locations.spot
        """).fetchall()


def add_location(floor, room, spot, actor="system"):
    with database_connection() as conn:
        try:
            cursor = conn.execute("""
                INSERT INTO locations (floor, room, spot)
                VALUES (?, ?, ?)
            """, (floor, room, spot))
            _add_audit_log(
                conn,
                "create",
                "location",
                cursor.lastrowid,
                f"{floor} - {room} - {spot}",
                actor,
            )
            conn.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            raise ValueError("这个位置已经存在") from exc


def location_exists(conn, location_id):
    return conn.execute(
        "SELECT 1 FROM locations WHERE id = ?",
        (location_id,)
    ).fetchone() is not None


def add_item(name, location_id, quantity, tags, actor="system"):
    with database_connection() as conn:
        if not location_exists(conn, location_id):
            raise ValueError("选择的存放位置不存在")

        cursor = conn.execute("""
            INSERT INTO items (name, location_id, quantity, tags)
            VALUES (?, ?, ?, ?)
        """, (name, location_id, quantity, tags))
        _add_audit_log(
            conn,
            "create",
            "item",
            cursor.lastrowid,
            name,
            actor,
        )
        conn.commit()
        return cursor.lastrowid


ITEM_SELECT = """
    SELECT
        items.id,
        items.name,
        items.location_id,
        items.quantity,
        items.tags,
        items.deleted_at,
        items.deleted_by,
        locations.floor AS floor_name,
        locations.room AS room_name,
        locations.spot AS spot_name
    FROM items
    JOIN locations ON items.location_id = locations.id
"""


def search_items(keyword=""):
    keyword = keyword.strip()

    with database_connection() as conn:
        if not keyword:
            return conn.execute(
                ITEM_SELECT
                + """
                    WHERE items.deleted_at IS NULL
                    ORDER BY items.created_at DESC, items.id DESC
                """
            ).fetchall()

        escaped_keyword = (
            keyword
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        keyword_like = f"%{escaped_keyword}%"

        return conn.execute(
            ITEM_SELECT + """
                WHERE items.deleted_at IS NULL
                  AND (
                       items.name LIKE ? ESCAPE '\\'
                    OR items.quantity LIKE ? ESCAPE '\\'
                    OR items.tags LIKE ? ESCAPE '\\'
                    OR locations.floor LIKE ? ESCAPE '\\'
                    OR locations.room LIKE ? ESCAPE '\\'
                    OR locations.spot LIKE ? ESCAPE '\\'
                  )
                ORDER BY items.created_at DESC, items.id DESC
            """,
            (keyword_like,) * 6
        ).fetchall()


def get_item_by_id(item_id):
    with database_connection() as conn:
        return conn.execute(
            ITEM_SELECT
            + " WHERE items.id = ? AND items.deleted_at IS NULL",
            (item_id,)
        ).fetchone()


def update_item(
    item_id,
    name,
    location_id,
    quantity,
    tags,
    actor="system",
):
    with database_connection() as conn:
        if not location_exists(conn, location_id):
            raise ValueError("选择的存放位置不存在")

        cursor = conn.execute("""
            UPDATE items
            SET
                name = ?,
                location_id = ?,
                quantity = ?,
                tags = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND deleted_at IS NULL
        """, (name, location_id, quantity, tags, item_id))
        if cursor.rowcount > 0:
            _add_audit_log(
                conn,
                "update",
                "item",
                item_id,
                name,
                actor,
            )
        conn.commit()
        return cursor.rowcount > 0


def delete_item(item_id, actor="system"):
    with database_connection() as conn:
        item = conn.execute("""
            SELECT name
            FROM items
            WHERE id = ? AND deleted_at IS NULL
        """, (item_id,)).fetchone()
        if item is None:
            return False

        cursor = conn.execute("""
            UPDATE items
            SET
                deleted_at = CURRENT_TIMESTAMP,
                deleted_by = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND deleted_at IS NULL
        """, (actor, item_id))
        if cursor.rowcount > 0:
            _add_audit_log(
                conn,
                "trash",
                "item",
                item_id,
                item["name"],
                actor,
            )
        conn.commit()
        return cursor.rowcount > 0


def get_deleted_items():
    with database_connection() as conn:
        return conn.execute(
            ITEM_SELECT
            + """
                WHERE items.deleted_at IS NOT NULL
                ORDER BY items.deleted_at DESC, items.id DESC
            """
        ).fetchall()


def restore_item(item_id, actor="system"):
    with database_connection() as conn:
        item = conn.execute("""
            SELECT name
            FROM items
            WHERE id = ? AND deleted_at IS NOT NULL
        """, (item_id,)).fetchone()
        if item is None:
            return False

        cursor = conn.execute("""
            UPDATE items
            SET
                deleted_at = NULL,
                deleted_by = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND deleted_at IS NOT NULL
        """, (item_id,))
        if cursor.rowcount > 0:
            _add_audit_log(
                conn,
                "restore",
                "item",
                item_id,
                item["name"],
                actor,
            )
        conn.commit()
        return cursor.rowcount > 0


def permanently_delete_item(item_id, actor="system"):
    with database_connection() as conn:
        item = conn.execute("""
            SELECT name
            FROM items
            WHERE id = ? AND deleted_at IS NOT NULL
        """, (item_id,)).fetchone()
        if item is None:
            return False

        cursor = conn.execute("""
            DELETE FROM items
            WHERE id = ? AND deleted_at IS NOT NULL
        """, (item_id,))
        if cursor.rowcount > 0:
            _add_audit_log(
                conn,
                "permanent_delete",
                "item",
                item_id,
                item["name"],
                actor,
            )
        conn.commit()
        return cursor.rowcount > 0


def purge_deleted_items(days=30, actor="system"):
    days = int(days)
    if days < 1:
        raise ValueError("保留天数必须大于 0")

    with database_connection() as conn:
        deleted_items = conn.execute("""
            SELECT id, name
            FROM items
            WHERE deleted_at IS NOT NULL
              AND deleted_at <= datetime('now', ?)
        """, (f"-{days} days",)).fetchall()

        for item in deleted_items:
            conn.execute(
                "DELETE FROM items WHERE id = ?",
                (item["id"],),
            )
            _add_audit_log(
                conn,
                "auto_purge",
                "item",
                item["id"],
                item["name"],
                actor,
            )

        conn.commit()
        return len(deleted_items)


def get_audit_log(limit=200):
    limit = max(1, min(int(limit), 500))
    with database_connection() as conn:
        return conn.execute("""
            SELECT
                id,
                action,
                entity_type,
                entity_id,
                summary,
                actor,
                created_at
            FROM audit_log
            ORDER BY created_at DESC, id DESC
            LIMIT ?
        """, (limit,)).fetchall()


def check_database():
    with database_connection() as conn:
        conn.execute("SELECT 1").fetchone()
        return True


def _readonly_connection(path):
    conn = sqlite3.connect(Path(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def validate_database_file(path):
    database_path = Path(path)
    if not database_path.is_file() or database_path.stat().st_size == 0:
        raise ValueError("备份文件为空或不存在")

    try:
        with closing(_readonly_connection(database_path)) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise ValueError("数据库完整性检查失败")

            table_names = {
                row["name"]
                for row in conn.execute("""
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                """).fetchall()
            }

            for table_name, required_columns in REQUIRED_COLUMNS.items():
                if table_name not in table_names:
                    raise ValueError(f"备份缺少 {table_name} 表")

                columns = {
                    row["name"]
                    for row in conn.execute(
                        f"PRAGMA table_info({table_name})"
                    ).fetchall()
                }
                if not required_columns.issubset(columns):
                    raise ValueError(f"{table_name} 表结构不兼容")

            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("备份中存在无效的位置引用")
    except sqlite3.DatabaseError as exc:
        raise ValueError("文件不是有效的 SQLite 数据库") from exc

    return True


def _prepare_backup_directory():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)


def create_database_backup(destination):
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    with BACKUP_LOCK:
        source = get_connection()
        target = sqlite3.connect(destination_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    os.chmod(destination_path, 0o600)
    validate_database_file(destination_path)
    return destination_path


def create_timestamped_backup(kind="manual"):
    if kind not in {"daily", "manual", "pre_restore"}:
        raise ValueError("不支持的备份类型")

    with BACKUP_LOCK:
        _prepare_backup_directory()
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"{kind}_{timestamp}_{uuid4().hex[:8]}.db"
        return create_database_backup(BACKUP_DIR / filename)


def list_data_backups():
    if not BACKUP_DIR.exists():
        return []

    backups = []
    for path in BACKUP_DIR.glob("*.db"):
        stat = path.stat()
        backups.append({
            "name": path.name,
            "size": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_mtime),
        })

    return sorted(
        backups,
        key=lambda backup: backup["created_at"],
        reverse=True,
    )


def prune_data_backups(kind, keep):
    matching = [
        backup
        for backup in list_data_backups()
        if backup["name"].startswith(f"{kind}_")
    ]
    for backup in matching[keep:]:
        (BACKUP_DIR / backup["name"]).unlink(missing_ok=True)


def ensure_daily_backup():
    with BACKUP_LOCK:
        _prepare_backup_directory()
        today_prefix = f"daily_{date.today().isoformat()}_"
        existing = sorted(BACKUP_DIR.glob(f"{today_prefix}*.db"))
        if existing:
            return existing[-1]

        backup_path = create_timestamped_backup("daily")
        prune_data_backups("daily", keep=30)
        return backup_path


def restore_database_from_file(source_path, actor="system"):
    source_path = Path(source_path)
    validate_database_file(source_path)

    with BACKUP_LOCK:
        safety_backup = create_timestamped_backup("pre_restore")
        source = _readonly_connection(source_path)
        target = sqlite3.connect(DB_PATH, timeout=10)
        try:
            source.backup(target)
        except sqlite3.DatabaseError as exc:
            raise ValueError("恢复数据库失败") from exc
        finally:
            target.close()
            source.close()

        os.chmod(DB_PATH, 0o600)
        init_db()
        validate_database_file(DB_PATH)
        with database_connection() as conn:
            _add_audit_log(
                conn,
                "restore_database",
                "database",
                None,
                source_path.name,
                actor,
            )
            conn.commit()
        prune_data_backups("pre_restore", keep=20)
        return safety_backup

def get_location_by_id(location_id):
    with database_connection() as conn:
        return conn.execute("""
            SELECT id, floor, room, spot
            FROM locations
            WHERE id = ?
        """, (location_id,)).fetchone()


def update_location(
    location_id,
    floor,
    room,
    spot,
    actor="system",
):
    with database_connection() as conn:
        try:
            cursor = conn.execute("""
                UPDATE locations
                SET floor = ?, room = ?, spot = ?
                WHERE id = ?
            """, (floor, room, spot, location_id))
            if cursor.rowcount > 0:
                _add_audit_log(
                    conn,
                    "update",
                    "location",
                    location_id,
                    f"{floor} - {room} - {spot}",
                    actor,
                )
            conn.commit()
            return cursor.rowcount > 0
        except sqlite3.IntegrityError as exc:
            raise ValueError("这个位置已经存在") from exc


def count_items_at_location(location_id):
    with database_connection() as conn:
        result = conn.execute("""
            SELECT COUNT(*) AS count
            FROM items
            WHERE location_id = ? AND deleted_at IS NULL
        """, (location_id,)).fetchone()
        return result["count"]


def delete_location(location_id, actor="system"):
    with database_connection() as conn:
        counts = conn.execute("""
            SELECT
                COUNT(
                    CASE WHEN deleted_at IS NULL THEN id END
                ) AS active_count,
                COUNT(
                    CASE WHEN deleted_at IS NOT NULL THEN id END
                ) AS deleted_count
            FROM items
            WHERE location_id = ?
        """, (location_id,)).fetchone()

        if counts["active_count"] > 0:
            raise ValueError("不能删除：这个位置下面还有物品")
        if counts["deleted_count"] > 0:
            raise ValueError("不能删除：回收站中仍有物品使用这个位置")

        location = conn.execute("""
            SELECT floor, room, spot
            FROM locations
            WHERE id = ?
        """, (location_id,)).fetchone()
        if location is None:
            return False

        try:
            cursor = conn.execute("""
                DELETE FROM locations
                WHERE id = ?
            """, (location_id,))
            if cursor.rowcount > 0:
                _add_audit_log(
                    conn,
                    "delete",
                    "location",
                    location_id,
                    (
                        f"{location['floor']} - "
                        f"{location['room']} - "
                        f"{location['spot']}"
                    ),
                    actor,
                )
            conn.commit()
            return cursor.rowcount > 0
        except sqlite3.IntegrityError as exc:
            raise ValueError("不能删除：这个位置下面还有物品") from exc
