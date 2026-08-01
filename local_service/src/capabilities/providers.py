from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..tools.primitives import (
    _resolve_allowed_path,
    fetch_url,
    inspect_path,
    search_files,
    web_search,
)
from .base import (
    ActionDefinition,
    ActionParameter,
    ActionRoute,
    CapabilityProvider,
    CapabilityRequest,
    CapabilityResult,
    ProgressCallback,
    ProviderFailed,
    ProviderInfo,
)

MAX_SOURCE_CHARS = 2_500
MAX_CODE_RESULT_CHARS = 10_000
CODE_TIMEOUT_SECONDS = 180
PERMISSIONS = {"read_only", "low_risk_write", "sensitive"}


def _error_from_data(data: dict[str, Any] | None) -> str | None:
    if not data:
        return None
    error = data.get("error")
    return str(error) if error else None


class FileProvider(CapabilityProvider):
    info = ProviderInfo(
        provider_id="files-direct",
        name="local files",
        description="Lists, reads, and searches allowed local files.",
        capabilities=("files",),
        priority=100,
        reliability=0.98,
        latency=0,
    )

    async def execute(
        self,
        request: CapabilityRequest,
        progress: ProgressCallback,
    ) -> CapabilityResult:
        operation = str(request.inputs.get("operation") or "search").casefold()
        root = str(request.inputs.get("root") or "Documents")
        if operation in {"list", "read", "inspect"}:
            path = str(request.inputs.get("path") or root)
            await progress("inspect", f"Inspecting {path}.")
            tool_result = await inspect_path(path=path, max_chars=10_000)
        elif operation == "search":
            query = str(request.inputs.get("query") or request.goal).strip()
            await progress("search", f"Searching {root}.")
            tool_result = await search_files(
                root=root,
                query=query,
                max_results=30,
            )
        else:
            raise ProviderFailed(
                "files operation must be list, read, inspect, or search"
            )
        error = _error_from_data(tool_result.data)
        if error:
            raise ProviderFailed(tool_result.spoken or error)
        return CapabilityResult(
            summary=tool_result.spoken or "File task completed.",
            data=tool_result.data or {},
        )


class ResearchProvider(CapabilityProvider):
    info = ProviderInfo(
        provider_id="research-direct",
        name="web research",
        description="Searches the public web and reads the best matching pages.",
        capabilities=("research", "web"),
        priority=100,
        reliability=0.9,
        latency=1,
    )

    async def execute(
        self,
        request: CapabilityRequest,
        progress: ProgressCallback,
    ) -> CapabilityResult:
        exact_url = str(request.inputs.get("url") or "").strip()
        if exact_url:
            await progress("read", f"Reading {exact_url}.")
            fetched = await fetch_url(
                url=exact_url,
                max_chars=10_000,
            )
            error = _error_from_data(fetched.data)
            if error:
                raise ProviderFailed(fetched.spoken or error)
            return CapabilityResult(
                summary=fetched.spoken or "Read the requested web page.",
                data=fetched.data or {},
            )

        query = str(request.inputs.get("query") or request.goal).strip()
        max_sources = max(
            1,
            min(int(request.inputs.get("max_sources") or 3), 5),
        )
        if not query:
            raise ProviderFailed("research query cannot be empty")
        await progress("search", f"Searching the web for {query}.")
        search_result = await web_search(query=query, max_results=max_sources)
        error = _error_from_data(search_result.data)
        if error:
            raise ProviderFailed(search_result.spoken or error)
        results = (search_result.data or {}).get("results") or []
        if not results:
            raise ProviderFailed("the web search returned no results")

        await progress("read", f"Reading {len(results)} sources.")

        async def read_source(item: dict[str, Any]) -> dict[str, Any]:
            value = {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("snippet") or ""),
            }
            if not value["url"]:
                return value
            fetched = await fetch_url(
                url=value["url"],
                max_chars=MAX_SOURCE_CHARS,
            )
            if not _error_from_data(fetched.data):
                value["content"] = str((fetched.data or {}).get("content") or "")
            return value

        sources = await asyncio.gather(
            *(read_source(item) for item in results),
        )
        readable = sum(bool(source.get("content")) for source in sources)
        return CapabilityResult(
            summary=(
                f"Found {len(sources)} sources for {query} and read {readable} of them."
            ),
            data={
                "query": query,
                "sources": sources,
                "searchProvider": (search_result.data or {}).get("provider"),
            },
        )

    async def verify(
        self,
        request: CapabilityRequest,
        result: CapabilityResult,
    ) -> tuple[bool, str | None]:
        if request.inputs.get("url") and result.data.get("content"):
            return True, None
        if not result.data.get("sources"):
            return False, "research returned no sources"
        return True, None


