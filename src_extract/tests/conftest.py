"""Keep service singletons away from the developer's account database."""
import os
import tempfile
from pathlib import Path

_database_dir = Path(tempfile.mkdtemp(prefix="chatgpt2api-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_database_dir / 'application.db').as_posix()}"
os.environ["CHATGPT2API_AUTH_KEY"] = "test-only"
