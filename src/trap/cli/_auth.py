from __future__ import annotations

import sys
from typing import Annotated

import typer

from trap.auth import (
    DEFAULT_SERVER,
    ApiClient,
    ApiError,
    AuthMismatchError,
    AuthStore,
    BrowserProvider,
    TokenProvider,
    resolve_auth,
)
from trap.cli._console import _die, console

auth_app = typer.Typer(help="Manage authentication.")


@auth_app.command("login")
def auth_login(
    server: Annotated[
        str | None,
        typer.Option("--server", envvar="TRAPSTREET_URL", help="Trapstreet server URL."),
    ] = None,
    timeout: Annotated[int, typer.Option("--timeout", help="Seconds to wait for browser approval.")] = 300,
    with_token: Annotated[
        bool,
        typer.Option("--with-token", help="Read api_key from stdin instead of opening a browser."),
    ] = False,
) -> None:
    """Authenticate this machine with Trapstreet.

    By default opens a browser to complete OAuth. Pass --with-token to supply
    an api_key directly (useful for CI or headless environments).

    Tokens are stored per server in ~/.config/trapstreet/auth.json (mode 600):
    logging in to one server leaves every other server's pairing untouched.
    """
    resolved_server = server or DEFAULT_SERVER

    # BrowserProvider raises ValueError for a non-default server (the same check the
    # CLI used to duplicate), so provider construction sits inside the try alongside
    # acquire() — one error path for both.
    try:
        if with_token:
            if sys.stdin.isatty():  # pragma: no cover - interactive prompt, no TTY under tests
                token = typer.prompt("API key", hide_input=True)
            else:
                token = sys.stdin.read().strip()
            provider = TokenProvider(resolved_server, token)
        else:
            provider = BrowserProvider(resolved_server, timeout)
        console.print(provider.pre_message)
        auth_data = provider.acquire()
    except (ValueError, TimeoutError) as e:
        raise _die(e) from None

    path = AuthStore().save(auth_data)
    console.print(
        f"[green]✓ logged in[/green] to {auth_data.server}"
        + (f" · account [bold]{auth_data.account}[/bold]" if auth_data.account else "")
        + f" · token saved to {path}"
    )


@auth_app.command("logout")
def auth_logout(
    server: Annotated[
        str | None,
        typer.Option("--server", envvar="TRAPSTREET_URL", help="Which server's token to remove."),
    ] = None,
) -> None:
    """Delete the locally-stored api_key for one server (other servers keep theirs)."""
    target = server or DEFAULT_SERVER
    auth_store = AuthStore()
    if not auth_store.delete(target):
        console.print(f"already logged out — no token stored for {target}")
        return
    console.print(f"[green]✓[/green] removed the {target} profile from {auth_store.PATH}")


@auth_app.command("status")
def auth_status(
    server: Annotated[
        str | None,
        typer.Option("--server", envvar="TRAPSTREET_URL", help="Which server's pairing to show."),
    ] = None,
    verify: Annotated[
        bool,
        typer.Option("--verify/--no-verify", help="Ping server to verify token validity."),
    ] = True,
) -> None:
    """Show the authentication state in effect (env overrides applied) — exactly
    the server/token pair `tp submit` would use. --server inspects another profile."""
    auth_store = AuthStore()
    target = server or DEFAULT_SERVER
    stored = auth_store.load(target)
    resolved = resolve_auth(stored, server_override=server)
    if not resolved.api_key:
        hint = "" if target == DEFAULT_SERVER else f" --server {target}"
        console.print(
            f"[red]not logged in[/red] to {target}. "
            f"Run [bold]tp auth login{hint}[/bold] or set [bold]TRAPSTREET_API_KEY[/bold]."
        )
        if auth_store.servers():
            console.print(f"  stored profiles: {', '.join(auth_store.servers())}")
        raise typer.Exit(code=1)

    console.print(f"  server    {resolved.server} [dim]({resolved.server_source})[/dim]")
    console.print(f"  token     [dim]({resolved.api_key_source})[/dim]")
    if stored and stored.account and resolved.api_key_source == "stored":
        console.print(f"  account   [bold]{stored.account}[/bold]")
    others = [s for s in auth_store.servers() if s != resolved.server.rstrip("/")]
    if others:
        console.print(f"  profiles  [dim]{', '.join(others)} — select with --server[/dim]")

    try:
        resolved.ensure_paired()
    except AuthMismatchError as e:
        console.print(f"[red]error[/red]: {e}")
        raise typer.Exit(code=1) from None

    if not verify:
        return

    client = ApiClient(resolved.server, resolved.api_key)
    try:
        me = client.get_me()
    except ApiError as e:
        console.print(f"[red]error[/red]: {e}")
        raise typer.Exit(code=1) from None
    user = me.get("user") or {}
    identity = user.get("name") or user.get("email") or "(unknown)"
    console.print(f"  user      {identity}\n[green]✓ token is valid[/green]")
