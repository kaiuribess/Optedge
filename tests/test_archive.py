from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import archive


def _write(path: Path, text: str = "x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_archive_moves_data_and_logs():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "data" / "validation_summary.json")
        _write(root / "logs" / "example.log")
        archive_root, moved = archive.run_archive(root, dry_run=False, keep_learned=False)
        assert len(moved) == 2
        assert (archive_root / "data" / "validation_summary.json").exists()
        assert (archive_root / "logs" / "example.log").exists()
        assert not (root / "data" / "validation_summary.json").exists()


def test_archive_dry_run_moves_nothing():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "data" / "validation_summary.json")
        _, moved = archive.run_archive(root, dry_run=True, keep_learned=False)
        assert len(moved) == 1
        assert (root / "data" / "validation_summary.json").exists()


def test_archive_does_not_move_source_files():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "config.py")
        _write(root / "run.py")
        _write(root / "data" / "dashboard_test.html")
        archive.run_archive(root, dry_run=False, keep_learned=False)
        assert (root / "config.py").exists()
        assert (root / "run.py").exists()


def test_archive_keep_learned_preserves_policy_files():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "data" / "model_weights.json")
        _write(root / "data" / "exit_policy.json")
        _write(root / "data" / "exit_policy_history.jsonl")
        _write(root / "data" / "exit_reviews.jsonl")
        _write(root / "data" / "validation_summary.json")
        archive.run_archive(root, dry_run=False, keep_learned=True)
        assert (root / "data" / "model_weights.json").exists()
        assert (root / "data" / "exit_policy.json").exists()
        assert (root / "data" / "exit_policy_history.jsonl").exists()
        assert (root / "data" / "exit_reviews.jsonl").exists()
        assert not (root / "data" / "validation_summary.json").exists()


def test_archive_does_not_move_keep_placeholders():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "logs" / ".keep", "")
        _write(root / "logs" / "example.log")
        archive.run_archive(root, dry_run=False, keep_learned=False)
        assert (root / "logs" / ".keep").exists()
        assert not (root / "logs" / "example.log").exists()


if __name__ == "__main__":
    test_archive_moves_data_and_logs()
    test_archive_dry_run_moves_nothing()
    test_archive_does_not_move_source_files()
    test_archive_keep_learned_preserves_policy_files()
    test_archive_does_not_move_keep_placeholders()
    print("5/5 archive tests passed")
