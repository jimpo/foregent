"""Which harness a provider names, and how its agents are started.

Two things about running an agent depend on the harness: the agent kind herdr
detects the process as, and the arguments a :class:`LaunchSpec` renders to.
Both are looked up here, so :class:`~foregent.agents.herdr_manager.HerdrManager`
holds one copy of every socket call rather than one copy per harness — every
call it makes below ``launch`` is herdr's own and says nothing about what is
running in the pane.

The provider is also the herdr agent kind, which is what lets
:meth:`~foregent.agents.herdr_manager.HerdrManager.list_agents` name the
harness of an agent it did not start.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from foregent.agents import claude, codex
from foregent.agents.base import LaunchSpec, Provider


@dataclass(frozen=True, slots=True)
class Harness:
    """How one agent harness is started."""

    provider: Provider
    # herdr's name for the integration that detects this agent's state.
    kind: str
    # The command-line arguments the harness's binary is given. herdr supplies
    # the binary itself from the agent kind's manifest.
    render_args: Callable[[LaunchSpec], list[str]]
    # The opening message an agent is given, from an issue key and a mode.
    # Harness-specific because invoking a skill is: a slash command in one, a
    # sentence naming it in the other.
    brief: Callable[[str, str], str]


HARNESSES: dict[Provider, Harness] = {
    Provider.CLAUDE: Harness(
        Provider.CLAUDE, claude.KIND, claude.render_args, claude.brief
    ),
    Provider.CODEX: Harness(
        Provider.CODEX, codex.KIND, codex.render_args, codex.brief
    ),
}


def harness_for(provider: Provider) -> Harness:
    """How ``provider``'s agents are started.

    A provider with no harness is a programming error rather than an operator
    one: every value the CLI accepts is a member of the enum, and every member
    is registered above.
    """
    try:
        return HARNESSES[provider]
    except KeyError:
        raise ValueError(f"no harness is registered for {provider}") from None


def provider_for_kind(kind: str) -> Provider | None:
    """The provider herdr's agent ``kind`` names, or ``None`` for a stranger.

    ``None`` rather than a default: an agent running a harness foregent does
    not know is one no answer here describes, and the caller decides what an
    unattributable agent costs it.
    """
    return next(
        (harness.provider for harness in HARNESSES.values() if harness.kind == kind),
        None,
    )
