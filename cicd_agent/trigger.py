"""
Trigger — Event-based pipeline invocation.
==========================================
Manages triggers that start pipeline runs: webhooks, schedules,
manual invocations, file-watching, and commit-message patterns.
"""

from __future__ import annotations

import re
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional


class TriggerType(Enum):
    """Types of pipeline triggers."""
    MANUAL = "manual"
    WEBHOOK = "webhook"
    SCHEDULE = "schedule"
    FILE_WATCH = "file_watch"
    COMMIT_PATTERN = "commit_pattern"
    DEPENDENCY = "dependency"


@dataclass
class TriggerEvent:
    """An event that can trigger a pipeline run.

    Attributes:
        trigger_type: The kind of trigger.
        source: Origin identifier (repo name, schedule name, etc.).
        payload: Arbitrary data associated with the event.
        commit_sha: Optional git commit SHA.
        commit_message: Optional commit message.
        branch: Git branch name.
        timestamp: When the event was created.
    """

    trigger_type: TriggerType
    source: str = ""
    payload: dict = field(default_factory=dict)
    commit_sha: str = ""
    commit_message: str = ""
    branch: str = "main"
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "trigger_type": self.trigger_type.value,
            "source": self.source,
            "payload": self.payload,
            "commit_sha": self.commit_sha,
            "commit_message": self.commit_message,
            "branch": self.branch,
            "timestamp": self.timestamp,
        }


# Type alias for the callback invoked when a trigger fires
TriggerCallback = Callable[[TriggerEvent], None]


@dataclass
class _TriggerRule:
    """Internal representation of a registered trigger rule."""
    name: str
    trigger_type: TriggerType
    pattern: str  # regex or cron expression or glob
    callback: TriggerCallback
    enabled: bool = True


class TriggerManager:
    """Register and evaluate triggers that start pipeline runs.

    Supports multiple trigger types:
    - **manual**: Directly invoked via ``fire()``.
    - **webhook**: Fired by ``fire(TriggerType.WEBHOOK, ...)``.
    - **schedule**: Simple interval-based polling.
    - **commit_pattern**: Regex match on commit messages.
    - **file_watch**: Placeholder for extension.
    - **dependency**: Fired when another pipeline completes.

    Usage::

        triggers = TriggerManager()
        triggers.register("on-push", TriggerType.WEBHOOK, source="my-repo", callback=run_pipeline)
        triggers.register("deploy-tag", TriggerType.COMMIT_PATTERN, pattern=r"\[deploy\]", callback=deploy)
        triggers.fire(TriggerType.WEBHOOK, source="my-repo", commit_message="fix: bug [deploy]")
    """

    def __init__(self):
        self._rules: dict[str, _TriggerRule] = {}
        self._history: list[TriggerEvent] = []
        self._timers: dict[str, threading.Thread] = {}
        self._running = False

    def register(
        self,
        name: str,
        trigger_type: TriggerType,
        callback: TriggerCallback,
        source: str = "",
        pattern: str = "",
        interval: float = 0.0,
        enabled: bool = True,
    ) -> None:
        """Register a new trigger rule."""
        self._rules[name] = _TriggerRule(
            name=name,
            trigger_type=trigger_type,
            pattern=pattern,
            callback=callback,
            enabled=enabled,
        )
        if trigger_type == TriggerType.SCHEDULE and interval > 0:
            self._start_schedule(name, interval)

    def unregister(self, name: str) -> bool:
        """Remove a trigger rule."""
        rule = self._rules.pop(name, None)
        return rule is not None

    def enable(self, name: str) -> bool:
        """Enable a trigger rule."""
        rule = self._rules.get(name)
        if rule:
            rule.enabled = True
            return True
        return False

    def disable(self, name: str) -> bool:
        """Disable a trigger rule."""
        rule = self._rules.get(name)
        if rule:
            rule.enabled = False
            return True
        return False

    def fire(
        self,
        trigger_type: TriggerType,
        source: str = "",
        commit_sha: str = "",
        commit_message: str = "",
        branch: str = "main",
        payload: Optional[dict] = None,
    ) -> list[str]:
        """Fire an event and invoke all matching trigger callbacks.

        Returns list of rule names that were triggered.
        """
        event = TriggerEvent(
            trigger_type=trigger_type,
            source=source,
            payload=payload or {},
            commit_sha=commit_sha,
            commit_message=commit_message,
            branch=branch,
        )
        self._history.append(event)

        triggered: list[str] = []
        for name, rule in self._rules.items():
            if not rule.enabled:
                continue
            if rule.trigger_type != trigger_type:
                continue
            if not self._matches(rule, event):
                continue
            try:
                rule.callback(event)
                triggered.append(name)
            except Exception:
                pass  # Swallow callback errors; caller should handle in callback

        return triggered

    def get_history(self, limit: int = 50) -> list[dict]:
        """Return recent trigger events."""
        return [e.to_dict() for e in self._history[-limit:]]

    def list_rules(self) -> list[dict]:
        """Return all registered trigger rules."""
        return [
            {
                "name": r.name,
                "trigger_type": r.trigger_type.value,
                "pattern": r.pattern,
                "enabled": r.enabled,
            }
            for r in self._rules.values()
        ]

    def stop_all(self) -> None:
        """Stop all scheduled triggers."""
        self._running = False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _matches(self, rule: _TriggerRule, event: TriggerEvent) -> bool:
        """Check if a rule matches an event."""
        # Source filter
        if rule.pattern and rule.trigger_type == TriggerType.COMMIT_PATTERN:
            return bool(re.search(rule.pattern, event.commit_message or ""))
        return True

    def _start_schedule(self, name: str, interval: float) -> None:
        """Start a simple interval-based schedule."""
        self._running = True
        rule = self._rules[name]

        def loop():
            while self._running and rule.enabled:
                time.sleep(interval)
                if not self._running or not rule.enabled:
                    break
                try:
                    rule.callback(TriggerEvent(
                        trigger_type=TriggerType.SCHEDULE,
                        source=name,
                    ))
                except Exception:
                    pass

        t = threading.Thread(target=loop, daemon=True, name=f"trigger-{name}")
        t.start()
        self._timers[name] = t
