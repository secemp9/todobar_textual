from __future__ import annotations

import argparse

from .app import Statusbar2App


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="statusbar_textual",
        description="Textual port of Statusbar2 (Tauri + React).",
    )
    parser.add_argument(
        "--db",
        dest="db_path",
        default=None,
        help="Path to the SQLite DB file (defaults to an OS user-data location).",
    )
    args = parser.parse_args()

    Statusbar2App(db_path=args.db_path).run()


if __name__ == "__main__":
    main()
