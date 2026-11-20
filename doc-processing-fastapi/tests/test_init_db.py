import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_init_db_command_creates_tables(tmp_path):
    url = f"sqlite:///{(tmp_path / 'fresh.db').as_posix()}"
    env = {**os.environ, "DATABASE_URL": url}

    subprocess.run([sys.executable, "-m", "app.db"], cwd=PROJECT_ROOT, env=env, check=True)

    engine = create_engine(url)
    assert {"documents", "jobs", "results"} <= set(inspect(engine).get_table_names())
    engine.dispose()
