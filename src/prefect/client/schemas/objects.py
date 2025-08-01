from __future__ import annotations

import datetime
import warnings
from collections.abc import Callable, Mapping
from enum import Enum
from functools import partial
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Generic,
    Optional,
    Union,
    overload,
)
from uuid import UUID

import orjson
from pydantic import (
    AfterValidator,
    ConfigDict,
    Discriminator,
    Field,
    HttpUrl,
    IPvAnyNetwork,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    Tag,
    field_validator,
    model_serializer,
    model_validator,
)
from typing_extensions import Literal, Self, TypeVar

from prefect._internal.compatibility.async_dispatch import async_dispatch
from prefect._internal.compatibility.migration import getattr_migration
from prefect._internal.schemas.bases import (
    ObjectBaseModel,
    PrefectBaseModel,
    TimeSeriesBaseModel,
)
from prefect._internal.schemas.fields import CreatedBy, UpdatedBy
from prefect._internal.schemas.validators import (
    get_or_create_run_name,
    list_length_50_or_less,
    set_run_policy_deprecated_fields,
    validate_default_queue_id_not_none,
    validate_max_metadata_length,
    validate_name_present_on_nonanonymous_blocks,
    validate_not_negative,
    validate_parent_and_ref_diff,
)
from prefect._internal.uuid7 import uuid7
from prefect._result_records import ResultRecordMetadata
from prefect.client.schemas.schedules import SCHEDULE_TYPES
from prefect.settings import PREFECT_CLOUD_API_URL, PREFECT_CLOUD_UI_URL
from prefect.types import (
    MAX_VARIABLE_NAME_LENGTH,
    KeyValueLabelsField,
    Name,
    NonNegativeInteger,
    PositiveInteger,
    StrictVariableValue,
)
from prefect.types._datetime import DateTime, now
from prefect.types.names import (
    BlockDocumentName,
    raise_on_name_alphanumeric_dashes_only,
)
from prefect.utilities.asyncutils import run_coro_as_sync
from prefect.utilities.collections import AutoEnum, visit_collection
from prefect.utilities.names import generate_slug
from prefect.utilities.pydantic import handle_secret_render

R = TypeVar("R", default=Any)


DEFAULT_BLOCK_SCHEMA_VERSION: Literal["non-versioned"] = "non-versioned"
DEFAULT_AGENT_WORK_POOL_NAME: Literal["default-agent-pool"] = "default-agent-pool"


class RunType(AutoEnum):
    FLOW_RUN = "flow_run"
    TASK_RUN = "task_run"


class StateType(AutoEnum):
    """Enumeration of state types."""

    SCHEDULED = AutoEnum.auto()
    PENDING = AutoEnum.auto()
    RUNNING = AutoEnum.auto()
    COMPLETED = AutoEnum.auto()
    FAILED = AutoEnum.auto()
    CANCELLED = AutoEnum.auto()
    CRASHED = AutoEnum.auto()
    PAUSED = AutoEnum.auto()
    CANCELLING = AutoEnum.auto()


TERMINAL_STATES: set[StateType] = {
    StateType.COMPLETED,
    StateType.CANCELLED,
    StateType.FAILED,
    StateType.CRASHED,
}


class WorkPoolStatus(AutoEnum):
    """Enumeration of work pool statuses."""

    READY = AutoEnum.auto()
    NOT_READY = AutoEnum.auto()
    PAUSED = AutoEnum.auto()

    @property
    def display_name(self) -> str:
        return self.name.replace("_", " ").capitalize()


class WorkerStatus(AutoEnum):
    """Enumeration of worker statuses."""

    ONLINE = AutoEnum.auto()
    OFFLINE = AutoEnum.auto()


class DeploymentStatus(AutoEnum):
    """Enumeration of deployment statuses."""

    READY = AutoEnum.auto()
    NOT_READY = AutoEnum.auto()


class WorkQueueStatus(AutoEnum):
    """Enumeration of work queue statuses."""

    READY = AutoEnum.auto()
    NOT_READY = AutoEnum.auto()
    PAUSED = AutoEnum.auto()


class ConcurrencyLimitStrategy(AutoEnum):
    """Enumeration of concurrency limit strategies."""

    ENQUEUE = AutoEnum.auto()
    CANCEL_NEW = AutoEnum.auto()


class ConcurrencyOptions(PrefectBaseModel):
    """
    Class for storing the concurrency config in database.
    """

    collision_strategy: ConcurrencyLimitStrategy


class ConcurrencyLimitConfig(PrefectBaseModel):
    """
    Class for storing the concurrency limit config in database.
    """

    limit: int
    collision_strategy: ConcurrencyLimitStrategy = ConcurrencyLimitStrategy.ENQUEUE


class StateDetails(PrefectBaseModel):
    flow_run_id: Optional[UUID] = None
    task_run_id: Optional[UUID] = None
    child_flow_run_id: Optional[UUID] = None
    scheduled_time: Optional[DateTime] = None
    cache_key: Optional[str] = None
    cache_expiration: Optional[DateTime] = None
    deferred: Optional[bool] = None
    untrackable_result: bool = False
    pause_timeout: Optional[DateTime] = None
    pause_reschedule: bool = False
    pause_key: Optional[str] = None
    run_input_keyset: Optional[dict[str, str]] = None
    refresh_cache: Optional[bool] = None
    retriable: Optional[bool] = None
    transition_id: Optional[UUID] = None
    task_parameters_id: Optional[UUID] = None
    # Captures the trace_id and span_id of the span where this state was created
    traceparent: Optional[str] = None
    # The ID of the lease that is currently holding the deployment concurrency slot
    # for this run.
    deployment_concurrency_lease_id: Optional[UUID] = None

    def to_run_result(
        self, run_type: RunType
    ) -> Optional[Union[FlowRunResult, TaskRunResult]]:
        if run_type == run_type.FLOW_RUN and self.flow_run_id:
            return FlowRunResult(id=self.flow_run_id)
        elif run_type == run_type.TASK_RUN and self.task_run_id:
            return TaskRunResult(id=self.task_run_id)
        else:
            return None


def data_discriminator(x: Any) -> str:
    if isinstance(x, dict) and "storage_key" in x:
        return "ResultRecordMetadata"
    return "Any"


