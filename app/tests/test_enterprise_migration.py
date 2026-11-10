from pathlib import Path
import shutil
import sqlite3

from taxharness.enterprise.storage import EnterpriseDatabase


ROOT = Path(__file__).resolve().parents[1]
OLD_DATABASE = ROOT.parent / "test-results/experiments/enterprise-live-20261110-191130-277900/enterprise.sqlite3"


def test_existing_enterprise_database_keeps_drafts_and_reviews_during_migration(tmp_path):
    path = tmp_path / "old-enterprise.sqlite3"
    shutil.copy2(OLD_DATABASE, path)
    with sqlite3.connect(path) as db:
        draft_count = db.execute("SELECT COUNT(*) FROM enterprise_drafts").fetchone()[0]
        review_count = db.execute("SELECT COUNT(*) FROM enterprise_reviews").fetchone()[0]
        assert next(row for row in db.execute("PRAGMA table_info(enterprise_drafts)") if row[1] == "response_id")[3] == 1
    with EnterpriseDatabase(path) as db:
        columns = {row[1]: row for row in db.connection.execute("PRAGMA table_info(enterprise_drafts)")}
        reviews = {row[1] for row in db.connection.execute("PRAGMA table_info(enterprise_reviews)")}
        assert columns["response_id"][3] == 0 and "field_context_json" in columns
        assert {"review_token", "reviewed_snapshot_json"} <= reviews
        assert db.connection.execute("SELECT COUNT(*) FROM enterprise_drafts").fetchone()[0] == draft_count
        assert db.connection.execute("SELECT COUNT(*) FROM enterprise_reviews").fetchone()[0] == review_count
        assert db.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
