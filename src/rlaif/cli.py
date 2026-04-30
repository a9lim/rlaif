"""Unified command-line interface for rlaif.

Subcommands:

* ``serve`` — start the MCP server (stdio). This is what MCP clients invoke.
* ``init`` — interactive first-run setup wizard.
* ``doctor`` — read-only health check against the configured device.
* ``dry-run`` — exercise every tool against a mock device; exits nonzero if
  a safety invariant is violated.
* ``live-smoke`` — fire one real minimum-intensity shock (confirmation prompt).
* ``snippet CLIENT`` — emit an MCP client config snippet.
* ``install CLIENT`` — write rlaif into a supported MCP client config file
  (claude-desktop, claude-code, cursor, windsurf, antigravity, opencode,
  codex, hermes). For vscode and zed (JSONC inside multi-purpose
  settings), exits nonzero with a hint to use ``snippet`` and paste
  manually.
* ``uninstall CLIENT`` — remove rlaif from the same eight supported configs.

The ``rlaif`` console script is wired to :func:`main` via ``[project.scripts]``.
"""

from __future__ import annotations

import argparse

from rlaif import __version__
from rlaif.snippet import CLIENTS


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rlaif",
        description="PiShock MCP server.",
    )
    parser.add_argument(
        "--version", action="version", version=f"rlaif {__version__}"
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    sub.add_parser("serve", help="start the MCP server over stdio")
    sub.add_parser("init", help="interactive first-run setup")
    sub.add_parser("doctor", help="read-only health check")
    sub.add_parser(
        "dry-run", help="mock-device integration test; nonzero on violation"
    )
    sub.add_parser("live-smoke", help="fire one real minimum-intensity shock")

    log_p = sub.add_parser("log", help="tail the on-disk ops log")
    log_p.add_argument(
        "--tail",
        type=int,
        default=10,
        metavar="N",
        help="show the last N entries (default 10; pass 0 for all)",
    )
    log_p.add_argument(
        "--raw",
        action="store_true",
        help="print each line as stored (one JSON object per line, no pretty-print)",
    )
    log_p.add_argument(
        "--stats",
        action="store_true",
        help="print rolling histograms (intensity buckets, refusal reasons, "
        "hourly volume) instead of tailing entries",
    )

    snip = sub.add_parser("snippet", help="print an MCP client config snippet")
    snip.add_argument(
        "client",
        choices=list(CLIENTS),
        help="MCP client to emit a config snippet for",
    )
    snip.add_argument(
        "--dev-path",
        metavar="PATH",
        default=None,
        help="emit a `uv run --directory PATH` snippet for running from a source checkout",
    )

    inst = sub.add_parser(
        "install",
        help="write rlaif into a supported MCP client config (8 clients)",
    )
    inst.add_argument(
        "client",
        choices=list(CLIENTS),
        help="MCP client to install into",
    )
    inst.add_argument(
        "--dev-path",
        metavar="PATH",
        default=None,
        help="install a `uv run --directory PATH` variant (source checkout)",
    )
    inst.add_argument(
        "--dry-run",
        action="store_true",
        help="print the would-be config without writing",
    )
    inst.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing differing rlaif entry",
    )

    uninst = sub.add_parser(
        "uninstall",
        help="remove rlaif from a supported MCP client config (8 clients)",
    )
    uninst.add_argument(
        "client",
        choices=list(CLIENTS),
        help="MCP client to remove rlaif from",
    )
    uninst.add_argument(
        "--dry-run",
        action="store_true",
        help="print the would-be config without writing",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "serve":
        from rlaif.server import main as serve_main
        return serve_main()
    if args.command == "init":
        from rlaif.init import run as init_run
        return init_run()
    if args.command == "doctor":
        from rlaif.doctor import run as doctor_run
        return doctor_run()
    if args.command == "dry-run":
        from rlaif.dry_run import run as dry_run_run
        return dry_run_run()
    if args.command == "live-smoke":
        from rlaif.live_smoke import run as live_smoke_run
        return live_smoke_run()
    if args.command == "log":
        from rlaif.log import run as log_run
        return log_run(tail=args.tail, raw=args.raw, stats=args.stats)
    if args.command == "snippet":
        from rlaif.snippet import run as snippet_run
        return snippet_run(client=args.client, dev_path=args.dev_path)
    if args.command == "install":
        from rlaif.installer import install
        return install(
            args.client,
            dev_path=args.dev_path,
            dry_run=args.dry_run,
            force=args.force,
        )
    if args.command == "uninstall":
        from rlaif.installer import uninstall
        return uninstall(args.client, dry_run=args.dry_run)

    parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
