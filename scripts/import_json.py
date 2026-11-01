"""Import a local JSON source file through the authenticated NODUS API."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import quote
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from dotenv import load_dotenv


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    parser = argparse.ArgumentParser()
    parser.add_argument("file", type=Path)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--api-url", default=os.getenv("GRAPH_HACKS_API_URL", "http://127.0.0.1:8000"))
    args = parser.parse_args()
    token = os.getenv("GRAPH_HACKS_API_TOKEN")
    if not token:
        parser.error("Set GRAPH_HACKS_API_TOKEN in the process environment")
    document = json.loads(args.file.read_text(encoding="utf-8"))
    url = f"{args.api_url.rstrip('/')}/api/v1/workspaces/{quote(args.workspace, safe='')}/imports/json"
    request = Request(url, data=json.dumps(document).encode("utf-8"), method="POST",
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=60) as response:
            print(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise SystemExit(f"Import failed ({exc.code}): {exc.read().decode('utf-8')}") from exc
    except URLError as exc:
        raise SystemExit(f"Import failed: {exc.reason}") from exc


if __name__ == "__main__":
    main()
