from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def main() -> None:
    import uvicorn

    host = os.getenv("TAXHARNESS_ENTERPRISE_HOST", "127.0.0.1")
    port = int(os.getenv("TAXHARNESS_ENTERPRISE_PORT", "8011"))
    uvicorn.run("taxharness.enterprise.web:app", host=host, port=port, reload=False, access_log=True)


if __name__ == "__main__":
    main()