class State(TimeSeriesBaseModel, ObjectBaseModel, Generic[R]):
    """
    The state of a run.
    """

    type: StateType
    name: Optional[str] = Field(default=None)
    timestamp: datetime.datetime = Field(default_factory=lambda: now("UTC"))
    message: Optional[str] = Field(default=None, examples=["Run started"])
    state_details: StateDetails = Field(default_factory=StateDetails)
    data: Annotated[
        Union[
            Annotated["ResultRecordMetadata", Tag("ResultRecordMetadata")],
            Annotated[Any, Tag("Any")],
        ],
        Discriminator(data_discriminator),
    ] = Field(default=None)

    @overload
    async def aresult(
        self: "State[R]",
        raise_on_failure: Literal[True] = ...,
        retry_result_failure: bool = ...,
    ) -> R: ...

    @overload
    async def aresult(
        self: "State[R]",
        raise_on_failure: Literal[False] = False,
        retry_result_failure: bool = ...,
    ) -> Union[R, Exception]: ...

    @overload
    async def aresult(
        self: "State[R]",
        raise_on_failure: bool = ...,
        retry_result_failure: bool = ...,
    ) -> Union[R, Exception]: ...

    async def aresult(
        self,
        raise_on_failure: bool = True,
        retry_result_failure: bool = True,
    ) -> Union[R, Exception]:
        """
        Retrieve the result attached to this state.
        """
        from prefect.states import get_state_result

        return await get_state_result(
            self,
            raise_on_failure=raise_on_failure,
            retry_result_failure=retry_result_failure,
        )

    @overload
    def result(
        self: "State[R]",
        raise_on_failure: Literal[True] = ...,
        retry_result_failure: bool = ...,
    ) -> R: ...

    @overload
    def result(
        self: "State[R]",
        raise_on_failure: Literal[False] = False,
        retry_result_failure: bool = ...,
    ) -> Union[R, Exception]: ...

    @overload
    def result(
        self: "State[R]",
        raise_on_failure: bool = ...,
        retry_result_failure: bool = ...,
    ) -> Union[R, Exception]: ...

    @async_dispatch(aresult)
    def result(
        self,
        raise_on_failure: bool = True,
        retry_result_failure: bool = True,
    ) -> Union[R, Exception]:
        """
        Retrieve the result attached to this state.

        Args:
            raise_on_failure: a boolean specifying whether to raise an exception
                if the state is of type `FAILED` and the underlying data is an exception. When flow
                was run in a different memory space (using `run_deployment`), this will only raise
                if `fetch` is `True`.
            retry_result_failure: a boolean specifying whether to retry on failures to
                load the result from result storage

        Raises:
            TypeError: If the state is failed but the result is not an exception.

        Returns:
            The result of the run

        Examples:
            Get the result from a flow state

            ```python
            @flow
            def my_flow():
                return "hello"
            my_flow(return_state=True).result()
            # hello
            ```

            Get the result from a failed state

            ```python
            @flow
            def my_flow():
                raise ValueError("oh no!")
            state = my_flow(return_state=True)  # Error is wrapped in FAILED state
            state.result()  # Raises `ValueError`
            ```

            Get the result from a failed state without erroring

            ```python
            @flow
            def my_flow():
                raise ValueError("oh no!")
            state = my_flow(return_state=True)
            result = state.result(raise_on_failure=False)
            print(result)
            # ValueError("oh no!")
            ```


            Get the result from a flow state in an async context

            ```python
            @flow
            async def my_flow():
                return "hello"
            state = await my_flow(return_state=True)
            await state.result()
            # hello
            ```

            Get the result with `raise_on_failure` from a flow run in a different memory space

            ```python
            @flow
            async def my_flow():
                raise ValueError("oh no!")
            my_flow.deploy("my_deployment/my_flow")
            flow_run = run_deployment("my_deployment/my_flow")
            await flow_run.state.result(raise_on_failure=True) # Raises `ValueError("oh no!")`
            ```
        """
        from prefect.states import get_state_result

        return run_coro_as_sync(
            get_state_result(
                self,
                raise_on_failure=raise_on_failure,
                retry_result_failure=retry_result_failure,
            )
        )

    @model_validator(mode="after")
    def default_name_from_type(self) -> Self:
        """If a name is not provided, use the type"""
        # if `type` is not in `values` it means the `type` didn't pass its own
        # validation check and an error will be raised after this function is called
        name = self.name
        if name is None and self.type:
            self.name = " ".join([v.capitalize() for v in self.type.split("_")])
        return self

    @model_validator(mode="after")
    def default_scheduled_start_time(self) -> Self:
        if self.type == StateType.SCHEDULED:
            if not self.state_details.scheduled_time:
                self.state_details.scheduled_time = now("UTC")  # pyright: ignore[reportAttributeAccessIssue] DateTime is split into two types depending on Python version
        return self

    @model_validator(mode="after")
    def set_unpersisted_results_to_none(self) -> Self:
        if isinstance(self.data, dict) and self.data.get("type") == "unpersisted":  # pyright: ignore[reportUnknownMemberType] unable to narrow dict type
            self.data = None
        return self

    def is_scheduled(self) -> bool:
        return self.type == StateType.SCHEDULED

    def is_pending(self) -> bool:
        return self.type == StateType.PENDING

    def is_running(self) -> bool:
        return self.type == StateType.RUNNING

    def is_completed(self) -> bool:
        return self.type == StateType.COMPLETED

    def is_failed(self) -> bool:
        return self.type == StateType.FAILED

    def is_crashed(self) -> bool:
        return self.type == StateType.CRASHED

    def is_cancelled(self) -> bool:
        return self.type == StateType.CANCELLED

    def is_cancelling(self) -> bool:
        return self.type == StateType.CANCELLING

    def is_final(self) -> bool:
        return self.type in TERMINAL_STATES

    def is_paused(self) -> bool:
        return self.type == StateType.PAUSED

    def model_copy(
        self, *, update: Optional[Mapping[str, Any]] = None, deep: bool = False
    ) -> Self:
        """
        Copying API models should return an object that could be inserted into the
        database again. The 'timestamp' is reset using the default factory.
        """
        update = {
            "timestamp": type(self).model_fields["timestamp"].get_default(),
            **(update or {}),
        }
        return super().model_copy(update=update, deep=deep)

    def fresh_copy(self, **kwargs: Any) -> Self:
        """
        Return a fresh copy of the state with a new ID.
        """
        return self.model_copy(
            update={
                "id": uuid7(),
                "created": now("UTC"),
                "updated": now("UTC"),
                "timestamp": now("UTC"),
            },
            **kwargs,
        )

    def __repr__(self) -> str:
        """
        Generates a complete state representation appropriate for introspection
        and debugging, including the result:

        `MyCompletedState(message="my message", type=COMPLETED, result=...)`
        """
        result = self.data

        display = dict(
            message=repr(self.message),
            type=str(self.type.value),
            result=repr(result),
        )

        return f"{self.name}({', '.join(f'{k}={v}' for k, v in display.items())})"

    def __str__(self) -> str:
        """
        Generates a simple state representation appropriate for logging:

        `MyCompletedState("my message", type=COMPLETED)`
        """

        display: list[str] = []

        if self.message:
            display.append(repr(self.message))

        if TYPE_CHECKING:
            assert self.name is not None
        if self.type.lower() != self.name.lower():
            display.append(f"type={self.type.value}")

        return f"{self.name}({', '.join(display)})"

    def __hash__(self) -> int:
        return hash(
            (
                getattr(self.state_details, "flow_run_id", None),
                getattr(self.state_details, "task_run_id", None),
                self.timestamp,
                self.type,
            )
        )


