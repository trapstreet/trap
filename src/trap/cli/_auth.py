from __future__ import annotations

import sys
from typing import Annotated

import typer

from trap.auth import (
    DEFAULT_SERVER,
    ApiClient,
    ApiError,
    BrowserProvider,
    Credential,
    CredentialStore,
    CredentialStoreError,
    ResolvedAuth,
    TokenProvider,
)
from trap.cli._console import _die, console, err_console

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

    By default opens a browser to complete OAuth. Pass --with-token to supply an
    api_key directly (useful for CI or headless environments).

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

    # Pairing is where the account behind the token is established. The id is
    # stored beside the token so that every later run can freeze its owner without
    # a network call; a token the server refuses outright is not stored at all.
    try:
        auth_data = auth_data.model_copy(update={"user_id": _verified_user_id(auth_data)})
    except ApiError as e:
        raise _die(f"{e} — nothing was saved") from None

    try:
        path = CredentialStore().save(auth_data)
    except CredentialStoreError as e:
        raise _die(e) from None
    console.print(
        f"[green]✓ logged in[/green] to {auth_data.server}"
        + (f" · account [bold]{auth_data.account}[/bold]" if auth_data.account else "")
        + f" · token saved to {path}"
    )
    if auth_data.user_id is None:
        err_console.print(
            "[yellow]the server did not confirm which account this token belongs to; runs "
            "tracked with it have no frozen owner until [bold]tp auth status[/bold] can verify "
            "it — `tp sync` will then need --claim for those runs.[/yellow]"
        )


def _verified_user_id(credential: Credential) -> str | None:
    """Ask the server who this token is, at pairing time.

    A refused token (401) raises so the login fails visibly. An unreachable server
    or a server error does not: the token may well be good, and refusing to pair
    offline would make CI setups that pre-provision a token impossible. Those
    return None, which the caller reports once."""
    try:
        return ApiClient(credential.server, credential.api_key).verified_user_id()
    except ApiError as e:
        if e.status == 401:
            raise
        err_console.print(f"[yellow]could not verify the token with {credential.server}: {e}[/yellow]")
        return None


@auth_app.command("logout")
def auth_logout(
    server: Annotated[
        str | None,
        typer.Option("--server", envvar="TRAPSTREET_URL", help="Which server's token to remove."),
    ] = None,
) -> None:
    """Delete the stored api_key for one server (other servers keep theirs)."""
    target = server or DEFAULT_SERVER
    try:
        removed = CredentialStore().delete(target)
    except CredentialStoreError as e:
        raise _die(e) from None
    if not removed:
        console.print(f"already logged out — no token stored for {target}")
        return
    console.print(f"[green]✓[/green] removed the {target} credential")


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
    """Show the authentication in effect (env overrides applied) — exactly the
    server/token pair `tp submit` would use. --server inspects another credential."""
    store = CredentialStore()
    try:
        resolved = ResolvedAuth.resolve(store, server_override=server)
    except CredentialStoreError as e:
        raise _die(e) from None
    if resolved.api_key is None:
        hint = "" if resolved.server == DEFAULT_SERVER else f" --server {resolved.server}"
        console.print(
            f"[red]not logged in[/red] to {resolved.server}. "
            f"Run [bold]tp auth login{hint}[/bold] or set [bold]TRAPSTREET_API_KEY[/bold]."
        )
        if store.servers():
            console.print(f"  stored credentials: {', '.join(store.servers())}")
        raise typer.Exit(code=1)

    console.print(f"  server    {resolved.server} [dim]({resolved.server_source})[/dim]")
    console.print(f"  token     [dim]({resolved.api_key_source})[/dim]")

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
    _refresh_stored_user_id(store, resolved, user.get("id"))


def _refresh_stored_user_id(store: CredentialStore, resolved: ResolvedAuth, user_id: object) -> None:
    """Record a freshly verified account id beside the stored token.

    This is how a pairing made by an older CLI — which stored only the token —
    acquires the frozen owner that live sync needs. Only the stored credential is
    touched, only when the id is new, and a write failure is not an error: status
    was asked to report, and it has."""
    if resolved.api_key_source != "stored" or not isinstance(user_id, str) or user_id == resolved.user_id:
        return
    try:
        stored = store.load(resolved.server)
        if stored is not None:
            store.save(stored.model_copy(update={"user_id": user_id}))
    except (CredentialStoreError, OSError):
        return
