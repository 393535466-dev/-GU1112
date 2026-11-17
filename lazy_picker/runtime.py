"""Runtime paths and explicit host allowlists for local and Vercel hosting."""

import os
from pathlib import Path
import tempfile


def on_vercel() -> bool:
    return os.environ.get("VERCEL") == "1"


def data_directory(root: Path) -> Path:
    if on_vercel():
        # Only replaceable market caches live here; results use private Blob.
        return Path(tempfile.gettempdir()) / "lazy-stock-picker"
    return root / "data"


def allowed_hosts() -> list[str]:
    hosts = ["localhost", "127.0.0.1"]
    if on_vercel():
        for key in ("VERCEL_URL", "VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_BRANCH_URL"):
            if value := os.environ.get(key, "").strip():
                hosts.append(value)
    hosts.extend(value.strip() for value in os.environ.get("LAZY_PICKER_ALLOWED_HOSTS", "").split(",")
                 if value.strip())
    return list(dict.fromkeys(hosts))
