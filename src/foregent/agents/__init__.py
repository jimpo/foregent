"""Agent harnesses.

:mod:`foregent.agents.base` defines the seam and
:mod:`foregent.agents.herdr_manager` drives every harness behind it, through
herdr. One module per harness — :mod:`foregent.agents.claude` — holds what is
that harness's own, and :mod:`foregent.agents.harness` maps a provider onto
it.
"""

from foregent.agents.base import (
    DEFAULT_PROVIDER,
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentManager,
    AgentRecord,
    AgentRef,
    AgentStatus,
    LaunchSpec,
    Provider,
    issue_key_from_label,
    label_for,
)

__all__ = [
    "DEFAULT_PROVIDER",
    "AgentError",
    "AgentEvent",
    "AgentEventKind",
    "AgentManager",
    "AgentRecord",
    "AgentRef",
    "AgentStatus",
    "LaunchSpec",
    "Provider",
    "issue_key_from_label",
    "label_for",
]
