import os
import re
import sqlite3
import tempfile
from pathlib import Path

import pytest

import database
import security


TEST_USERNAME = "family"
TEST_PASSWORD = "test-password-123"
TEST_CONFIG_PATH = Path(tempfile.mkdtemp()) / "security.test.json"
security.write_security_config(
    TEST_USERNAME,
    TEST_PASSWORD,
    path=TEST_CONFIG_PATH,
)
os.environ["HOME_INVENTORY_SECURITY_CONFIG"] = str(TEST_CONFIG_PATH)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402


@pytest.fixture(autouse=True)
def clear_login_limiter():
    main.login_limiter.clear()


def make_client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test_inventory.db")
    monkeypatch.setattr(database, "BACKUP_DIR", tmp_path / "data_backups")
    return TestClient(main.app, follow_redirects=False)


def extract_csrf(response):
    match = re.search(
        r'name="csrf_token"\s+value="([^"]+)"',
        response.text,
    )
    assert match, "页面中缺少 CSRF token"
    return match.group(1)


def log_in(client):
    login_page = client.get("/login")
    assert login_page.status_code == 200
    csrf_token = extract_csrf(login_page)

    response = client.post(
        "/login",
        data={
            "username": TEST_USERNAME,
            "password": TEST_PASSWORD,
            "csrf_token": csrf_token,
            "next": "/",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    home = client.get("/")
    assert home.status_code == 200
    return extract_csrf(home)


def find_location(floor, room, spot):
    return next(
        location
        for location in database.get_all_locations()
        if (
            location["floor"] == floor
            and location["room"] == room
            and location["spot"] == spot
        )
    )


def test_login_logout_cookie_and_security_headers(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        protected = client.get("/")
        assert protected.status_code == 303
        assert protected.headers["location"].startswith("/login?next=")

        login_page = client.get("/login")
        assert login_page.status_code == 200
        assert login_page.headers["x-frame-options"] == "DENY"
        assert login_page.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in login_page.headers[
            "content-security-policy"
        ]

        csrf_token = extract_csrf(login_page)
        wrong = client.post(
            "/login",
            data={
                "username": TEST_USERNAME,
                "password": "wrong-password",
                "csrf_token": csrf_token,
            },
        )
        assert wrong.status_code == 401
        assert "用户名或密码错误" in wrong.text

        csrf_token = extract_csrf(wrong)
        logged_in = client.post(
            "/login",
            data={
                "username": TEST_USERNAME,
                "password": TEST_PASSWORD,
                "csrf_token": csrf_token,
            },
        )
        assert logged_in.status_code == 303
        cookie = logged_in.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=strict" in cookie

        home = client.get("/")
        assert home.status_code == 200
        session_csrf = extract_csrf(home)

        logged_out = client.post(
            "/logout",
            data={"csrf_token": session_csrf},
        )
        assert logged_out.status_code == 303
        assert client.get("/").status_code == 303


def test_csrf_is_required_for_changes(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        csrf_token = log_in(client)

        missing = client.post(
            "/items/add",
            data={"name": "测试物品", "location_id": 1},
        )
        assert missing.status_code == 403

        invalid = client.post(
            "/items/add",
            data={
                "name": "测试物品",
                "location_id": 1,
                "csrf_token": "invalid-token",
            },
        )
        assert invalid.status_code == 403

        valid = client.post(
            "/items/add",
            data={
                "name": "测试物品",
                "location_id": 1,
                "csrf_token": csrf_token,
            },
        )
        assert valid.status_code == 303


def test_login_rate_limit(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        login_page = client.get("/login")
        csrf_token = extract_csrf(login_page)

        for _ in range(5):
            response = client.post(
                "/login",
                data={
                    "username": TEST_USERNAME,
                    "password": "wrong-password",
                    "csrf_token": csrf_token,
                },
            )
            assert response.status_code == 401
            csrf_token = extract_csrf(response)

        limited = client.post(
            "/login",
            data={
                "username": TEST_USERNAME,
                "password": "wrong-password",
                "csrf_token": csrf_token,
            },
        )
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) > 0


def test_security_config_can_be_bootstrapped_from_environment(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "generated-security.json"
    monkeypatch.setenv("HOME_INVENTORY_SECURITY_CONFIG", str(config_path))
    monkeypatch.setenv("HOME_INVENTORY_USERNAME", "cloud-family")
    monkeypatch.setenv("HOME_INVENTORY_PASSWORD", "cloud-password-123")

    loaded = security.load_security_config()

    assert config_path.exists()
    assert security.verify_credentials(
        loaded,
        "cloud-family",
        "cloud-password-123",
    )
    assert not security.verify_credentials(
        loaded,
        "cloud-family",
        "wrong-password",
    )


def test_home_health_and_static_files(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        log_in(client)

        home = client.get("/")
        assert home.status_code == 200
        assert "家庭物品位置系统" in home.text

        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "database": "ok"}

        assert client.get("/static/styles.css").status_code == 200
        assert client.get("/static/app.js").status_code == 200


def test_user_content_is_html_escaped(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        csrf_token = log_in(client)
        script = "<script>alert(1)</script>"

        location_response = client.post(
            "/locations/add",
            data={
                "floor": "一楼",
                "room": script,
                "spot": '柜子" onclick="alert(2)',
                "csrf_token": csrf_token,
            },
        )
        assert location_response.status_code == 303

        locations_page = client.get("/locations")
        assert locations_page.status_code == 200
        assert script not in locations_page.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in locations_page.text

        item_response = client.post(
            "/items/add",
            data={
                "name": script,
                "location_id": 1,
                "quantity": "1",
                "tags": "测试",
                "csrf_token": csrf_token,
            },
        )
        assert item_response.status_code == 303

        home = client.get("/")
        assert script not in home.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in home.text


def test_location_crud_and_counts(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        csrf_token = log_in(client)
        created = client.post(
            "/locations/add",
            data={
                "floor": "一楼",
                "room": "测试房",
                "spot": "测试柜",
                "csrf_token": csrf_token,
            },
        )
        assert created.status_code == 303

        location = find_location("一楼", "测试房", "测试柜")
        edited = client.post(
            f"/locations/{location['id']}/edit",
            data={
                "floor": "二楼",
                "room": "新测试房",
                "spot": "新测试柜",
                "csrf_token": csrf_token,
            },
        )
        assert edited.status_code == 303
        assert database.get_location_by_id(location["id"])["floor"] == "二楼"

        counts = {
            row["id"]: row["item_count"]
            for row in database.get_locations_with_item_counts()
        }
        assert counts[location["id"]] == 0

        deleted = client.post(
            f"/locations/{location['id']}/delete",
            data={"csrf_token": csrf_token},
        )
        assert deleted.status_code == 303
        assert database.get_location_by_id(location["id"]) is None


def test_location_errors_and_validation(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        csrf_token = log_in(client)
        assert client.get("/locations/999999/edit").status_code == 404
        assert client.post(
            "/locations/999999/edit",
            data={
                "floor": "一楼",
                "room": "房间",
                "spot": "柜子",
                "csrf_token": csrf_token,
            },
        ).status_code == 404
        assert client.post(
            "/locations/999999/delete",
            data={"csrf_token": csrf_token},
        ).status_code == 404

        too_long = client.post(
            "/locations/add",
            data={
                "floor": "一楼",
                "room": "房" * 51,
                "spot": "柜子",
                "csrf_token": csrf_token,
            },
        )
        assert too_long.status_code == 422

        invalid_floor = client.post(
            "/locations/add",
            data={
                "floor": "四楼",
                "room": "房间",
                "spot": "柜子",
                "csrf_token": csrf_token,
            },
        )
        assert invalid_floor.status_code == 400

        database.add_item("占用测试", 1, "", "")
        occupied = client.post(
            "/locations/1/delete",
            data={"csrf_token": csrf_token},
        )
        assert occupied.status_code == 400
        assert database.get_location_by_id(1) is not None


def test_duplicate_location_is_rejected(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        csrf_token = log_in(client)
        response = client.post(
            "/locations/add",
            data={
                "floor": "一楼",
                "room": "客厅",
                "spot": "茶几",
                "csrf_token": csrf_token,
            },
        )
        assert response.status_code == 400


def test_backup_download_requires_login_and_csrf(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        assert client.get("/backups").status_code == 303

        csrf_token = log_in(client)
        backup_page = client.get("/backups")
        assert backup_page.status_code == 200
        assert "下载当前备份" in backup_page.text

        missing_csrf = client.post("/backups/download")
        assert missing_csrf.status_code == 403

        downloaded = client.post(
            "/backups/download",
            data={"csrf_token": csrf_token},
        )
        assert downloaded.status_code == 200
        assert downloaded.content.startswith(b"SQLite format 3\x00")
        assert "attachment" in downloaded.headers["content-disposition"]

        downloaded_path = tmp_path / "downloaded.db"
        downloaded_path.write_bytes(downloaded.content)
        assert database.validate_database_file(downloaded_path)


def test_restore_validates_and_recovers_data(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        csrf_token = log_in(client)
        database.add_item("备份中的物品", 1, "", "")

        downloaded = client.post(
            "/backups/download",
            data={"csrf_token": csrf_token},
        )
        backup_bytes = downloaded.content

        database.add_item("备份后新增", 1, "", "")
        assert database.search_items("备份后新增")

        restored = client.post(
            "/backups/restore",
            data={"csrf_token": csrf_token},
            files={
                "backup_file": (
                    "inventory.db",
                    backup_bytes,
                    "application/vnd.sqlite3",
                )
            },
        )
        assert restored.status_code == 303
        assert restored.headers["location"] == "/backups?restored=true"
        assert database.search_items("备份中的物品")
        assert not database.search_items("备份后新增")
        assert any(
            backup["name"].startswith("pre_restore_")
            for backup in database.list_data_backups()
        )

        invalid = client.post(
            "/backups/restore",
            data={"csrf_token": csrf_token},
            files={
                "backup_file": (
                    "broken.db",
                    b"not a sqlite database",
                    "application/octet-stream",
                )
            },
        )
        assert invalid.status_code == 400
        assert database.search_items("备份中的物品")


def test_legacy_database_is_migrated(tmp_path, monkeypatch):
    legacy_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy_path)
    conn.executescript("""
        CREATE TABLE locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            floor TEXT NOT NULL,
            room TEXT NOT NULL,
            spot TEXT NOT NULL,
            UNIQUE(floor, room, spot)
        );
        CREATE TABLE items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            location_id INTEGER NOT NULL,
            quantity TEXT,
            tags TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (location_id) REFERENCES locations(id)
        );
        INSERT INTO locations (floor, room, spot)
        VALUES ('一楼', '测试房', '测试柜');
        INSERT INTO items (name, location_id, quantity, tags)
        VALUES ('旧版物品', 1, '1个', 'legacy');
    """)
    conn.commit()
    conn.close()

    monkeypatch.setattr(database, "DB_PATH", legacy_path)
    database.init_db()

    with database.database_connection() as migrated:
        item_columns = {
            row["name"]
            for row in migrated.execute("PRAGMA table_info(items)").fetchall()
        }
        tables = {
            row["name"]
            for row in migrated.execute("""
                SELECT name FROM sqlite_master WHERE type = 'table'
            """).fetchall()
        }

    assert {"deleted_at", "deleted_by"}.issubset(item_columns)
    assert "audit_log" in tables
    assert database.search_items("旧版物品")


def test_recycle_bin_restore_and_history(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        csrf_token = log_in(client)
        created = client.post(
            "/items/add",
            data={
                "name": "可恢复物品",
                "location_id": 1,
                "quantity": "1个",
                "tags": "测试",
                "csrf_token": csrf_token,
            },
        )
        assert created.status_code == 303
        item_id = database.search_items("可恢复物品")[0]["id"]

        moved_to_trash = client.post(
            f"/items/{item_id}/delete",
            data={"csrf_token": csrf_token},
        )
        assert moved_to_trash.status_code == 303
        assert not database.search_items("可恢复物品")
        assert database.get_deleted_items()[0]["name"] == "可恢复物品"

        trash_page = client.get("/trash")
        assert trash_page.status_code == 200
        assert "可恢复物品" in trash_page.text

        location_counts = {
            row["id"]: row
            for row in database.get_locations_with_item_counts()
        }
        assert location_counts[1]["item_count"] == 0
        assert location_counts[1]["deleted_item_count"] == 1

        blocked_location_delete = client.post(
            "/locations/1/delete",
            data={"csrf_token": csrf_token},
        )
        assert blocked_location_delete.status_code == 400

        restored = client.post(
            f"/items/{item_id}/restore",
            data={"csrf_token": csrf_token},
        )
        assert restored.status_code == 303
        assert database.search_items("可恢复物品")
        assert not database.get_deleted_items()

        history = client.get("/history")
        assert history.status_code == 200
        assert "移入回收站" in history.text
        assert "恢复" in history.text

        actions = [entry["action"] for entry in database.get_audit_log()]
        assert "create" in actions
        assert "trash" in actions
        assert "restore" in actions


def test_permanent_delete_and_automatic_purge(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        csrf_token = log_in(client)

        permanent_id = database.add_item("永久删除物品", 1, "", "")
        assert database.delete_item(permanent_id, actor=TEST_USERNAME)
        permanently_deleted = client.post(
            f"/items/{permanent_id}/permanent-delete",
            data={"csrf_token": csrf_token},
        )
        assert permanently_deleted.status_code == 303
        assert not database.get_deleted_items()

        expired_id = database.add_item("过期物品", 1, "", "")
        assert database.delete_item(expired_id, actor=TEST_USERNAME)
        with database.database_connection() as conn:
            conn.execute("""
                UPDATE items
                SET deleted_at = datetime('now', '-31 days')
                WHERE id = ?
            """, (expired_id,))
            conn.commit()

        assert database.purge_deleted_items(days=30) == 1
        assert not database.get_deleted_items()

        actions = [entry["action"] for entry in database.get_audit_log()]
        assert "permanent_delete" in actions
        assert "auto_purge" in actions
