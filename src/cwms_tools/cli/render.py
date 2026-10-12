"""Output rendering for the CLI.

`--machine` and non-TTY stdout produce stable, deterministic JSON on stdout
and route diagnostics to stderr; TTY mode allows pretty-print and rich
formatting (kept minimal for v0.1.0).
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import TYPE_CHECKING, Any, NoReturn

import typer

from cwms_tools.core._compact import surface_payload
from cwms_tools.core.errors import (
    CwmsToolsError,
    ErrorCode,
    RepairHint,
    exit_code_for,
    surface_field_name,
)
from cwms_tools.core.offices import ghost_office_repair, nw_rollup_target
from cwms_tools.core.rounding import round_floats

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel

# Per `agent-friendly-cli` §"Agent-Safe Invocation": machine mode is forced on
# whenever stdout is not a TTY, regardless of flags. Explicit `--machine` /
# `--json` flags also force it on.

_state: dict[str, bool] = {"machine": False, "isolated": False, "no_cache": False}


def set_machine(value: bool) -> None:
    _state["machine"] = bool(value)


def set_isolated(value: bool) -> None:
    _state["isolated"] = bool(value)
    if value:
        # Mark for downstream consumers (cache/session).
        os.environ["_CWMS_TOOLS_ISOLATED"] = "1"
        os.environ["_CWMS_TOOLS_NO_CACHE"] = "1"


def set_no_cache(value: bool) -> None:
    _state["no_cache"] = bool(value)
    if value:
        os.environ["_CWMS_TOOLS_NO_CACHE"] = "1"


class OutputMode:
    """Resolved output mode for a single CLI invocation."""

    def __init__(self, *, machine: bool | None = None, json_only: bool = False) -> None:
        flag_set = _state["machine"] if machine is None else bool(machine)
        self.machine = flag_set or json_only or not sys.stdout.isatty()
        self.json_only = json_only or self.machine

    @property
    def pretty(self) -> bool:
        return not self.machine

    @property
    def indent(self) -> int | None:
        """JSON indent: pretty in TTY mode, compact in machine mode."""
        return 2 if self.pretty else None


def emit(value: Any, *, mode: OutputMode | None = None) -> None:
    """Print a Python value to stdout as JSON in the resolved mode.

    Use this for every success payload — stdout is success only.
    """
    out_mode = mode or OutputMode()
    indent = out_mode.indent
    # Round float values to strip unit-conversion noise (issue #45). Tool
    # payloads with an MCP peer already arrive null-stripped and rounded via
    # `core._compact.surface_payload` (#143); this covers the CLI-only payloads
    # (config, env, schema, ...) that have no response model. Idempotent.
    typer.echo(
        json.dumps(round_floats(value), indent=indent, sort_keys=False, default=str),
        nl=True,
    )


def diagnostic(message: str) -> None:
    """Write a single diagnostic line to stderr."""
    typer.echo(message, err=True)


def emit_model(model_cls: type[BaseModel], payload: Mapping[str, Any]) -> None:
    """Serialize `payload` through its MCP response model and print it (#143).

    The CLI's success path for every tool with an MCP peer. A payload the model
    rejects is a cwms-tools defect; `surface_payload` reports it as a
    `wrapper_bug` `CwmsToolsError`, routed here through `emit_error` so the
    agent gets the structured envelope on stderr (exit 11), never a traceback.
    """
    try:
        data = surface_payload(model_cls, payload)
    except CwmsToolsError as err:
        emit_error(err)
    emit(data)


def emit_error(error: CwmsToolsError) -> NoReturn:
    """Write a structured error envelope to STDERR and exit with its mapped code.

    Per `agent-friendly-cli`, stdout stays success-only; failures go to stderr
    as the full `{ok: false, error: {...}}` envelope (the same shape the MCP
    surface returns), branchable by the symbolic `error.code` and the numeric
    exit code. Every CLI command routes whole-command failures through here so
    the error shape and stream are uniform. Also where `field` gets translated
    to the CLI-facing flag name (#68), mirroring `mcp.tools.stamp_envelope` —
    core producers emit whatever internal name suits their own domain.
    """
    if error.envelope.details is not None:
        error.envelope.details.field = surface_field_name(error.envelope.details.field)
    payload = {"ok": False, "error": error.envelope.model_dump(mode="json")}
    typer.echo(json.dumps(payload, default=str), err=True)
    raise typer.Exit(code=exit_code_for(error.envelope.code))


def rewrite_error_field(error: CwmsToolsError, *, when: str, to: str) -> CwmsToolsError:
    """Override `error.envelope.details.field` to `to` when it currently equals `when`.

    `emit_error`'s `surface_field_name()` translation assumes a flag exists
    for the producer-internal name it's correcting to (true for MCP tools
    and CLI commands with a real `--office` flag). Some CLI commands take a
    combined positional instead (`place describe/parameters`'s `OFFICE/NAME`
    `spec`; `value get/history/profile`'s `OFFICE/NAME/PARAMETER` id) — for
    those, `office`/`office_id` isn't a real argument on the command at all,
    so the surface-facing `details.field` needs command-specific redirection
    to the positional the caller can actually retry with (#68 review
    feedback). Call this BEFORE `emit_error`/serialization so
    `surface_field_name`'s default translation doesn't run first (it's a
    no-op on an already-rewritten name, but checking the untranslated
    producer name here keeps the intent obvious at each call site).
    """
    if error.envelope.details is not None and error.envelope.details.field == when:
        error.envelope.details.field = to
    return error


def attach_ghost_office_repair(
    error: CwmsToolsError, *, tool: str, args: dict[str, Any]
) -> CwmsToolsError:
    """If `error` is `ghost_office`, attach a same-command retry repair.

    `tool`/`args` are THIS command's own name and original (wire-format,
    `--flag`-ready) arguments; `office` is set to the rollup target whether or
    not `args` carries it — core no longer hardcodes a
    same-tool-switching repair (it doesn't know which command is calling),
    so each CLI command supplies its own identity here, mirroring
    `mcp.tools._safe`'s `_repair_call` (#69). No-op for any other error code.
    Independent of `rewrite_error_field` (that touches `field`; this touches
    `repair`) — call in either order, but before `emit_error`, which
    serializes the envelope and exits.
    """
    office_id = error.envelope.details.value if error.envelope.details else None
    if error.envelope.code is ErrorCode.GHOST_OFFICE and isinstance(office_id, str):
        error.envelope.repair = ghost_office_repair(office_id, tool=tool, args=args)
    return error


def attach_ghost_office_spec_repair(
    error: CwmsToolsError, *, tool: str, spec_key: str, spec_suffix: str, args: dict[str, Any]
) -> CwmsToolsError:
    """Like `attach_ghost_office_repair`, but for commands with a combined
    `OFFICE/...` positional instead of a separate `--office` flag (`place
    describe`/`parameters`'s `spec`; `value get`/`history`/`profile`'s
    `id_specs`/`id_spec`) — `spec_key` names that positional and
    `spec_suffix` is everything after `OFFICE/` (e.g. `NAME` or
    `NAME/PARAMETER`); the repaired value becomes `{target}/{spec_suffix}`.
    """
    office_id = error.envelope.details.value if error.envelope.details else None
    if error.envelope.code is ErrorCode.GHOST_OFFICE and isinstance(office_id, str):
        target = nw_rollup_target(office_id)
        error.envelope.repair = RepairHint(
            next_step="retry_with_rollup_office",
            tool=tool,
            arguments={**args, spec_key: f"{target}/{spec_suffix}"},
        )
    return error


def attach_repair_intent(
    error: CwmsToolsError,
    *,
    tool: str,
    args: dict[str, Any],
    spec_key: str | None = None,
    spec_fields: dict[str, str] | None = None,
    spec_as_list: bool = False,
) -> CwmsToolsError:
    """Resolve a core `RepairIntent` into a same-command retry (#96/#163).

    `args` are this command's own wire-format options (`--flag` names as
    keys). Commands that take a combined `OFFICE/NAME[/PARAMETER]` positional
    pass `spec_key` (the positional's name) and `spec_fields` — an ordered
    `{"office": ..., "name": ..., "parameter": ...}` — so an override on any
    part is re-joined into the positional; `spec_as_list` wraps it for
    `value get`'s variadic `id_specs`. A `ghost_office` error keeps its own
    rollup repair (`attach_ghost_office_*`). No-op when there is no intent.
    """
    intent = error.repair_intent
    if intent is None or error.envelope.code is ErrorCode.GHOST_OFFICE:
        return error
    if spec_key is None:
        arguments: dict[str, Any] = {**args, **intent.overrides}
    else:
        fields = dict(spec_fields or {})
        fields.update({k: v for k, v in intent.overrides.items() if k in fields})
        spec = "/".join(str(v) for v in fields.values())
        # Overrides that are not part of the positional are ordinary options.
        extra = {k: v for k, v in intent.overrides.items() if k not in fields}
        arguments = {**args, **extra, spec_key: [spec] if spec_as_list else spec}
    error.envelope.repair = RepairHint(
        next_step=intent.next_step,
        tool=tool,
        arguments=arguments,
        alternative=intent.alternative,
    )
    return error


#: `cwms_get_profile with office='O', name='N', parameter='P'` — the profile
#: alternative `core.timeseries` writes on a depth-string ghost (either quote
#: style, since `repr` switches to double quotes around an apostrophe).
_PROFILE_ALTERNATIVE = re.compile(
    r"cwms_get_profile with office=(?P<q1>['\"])(?P<office>.*?)(?P=q1), "
    r"name=(?P<q2>['\"])(?P<name>.*?)(?P=q2), "
    r"parameter=(?P<q3>['\"])(?P<parameter>.*?)(?P=q3)"
)


def _localize_tool(repair: RepairHint) -> tuple[str, dict[str, Any]] | None:
    """The CLI `(command, arguments)` for an MCP-named repair, or None if unmapped."""
    args = repair.arguments
    if repair.tool == "cwms_search_places":
        out: dict[str, Any] = {}
        if args.get("query") is not None:
            out["query"] = args["query"]
        office = args.get("office")
        if office is not None:
            # `place search --office` is a repeatable (list) option.
            out["office"] = list(office) if isinstance(office, list) else [office]
        if args.get("parameter") is not None:
            out["parameter"] = args["parameter"]
        return "cwms-tools place search", out
    if repair.tool == "cwms_list_parameters" and "office" in args and "name" in args:
        return "cwms-tools place parameters", {"spec": f"{args['office']}/{args['name']}"}
    if repair.tool == "cwms_list_offices":
        return "cwms-tools offices", {}
    return None


def localize_repair(error: CwmsToolsError) -> CwmsToolsError:
    """Rewrite a core-built repair that names an MCP tool into a runnable CLI command.

    Core producers build generic repairs against the MCP tools
    (`cwms_search_places`, `cwms_list_parameters`, `cwms_list_offices`) and
    name `cwms_get_profile` in the depth-string `alternative`; printed verbatim
    by the CLI, none of them can be replayed (#96). The tool rewrite is a no-op
    when `repair.tool` is already a `cwms-tools ...` command (an intent- or
    ghost-office-built repair) and for unmapped tool names; the `alternative`
    rewrite applies to any repair, since an intent-built CLI repair carries
    the core alternative text. Call after `attach_repair_intent`.
    """
    repair = error.envelope.repair
    if repair is None:
        return error
    update: dict[str, Any] = {}
    if not repair.tool.startswith("cwms-tools "):
        mapped = _localize_tool(repair)
        if mapped is not None:
            update["tool"], update["arguments"] = mapped
    if repair.alternative is not None:
        alternative = _PROFILE_ALTERNATIVE.sub(
            lambda m: f"cwms-tools value profile {m['office']}/{m['name']}/{m['parameter']}",
            repair.alternative,
        )
        if alternative != repair.alternative:
            update["alternative"] = alternative
    if update:
        error.envelope.repair = repair.model_copy(update=update)
    return error


def isolated() -> bool:
    """Return True if the caller asked to bypass on-disk cache + env reads."""
    return _state["isolated"] or os.environ.get("_CWMS_TOOLS_ISOLATED") == "1"


def no_cache() -> bool:
    """Return True if the caller asked to bypass on-disk cache."""
    return _state["no_cache"] or _state["isolated"] or os.environ.get("_CWMS_TOOLS_NO_CACHE") == "1"


__all__ = [
    "OutputMode",
    "attach_ghost_office_repair",
    "attach_ghost_office_spec_repair",
    "attach_repair_intent",
    "diagnostic",
    "emit",
    "emit_error",
    "isolated",
    "localize_repair",
    "no_cache",
    "rewrite_error_field",
    "set_isolated",
    "set_machine",
    "set_no_cache",
]
