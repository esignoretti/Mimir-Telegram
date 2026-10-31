"""Slash commands.

Kept free of task state on purpose: ``/stop`` only *reports* that the user asked
to stop, and the dispatcher does the cancelling. That keeps this module testable
without an event loop full of in-flight turns.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import EFFORTS, MODELS, Config
from .store import Store

HELP = (
    "commands:\n"
    "/model [name] — show or switch the model\n"
    "/thinking [level] — show or switch reasoning effort\n"
    "/new — forget this conversation\n"
    "/stop — cancel the answer being generated\n"
    "/help — this list\n"
    "\n"
    "Anything else is sent to the model. Send a photo to ask about it."
)

STOP_MARKER = "stopped"


@dataclass(frozen=True)
class CommandResult:
    text: str
    handled: bool = True


def _short(model: str) -> str:
    return model.rsplit("/", 1)[-1]


def resolve_model(arg: str, models: dict[str, int]) -> str | None:
    arg = arg.strip().lower()
    if not arg:
        return None
    if arg in models:
        return arg

    # An exact short name wins over a prefix: "mimir" means vllm/mimir even
    # though three other ids start with it.
    exact = [model for model in models if _short(model) == arg]
    if len(exact) == 1:
        return exact[0]

    matches = [
        model
        for model in models
        if _short(model).startswith(arg) or _short(model).endswith(f"-{arg}")
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def models_menu(current: str, models: dict[str, int]) -> str:
    width = max(len(m) for m in models)
    lines = ["models (use /model <name>):"]
    for model, max_len in models.items():
        marker = " ←" if model == current else ""
        lines.append(f"  {model:<{width}}  {max_len:>7} tokens{marker}")
    return "\n".join(lines)


def efforts_menu(current: str) -> str:
    options = " · ".join(
        f"[{e}]" if e == current else e for e in EFFORTS
    )
    return f"thinking effort: {options}\nuse /thinking <level>"


async def handle_command(
    text: str, chat_id: int, store: Store, cfg: Config
) -> CommandResult | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None

    # Telegram appends @botname to commands in groups.
    head, _, rest = stripped.partition(" ")
    command = head[1:].split("@", 1)[0].lower()
    arg = rest.strip()

    if command == "model":
        settings = await store.get_chat(chat_id)
        if not arg:
            return CommandResult(models_menu(settings.model, MODELS))
        resolved = resolve_model(arg, MODELS)
        if resolved is None:
            return CommandResult(
                f"unknown model {arg!r}\n\n" + models_menu(settings.model, MODELS)
            )
        await store.set_model(chat_id, resolved)
        return CommandResult(f"model → {resolved}")

    if command == "thinking":
        settings = await store.get_chat(chat_id)
        if not arg:
            return CommandResult(efforts_menu(settings.effort))
        level = arg.lower()
        if level not in EFFORTS:
            return CommandResult(
                f"unknown effort {arg!r}\n\n" + efforts_menu(settings.effort)
            )
        await store.set_effort(chat_id, level)
        return CommandResult(f"thinking → {level}")

    if command == "new":
        await store.reset_chat(chat_id)
        return CommandResult("new conversation started; history cleared")

    if command == "stop":
        return CommandResult(STOP_MARKER)

    if command in ("help", "start"):
        return CommandResult(HELP)

    return CommandResult(f"unknown command /{command}\n\n{HELP}")