class FlowRunPolicy(PrefectBaseModel):
    """Defines of how a flow run should be orchestrated."""

    max_retries: int = Field(
        default=0,
        description=(
            "The maximum number of retries. Field is not used. Please use `retries`"
            " instead."
        ),
        deprecated=True,
    )
    retry_delay_seconds: float = Field(
        default=0,
        description=(
            "The delay between retries. Field is not used. Please use `retry_delay`"
            " instead."
        ),
        deprecated=True,
    )
    retries: Optional[int] = Field(default=None, description="The number of retries.")
    retry_delay: Optional[int] = Field(
        default=None, description="The delay time between retries, in seconds."
    )
    pause_keys: Optional[set[str]] = Field(
        default_factory=set, description="Tracks pauses this run has observed."
    )
    resuming: Optional[bool] = Field(
        default=False, description="Indicates if this run is resuming from a pause."
    )
    retry_type: Optional[Literal["in_process", "reschedule"]] = Field(
        default=None, description="The type of retry this run is undergoing."
    )

    @model_validator(mode="before")
    @classmethod
    def populate_deprecated_fields(cls, values: Any) -> Any:
        if isinstance(values, dict):
            return set_run_policy_deprecated_fields(values)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType] unable to narrow dict type
        return values


class FlowRun(TimeSeriesBaseModel, ObjectBaseModel):
    name: str = Field(
        default_factory=lambda: generate_slug(2),
        description=(
            "The name of the flow run. Defaults to a random slug if not specified."
        ),
        examples=["my-flow-run"],
    )
    flow_id: UUID = Field(default=..., description="The id of the flow being run.")
    state_id: Optional[UUID] = Field(
        default=None, description="The id of the flow run's current state."
    )
    deployment_id: Optional[UUID] = Field(
        default=None,
        description=(
            "The id of the deployment associated with this flow run, if available."
        ),
    )
    deployment_version: Optional[str] = Field(
        default=None,
        description="The version of the deployment associated with this flow run.",
        examples=["1.0"],
    )
    work_queue_name: Optional[str] = Field(
        default=None, description="The work queue that handled this flow run."
    )
    flow_version: Optional[str] = Field(
        default=None,
        description="The version of the flow executed in this flow run.",
        examples=["1.0"],
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="Parameters for the flow run."
    )
    idempotency_key: Optional[str] = Field(
        default=None,
        description=(
            "An optional idempotency key for the flow run. Used to ensure the same flow"
            " run is not created multiple times."
        ),
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional context for the flow run.",
        examples=[{"my_var": "my_val"}],
    )
    empirical_policy: FlowRunPolicy = Field(
        default_factory=FlowRunPolicy,
    )
    tags: list[str] = Field(
        default_factory=list,
        description="A list of tags on the flow run",
        examples=[["tag-1", "tag-2"]],
    )
    labels: KeyValueLabelsField = Field(default_factory=dict)
    parent_task_run_id: Optional[UUID] = Field(
        default=None,
        description=(
            "If the flow run is a subflow, the id of the 'dummy' task in the parent"
            " flow used to track subflow state."
        ),
    )
    run_count: int = Field(
        default=0, description="The number of times the flow run was executed."
    )
    expected_start_time: Optional[DateTime] = Field(
        default=None,
        description="The flow run's expected start time.",
    )
    next_scheduled_start_time: Optional[DateTime] = Field(
        default=None,
        description="The next time the flow run is scheduled to start.",
    )
    start_time: Optional[DateTime] = Field(
        default=None, description="The actual start time."
    )
    end_time: Optional[DateTime] = Field(
        default=None, description="The actual end time."
    )
    total_run_time: datetime.timedelta = Field(
        default=datetime.timedelta(0),
        description=(
            "Total run time. If the flow run was executed multiple times, the time of"
            " each run will be summed."
        ),
    )
    estimated_run_time: datetime.timedelta = Field(
        default=datetime.timedelta(0),
        description="A real-time estimate of the total run time.",
    )
    estimated_start_time_delta: datetime.timedelta = Field(
        default=datetime.timedelta(0),
        description="The difference between actual and expected start time.",
    )
    auto_scheduled: bool = Field(
        default=False,
        description="Whether or not the flow run was automatically scheduled.",
    )
    infrastructure_document_id: Optional[UUID] = Field(
        default=None,
        description="The block document defining infrastructure to use this flow run.",
    )
    infrastructure_pid: Optional[str] = Field(
        default=None,
        description="The id of the flow run as returned by an infrastructure block.",
    )
    created_by: Optional[CreatedBy] = Field(
        default=None,
        description="Optional information about the creator of this flow run.",
    )
    work_queue_id: Optional[UUID] = Field(
        default=None, description="The id of the run's work pool queue."
    )

    work_pool_id: Optional[UUID] = Field(
        default=None, description="The work pool with which the queue is associated."
    )
    work_pool_name: Optional[str] = Field(
        default=None,
        description="The name of the flow run's work pool.",
        examples=["my-work-pool"],
    )
    state: Optional[State] = Field(
        default=None,
        description="The state of the flow run.",
        examples=["State(type=StateType.COMPLETED)"],
    )
    job_variables: Optional[dict[str, Any]] = Field(
        default=None,
        description="Job variables for the flow run.",
    )

    # These are server-side optimizations and should not be present on client models
    # TODO: Deprecate these fields

    state_type: Optional[StateType] = Field(
        default=None, description="The type of the current flow run state."
    )
    state_name: Optional[str] = Field(
        default=None, description="The name of the current flow run state."
    )

    def __eq__(self, other: Any) -> bool:
        """
        Check for "equality" to another flow run schema

        Estimates times are rolling and will always change with repeated queries for
        a flow run so we ignore them during equality checks.
        """
        if isinstance(other, FlowRun):
            exclude_fields = {"estimated_run_time", "estimated_start_time_delta"}
            return self.model_dump(exclude=exclude_fields) == other.model_dump(
                exclude=exclude_fields
            )
        return super().__eq__(other)

    @field_validator("name", mode="before")
    @classmethod
    def set_default_name(cls, name: Optional[str]) -> str:
        return get_or_create_run_name(name)


