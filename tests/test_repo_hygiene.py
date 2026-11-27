"""Guards against the two repo files that lost their leading dot (secret-leak class)."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_gitignore_is_named_with_leading_dot():
    assert (ROOT / ".gitignore").is_file(), "tanpa .gitignore, git add -A akan meng-commit .env"
    assert not (ROOT / "gitignore").exists(), "file gitignore tanpa titik tidak pernah dibaca git"


@pytest.mark.skipif(not shutil.which("git"), reason="git tidak tersedia")
@pytest.mark.parametrize("path", [
    ".env",
    ".env.local",
    "state.json",
    "state.testnet.json",
    "state.testnet.json.lock",
    "data/state.json",
    "data/trades.csv",
    "data/bot.log",
    "venv/x.py",
    ".venv/x.py",
    "__pycache__/bot.pyc",
    "bot.log",
])
def test_runtime_and_secret_paths_are_ignored(path):
    rc = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", path]).returncode
    assert rc == 0, f"{path} tidak di-ignore .gitignore"


@pytest.mark.skipif(not shutil.which("git"), reason="git tidak tersedia")
def test_env_example_stays_trackable():
    rc = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", ".env.example"]).returncode
    assert rc == 1, ".env.example harus tetap ikut ter-commit"


@pytest.mark.skipif(not shutil.which("git"), reason="git tidak tersedia")
def test_no_secret_is_already_tracked():
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                         capture_output=True, text=True).stdout.split()
    for name in out:
        assert Path(name).name not in {".env", "state.json", "trades.csv"}, name
        assert not Path(name).suffix in {".log", ".lock"}, name