def _find_codex() -> str | None:
    configured = os.getenv("FRIDAY_CODE_AGENT")
    if configured:
        resolved = Path(configured).expanduser()
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return str(resolved)
        return shutil.which(configured)

    on_path = shutil.which("codex")
    if on_path:
        return on_path
    candidates = sorted(
        Path.home().glob(".nvm/versions/node/*/bin/codex"),
        reverse=True,
    )
    return next(
        (
            str(candidate)
            for candidate in candidates
            if candidate.is_file() and os.access(candidate, os.X_OK)
        ),
        None,
    )


class CodexProvider(CapabilityProvider):
    info = ProviderInfo(
        provider_id="codex-readonly",
        name="coding specialist",
        description="Uses a read-only coding agent to inspect and explain a project.",
        capabilities=("coding",),
        priority=100,
        reliability=0.9,
        latency=3,
    )

    async def available(self) -> bool:
        return _find_codex() is not None

    async def execute(
        self,
        request: CapabilityRequest,
        progress: ProgressCallback,
    ) -> CapabilityResult:
        executable = _find_codex()
        if not executable:
            raise ProviderFailed("the coding specialist is not installed")
        root_value = str(request.inputs.get("root") or "Friday project")
        try:
            root = _resolve_allowed_path(root_value)
        except ValueError as error:
            raise ProviderFailed(str(error)) from error
        if not root.is_dir():
            raise ProviderFailed(f"{root} is not a directory")

        await progress("analyze", f"Analyzing {root.name} without changing it.")
        prompt = (
            "Work in read-only mode. Do not edit files, install software, make "
            "network mutations, or ask for approval. Inspect the repository as "
            f"needed and answer this task clearly:\n\n{request.goal}"
        )
        output_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="friday-codex-",
                suffix=".txt",
                delete=False,
            ) as output:
                output_path = Path(output.name)
            process = await asyncio.create_subprocess_exec(
                executable,
                "exec",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--color",
                "never",
                "--output-last-message",
                str(output_path),
                "--cd",
                str(root),
                prompt,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=CODE_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                process.kill()
                await process.wait()
                raise
            except TimeoutError:
                process.kill()
                await process.wait()
                raise ProviderFailed("the coding specialist timed out")
            if process.returncode != 0:
                detail = stderr.decode(errors="replace")[-2_000:].strip()
                raise ProviderFailed(
                    detail or f"coding specialist exited {process.returncode}"
                )
            answer = output_path.read_text(errors="replace").strip()
            if not answer:
                answer = stdout.decode(errors="replace").strip()
            if not answer:
                raise ProviderFailed("the coding specialist returned no answer")
            truncated = len(answer) > MAX_CODE_RESULT_CHARS
            answer = answer[:MAX_CODE_RESULT_CHARS]
            return CapabilityResult(
                summary="The coding specialist finished its read-only analysis.",
                data={
                    "answer": answer,
                    "root": str(root),
                    "truncated": truncated,
                },
            )
        finally:
            if output_path is not None:
                output_path.unlink(missing_ok=True)

    async def verify(
        self,
        request: CapabilityRequest,
        result: CapabilityResult,
    ) -> tuple[bool, str | None]:
        if not str(result.data.get("answer") or "").strip():
            return False, "coding specialist returned an empty answer"
        return True, None


class CommandProvider(CapabilityProvider):
    def __init__(self, config: dict[str, Any]) -> None:
        command = config.get("command")
        capabilities = config.get("capabilities")
        if not isinstance(command, list) or not all(
            isinstance(item, str) and item for item in command
        ):
            raise ValueError("external provider command must be a string array")
        if not isinstance(capabilities, list) or not all(
            isinstance(item, str) and item for item in capabilities
        ):
            raise ValueError("external provider capabilities must be a string array")
        permission = str(config.get("permission") or "read_only")
        if permission not in PERMISSIONS:
            raise ValueError("external provider permission is invalid")
        actions = _configured_actions(
            config.get("actions"),
            set(capabilities),
            permission,
        )
        self._command = tuple(command)
        provider_id = str(config.get("id") or command[0])
        self.info = ProviderInfo(
            provider_id=provider_id,
            name=str(config.get("name") or provider_id),
            description=str(
                config.get("description") or "Configured external capability provider."
            ),
            capabilities=tuple(capabilities),
            actions=actions,
            permission=permission,
            priority=int(config.get("priority") or 50),
            reliability=float(config.get("reliability") or 0.8),
            latency=int(config.get("latency") or 2),
        )
        self._timeout = max(1, min(int(config.get("timeout_seconds") or 120), 600))

    async def available(self) -> bool:
        executable = self._command[0]
        return (
            Path(executable).expanduser().is_file()
            if "/" in executable
            else shutil.which(executable) is not None
        )

    async def execute(
        self,
        request: CapabilityRequest,
        progress: ProgressCallback,
    ) -> CapabilityResult:
        await progress("external", f"Running {self.info.name}.")
        command = list(self._command)
        if "/" in command[0]:
            command[0] = str(Path(command[0]).expanduser())
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = json.dumps(
            {
                "capability": request.capability,
                "goal": request.goal,
                "inputs": request.inputs,
                "permission": request.permission,
            }
        ).encode()
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload),
                timeout=self._timeout,
            )
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        except TimeoutError:
            process.kill()
            await process.wait()
            raise ProviderFailed(f"{self.info.name} timed out")
        if process.returncode != 0:
            detail = stderr.decode(errors="replace")[-2_000:].strip()
            raise ProviderFailed(
                detail or f"{self.info.name} exited {process.returncode}"
            )
        try:
            value = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise ProviderFailed(f"{self.info.name} returned invalid JSON") from error
        if not isinstance(value, dict):
            raise ProviderFailed(f"{self.info.name} returned a non-object")
        return CapabilityResult(
            summary=str(value.get("summary") or f"{self.info.name} finished."),
            data=value.get("data") if isinstance(value.get("data"), dict) else value,
        )


