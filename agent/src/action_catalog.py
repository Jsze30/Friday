from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("friday-agent.actions")

_REFERENCE_ARGUMENT_PATTERN = re.compile(
    r"^(?:it|this|that|there|here|the\s+(?:app|application|page|site|"
    r"website|tab|window|file|folder|song|track|playlist|project|repo|repository|"
    r"document))$",
    re.IGNORECASE,
)

_MULTI_ACTION_PATTERN = re.compile(
    r"\b(?:and|then|after\s+that|followed\s+by)\s+"
    r"(?:open|close|quit|play|pause|press|click|select|choose|type|scroll|"
    r"search|find|analy[sz]e|debug|explain|write|move|delete|run)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ActionMatch:
    action_id: str
    arguments: dict[str, Any]
    target: dict[str, Any]
    reason: str


@dataclass(frozen=True)
class _CompiledRoute:
    action: dict[str, Any]
    pattern: re.Pattern[str]
    fixed_arguments: dict[str, Any]
    score: tuple[int, int]


def _command_text(text: str | None) -> str:
    command = " ".join((text or "").split()).strip()
    command = re.sub(
        r"^(?:(?:hey\s+)?friday[\s,]+)?"
        r"(?:(?:please|(?:can|could|would)\s+you)\s+)?",
        "",
        command,
        flags=re.IGNORECASE,
    )
    return command.rstrip(".!?").strip()


def action_arguments_need_resolution(
    arguments: dict[str, Any],
    resolutions: list[dict[str, Any]] | None = None,
) -> bool:
    raw_values = {
        value.strip().casefold()
        for value in arguments.values()
        if isinstance(value, str)
    }
    if any(_REFERENCE_ARGUMENT_PATTERN.fullmatch(value) for value in raw_values):
        return True
    values = {
        re.sub(
            r"^(?:the|this|that)\s+", "", value.strip(), flags=re.IGNORECASE
        ).casefold()
        for value in arguments.values()
        if isinstance(value, str)
    }
    resolution_phrases = {
        re.sub(
            r"^(?:the|this|that)\s+",
            "",
            str(resolution.get("phrase") or "").strip(),
            flags=re.IGNORECASE,
        ).casefold()
        for resolution in resolutions or []
        if isinstance(resolution, dict)
    }
    return bool(values & resolution_phrases)


def merge_action_manifests(
    capability_catalog: dict[str, Any] | None,
    primitive_manifests: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for raw in (capability_catalog or {}).get("actions") or []:
        if isinstance(raw, dict):
            actions.append(dict(raw))
    for primitive in primitive_manifests or []:
        if not isinstance(primitive, dict):
            continue
        for raw in primitive.get("actions") or []:
            if not isinstance(raw, dict):
                continue
            action = dict(raw)
            action.setdefault(
                "target",
                {
                    "kind": "primitive",
                    "tool": primitive.get("name"),
                },
            )
            action.setdefault("permission", primitive.get("permission", "read_only"))
            action.setdefault("parameters", primitive.get("parameters") or [])
            actions.append(action)
    return actions


class ActionCatalog:
    def __init__(self, manifests: list[dict[str, Any]] | None = None) -> None:
        self._actions: dict[str, dict[str, Any]] = {}
        self._routes: list[_CompiledRoute] = []
        for manifest in manifests or []:
            self._register(manifest)
        self._routes.sort(key=lambda route: route.score, reverse=True)

    @property
    def action_ids(self) -> list[str]:
        return sorted(self._actions)

    def tool_summary(self) -> str:
        rows = []
        for action_id in self.action_ids:
            action = self._actions[action_id]
            parameters = ", ".join(
                str(parameter.get("name"))
                + ("" if parameter.get("required", True) else "?")
                for parameter in action.get("parameters") or []
                if isinstance(parameter, dict) and parameter.get("name")
            )
            rows.append(f"{action_id}({parameters})")
        return ", ".join(rows) or "none"

    def get(self, action_id: str) -> dict[str, Any] | None:
        return self._actions.get(action_id.casefold())

    def normalize_arguments(
        self,
        action_id: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        action = self.get(action_id)
        if action is None:
            return None
        return self._normalize_arguments(action, arguments)

    def match(self, text: str | None) -> ActionMatch | None:
        command = _command_text(text)
        if not command or _MULTI_ACTION_PATTERN.search(command):
            return None

        matches: list[tuple[_CompiledRoute, dict[str, Any]]] = []
        for route in self._routes:
            matched = route.pattern.fullmatch(command)
            if not matched:
                continue
            arguments = {
                name: value.strip() if isinstance(value, str) else value
                for name, value in matched.groupdict().items()
                if value is not None and (not isinstance(value, str) or value.strip())
            }
            arguments.update(route.fixed_arguments)
            normalized = self._normalize_arguments(route.action, arguments)
            if normalized is not None:
                matches.append((route, normalized))

        if not matches:
            return None
        best_score = matches[0][0].score
        best = [match for match in matches if match[0].score == best_score]
        action_ids = {match[0].action["id"] for match in best}
        if len(action_ids) > 1:
            log.warning(
                "ambiguous deterministic action command=%r actions=%s",
                command,
                sorted(action_ids),
            )
            return None
        route, arguments = best[0]
        action = route.action
        return ActionMatch(
            action_id=action["id"],
            arguments=arguments,
            target=action["target"],
            reason=f"catalog action {action['id']}",
        )

    def _register(self, raw: dict[str, Any]) -> None:
        action_id = str(raw.get("id") or "").strip().casefold()
        target = raw.get("target")
        routes = raw.get("routes")
        if (
            not action_id
            or not isinstance(target, dict)
            or target.get("kind") not in {"capability", "primitive"}
            or not isinstance(routes, list)
        ):
            return
        action = dict(raw)
        action["id"] = action_id
        action["target"] = dict(target)
        self._actions[action_id] = action
        priority = int(action.get("priority") or 50)
        for route in routes[:20]:
            if not isinstance(route, dict):
                continue
            pattern_text = str(route.get("pattern") or "")
            if not pattern_text or len(pattern_text) > 1_000:
                continue
            try:
                pattern = re.compile(pattern_text, re.IGNORECASE)
            except re.error:
                log.warning(
                    "ignored invalid action route action=%s pattern=%r",
                    action_id,
                    pattern_text,
                )
                continue
            fixed = route.get("arguments") or {}
            if not isinstance(fixed, dict):
                continue
            literal_weight = len(re.sub(r"[^a-z0-9]+", "", pattern_text.casefold()))
            self._routes.append(
                _CompiledRoute(
                    action=action,
                    pattern=pattern,
                    fixed_arguments=dict(fixed),
                    score=(priority, literal_weight),
                )
            )

    @staticmethod
    def _normalize_arguments(
        action: dict[str, Any],
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        parameters = {
            str(parameter.get("name")): parameter
            for parameter in action.get("parameters") or []
            if isinstance(parameter, dict) and parameter.get("name")
        }
        if set(arguments) - set(parameters):
            return None
        normalized: dict[str, Any] = {}
        for name, parameter in parameters.items():
            if name not in arguments:
                if parameter.get("required", True):
                    return None
                continue
            value = arguments[name]
            parameter_type = parameter.get("type", "string")
            try:
                if parameter_type == "integer":
                    value = int(value)
                elif parameter_type == "number":
                    value = float(value)
                elif parameter_type == "boolean":
                    if not isinstance(value, bool):
                        normalized_boolean = str(value).casefold()
                        if normalized_boolean not in {"true", "false"}:
                            return None
                        value = normalized_boolean == "true"
                elif parameter_type == "string":
                    value = str(value).strip()
                else:
                    return None
            except (TypeError, ValueError):
                return None
            minimum = parameter.get("minimum")
            maximum = parameter.get("maximum")
            if minimum is not None and value < minimum:
                return None
            if maximum is not None and value > maximum:
                return None
            choices = parameter.get("choices") or []
            if choices and value not in choices:
                return None
            normalized[name] = value
        return normalized
