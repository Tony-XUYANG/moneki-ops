from __future__ import annotations

import argparse

from app.config import Settings, settings
from app.importer import ensure_imported
from app.server import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="Moneki operations intelligence")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--import-only", action="store_true")
    parser.add_argument("--force-import", action="store_true")
    args = parser.parse_args()
    configured = Settings(host=args.host, port=args.port)
    if args.import_only:
        result = ensure_imported(configured.data_dir, configured.database_path, args.force_import)
        print(result)
        return
    if args.force_import:
        ensure_imported(configured.data_dir, configured.database_path, True)
    serve(configured)


if __name__ == "__main__":
    main()