class TaskRunPolicy(PrefectBaseModel):
    """Defines of how a task run should retry."""

    max_retries: int = Field(
        default=0,
        description=(
            "The maximum number of retries. Field is not used. Please use `retries`"
            " instead."
        ),
        deprecated=True,
    )
    retry_delay_seconds: float = Field(
        default=0,
        description=(
            "The delay between retries. Field is not used. Please use `retry_delay`"
            " instead."
        ),
        deprecated=True,
    )
    retries: Optional[int] = Field(default=None, description="The number of retries.")
    retry_delay: Union[None, int, float, list[int], list[float]] = Field(
        default=None,
        description="A delay time or list of delay times between retries, in seconds.",
    )
    retry_jitter_factor: Optional[float] = Field(
        default=None, description="Determines the amount a retry should jitter"
    )

    @model_validator(mode="after")
    def populate_deprecated_fields(self):
        """
        If deprecated fields are provided, populate the corresponding new fields
        to preserve orchestration behavior.
        """
        # We have marked these fields as deprecated, so we need to filter out the
        # deprecation warnings _we're_ generating here
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)

            if not self.retries and self.max_retries != 0:
                self.retries = self.max_retries

            if not self.retry_delay and self.retry_delay_seconds != 0:
                self.retry_delay = int(self.retry_delay_seconds)

        return self

    @field_validator("retry_delay")
    @classmethod
    def validate_configured_retry_delays(
        cls, v: Optional[int | float | list[int] | list[float]]
    ) -> Optional[int | float | list[int] | list[float]]:
        return list_length_50_or_less(v)

    @field_validator("retry_jitter_factor")
    @classmethod
    def validate_jitter_factor(cls, v: Optional[float]) -> Optional[float]:
        return validate_not_negative(v)


