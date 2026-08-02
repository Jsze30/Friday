from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .config import settings


@dataclass(frozen=True)
class ApplicationIdentity:
    name: str
    bundle_id: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class WebDestination:
    name: str
    url: str
    aliases: tuple[str, ...]


APPLICATIONS = (
    ApplicationIdentity(
        "Minecraft Launcher",
        "com.mojang.minecraftlauncher",
        ("minecraft", "minecraft launcher"),
    ),
    ApplicationIdentity(
        "Arc",
        "company.thebrowser.Browser",
        ("arc", "arc browser"),
    ),
    ApplicationIdentity(
        "Google Chrome",
        "com.google.Chrome",
        ("chrome", "google chrome"),
    ),
    ApplicationIdentity(
        "Safari",
        "com.apple.Safari",
        ("safari",),
    ),
    ApplicationIdentity(
        "Spotify",
        "com.spotify.client",
        ("spotify",),
    ),
    ApplicationIdentity(
        "Visual Studio Code",
        "com.microsoft.VSCode",
        ("code", "vs code", "vscode", "visual studio code"),
    ),
    ApplicationIdentity(
        "System Settings",
        "com.apple.systempreferences",
        ("settings", "system settings", "preferences", "system preferences"),
    ),
)

WEB_DESTINATIONS = (
    WebDestination(
        "YouTube",
        "https://www.youtube.com",
        ("youtube", "you tube"),
    ),
    WebDestination(
        "GitHub",
        "https://github.com",
        ("github", "git hub"),
    ),
    WebDestination(
        "Gmail",
        "https://mail.google.com",
        ("gmail", "google mail"),
    ),
    WebDestination(
        "Google Calendar",
        "https://calendar.google.com",
        ("google calendar",),
    ),
)


def _normalized(value: Any) -> str:
    return "".join(
        character for character in str(value or "").casefold() if character.isalnum()
    )


def application_aliases() -> dict[str, ApplicationIdentity]:
    aliases: dict[str, ApplicationIdentity] = {}
    for identity in APPLICATIONS:
        for alias in (*identity.aliases, identity.name, identity.bundle_id):
            aliases[_normalized(alias)] = identity
    for alias, target in settings.app_aliases.items():
        target_identity = next(
            (
                identity
                for identity in APPLICATIONS
                if _normalized(target)
                in {
                    _normalized(identity.name),
                    _normalized(identity.bundle_id),
                    *(_normalized(value) for value in identity.aliases),
                }
            ),
            None,
        )
        if target_identity:
            aliases[_normalized(alias)] = target_identity
        else:
            aliases[_normalized(alias)] = ApplicationIdentity(
                name=str(alias),
                bundle_id=str(target),
                aliases=(str(alias),),
            )
    return aliases


def resolve_application(value: Any) -> ApplicationIdentity:
    requested = " ".join(str(value or "").split()).strip()
    identity = application_aliases().get(_normalized(requested))
    if identity:
        return identity
    return ApplicationIdentity(requested, requested, (requested,))


def destination_aliases() -> dict[str, WebDestination]:
    aliases: dict[str, WebDestination] = {}
    for destination in WEB_DESTINATIONS:
        for alias in (*destination.aliases, destination.name):
            aliases[_normalized(alias)] = destination
    for alias, url in settings.web_destinations.items():
        destination = WebDestination(str(alias), str(url), (str(alias),))
        aliases[_normalized(alias)] = destination
    return aliases


def resolve_destination(value: Any) -> WebDestination | None:
    requested = " ".join(str(value or "").split()).strip()
    destination = destination_aliases().get(_normalized(requested))
    if destination:
        return destination
    candidate = requested if "://" in requested else f"https://{requested}"
    parsed = urlsplit(candidate)
    if (
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and "." in parsed.hostname
    ):
        return WebDestination(parsed.hostname, candidate, (requested,))
    return None


def destination_action_routes() -> tuple[tuple[str, str], ...]:
    routes: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    destinations: dict[str, WebDestination] = {}
    for destination in destination_aliases().values():
        destinations[destination.url] = destination
    for destination in destinations.values():
        aliases = sorted(
            set(destination.aliases) | {destination.name},
            key=len,
            reverse=True,
        )
        alternative = "|".join(
            re.escape(alias).replace(r"\ ", r"\s+") for alias in aliases
        )
        pattern = (
            rf"(?:open|launch|visit|go\s+to)\s+(?:the\s+)?(?:{alternative})"
            rf"(?:\s+(?:website|site))?"
            rf"(?:\s+(?:in|with|using)\s+(?P<browser>[\w .'-]+))?"
        )
        route = (pattern, destination.name)
        if route not in seen:
            routes.append(route)
            seen.add(route)
    return tuple(routes)


def destination_in_goal(goal: str) -> WebDestination | None:
    normalized_goal = _normalized(goal)
    if not re.search(r"\b(?:open|launch|visit|go\s+to)\b", goal, re.IGNORECASE):
        return None
    candidates = sorted(
        destination_aliases().items(), key=lambda item: len(item[0]), reverse=True
    )
    return next(
        (destination for alias, destination in candidates if alias in normalized_goal),
        None,
    )


def explicit_browser_in_goal(goal: str) -> str | None:
    match = re.search(
        r"\b(?:in|with|using)\s+"
        r"(?P<browser>arc(?:\s+browser)?|google\s+chrome|chrome|safari)\b",
        goal,
        re.IGNORECASE,
    )
    return " ".join(match.group("browser").split()) if match else None


def application_matches(
    identity: ApplicationIdentity,
    *,
    name: Any = None,
    bundle_id: Any = None,
) -> bool:
    actual_bundle = str(bundle_id or "").casefold()
    if identity.bundle_id and actual_bundle == identity.bundle_id.casefold():
        return True
    actual_name = _normalized(name)
    expected_names = {
        _normalized(identity.name),
        *(_normalized(alias) for alias in identity.aliases),
    }
    return bool(
        actual_name
        and any(
            actual_name == expected
            or actual_name in expected
            or expected in actual_name
            for expected in expected_names
            if expected
        )
    )
