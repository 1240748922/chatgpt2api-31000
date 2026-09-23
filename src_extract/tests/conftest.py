"""Keep service singletons away from the developer's account database."""
import os
import tempfile
from pathlib import Path

_database_dir = Path(tempfile.mkdtemp(prefix="chatgpt2api-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_database_dir / 'application.db').as_posix()}"
os.environ["CHATGPT2API_AUTH_KEY"] = "test-only"

# Existing suites include synthetic opaque tokens and exercise compatibility mode.
# test_strict_account_dispatch explicitly enables and validates the production default.
os.environ["CHATGPT2API_STRICT_IMAGE_CREDENTIALS"] = "0"
os.environ["CHATGPT2API_PAGE_PREWARM_TARGET"] = "0"