class RunInput(PrefectBaseModel):
    """
    Base class for classes that represent inputs to task runs, which
    could include, constants, parameters, or other task runs.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    if not TYPE_CHECKING:
        # subclasses provide the concrete type for this field
        input_type: str


class TaskRunResult(RunInput):
    """Represents a task run result input to another task run."""

    input_type: Literal["task_run"] = "task_run"
    id: UUID


class FlowRunResult(RunInput):
    input_type: Literal["flow_run"] = "flow_run"
    id: UUID


class Parameter(RunInput):
    """Represents a parameter input to a task run."""

    input_type: Literal["parameter"] = "parameter"
    name: str


class Constant(RunInput):
    """Represents constant input value to a task run."""

    input_type: Literal["constant"] = "constant"
    type: str


class TaskRun(TimeSeriesBaseModel, ObjectBaseModel):
    name: str = Field(
        default_factory=lambda: generate_slug(2), examples=["my-task-run"]
    )
    flow_run_id: Optional[UUID] = Field(
        default=None, description="The flow run id of the task run."
    )
    task_key: str = Field(
        default=..., description="A unique identifier for the task being run."
    )
    dynamic_key: str = Field(
        default=...,
        description=(
            "A dynamic key used to differentiate between multiple runs of the same task"
            " within the same flow run."
        ),
    )
    cache_key: Optional[str] = Field(
        default=None,
        description=(
            "An optional cache key. If a COMPLETED state associated with this cache key"
            " is found, the cached COMPLETED state will be used instead of executing"
            " the task run."
        ),
    )
    cache_expiration: Optional[DateTime] = Field(
        default=None, description="Specifies when the cached state should expire."
    )
    task_version: Optional[str] = Field(
        default=None, description="The version of the task being run."
    )
    empirical_policy: TaskRunPolicy = Field(
        default_factory=TaskRunPolicy,
    )
    tags: list[str] = Field(
        default_factory=list,
        description="A list of tags for the task run.",
        examples=[["tag-1", "tag-2"]],
    )
    labels: KeyValueLabelsField = Field(default_factory=dict)
    state_id: Optional[UUID] = Field(
        default=None, description="The id of the current task run state."
    )
    task_inputs: dict[
        str, list[Union[TaskRunResult, FlowRunResult, Parameter, Constant]]
    ] = Field(
        default_factory=dict,
        description=(
            "Tracks the source of inputs to a task run. Used for internal bookkeeping. "
            "Note the special __parents__ key, used to indicate a parent/child "
            "relationship that may or may not include an input or wait_for semantic."
        ),
    )
    state_type: Optional[StateType] = Field(
        default=None, description="The type of the current task run state."
    )
    state_name: Optional[str] = Field(
        default=None, description="The name of the current task run state."
    )
    run_count: int = Field(
        default=0, description="The number of times the task run has been executed."
    )
    flow_run_run_count: int = Field(
        default=0,
        description=(
            "If the parent flow has retried, this indicates the flow retry this run is"
            " associated with."
        ),
    )
    expected_start_time: Optional[DateTime] = Field(
        default=None,
        description="The task run's expected start time.",
    )

    # the next scheduled start time will be populated
    # whenever the run is in a scheduled state
    next_scheduled_start_time: Optional[DateTime] = Field(
        default=None,
        description="The next time the task run is scheduled to start.",
    )
    start_time: Optional[DateTime] = Field(
        default=None, description="The actual start time."
    )
    end_time: Optional[DateTime] = Field(
        default=None, description="The actual end time."
    )
    total_run_time: datetime.timedelta = Field(
        default=datetime.timedelta(0),
        description=(
            "Total run time. If the task run was executed multiple times, the time of"
            " each run will be summed."
        ),
    )
    estimated_run_time: datetime.timedelta = Field(
        default=datetime.timedelta(0),
        description="A real-time estimate of total run time.",
    )
    estimated_start_time_delta: datetime.timedelta = Field(
        default=datetime.timedelta(0),
        description="The difference between actual and expected start time.",
    )

    state: Optional[State] = Field(
        default=None,
        description="The state of the task run.",
        examples=["State(type=StateType.COMPLETED)"],
    )

    @field_validator("name", mode="before")
    @classmethod
    def set_default_name(cls, name: Optional[str]) -> Name:
        return get_or_create_run_name(name)


class Workspace(PrefectBaseModel):
    """
    A Prefect Cloud workspace.

    Expected payload for each workspace returned by the `me/workspaces` route.
    """

    account_id: UUID = Field(..., description="The account id of the workspace.")
    account_name: str = Field(..., description="The account name.")
    account_handle: str = Field(..., description="The account's unique handle.")
    workspace_id: UUID = Field(..., description="The workspace id.")
    workspace_name: str = Field(..., description="The workspace name.")
    workspace_description: str = Field(..., description="Description of the workspace.")
    workspace_handle: str = Field(..., description="The workspace's unique handle.")
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    @property
    def handle(self) -> str:
        """
        The full handle of the workspace as `account_handle` / `workspace_handle`
        """
        return self.account_handle + "/" + self.workspace_handle

    def api_url(self) -> str:
        """
        Generate the API URL for accessing this workspace
        """
        return (
            f"{PREFECT_CLOUD_API_URL.value()}"
            f"/accounts/{self.account_id}"
            f"/workspaces/{self.workspace_id}"
        )

    def ui_url(self) -> str:
        """
        Generate the UI URL for accessing this workspace
        """
        return (
            f"{PREFECT_CLOUD_UI_URL.value()}"
            f"/account/{self.account_id}"
            f"/workspace/{self.workspace_id}"
        )

    def __hash__(self) -> int:
        return hash(self.handle)


class IPAllowlistEntry(PrefectBaseModel):
    ip_network: IPvAnyNetwork
    enabled: bool
    description: Optional[str] = Field(
        default=None, description="A description of the IP entry."
    )
    last_seen: Optional[str] = Field(
        default=None,
        description="The last time this IP was seen accessing Prefect Cloud.",
    )


class IPAllowlist(PrefectBaseModel):
    """
    A Prefect Cloud IP allowlist.

    Expected payload for an IP allowlist from the Prefect Cloud API.
    """

    entries: list[IPAllowlistEntry]


class IPAllowlistMyAccessResponse(PrefectBaseModel):
    """Expected payload for an IP allowlist access response from the Prefect Cloud API."""

    allowed: bool
    detail: str


class BlockType(ObjectBaseModel):
    """An ORM representation of a block type"""

    name: Name = Field(default=..., description="A block type's name")
    slug: str = Field(default=..., description="A block type's slug")
    logo_url: Optional[HttpUrl] = Field(
        default=None, description="Web URL for the block type's logo"
    )
    documentation_url: Optional[HttpUrl] = Field(
        default=None, description="Web URL for the block type's documentation"
    )
    description: Optional[str] = Field(
        default=None,
        description="A short blurb about the corresponding block's intended use",
    )
    code_example: Optional[str] = Field(
        default=None,
        description="A code snippet demonstrating use of the corresponding block",
    )
    is_protected: bool = Field(
        default=False, description="Protected block types cannot be modified via API."
    )


class BlockSchema(ObjectBaseModel):
    """A representation of a block schema."""

    checksum: str = Field(default=..., description="The block schema's unique checksum")
    fields: dict[str, Any] = Field(
        default_factory=dict, description="The block schema's field schema"
    )
    block_type_id: Optional[UUID] = Field(default=..., description="A block type ID")
    block_type: Optional[BlockType] = Field(
        default=None, description="The associated block type"
    )
    capabilities: list[str] = Field(
        default_factory=list,
        description="A list of Block capabilities",
    )
    version: str = Field(
        default=DEFAULT_BLOCK_SCHEMA_VERSION,
        description="Human readable identifier for the block schema",
    )


class BlockDocument(ObjectBaseModel):
    """An ORM representation of a block document."""

    name: Optional[BlockDocumentName] = Field(
        default=None,
        description=(
            "The block document's name. Not required for anonymous block documents."
        ),
    )
    data: dict[str, Any] = Field(
        default_factory=dict, description="The block document's data"
    )
    block_schema_id: UUID = Field(default=..., description="A block schema ID")
    block_schema: Optional[BlockSchema] = Field(
        default=None, description="The associated block schema"
    )
    block_type_id: UUID = Field(default=..., description="A block type ID")
    block_type_name: Optional[str] = Field(None, description="A block type name")
    block_type: Optional[BlockType] = Field(
        default=None, description="The associated block type"
    )
    block_document_references: dict[str, dict[str, Any]] = Field(
        default_factory=dict, description="Record of the block document's references"
    )
    is_anonymous: bool = Field(
        default=False,
        description=(
            "Whether the block is anonymous (anonymous blocks are usually created by"
            " Prefect automatically)"
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def validate_name_is_present_if_not_anonymous(
        cls, values: dict[str, Any]
    ) -> dict[str, Any]:
        return validate_name_present_on_nonanonymous_blocks(values)

    @model_serializer(mode="wrap")
    def serialize_data(
        self, handler: SerializerFunctionWrapHandler, info: SerializationInfo
    ) -> Any:
        self.data = visit_collection(
            self.data,
            visit_fn=partial(handle_secret_render, context=info.context or {}),
            return_data=True,
        )
        return handler(self)


class Flow(ObjectBaseModel):
    """An ORM representation of flow data."""

    name: Name = Field(
        default=..., description="The name of the flow", examples=["my-flow"]
    )
    tags: list[str] = Field(
        default_factory=list,
        description="A list of flow tags",
        examples=[["tag-1", "tag-2"]],
    )
    labels: KeyValueLabelsField


class DeploymentSchedule(ObjectBaseModel):
    deployment_id: Optional[UUID] = Field(
        default=None,
        description="The deployment id associated with this schedule.",
    )
    schedule: SCHEDULE_TYPES = Field(
        default=..., description="The schedule for the deployment."
    )
    active: bool = Field(
        default=True, description="Whether or not the schedule is active."
    )
    max_scheduled_runs: Optional[PositiveInteger] = Field(
        default=None,
        description="The maximum number of scheduled runs for the schedule.",
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Parameter overrides for the schedule.",
    )
    slug: Optional[str] = Field(
        default=None,
        description="A unique identifier for the schedule.",
    )


class VersionInfo(PrefectBaseModel, extra="allow"):
    type: str = Field(default=..., description="The type of version info.")
    version: str = Field(default=..., description="The version of the deployment.")


class BranchingScheduleHandling(str, Enum):
    KEEP = "keep"
    REMOVE = "remove"
    INACTIVE = "inactive"


class DeploymentBranchingOptions(ObjectBaseModel):
    schedule_handling: BranchingScheduleHandling = Field(
        default=BranchingScheduleHandling.REMOVE,
        description="Whether to keep, remove, or set inactive the existing schedules when branching",
    )


class Deployment(ObjectBaseModel):
    """An ORM representation of deployment data."""

    name: Name = Field(default=..., description="The name of the deployment.")
    version: Optional[str] = Field(
        default=None, description="An optional version for the deployment."
    )
    version_id: Optional[UUID] = Field(
        default=None, description="The ID of the current version of the deployment."
    )
    version_info: Optional[VersionInfo] = Field(
        default=None, description="A description of this version of the deployment."
    )
    description: Optional[str] = Field(
        default=None, description="A description for the deployment."
    )
    flow_id: UUID = Field(
        default=..., description="The flow id associated with the deployment."
    )
    paused: bool = Field(
        default=False, description="Whether or not the deployment is paused."
    )
    concurrency_limit: Optional[int] = Field(
        default=None, description="The concurrency limit for the deployment."
    )
    schedules: list[DeploymentSchedule] = Field(
        default_factory=lambda: [],
        description="A list of schedules for the deployment.",
    )
    job_variables: dict[str, Any] = Field(
        default_factory=dict,
        description="Overrides to apply to flow run infrastructure at runtime.",
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Parameters for flow runs scheduled by the deployment.",
    )
    pull_steps: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description="Pull steps for cloning and running this deployment.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="A list of tags for the deployment",
        examples=[["tag-1", "tag-2"]],
    )
    labels: KeyValueLabelsField
    work_queue_name: Optional[str] = Field(
        default=None,
        description=(
            "The work queue for the deployment. If no work queue is set, work will not"
            " be scheduled."
        ),
    )
    last_polled: Optional[DateTime] = Field(
        default=None,
        description="The last time the deployment was polled for status updates.",
    )
    parameter_openapi_schema: Optional[dict[str, Any]] = Field(
        default=None,
        description="The parameter schema of the flow, including defaults.",
    )
    path: Optional[str] = Field(
        default=None,
        description=(
            "The path to the working directory for the workflow, relative to remote"
            " storage or an absolute path."
        ),
    )
    entrypoint: Optional[str] = Field(
        default=None,
        description=(
            "The path to the entrypoint for the workflow, relative to the `path`."
        ),
    )
    storage_document_id: Optional[UUID] = Field(
        default=None,
        description="The block document defining storage used for this flow.",
    )
    infrastructure_document_id: Optional[UUID] = Field(
        default=None,
        description="The block document defining infrastructure to use for flow runs.",
    )
    created_by: Optional[CreatedBy] = Field(
        default=None,
        description="Optional information about the creator of this deployment.",
    )
    updated_by: Optional[UpdatedBy] = Field(
        default=None,
        description="Optional information about the updater of this deployment.",
    )
    work_queue_id: Optional[UUID] = Field(
        default=None,
        description=(
            "The id of the work pool queue to which this deployment is assigned."
        ),
    )
    enforce_parameter_schema: bool = Field(
        default=True,
        description=(
            "Whether or not the deployment should enforce the parameter schema."
        ),
    )


class ConcurrencyLimit(ObjectBaseModel):
    """An ORM representation of a concurrency limit."""

    tag: str = Field(
        default=..., description="A tag the concurrency limit is applied to."
    )
    concurrency_limit: int = Field(default=..., description="The concurrency limit.")
    active_slots: list[UUID] = Field(
        default_factory=lambda: [],
        description="A list of active run ids using a concurrency slot",
    )


class BlockSchemaReference(ObjectBaseModel):
    """An ORM representation of a block schema reference."""

    parent_block_schema_id: UUID = Field(
        default=..., description="ID of block schema the reference is nested within"
    )
    parent_block_schema: Optional[BlockSchema] = Field(
        default=None, description="The block schema the reference is nested within"
    )
    reference_block_schema_id: UUID = Field(
        default=..., description="ID of the nested block schema"
    )
    reference_block_schema: Optional[BlockSchema] = Field(
        default=None, description="The nested block schema"
    )
    name: str = Field(
        default=..., description="The name that the reference is nested under"
    )


class BlockDocumentReference(ObjectBaseModel):
    """An ORM representation of a block document reference."""

    parent_block_document_id: UUID = Field(
        default=..., description="ID of block document the reference is nested within"
    )
    parent_block_document: Optional[BlockDocument] = Field(
        default=None, description="The block document the reference is nested within"
    )
    reference_block_document_id: UUID = Field(
        default=..., description="ID of the nested block document"
    )
    reference_block_document: Optional[BlockDocument] = Field(
        default=None, description="The nested block document"
    )
    name: str = Field(
        default=..., description="The name that the reference is nested under"
    )

    @model_validator(mode="before")
    @classmethod
    def validate_parent_and_ref_are_different(cls, values: Any) -> Any:
        if isinstance(values, dict):
            return validate_parent_and_ref_diff(values)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType] unable to narrow dict type
        return values


class Configuration(ObjectBaseModel):
    """An ORM representation of account info."""

    key: str = Field(default=..., description="Account info key")
    value: dict[str, Any] = Field(default=..., description="Account info")


class SavedSearchFilter(PrefectBaseModel):
    """A filter for a saved search model. Intended for use by the Prefect UI."""

    object: str = Field(default=..., description="The object over which to filter.")
    property: str = Field(
        default=..., description="The property of the object on which to filter."
    )
    type: str = Field(default=..., description="The type of the property.")
    operation: str = Field(
        default=...,
        description="The operator to apply to the object. For example, `equals`.",
    )
    value: Any = Field(
        default=..., description="A JSON-compatible value for the filter."
    )


class SavedSearch(ObjectBaseModel):
    """An ORM representation of saved search data. Represents a set of filter criteria."""

    name: str = Field(default=..., description="The name of the saved search.")
    filters: list[SavedSearchFilter] = Field(
        default_factory=lambda: [],
        description="The filter set for the saved search.",
    )


class Log(TimeSeriesBaseModel, ObjectBaseModel):
    """An ORM representation of log data."""

    name: str = Field(default=..., description="The logger name.")
    level: int = Field(default=..., description="The log level.")
    message: str = Field(default=..., description="The log message.")
    timestamp: DateTime = Field(default=..., description="The log timestamp.")
    flow_run_id: Optional[UUID] = Field(
        default=None, description="The flow run ID associated with the log."
    )
    task_run_id: Optional[UUID] = Field(
        default=None, description="The task run ID associated with the log."
    )


class QueueFilter(PrefectBaseModel):
    """Filter criteria definition for a work queue."""

    tags: Optional[list[str]] = Field(
        default=None,
        description="Only include flow runs with these tags in the work queue.",
    )
    deployment_ids: Optional[list[UUID]] = Field(
        default=None,
        description="Only include flow runs from these deployments in the work queue.",
    )


class WorkQueue(ObjectBaseModel):
    """An ORM representation of a work queue"""

    name: Name = Field(default=..., description="The name of the work queue.")
    description: Optional[str] = Field(
        default="", description="An optional description for the work queue."
    )
    is_paused: bool = Field(
        default=False, description="Whether or not the work queue is paused."
    )
    concurrency_limit: Optional[NonNegativeInteger] = Field(
        default=None, description="An optional concurrency limit for the work queue."
    )
    priority: PositiveInteger = Field(
        default=1,
        description=(
            "The queue's priority. Lower values are higher priority (1 is the highest)."
        ),
    )
    work_pool_name: Optional[str] = Field(default=None)
    # Will be required after a future migration
    work_pool_id: Optional[UUID] = Field(
        description="The work pool with which the queue is associated."
    )
    filter: Optional[QueueFilter] = Field(
        default=None,
        description="DEPRECATED: Filter criteria for the work queue.",
        deprecated=True,
    )
    last_polled: Optional[DateTime] = Field(
        default=None, description="The last time an agent polled this queue for work."
    )
    status: Optional[WorkQueueStatus] = Field(
        default=None, description="The queue status."
    )


class WorkQueueHealthPolicy(PrefectBaseModel):
    maximum_late_runs: Optional[int] = Field(
        default=0,
        description=(
            "The maximum number of late runs in the work queue before it is deemed"
            " unhealthy. Defaults to `0`."
        ),
    )
    maximum_seconds_since_last_polled: Optional[int] = Field(
        default=60,
        description=(
            "The maximum number of time in seconds elapsed since work queue has been"
            " polled before it is deemed unhealthy. Defaults to `60`."
        ),
    )

    def evaluate_health_status(
        self, late_runs_count: int, last_polled: datetime.datetime | None = None
    ) -> bool:
        """
        Given empirical information about the state of the work queue, evaluate its health status.

        Args:
            late_runs: the count of late runs for the work queue.
            last_polled: the last time the work queue was polled, if available.

        Returns:
            bool: whether or not the work queue is healthy.
        """
        healthy = True
        if (
            self.maximum_late_runs is not None
            and late_runs_count > self.maximum_late_runs
        ):
            healthy = False

        if self.maximum_seconds_since_last_polled is not None:
            if (
                last_polled is None
                or (now("UTC") - last_polled).total_seconds()
                > self.maximum_seconds_since_last_polled
            ):
                healthy = False

        return healthy


class WorkQueueStatusDetail(PrefectBaseModel):
    healthy: bool = Field(..., description="Whether or not the work queue is healthy.")
    late_runs_count: int = Field(
        default=0, description="The number of late flow runs in the work queue."
    )
    last_polled: Optional[DateTime] = Field(
        default=None, description="The last time an agent polled this queue for work."
    )
    health_check_policy: WorkQueueHealthPolicy = Field(
        ...,
        description=(
            "The policy used to determine whether or not the work queue is healthy."
        ),
    )


class Agent(ObjectBaseModel):
    """An ORM representation of an agent"""

    name: str = Field(
        default_factory=lambda: generate_slug(2),
        description=(
            "The name of the agent. If a name is not provided, it will be"
            " auto-generated."
        ),
    )
    work_queue_id: UUID = Field(
        default=..., description="The work queue with which the agent is associated."
    )
    last_activity_time: Optional[DateTime] = Field(
        default=None, description="The last time this agent polled for work."
    )


class WorkPoolStorageConfiguration(PrefectBaseModel):
    """A work pool storage configuration"""

    bundle_upload_step: Optional[dict[str, Any]] = Field(
        default=None, description="The bundle upload step for the work pool."
    )
    bundle_execution_step: Optional[dict[str, Any]] = Field(
        default=None, description="The bundle execution step for the work pool."
    )
    default_result_storage_block_id: Optional[UUID] = Field(
        default=None,
        description="The block document ID of the default result storage block.",
    )


class WorkPool(ObjectBaseModel):
    """An ORM representation of a work pool"""

    name: Name = Field(
        description="The name of the work pool.",
    )
    description: Optional[str] = Field(
        default=None, description="A description of the work pool."
    )
    type: str = Field(description="The work pool type.")
    base_job_template: dict[str, Any] = Field(
        default_factory=dict, description="The work pool's base job template."
    )
    is_paused: bool = Field(
        default=False,
        description="Pausing the work pool stops the delivery of all work.",
    )
    concurrency_limit: Optional[NonNegativeInteger] = Field(
        default=None, description="A concurrency limit for the work pool."
    )
    status: Optional[WorkPoolStatus] = Field(
        default=None, description="The current status of the work pool."
    )

    storage_configuration: WorkPoolStorageConfiguration = Field(
        default_factory=WorkPoolStorageConfiguration,
        description="The storage configuration for the work pool.",
    )

    # this required field has a default of None so that the custom validator
    # below will be called and produce a more helpful error message. Because
    # the field metadata is attached via an annotation, the default is hidden
    # from type checkers.
    default_queue_id: Annotated[
        UUID, Field(default=None, description="The id of the pool's default queue.")
    ]

    @property
    def is_push_pool(self) -> bool:
        return self.type.endswith(":push")

    @property
    def is_managed_pool(self) -> bool:
        return self.type.endswith(":managed")

    @field_validator("default_queue_id")
    @classmethod
    def helpful_error_for_missing_default_queue_id(cls, v: Optional[UUID]) -> UUID:
        return validate_default_queue_id_not_none(v)


class Worker(ObjectBaseModel):
    """An ORM representation of a worker"""

    name: str = Field(description="The name of the worker.")
    work_pool_id: UUID = Field(
        description="The work pool with which the queue is associated."
    )
    last_heartbeat_time: Optional[datetime.datetime] = Field(
        default=None, description="The last time the worker process sent a heartbeat."
    )
    heartbeat_interval_seconds: Optional[int] = Field(
        default=None,
        description=(
            "The number of seconds to expect between heartbeats sent by the worker."
        ),
    )
    status: WorkerStatus = Field(
        WorkerStatus.OFFLINE,
        description="Current status of the worker.",
    )


Flow.model_rebuild()
# FlowRun.model_rebuild()


class Artifact(ObjectBaseModel):
    key: Optional[str] = Field(
        default=None, description="An optional unique reference key for this artifact."
    )
    type: Optional[str] = Field(
        default=None,
        description=(
            "An identifier that describes the shape of the data field. e.g. 'result',"
            " 'table', 'markdown'"
        ),
    )
    description: Optional[str] = Field(
        default=None, description="A markdown-enabled description of the artifact."
    )
    # data will eventually be typed as `Optional[Union[Result, Any]]`
    data: Optional[Union[dict[str, Any], Any]] = Field(
        default=None,
        description=(
            "Data associated with the artifact, e.g. a result.; structure depends on"
            " the artifact type."
        ),
    )
    metadata_: Optional[dict[str, str]] = Field(
        default=None,
        description=(
            "User-defined artifact metadata. Content must be string key and value"
            " pairs."
        ),
    )
    flow_run_id: Optional[UUID] = Field(
        default=None, description="The flow run associated with the artifact."
    )
    task_run_id: Optional[UUID] = Field(
        default=None, description="The task run associated with the artifact."
    )

    @field_validator("metadata_")
    @classmethod
    def validate_metadata_length(
        cls, v: Optional[dict[str, str]]
    ) -> Optional[dict[str, str]]:
        return validate_max_metadata_length(v)


class ArtifactCollection(ObjectBaseModel):
    key: str = Field(description="An optional unique reference key for this artifact.")
    latest_id: UUID = Field(
        description="The latest artifact ID associated with the key."
    )
    type: Optional[str] = Field(
        default=None,
        description=(
            "An identifier that describes the shape of the data field. e.g. 'result',"
            " 'table', 'markdown'"
        ),
    )
    description: Optional[str] = Field(
        default=None, description="A markdown-enabled description of the artifact."
    )
    data: Optional[Union[dict[str, Any], Any]] = Field(
        default=None,
        description=(
            "Data associated with the artifact, e.g. a result.; structure depends on"
            " the artifact type."
        ),
    )
    metadata_: Optional[dict[str, str]] = Field(
        default=None,
        description=(
            "User-defined artifact metadata. Content must be string key and value"
            " pairs."
        ),
    )
    flow_run_id: Optional[UUID] = Field(
        default=None, description="The flow run associated with the artifact."
    )
    task_run_id: Optional[UUID] = Field(
        default=None, description="The task run associated with the artifact."
    )


class Variable(ObjectBaseModel):
    name: str = Field(
        default=...,
        description="The name of the variable",
        examples=["my_variable"],
        max_length=MAX_VARIABLE_NAME_LENGTH,
    )
    value: StrictVariableValue = Field(
        default=...,
        description="The value of the variable",
        examples=["my_value"],
    )
    tags: list[str] = Field(
        default_factory=list,
        description="A list of variable tags",
        examples=[["tag-1", "tag-2"]],
    )


class FlowRunInput(ObjectBaseModel):
    flow_run_id: UUID = Field(description="The flow run ID associated with the input.")
    key: Annotated[str, AfterValidator(raise_on_name_alphanumeric_dashes_only)] = Field(
        description="The key of the input."
    )
    value: str = Field(description="The value of the input.")
    sender: Optional[str] = Field(default=None, description="The sender of the input.")

    @property
    def decoded_value(self) -> Any:
        """
        Decode the value of the input.

        Returns:
            Any: the decoded value
        """
        return orjson.loads(self.value)


class GlobalConcurrencyLimit(ObjectBaseModel):
    """An ORM representation of a global concurrency limit"""

    name: str = Field(description="The name of the global concurrency limit.")
    limit: int = Field(
        description=(
            "The maximum number of slots that can be occupied on this concurrency"
            " limit."
        )
    )
    active: Optional[bool] = Field(
        default=True,
        description="Whether or not the concurrency limit is in an active state.",
    )
    active_slots: Optional[int] = Field(
        default=0,
        description="Number of tasks currently using a concurrency slot.",
    )
    slot_decay_per_second: Optional[float] = Field(
        default=0.0,
        description=(
            "Controls the rate at which slots are released when the concurrency limit"
            " is used as a rate limit."
        ),
    )


class CsrfToken(ObjectBaseModel):
    token: str = Field(
        default=...,
        description="The CSRF token",
    )
    client: str = Field(
        default=..., description="The client id associated with the CSRF token"
    )
    expiration: datetime.datetime = Field(
        default=..., description="The expiration time of the CSRF token"
    )


__getattr__: Callable[[str], Any] = getattr_migration(__name__)


class Integration(PrefectBaseModel):
    """A representation of an installed Prefect integration."""

    name: str = Field(description="The name of the Prefect integration.")
    version: str = Field(description="The version of the Prefect integration.")


class WorkerMetadata(PrefectBaseModel):
    """
    Worker metadata.

    We depend on the structure of `integrations`, but otherwise, worker classes
    should support flexible metadata.
    """

    integrations: list[Integration] = Field(
        default=..., description="Prefect integrations installed in the worker."
    )
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")
