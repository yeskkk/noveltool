"""CLI: explicitly create or open exactly one local project."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError
import uvicorn

from . import __version__
from .app import create_app
from .db import ProjectError, ProjectStore


def port_number(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("端口必须是整数") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须在 1～65535 之间")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NovelTool 本地小说助手（本地项目、正文与版本管理）")
    parser.add_argument("--version", action="version", version=f"NovelTool {__version__}")
    parser.add_argument("--project", type=Path, required=True, help="SQLite 项目文件路径")
    parser.add_argument("--create", action="store_true", help="创建新项目；绝不覆盖现有文件")
    parser.add_argument("--title", help="新项目名称；省略时采用文件名")
    parser.add_argument("--init-only", action="store_true", help="仅创建数据库，不启动服务（需 --create）")
    parser.add_argument("--host", choices=("127.0.0.1", "::1"), default="127.0.0.1")
    parser.add_argument("--port", type=port_number, default=8765)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.title is not None and not args.create:
        parser.error("--title 只能与 --create 一起使用；已有项目请在页面改名")
    if args.init_only and not args.create:
        parser.error("--init-only 需要 --create")
    title = args.title if args.title is not None else args.project.stem
    if args.create:
        # Create before the server starts, so a path/validation error has a clear
        # CLI exit code. The web lifespan then opens the already validated file.
        try:
            with ProjectStore.create(args.project, title):
                pass
        except (ProjectError, OSError, ValidationError) as exc:
            parser.exit(2, f"无法创建项目：{exc}\n")
        print(f"已创建项目：{args.project.expanduser().resolve()}", flush=True)
    elif not args.project.expanduser().is_file():
        parser.error("项目文件不存在；首次创建请使用 --create")
    if args.init_only:
        return 0
    uvicorn.run(create_app(args.project), host=args.host, port=args.port, workers=1, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