def _configured_actions(
    value: Any,
    capabilities: set[str],
    provider_permission: str,
) -> tuple[ActionDefinition, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TypeError("external provider actions must be an array")
    actions: list[ActionDefinition] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise TypeError("external provider action must be an object")
        action_id = str(raw.get("id") or "").strip().casefold()
        capability = str(raw.get("capability") or "").strip().casefold()
        if not action_id or not capability:
            raise ValueError("external provider actions require id and capability")
        if capability not in capabilities:
            raise ValueError(f"external action capability {capability} is not declared")
        permission = str(raw.get("permission") or provider_permission)
        if permission not in PERMISSIONS:
            raise ValueError(f"external action {action_id} permission is invalid")
        parameters = raw.get("parameters") or []
        routes = raw.get("routes") or []
        if not isinstance(parameters, list) or not isinstance(routes, list):
            raise TypeError(
                f"external action {action_id} parameters and routes must be arrays"
            )
        actions.append(
            ActionDefinition(
                action_id=action_id,
                capability=capability,
                operation=str(raw.get("operation") or action_id.rsplit(".", 1)[-1]),
                description=str(raw.get("description") or action_id),
                parameters=tuple(
                    _configured_action_parameter(action_id, parameter)
                    for parameter in parameters
                ),
                routes=tuple(
                    _configured_action_route(action_id, route) for route in routes
                ),
                permission=permission,
                latency_ms=max(0, int(raw.get("latency_ms") or 500)),
                priority=int(raw.get("priority") or 50),
            )
        )
    return tuple(actions)


def _configured_action_parameter(
    action_id: str,
    raw: Any,
) -> ActionParameter:
    if not isinstance(raw, dict):
        raise TypeError(f"external action {action_id} parameter must be an object")
    name = str(raw.get("name") or "").strip()
    parameter_type = str(raw.get("type") or "string")
    if not name or parameter_type not in {"string", "integer", "number", "boolean"}:
        raise ValueError(f"external action {action_id} parameter is invalid")
    choices = raw.get("choices") or []
    if not isinstance(choices, list) or not all(
        isinstance(choice, str) for choice in choices
    ):
        raise ValueError(
            f"external action {action_id} parameter choices must be strings"
        )
    minimum = raw.get("minimum")
    maximum = raw.get("maximum")
    if minimum is not None and not isinstance(minimum, (int, float)):
        raise ValueError(
            f"external action {action_id} parameter minimum must be a number"
        )
    if maximum is not None and not isinstance(maximum, (int, float)):
        raise ValueError(
            f"external action {action_id} parameter maximum must be a number"
        )
    return ActionParameter(
        name=name,
        type=parameter_type,
        description=str(raw.get("description") or ""),
        required=bool(raw.get("required", True)),
        minimum=minimum,
        maximum=maximum,
        choices=tuple(choices),
    )


def _configured_action_route(
    action_id: str,
    raw: Any,
) -> ActionRoute:
    if not isinstance(raw, dict):
        raise TypeError(f"external action {action_id} route must be an object")
    pattern = str(raw.get("pattern") or "")
    arguments = raw.get("arguments") or {}
    if not pattern or not isinstance(arguments, dict):
        raise ValueError(f"external action {action_id} route is invalid")
    return ActionRoute(pattern=pattern, fixed_arguments=arguments)


def configured_command_providers() -> list[CommandProvider]:
    raw = os.getenv("FRIDAY_CAPABILITY_PROVIDERS_JSON", "").strip()
    if not raw:
        return []
    value = json.loads(raw)
    if not isinstance(value, list):
        raise TypeError("FRIDAY_CAPABILITY_PROVIDERS_JSON must be a JSON array")
    return [CommandProvider(item) for item in value if isinstance(item, dict)]
