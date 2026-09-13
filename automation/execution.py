"""Guarded execution requests and their explicit lifecycle.

An execution request is an immutable record.  The state machine owns the
mutable collection of requests so an execution key is reserved as soon as a
request is created; a later request with the same key is rejected rather than
being silently treated as a rerun.

The lifecycle is intentionally narrow::

    requested -> approved -> executing -> succeeded
                                      -> failed

Only a non-blank human decision reference can move a request to ``approved``.
The Decision Service will validate that reference when it exists (Task 30);
this module keeps that future seam without importing it.

``failed`` is terminal for one ATTEMPT, not for the work. The execution key is
derived from the task, recipe and input, so it is the idempotency key of the
work itself -- repeating a success must be refused. A failure is a different
question: the same input has to be runnable again once whatever broke is
fixed. ``attempt`` separates the two. The key stays stable across attempts so
approval bindings and confirmations keep pointing at the same work, while the
attempt number distinguishes the runs and keeps each one's artifacts apart.
"""

from collections.abc import Collection
from dataclasses import dataclass, replace
from typing import Any

from automation import identity



_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "requested": ("approved",),
    "approved": ("executing",),
    "executing": ("succeeded", "failed"),
    "succeeded": (),
    "failed": (),
}

_APPROVAL_REQUIRED_TARGETS = frozenset({"approved"})


class ExecutionError(ValueError):
    """Base class for execution request validation failures."""


class DuplicateExecutionError(ExecutionError):
    """Raised when an execution key has already been reserved."""


class ExecutionNotFoundError(ExecutionError):
    """Raised when a state-machine transition references an unknown key."""


@dataclass(frozen=True)
class ExecutionRequest:
    """Immutable request for one execution attempt and result version."""

    STATES = tuple(_TRANSITIONS)

    task_id: str
    recipe_id: str
    input_hash: str
    execution_key: str | None = None
    state: str = "requested"
    approval: str | None = None
    attempt: int = 1
    result_version: int = 1
    rebuild_request_id: str | None = None

    def __post_init__(self) -> None:
        # Runtime-scoped recipes are validated by the Core service after it
        # resolves the scope's immutable registry. This DTO only owns identity
        # and lifecycle invariants, so it must not consult the shipped registry.
        derived_key = identity.execution_key(self.task_id, self.recipe_id, self.input_hash)
        if self.rebuild_request_id is not None:
            derived_key = identity.rebuild_execution_key(
                self.task_id, self.recipe_id, self.input_hash, self.rebuild_request_id
            )
        if self.execution_key is None:
            object.__setattr__(self, "execution_key", derived_key)
        elif self.execution_key != derived_key:
            raise ValueError("execution_key does not match request identity")
        if self.state not in self.STATES:
            raise ValueError(f"state must be one of {self.STATES}")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 1:
            raise ValueError("attempt must be a positive integer")
        if isinstance(self.result_version, bool) or not isinstance(self.result_version, int) or self.result_version < 1:
            raise ValueError("result_version must be a positive integer")
        if self.state == "requested" and self.approval is not None:
            raise ValueError("requested execution cannot have approval")
        if self.rebuild_request_id is None and self.result_version != 1:
            raise ValueError("non-rebuild execution must use result_version 1")
        if self.rebuild_request_id is not None and self.result_version <= 1:
            raise ValueError("rebuild execution must use a later result_version")
        if self.state != "requested" and not (isinstance(self.approval, str) and self.approval.strip()):
            raise ValueError("approved or later execution states require approval")

    @classmethod
    def create(
        cls, task_id: str, recipe_id: str, input_hash: str, *,
        existing_execution_keys: Collection[str] = (), attempt: int = 1,
        result_version: int = 1, rebuild_request_id: str | None = None,
    ) -> "ExecutionRequest":
        request = cls(task_id, recipe_id, input_hash, attempt=attempt,
                      result_version=result_version, rebuild_request_id=rebuild_request_id)
        if attempt == 1 and request.execution_key in existing_execution_keys:
            raise DuplicateExecutionError(f"duplicate execution key: {request.execution_key}")
        return request

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionRequest":
        if not isinstance(data, dict):
            raise ValueError("execution request must be an object")
        required = ("task_id", "recipe_id", "input_hash", "execution_key", "state")
        missing = [key for key in required if key not in data]
        if missing:
            raise ValueError(f"{missing[0]} is required")
        return cls(task_id=data["task_id"], recipe_id=data["recipe_id"], input_hash=data["input_hash"],
                   execution_key=data["execution_key"], state=data["state"], approval=data.get("approval"),
                   attempt=data.get("attempt", 1), result_version=data.get("result_version", 1),
                   rebuild_request_id=data.get("rebuild_request_id"))

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": self.task_id,
            "recipe_id": self.recipe_id,
            "input_hash": self.input_hash,
            "execution_key": self.execution_key,
            "state": self.state,
            "attempt": self.attempt,
            "result_version": self.result_version,
        }
        if self.approval is not None:
            payload["approval"] = self.approval
        if self.rebuild_request_id is not None:
            payload["rebuild_request_id"] = self.rebuild_request_id
        return payload

    def transition(
        self, to_state: str, *, approval: str | None = None
    ) -> "ExecutionRequest":
        """Return a new request in ``to_state`` when the edge is permitted."""
        if to_state not in self.STATES:
            raise ValueError(f"state must be one of {self.STATES}")

        allowed = _TRANSITIONS.get(self.state, ())
        if to_state not in allowed:
            raise ValueError(
                f"illegal transition from {self.state!r} to {to_state!r}"
            )

        if to_state in _APPROVAL_REQUIRED_TARGETS:
            # TODO(Task 30): validate approval against a real Decision record.
            if not isinstance(approval, str) or not approval.strip():
                raise ValueError(
                    f"transition to {to_state!r} requires a human approval reference"
                )

        next_approval = approval if to_state == "approved" else self.approval
        return replace(self, state=to_state, approval=next_approval)


class ExecutionStateMachine:
    """Reserve execution keys and apply the guarded request transitions."""

    def __init__(self, *, existing_execution_keys: Collection[str] = ()) -> None:
        self._executions: dict[str, ExecutionRequest] = {}
        self._reserved_keys = set(existing_execution_keys)

    def request(
        self, task_id: str, recipe_id: str, input_hash: str, *, attempt: int = 1,
        result_version: int = 1, rebuild_request_id: str | None = None,
    ) -> ExecutionRequest:
        request = ExecutionRequest.create(task_id, recipe_id, input_hash,
            existing_execution_keys=self._reserved_keys, attempt=attempt,
            result_version=result_version, rebuild_request_id=rebuild_request_id)
        assert request.execution_key is not None
        self._executions[request.execution_key] = request
        self._reserved_keys.add(request.execution_key)
        return request

    def transition(
        self,
        execution_key: str,
        to_state: str,
        *,
        approval: str | None = None,
    ) -> ExecutionRequest:
        try:
            request = self._executions[execution_key]
        except KeyError as exc:
            raise ExecutionNotFoundError(
                f"unknown execution key: {execution_key}"
            ) from exc
        updated = request.transition(to_state, approval=approval)
        self._executions[execution_key] = updated
        return updated

    def get(self, execution_key: str) -> ExecutionRequest:
        try:
            return self._executions[execution_key]
        except KeyError as exc:
            raise ExecutionNotFoundError(
                f"unknown execution key: {execution_key}"
            ) from exc

    def __len__(self) -> int:
        return len(self._executions)
