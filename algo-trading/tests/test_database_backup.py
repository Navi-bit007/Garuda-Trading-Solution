from datetime import date, timedelta

from app.database.database import Database
from app.database.models import PositionRecord
from app.database.repository import Repository


def test_initialize_creates_a_backup_containing_todays_data(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    Repository(database).save_position(PositionRecord("AAA", "BUY", 10, 100, 95, date.today()))
    # A distinct backup_dir from `initialize()`'s own default location, so this explicit call
    # isn't a no-op against a backup `initialize()` already took (empty, before the save above).
    database.backup_daily(str(tmp_path / "manual_backups"))

    backup_path = tmp_path / "manual_backups" / f"trading_{date.today().isoformat()}.sqlite3"
    assert backup_path.exists()
    backup_positions = Repository(Database(str(backup_path))).load_positions()
    assert [position.symbol for position in backup_positions] == ["AAA"]
    database.close()


def test_backup_daily_does_not_overwrite_an_existing_backup_for_today(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    backup_dir = str(tmp_path / "backups")
    database.backup_daily(backup_dir)
    backup_path = tmp_path / "backups" / f"trading_{date.today().isoformat()}.sqlite3"
    first_mtime = backup_path.stat().st_mtime

    Repository(database).save_position(PositionRecord("BBB", "BUY", 5, 50, 45, date.today()))
    database.backup_daily(backup_dir)

    assert backup_path.stat().st_mtime == first_mtime
    database.close()


def test_backup_daily_prunes_backups_older_than_retention(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(exist_ok=True)
    stale = backup_dir / f"trading_{(date.today() - timedelta(days=30)).isoformat()}.sqlite3"
    stale.write_text("old")
    recent = backup_dir / f"trading_{(date.today() - timedelta(days=1)).isoformat()}.sqlite3"
    recent.write_text("recent")

    database.backup_daily(str(backup_dir), retention_days=14)

    assert not stale.exists()
    assert recent.exists()
    database.close()
