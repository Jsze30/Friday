from __future__ import annotations

import asyncio
import html
import ipaddress
import os
import shutil
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .base import ToolParam, ToolResult, tool

MAX_FILE_CHARS = 10_000
MAX_HTTP_CHARS = 10_000
MAX_PROCESS_CHARS = 6_000
MAX_WRITE_CHARS = 10_000
MAX_DIRECTORY_ENTRIES = 40
MAX_SEARCH_RESULTS = 30
MAX_SEARCHED_FILES = 20_000
HTTP_TIMEOUT_SECONDS = 8.0
PROCESS_TIMEOUT_SECONDS = 20
SENSITIVE_READ_COMPONENTS = {
    ".aws",
    ".git",
    ".gnupg",
    ".ssh",
}
SENSITIVE_READ_FILENAMES = {
    ".env",
    ".env.local",
    "credentials.json",
    "secrets.env",
}
IGNORED_SEARCH_DIRECTORIES = {
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "DerivedData",
    "node_modules",
    "venv",
}


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
        elif tag in {"br", "p", "div", "li", "article", "section", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag in {"p", "div", "li", "article", "section"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


def _allowed_roots() -> tuple[Path, ...]:
    home = Path.home().resolve()
    project = Path(__file__).resolve().parents[3]
    configured = os.getenv("FRIDAY_ALLOWED_PATHS", "")
    extra = [
        Path(value).expanduser().resolve()
        for value in configured.split(os.pathsep)
        if value.strip()
    ]
    return tuple(
        dict.fromkeys(
            [
                project,
                home / "Desktop",
                home / "Documents",
                home / "Downloads",
                *extra,
            ]
        )
    )


def _path_aliases() -> dict[str, Path]:
    home = Path.home().resolve()
    project = Path(__file__).resolve().parents[3]
    return {
        "desktop": home / "Desktop",
        "my desktop": home / "Desktop",
        "documents": home / "Documents",
        "my documents": home / "Documents",
        "downloads": home / "Downloads",
        "my downloads": home / "Downloads",
        "friday project": project,
        "project": project,
    }


def _resolve_allowed_path(raw_path: str) -> Path:
    normalized = raw_path.strip().casefold()
    aliased = _path_aliases().get(normalized)
    path = aliased if aliased is not None else Path(raw_path).expanduser()
    resolved = path.resolve(strict=False)
    for root in _allowed_roots():
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise ValueError(
        "path is outside Friday's allowed roots: "
        + ", ".join(str(root) for root in _allowed_roots())
    )


def _truncate(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _is_sensitive_read_path(path: Path) -> bool:
    if any(component in SENSITIVE_READ_COMPONENTS for component in path.parts):
        return True
    name = path.name.lower()
    if name in SENSITIVE_READ_FILENAMES:
        return True
    return name.startswith(".env.") and not name.endswith((".example", ".sample"))


def _path_error(raw_path: str) -> ToolResult | None:
    try:
        resolved = _resolve_allowed_path(raw_path)
    except ValueError as error:
        return ToolResult(spoken=str(error), data={"error": "path_not_allowed"})
    if _is_sensitive_read_path(resolved):
        return ToolResult(
            spoken="That path may contain credentials and cannot be accessed directly.",
            data={"error": "sensitive_path", "path": str(resolved)},
        )
    return None


@tool(
    name="inspect_path",
    description=(
        "Read a text file or list one directory on the user's Mac. Use this "
        "whenever the user asks what is in Desktop, Documents, Downloads, or "
        "the Friday project. Human names such as 'Downloads' are accepted."
    ),
    parameters=[
        ToolParam(
            name="path",
            type="string",
            description=(
                "Path, ~ path, or one of: Desktop, Documents, Downloads, "
                "Friday project."
            ),
        ),
        ToolParam(
            name="max_chars",
            type="integer",
            description="Maximum text characters to return. Defaults to 10000.",
            required=False,
        ),
    ],
    permission="read_only",
)
async def inspect_path(path: str, max_chars: int | None = None) -> ToolResult:
    error = _path_error(path)
    if error:
        return error
    resolved = _resolve_allowed_path(path)
    limit = max(1, min(max_chars or MAX_FILE_CHARS, MAX_FILE_CHARS))
    if not resolved.exists():
        return ToolResult(
            spoken=f"{resolved} does not exist.",
            data={"error": "not_found", "path": str(resolved)},
        )

    if resolved.is_dir():
        entries = await asyncio.to_thread(
            lambda: sorted(
                resolved.iterdir(),
                key=lambda item: (not item.is_dir(), item.name.lower()),
            )
        )
        visible = entries[:MAX_DIRECTORY_ENTRIES]
        return ToolResult(
            spoken=f"Listed {len(visible)} items in {resolved}.",
            data={
                "path": str(resolved),
                "kind": "directory",
                "entries": [
                    {
                        "name": item.name,
                        "kind": "directory" if item.is_dir() else "file",
                    }
                    for item in visible
                ],
                "truncated": len(entries) > len(visible),
            },
        )

    try:
        raw = await asyncio.to_thread(resolved.read_bytes)
    except OSError as error:
        return ToolResult(
            spoken=f"I could not read {resolved}.",
            data={"error": "read_failed", "detail": str(error)},
        )

    if b"\x00" in raw[:4096]:
        return ToolResult(
            spoken=f"{resolved} is not a text file.",
            data={"error": "binary_file", "path": str(resolved), "size": len(raw)},
        )

    text = raw.decode("utf-8", errors="replace")
    content, truncated = _truncate(text, limit)
    return ToolResult(
        spoken=f"Read {resolved}.",
        data={
            "path": str(resolved),
            "kind": "file",
            "content": content,
            "size": len(raw),
            "truncated": truncated,
        },
    )


@tool(
    name="search_files",
    description=(
        "Search file and folder names below an allowed directory. Use this when "
        "the user describes a file but does not provide its exact path."
    ),
    parameters=[
        ToolParam(
            name="root",
            type="string",
            description="Directory path or Desktop, Documents, Downloads, Friday project.",
        ),
        ToolParam(
            name="query",
            type="string",
            description="Case-insensitive text that should appear in the file or folder name.",
        ),
        ToolParam(
            name="max_results",
            type="integer",
            description="Maximum matching paths to return. Defaults to 30.",
            required=False,
        ),
    ],
    permission="read_only",
)
async def search_files(
    root: str,
    query: str,
    max_results: int | None = None,
) -> ToolResult:
    error = _path_error(root)
    if error:
        return error
    resolved = _resolve_allowed_path(root)
    if not resolved.is_dir():
        return ToolResult(
            spoken=f"{resolved} is not a directory.",
            data={"error": "not_directory", "path": str(resolved)},
        )

    needle = query.strip().casefold()
    if not needle:
        return ToolResult(
            spoken="The search query cannot be empty.",
            data={"error": "empty_query"},
        )
    limit = max(1, min(max_results or 50, MAX_SEARCH_RESULTS))

    def _search() -> tuple[list[dict[str, str]], int, bool]:
        matches: list[dict[str, str]] = []
        scanned = 0
        stopped_early = False
        for current_root, directories, files in os.walk(resolved):
            directories[:] = [
                name
                for name in directories
                if name not in SENSITIVE_READ_COMPONENTS
                and name not in IGNORED_SEARCH_DIRECTORIES
            ]
            for name, kind in [
                *((name, "directory") for name in directories),
                *((name, "file") for name in files),
            ]:
                scanned += 1
                if scanned > MAX_SEARCHED_FILES:
                    stopped_early = True
                    return matches, scanned - 1, stopped_early
                candidate = Path(current_root) / name
                if _is_sensitive_read_path(candidate):
                    continue
                if needle in name.casefold():
                    matches.append(
                        {
                            "path": str(candidate),
                            "kind": kind,
                        }
                    )
                    if len(matches) >= limit:
                        stopped_early = True
                        return matches, scanned, stopped_early
        return matches, scanned, stopped_early

    matches, scanned, stopped_early = await asyncio.to_thread(_search)
    return ToolResult(
        spoken=f"Found {len(matches)} matching paths below {resolved}.",
        data={
            "root": str(resolved),
            "query": query,
            "matches": matches,
            "scanned": scanned,
            "truncated": stopped_early,
        },
    )


@tool(
    name="create_directory",
    description="Create a folder inside an allowed user or project directory.",
    parameters=[
        ToolParam(
            name="path",
            type="string",
            description="New folder path inside Desktop, Documents, Downloads, or the project.",
        )
    ],
    permission="low_risk_write",
)
async def create_directory(path: str) -> ToolResult:
    try:
        resolved = _resolve_allowed_path(path)
        if _is_sensitive_read_path(resolved):
            return ToolResult(
                spoken="That path may contain credentials and cannot be changed directly.",
                data={"error": "sensitive_path", "path": str(resolved)},
            )
        await asyncio.to_thread(resolved.mkdir, parents=True, exist_ok=True)
    except (ValueError, OSError) as error:
        return ToolResult(
            spoken="I could not create that folder.",
            data={"error": "create_failed", "detail": str(error)},
        )
    return ToolResult(
        spoken=f"Created {resolved}.",
        data={"path": str(resolved)},
    )


@tool(
    name="write_file",
    description=(
        "Create or replace a UTF-8 text file immediately inside Friday's "
        "allowed directories."
    ),
    parameters=[
        ToolParam(
            name="path",
            type="string",
            description="Destination inside Desktop, Documents, Downloads, or the project.",
        ),
        ToolParam(
            name="content",
            type="string",
            description="Complete UTF-8 text to write.",
        ),
    ],
    permission="sensitive",
)
async def write_file(path: str, content: str) -> ToolResult:
    try:
        resolved = _resolve_allowed_path(path)
    except ValueError as error:
        return ToolResult(spoken=str(error), data={"error": "path_not_allowed"})
    if _is_sensitive_read_path(resolved):
        return ToolResult(
            spoken="That path may contain credentials and cannot be changed directly.",
            data={"error": "sensitive_path", "path": str(resolved)},
        )
    if len(content) > MAX_WRITE_CHARS:
        return ToolResult(
            spoken="The requested file content is too large.",
            data={"error": "content_too_large", "maxCharacters": MAX_WRITE_CHARS},
        )

    def _write() -> None:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=resolved.parent,
            delete=False,
        ) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        temporary.replace(resolved)

    try:
        await asyncio.to_thread(_write)
    except OSError as error:
        return ToolResult(
            spoken=f"I could not write {resolved}.",
            data={"error": "write_failed", "detail": str(error)},
        )
    return ToolResult(
        spoken=f"Wrote {resolved}.",
        data={"path": str(resolved), "characters": len(content)},
    )


@tool(
    name="move_path",
    description=(
        "Move or rename a file or folder immediately within Friday's allowed "
        "directories."
    ),
    parameters=[
        ToolParam(
            name="source", type="string", description="Existing file or folder path."
        ),
        ToolParam(
            name="destination", type="string", description="New file or folder path."
        ),
    ],
    permission="sensitive",
)
async def move_path(source: str, destination: str) -> ToolResult:
    try:
        resolved_source = _resolve_allowed_path(source)
        resolved_destination = _resolve_allowed_path(destination)
    except ValueError as error:
        return ToolResult(spoken=str(error), data={"error": "path_not_allowed"})
    if _is_sensitive_read_path(resolved_source) or _is_sensitive_read_path(
        resolved_destination
    ):
        return ToolResult(
            spoken="A requested path may contain credentials and cannot be changed directly.",
            data={"error": "sensitive_path"},
        )
    if not resolved_source.exists():
        return ToolResult(
            spoken=f"{resolved_source} does not exist.",
            data={"error": "not_found", "path": str(resolved_source)},
        )
    if resolved_destination.exists():
        return ToolResult(
            spoken=f"{resolved_destination} already exists.",
            data={"error": "destination_exists", "path": str(resolved_destination)},
        )
    try:
        await asyncio.to_thread(
            resolved_destination.parent.mkdir, parents=True, exist_ok=True
        )
        await asyncio.to_thread(shutil.move, resolved_source, resolved_destination)
    except OSError as error:
        return ToolResult(
            spoken="I could not move that path.",
            data={"error": "move_failed", "detail": str(error)},
        )
    return ToolResult(
        spoken=f"Moved {resolved_source} to {resolved_destination}.",
        data={
            "source": str(resolved_source),
            "destination": str(resolved_destination),
        },
    )


@tool(
    name="trash_path",
    description=(
        "Move a file or folder immediately to the user's Trash so it can be recovered."
    ),
    parameters=[
        ToolParam(
            name="path", type="string", description="Existing file or folder path."
        )
    ],
    permission="sensitive",
)
async def trash_path(path: str) -> ToolResult:
    try:
        resolved = _resolve_allowed_path(path)
    except ValueError as error:
        return ToolResult(spoken=str(error), data={"error": "path_not_allowed"})
    if _is_sensitive_read_path(resolved):
        return ToolResult(
            spoken="That path may contain credentials and cannot be changed directly.",
            data={"error": "sensitive_path", "path": str(resolved)},
        )
    if not resolved.exists():
        return ToolResult(
            spoken=f"{resolved} does not exist.",
            data={"error": "not_found", "path": str(resolved)},
        )

    def _trash() -> Path:
        trash = Path.home() / ".Trash"
        trash.mkdir(parents=True, exist_ok=True)
        candidate = trash / resolved.name
        suffix = 2
        while candidate.exists():
            candidate = trash / f"{resolved.stem} {suffix}{resolved.suffix}"
            suffix += 1
        shutil.move(resolved, candidate)
        return candidate

    try:
        destination = await asyncio.to_thread(_trash)
    except OSError as error:
        return ToolResult(
            spoken="I could not move that item to Trash.",
            data={"error": "trash_failed", "detail": str(error)},
        )
    return ToolResult(
        spoken=f"Moved {resolved.name} to Trash.",
        data={
            "originalPath": str(resolved),
            "trashPath": str(destination),
            "recoverable": True,
        },
    )


@tool(
    name="run_process",
    description=(
        "Run one executable directly without a shell. Use for app CLIs and "
        "system commands. Execute immediately when requested."
    ),
    parameters=[
        ToolParam(
            name="executable",
            type="string",
            description="Executable name on PATH or an absolute executable path.",
        ),
        ToolParam(
            name="arguments",
            type="array",
            description="Ordered string arguments passed directly to the executable.",
            required=False,
        ),
        ToolParam(
            name="timeout_seconds",
            type="integer",
            description="Timeout from 1 to 30 seconds. Defaults to 20.",
            required=False,
        ),
    ],
    permission="sensitive",
)
async def run_process(
    executable: str,
    arguments: list[str] | None = None,
    timeout_seconds: int | None = None,
) -> ToolResult:
    argv = arguments or []
    if not all(isinstance(value, str) for value in argv):
        return ToolResult(
            spoken="Every process argument must be text.",
            data={"error": "bad_arguments"},
        )
    resolved = (
        executable if Path(executable).is_absolute() else shutil.which(executable)
    )
    if not resolved:
        return ToolResult(
            spoken=f"I could not find {executable}.",
            data={"error": "executable_not_found", "executable": executable},
        )

    timeout = max(1, min(timeout_seconds or PROCESS_TIMEOUT_SECONDS, 30))
    process = await asyncio.create_subprocess_exec(
        resolved,
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout,
        )
    except TimeoutError:
        process.kill()
        await process.communicate()
        return ToolResult(
            spoken=f"{executable} timed out.",
            data={"error": "timeout", "timeoutSeconds": timeout},
        )

    stdout, stdout_truncated = _truncate(
        stdout_bytes.decode(errors="replace"),
        MAX_PROCESS_CHARS,
    )
    stderr, stderr_truncated = _truncate(
        stderr_bytes.decode(errors="replace"),
        MAX_PROCESS_CHARS,
    )
    return ToolResult(
        spoken=f"{executable} exited with code {process.returncode}.",
        data={
            "executable": resolved,
            "arguments": argv,
            "exitCode": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": stdout_truncated or stderr_truncated,
        },
    )


@tool(
    name="run_applescript",
    description=(
        "Run AppleScript for apps that publish an AppleScript interface. "
        "Prefer inspect_ui and interact_ui when no known script is available. "
        "Execute immediately when requested."
    ),
    parameters=[
        ToolParam(
            name="script",
            type="string",
            description="The complete AppleScript source.",
        ),
    ],
    permission="sensitive",
)
async def run_applescript(script: str) -> ToolResult:
    if len(script) > 10_000:
        return ToolResult(
            spoken="The AppleScript is too large.",
            data={"error": "script_too_large"},
        )
    executable = shutil.which("osascript")
    if not executable:
        return ToolResult(
            spoken="AppleScript is unavailable.",
            data={"error": "osascript_not_found"},
        )
    process = await asyncio.create_subprocess_exec(
        executable,
        "-e",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        process.kill()
        await process.communicate()
        return ToolResult(
            spoken="The AppleScript timed out.",
            data={"error": "timeout"},
        )
    stdout, stdout_truncated = _truncate(
        stdout_bytes.decode(errors="replace"),
        MAX_PROCESS_CHARS,
    )
    stderr, stderr_truncated = _truncate(
        stderr_bytes.decode(errors="replace"),
        MAX_PROCESS_CHARS,
    )
    return ToolResult(
        spoken=f"AppleScript exited with code {process.returncode}.",
        data={
            "exitCode": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": stdout_truncated or stderr_truncated,
        },
    )


def _validate_public_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only public http and https URLs are allowed")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port)
    except socket.gaierror as error:
        raise ValueError(f"could not resolve {parsed.hostname}") from error
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError(
                "private, local, and reserved network addresses are blocked"
            )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        _validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http_get(url: str) -> tuple[int, str, str, bytes]:
    _validate_public_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "Friday/0.2"})
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return (
            response.status,
            response.geturl(),
            response.headers.get_content_type(),
            response.read(MAX_HTTP_CHARS * 4),
        )


def _web_search(query: str, limit: int) -> list[dict[str, str]]:
    search_url = "https://www.bing.com/search?" + urllib.parse.urlencode(
        {"format": "rss", "q": query}
    )
    _, _, _, raw = _http_get(search_url)
    root = ET.fromstring(raw)
    results: list[dict[str, str]] = []
    for item in root.findall("./channel/item")[:limit]:
        title = item.findtext("title") or ""
        url = item.findtext("link") or ""
        snippet = item.findtext("description") or ""
        if not url:
            continue
        results.append(
            {
                "title": html.unescape(title).strip()[:300],
                "url": url.strip()[:1_000],
                "snippet": html.unescape(snippet).strip()[:700],
            }
        )
    return results


@tool(
    name="web_search",
    description=(
        "Search the public web and return titles, URLs, and snippets. Use this "
        "when the user asks to find, look up, research, or search for current "
        "information and no exact URL is already known."
    ),
    parameters=[
        ToolParam(
            name="query", type="string", description="Natural-language search query."
        ),
        ToolParam(
            name="max_results",
            type="integer",
            description="Maximum results from 1 to 10. Defaults to 5.",
            required=False,
        ),
    ],
    permission="read_only",
)
async def web_search(query: str, max_results: int | None = None) -> ToolResult:
    cleaned = query.strip()
    if not cleaned:
        return ToolResult(
            spoken="The search query cannot be empty.",
            data={"error": "empty_query"},
        )
    limit = max(1, min(max_results or 5, 10))
    try:
        results = await asyncio.to_thread(_web_search, cleaned, limit)
    except (ValueError, ET.ParseError, urllib.error.URLError, OSError) as error:
        return ToolResult(
            spoken="I could not search the web.",
            data={"error": "search_failed", "detail": str(error)},
        )
    return ToolResult(
        spoken=f"Found {len(results)} web results for {cleaned}.",
        data={
            "query": cleaned,
            "results": results,
            "provider": "Bing RSS",
        },
    )


@tool(
    name="fetch_url",
    description=(
        "Fetch one exact public HTTP or HTTPS URL. Use web_search first when "
        "the URL is unknown. HTML pages are converted to readable text."
    ),
    parameters=[
        ToolParam(name="url", type="string", description="Complete public URL."),
        ToolParam(
            name="max_chars",
            type="integer",
            description="Maximum response characters to return. Defaults to 10000.",
            required=False,
        ),
    ],
    permission="read_only",
)
async def fetch_url(url: str, max_chars: int | None = None) -> ToolResult:
    limit = max(1, min(max_chars or MAX_HTTP_CHARS, MAX_HTTP_CHARS))
    try:
        status, final_url, content_type, raw = await asyncio.to_thread(_http_get, url)
    except (ValueError, urllib.error.URLError, OSError) as error:
        return ToolResult(
            spoken="I could not fetch that URL.",
            data={"error": "fetch_failed", "detail": str(error)},
        )

    decoded = raw.decode("utf-8", errors="replace")
    if content_type == "text/html":
        parser = _HTMLTextExtractor()
        parser.feed(decoded)
        decoded = parser.text()
    content, truncated = _truncate(decoded, limit)
    return ToolResult(
        spoken=f"Fetched {final_url}.",
        data={
            "url": final_url,
            "status": status,
            "contentType": content_type,
            "content": content,
            "truncated": truncated,
        },
    )
