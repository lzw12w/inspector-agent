"""CLI entry — interactive REPL + one-shot subcommands.

Usage:
    ios-inspector-agent chat              # start REPL
    ios-inspector-agent chat -m "..."     # one-shot
    ios-inspector-agent doctor            # connectivity check
    ios-inspector-agent tools             # list available tools
"""
from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .actions import list_tool_schemas
from .agent import Agent, AgentConfig
from .config import Config
from .core import InspectorClient, InspectorError
from .llm import make_llm
from .session import InspectorSession, make_run_workdir
from .trace import Recorder


def _build_session(cfg: Config, console: Console):
    client = InspectorClient(
        host=cfg.inspector_host,
        port=cfg.inspector_port,
        timeout=cfg.inspector_timeout,
    )
    workdir = make_run_workdir(cfg.workdir_root)
    console.print(f"[dim]workdir: {workdir}[/dim]")
    return client, workdir


def _confirm_fn(console: Console):
    """Block on stdin until the user types y / n. Used for high-risk tools."""
    def _ask(tc):
        console.print(
            Panel(
                f"[yellow]Confirm action[/yellow]: [bold]{tc.name}[/bold]\n"
                f"args: {json.dumps(tc.arguments, ensure_ascii=False, indent=2)}",
                border_style="yellow",
            )
        )
        try:
            answer = input("allow? [y/N] ").strip().lower()
        except EOFError:
            return False
        return answer in ("y", "yes")
    return _ask


def cmd_doctor(args, cfg, console):
    client = InspectorClient(host=cfg.inspector_host, port=cfg.inspector_port,
                             timeout=cfg.inspector_timeout)
    console.print(f"[bold]ping[/bold] {client.base_url}/api/ping ...")
    try:
        result = client.ping()
        console.print(Panel(json.dumps(result, indent=2, ensure_ascii=False),
                            title="ping ok", border_style="green"))
    except InspectorError as e:
        console.print(Panel(json.dumps(e.to_dict(), indent=2, ensure_ascii=False),
                            title="ping failed", border_style="red"))
        return 1
    if cfg.llm_provider == "anthropic" and not cfg.anthropic_api_key:
        console.print("[yellow]warn:[/yellow] ANTHROPIC_API_KEY is not set; chat will fail.")
    return 0


def cmd_tools(args, cfg, console):
    schemas = list_tool_schemas()
    table = Table(title="Available agent tools")
    table.add_column("name", style="cyan")
    table.add_column("description")
    for s in schemas:
        table.add_row(s["name"], (s["description"] or "")[:80])
    console.print(table)
    return 0


def cmd_chat(args, cfg, console):
    if cfg.llm_provider == "anthropic" and not cfg.anthropic_api_key:
        console.print("[red]ANTHROPIC_API_KEY not set.[/red] export it or use --provider scripted.")
        return 2

    try:
        client, workdir = _build_session(cfg, console)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return 2

    llm = make_llm(cfg.llm_provider,
                   model=cfg.llm_model,
                   api_key=cfg.anthropic_api_key,
                   base_url=cfg.anthropic_base_url)

    with InspectorSession(client, workdir) as session, Recorder(workdir) as recorder:
        agent = Agent(
            llm=llm, session=session, recorder=recorder,
            config=AgentConfig(confirm_for=cfg.confirm_for),
            confirm_fn=_confirm_fn(console) if not args.yes else None,
            console=console,
        )

        if args.message:
            agent.chat(args.message)
            return 0

        console.print(Panel.fit(
            "[bold]iOS Inspector Agent[/bold]\n"
            "type your request; commands: /reset /trace /workdir /quit",
            border_style="cyan",
        ))
        while True:
            try:
                user_text = console.input("[bold cyan]you »[/bold cyan] ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if not user_text:
                continue
            if user_text in ("/quit", "/exit", ":q"):
                break
            if user_text == "/reset":
                agent.reset()
                console.print("[dim]conversation reset[/dim]")
                continue
            if user_text == "/trace":
                console.print(f"[dim]trace: {recorder.path}[/dim]")
                continue
            if user_text == "/workdir":
                console.print(f"[dim]workdir: {workdir}[/dim]")
                continue
            if user_text == "/help":
                console.print("/reset  /trace  /workdir  /quit")
                continue

            try:
                agent.chat(user_text)
            except KeyboardInterrupt:
                console.print("[yellow](interrupted)[/yellow]")
                continue
            except Exception as e:
                console.print(f"[red]agent error:[/red] {e}")
                continue
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ios-inspector-agent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("chat", help="start interactive agent (or -m for one-shot)")
    pc.add_argument("-m", "--message", help="one-shot message; skip REPL")
    pc.add_argument("-y", "--yes", action="store_true",
                    help="auto-confirm all tool calls (dangerous)")

    sub.add_parser("doctor", help="check inspector connectivity")
    sub.add_parser("tools", help="list available tools")

    args = parser.parse_args(argv)
    cfg = Config.load()
    console = Console()

    handlers = {
        "chat": cmd_chat,
        "doctor": cmd_doctor,
        "tools": cmd_tools,
    }
    return handlers[args.cmd](args, cfg, console)


if __name__ == "__main__":
    sys.exit(main())
