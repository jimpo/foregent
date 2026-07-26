"""Agent harnesses (``docs/PLAN.md`` §5.13).

:mod:`foregent.agents.base` defines the seam; each other module in this
package drives one harness behind it.
"""

from foregent.agents.base import (
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentManager,
    AgentRecord,
    AgentRef,
    AgentStatus,
    LaunchSpec,
    issue_key_from_label,
    label_for,
)

__all__ = [
    "AgentError",
    "AgentEvent",
    "AgentEventKind",
    "AgentManager",
    "AgentRecord",
    "AgentRef",
    "AgentStatus",
    "LaunchSpec",
    "issue_key_from_label",
    "label_for",
]
