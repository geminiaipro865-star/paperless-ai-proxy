"""CLI: chatgpt-proxy {login,status,logout,import-codex,serve}."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
from pathlib import Path

from .auth import AuthError
from .auth import AuthManager
from .auth import TokenStore
from .auth import import_codex_auth
from .auth import run_device_login
from .config import Settings


def _print_prompt(url: str, code: str) -> None:
    print(
        "\nSign in to ChatGPT to authorise this proxy:\n"
        f"\n  1. Open {url}\n"
        f"  2. Enter the code: {code}\n"
        "\nThe code expires in 15 minutes. Waiting for approval...\n",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chatgpt-proxy")
    parser.add_argument(
        "--token-file",
        type=Path,
        help="override CHATGPT_TOKEN_FILE",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="sign in with ChatGPT via device code")
    sub.add_parser("status", help="show the stored login")
    sub.add_parser("logout", help="delete the stored tokens")

    importer = sub.add_parser("import-codex", help="adopt an existing Codex CLI login")
    importer.add_argument(
        "--from",
        dest="source",
        type=Path,
        default=Path("~/.codex/auth.json").expanduser(),
    )

    serve = sub.add_parser("serve", help="run the HTTP proxy")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = Settings.from_env()
    if args.token_file:
        settings = dataclasses.replace(settings, token_file=args.token_file.expanduser())
    token_file = settings.token_file
    store = TokenStore(token_file)

    try:
        if args.command == "login":
            tokens = asyncio.run(run_device_login(store, on_prompt=_print_prompt))
            print(
                f"Signed in as {tokens.get('email') or 'unknown'} "
                f"(plan: {tokens.get('plan_type') or 'unknown'}). Tokens stored in {token_file}.",
            )
            return 0

        if args.command == "status":
            print(json.dumps(AuthManager(store).status(), indent=2))
            return 0

        if args.command == "logout":
            store.clear()
            print(f"Removed {token_file}.")
            return 0

        if args.command == "import-codex":
            tokens = import_codex_auth(args.source, store)
            print(
                f"Imported login for {tokens.get('email') or 'unknown'} from {args.source}.\n"
                "Note: both copies now share one refresh token. The first side to refresh "
                "invalidates the other -- sign in again there if Codex stops working.",
            )
            return 0

        if args.command == "serve":
            import uvicorn

            from .server import create_app

            uvicorn.run(
                create_app(settings),
                host=args.host or settings.host,
                port=args.port or settings.port,
                log_level="info",
            )
            return 0

    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
