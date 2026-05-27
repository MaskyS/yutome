from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import typer

from yutome import setup_prompts
from yutome.paths import ProjectPaths


CLOUDFLARE_WORKERS_DASHBOARD_URL = "https://dash.cloudflare.com/?to=/:account/workers-and-pages"
NODE_DOWNLOAD_URL = "https://nodejs.org/en/download"
CLOUDFLARE_MIN_NODE_VERSION = (22, 0, 0)


class _SpinnerContext:
    def __init__(self, message: str) -> None:
        self._message = message
        self._status = None

    def __enter__(self):
        if not (sys.stdout.isatty() and setup_prompts.is_interactive()):
            return self
        try:
            from rich.console import Console
            from rich.status import Status

            self._console = Console(file=sys.stdout)
            self._status = Status(self._message, console=self._console, spinner="dots")
            self._status.__enter__()
        except Exception:
            self._status = None
            typer.echo(f"… {self._message}")
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._status is not None:
            try:
                self._status.__exit__(exc_type, exc, tb)
            except Exception:
                pass
        return False


def _spinner(message: str) -> _SpinnerContext:
    return _SpinnerContext(message)

def _cloudflare_deploy_tools() -> dict[str, str | None]:
    return {
        "node": shutil.which("node"),
        "npm": shutil.which("npm"),
        "npx": shutil.which("npx"),
    }


def _parse_node_version(raw: str) -> tuple[int, int, int] | None:
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", raw)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _node_version() -> tuple[tuple[int, int, int] | None, str]:
    node_path = shutil.which("node")
    if node_path is None:
        return None, "not found"
    try:
        result = subprocess.run(
            [node_path, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{node_path} failed: {exc}"
    detail = (result.stdout or result.stderr or "").strip() or f"exit code {result.returncode}"
    if result.returncode != 0:
        return None, f"{node_path} failed: {detail}"
    return _parse_node_version(detail), f"{node_path} ({detail})"


def _cloudflare_deploy_runtime_problem() -> str | None:
    missing = _missing_cloudflare_deploy_tools()
    if missing:
        return f"Missing: {', '.join(missing)}"
    version, detail = _node_version()
    required = ".".join(str(part) for part in CLOUDFLARE_MIN_NODE_VERSION)
    if version is None:
        return f"Could not determine Node.js version from {detail}."
    if version < CLOUDFLARE_MIN_NODE_VERSION:
        return f"Node.js {required}+ is required by Wrangler; found {detail}."
    return None


def _require_cloudflare_deploy_runtime() -> None:
    problem = _cloudflare_deploy_runtime_problem()
    if problem is None:
        return
    typer.echo("Cannot deploy the Cloudflare Worker from this computer yet.", err=True)
    typer.echo(problem, err=True)
    typer.echo(f"Install Node.js 22 LTS or newer from {NODE_DOWNLOAD_URL}, then rerun `yutome connect --deploy`.", err=True)
    typer.echo("If you use Homebrew: `brew install node@22` and make sure that `node --version` prints v22+.", err=True)
    raise typer.Exit(code=1)


def _can_run_cloudflare_deploy() -> bool:
    return _cloudflare_deploy_runtime_problem() is None


def _missing_cloudflare_deploy_tools() -> list[str]:
    tools = _cloudflare_deploy_tools()
    return [name for name in ("node", "npm", "npx") if not tools[name]]


def _extract_worker_url(output: str) -> str | None:
    match = re.search(r"https://[a-zA-Z0-9.-]+\.workers\.dev(?:/[^\s]*)?", output)
    return match.group(0).rstrip("/") if match else None


def _should_use_interactive_command_stream() -> bool:
    return os.name == "posix" and setup_prompts.is_interactive()


def _command_exit_code(status: int) -> int:
    if hasattr(os, "waitstatus_to_exitcode"):
        return os.waitstatus_to_exitcode(status)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return status


def _run_command_streamed_pty(command: list[str], *, cwd: Path) -> tuple[int, str]:
    """Run an interactive subprocess through a PTY while capturing output.

    Wrangler detects piped stdout as non-interactive and skips prompts such as
    workers.dev subdomain registration. A PTY keeps those prompts available.
    """
    import pty

    output: list[bytes] = []

    def read_master(fd: int) -> bytes:
        try:
            chunk = os.read(fd, 1024)
        except OSError:
            return b""
        output.append(chunk)
        return chunk

    previous_cwd = Path.cwd()
    os.chdir(cwd)
    try:
        status = pty.spawn(command, master_read=read_master)
    finally:
        os.chdir(previous_cwd)
    return _command_exit_code(status), b"".join(output).decode(errors="replace")


def _run_command_streamed(command: list[str], *, cwd: Path) -> tuple[int, str]:
    if _should_use_interactive_command_stream():
        return _run_command_streamed_pty(command, cwd=cwd)

    process = subprocess.Popen(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output: list[str] = []
    if process.stdout is not None:
        for line in process.stdout:
            output.append(line)
            typer.echo(line.rstrip())
    return process.wait(), "".join(output)


def _cloudflare_workers_onboarding_url(output: str) -> str:
    match = re.search(r"/accounts/([0-9a-f]{32})/", output)
    if match:
        return f"https://dash.cloudflare.com/{match.group(1)}/workers/onboarding"
    return "https://dash.cloudflare.com/?to=/:account/workers/onboarding"


def _is_workers_dev_subdomain_error(output: str) -> bool:
    lower = output.lower()
    return "10063" in lower or "workers.dev subdomain" in lower


def _print_workers_dev_subdomain_help(output: str) -> None:
    onboarding_url = _cloudflare_workers_onboarding_url(output)
    typer.echo("", err=True)
    typer.echo("This Cloudflare account has not finished Workers setup yet.", err=True)
    typer.echo(
        "Create the account workers.dev subdomain, then rerun `yutome connect --deploy`.",
        err=True,
    )
    typer.echo(f"Cloudflare Workers onboarding: {onboarding_url}", err=True)
    if not _should_use_interactive_command_stream():
        typer.echo(
            "In an interactive terminal, Yutome runs Wrangler with a real TTY so Wrangler can prompt "
            "for this during deploy.",
            err=True,
        )


def _is_email_unverified_error(output: str) -> bool:
    lower = output.lower()
    return "10034" in lower or "verify your email" in lower


def _print_email_unverified_help(output: str) -> None:
    typer.echo("", err=True)
    typer.echo("This Cloudflare account hasn't verified its email address yet.", err=True)
    typer.echo(
        "Check the inbox for the address you signed into Cloudflare with for a "
        "verification email, click the link, then rerun `yutome connect --deploy`.",
        err=True,
    )
    typer.echo(
        "To resend the verification email, sign in at https://dash.cloudflare.com/ "
        "and open the profile menu in the top right.",
        err=True,
    )
    typer.echo(
        "More info: https://developers.cloudflare.com/fundamentals/setup/account/verify-email-address/",
        err=True,
    )


_WRANGLER_OAUTH_CONFIG_CANDIDATES = (
    Path.home() / "Library/Preferences/.wrangler/config/default.toml",
    Path.home() / ".config/.wrangler/config/default.toml",
    Path.home() / ".wrangler/config/default.toml",
)


def _read_wrangler_oauth_token() -> str | None:
    for path in _WRANGLER_OAUTH_CONFIG_CANDIDATES:
        try:
            content = path.read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError, OSError):
            continue
        match = re.search(r'^oauth_token\s*=\s*"([^"]+)"', content, re.MULTILINE)
        if match:
            return match.group(1)
    return None


def _cloudflare_bearer_token() -> str | None:
    if env_token := os.environ.get("CLOUDFLARE_API_TOKEN"):
        return env_token
    return _read_wrangler_oauth_token()


def _wrangler_account_id(worker_project: Path) -> str | None:
    if env_id := os.environ.get("CLOUDFLARE_ACCOUNT_ID"):
        return env_id
    completed = _run_wrangler_capture(worker_project, ["whoami"])
    if completed.returncode != 0:
        return None
    matches = re.findall(r"\b([0-9a-f]{32})\b", completed.stdout or "")
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


def _cloudflare_api_call(
    method: str,
    path: str,
    token: str,
    payload: dict | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4{path}",
        method=method,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
            return response.status, data
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8") or "{}")
        except (json.JSONDecodeError, OSError):
            data = {}
        return exc.code, data
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return 0, {}


def _workers_dev_subdomain_state(account_id: str, token: str) -> tuple[bool | None, str | None]:
    """Return (exists, name). exists=None means we couldn't tell."""
    status, body = _cloudflare_api_call(
        "GET", f"/accounts/{account_id}/workers/subdomain", token
    )
    if status == 200 and body.get("success"):
        name = (body.get("result") or {}).get("subdomain")
        return (True, name) if name else (False, None)
    error_codes = {error.get("code") for error in body.get("errors", []) if isinstance(error, dict)}
    if status in (404,) or error_codes & {10007, 10063}:
        return False, None
    return None, None


def _create_workers_dev_subdomain(account_id: str, token: str, name: str) -> tuple[bool, str]:
    """Return (success, message). On success, message is the created subdomain."""
    status, body = _cloudflare_api_call(
        "PUT",
        f"/accounts/{account_id}/workers/subdomain",
        token,
        {"subdomain": name},
    )
    if status == 200 and body.get("success"):
        return True, (body.get("result") or {}).get("subdomain", name)
    errors = body.get("errors") or [{}]
    return False, errors[0].get("message", f"HTTP {status}")


def _suggest_workers_dev_subdomain_name() -> str:
    return f"yutome-{secrets.token_hex(4)}"


def _ensure_workers_dev_subdomain(worker_project: Path) -> None:
    """Best-effort: make sure the user's account has a workers.dev subdomain.

    If we can't tell or can't create one (missing token, multi-account ambiguity,
    network error, insufficient permissions), we stay silent and let the
    subsequent ``wrangler deploy`` surface the real error with the existing
    10063 help message.
    """
    token = _cloudflare_bearer_token()
    if not token:
        return
    account_id = _wrangler_account_id(worker_project)
    if not account_id:
        return
    exists, _ = _workers_dev_subdomain_state(account_id, token)
    if exists is True or exists is None:
        return
    for _ in range(3):
        name = _suggest_workers_dev_subdomain_name()
        success, message = _create_workers_dev_subdomain(account_id, token, name)
        if success:
            typer.echo(f"[OK] Created workers.dev subdomain: {message}.workers.dev")
            return
        if "taken" not in message.lower() and "already" not in message.lower():
            return


def _run_wrangler_capture(worker_project: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["npx", "--yes", "wrangler", *args],
        cwd=worker_project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def _wrangler_auth_message() -> str:
    return (
        "Cloudflare authentication is required. Wrangler can open a browser sign-in from an "
        "interactive terminal, or you can set CLOUDFLARE_API_TOKEN for scripted/non-interactive runs."
    )


def _wrangler_whoami_authenticated(completed: subprocess.CompletedProcess[str]) -> bool:
    output = (completed.stdout or "").lower()
    if "not authenticated" in output or "not logged in" in output:
        return False
    return completed.returncode == 0


def _ensure_wrangler_authenticated(worker_project: Path) -> None:
    if os.environ.get("CLOUDFLARE_API_TOKEN"):
        return
    completed = _run_wrangler_capture(worker_project, ["whoami"])
    if _wrangler_whoami_authenticated(completed):
        return
    if not setup_prompts.is_interactive():
        typer.echo(_wrangler_auth_message(), err=True)
        typer.echo(
            "Create a token with Workers Scripts and Workers KV permissions, then rerun with "
            "CLOUDFLARE_API_TOKEN set.",
            err=True,
        )
        if completed.stdout:
            typer.echo(completed.stdout.rstrip(), err=True)
        raise typer.Exit(code=completed.returncode or 1)

    typer.echo("")
    typer.echo(_wrangler_auth_message())
    typer.echo("Starting Cloudflare browser sign-in with Wrangler...")
    login = subprocess.run(["npx", "--yes", "wrangler", "login"], cwd=worker_project, check=False)
    if login.returncode != 0:
        typer.echo("Cloudflare sign-in failed. Rerun `yutome connect --deploy` after signing in.", err=True)
        raise typer.Exit(code=login.returncode)
    verified = _run_wrangler_capture(worker_project, ["whoami"])
    if not _wrangler_whoami_authenticated(verified):
        typer.echo("Cloudflare sign-in did not complete successfully.", err=True)
        if verified.stdout:
            typer.echo(verified.stdout.rstrip(), err=True)
        raise typer.Exit(code=verified.returncode or 1)


# ---------- Tracked TypeScript Worker (cloudflare/yutome-capsule) ----------

WORKER_PROJECT_NAME = "yutome-remote-mcp"  # matches name in wrangler.toml
GENERATED_WRANGLER_FILENAME = "wrangler.generated.toml"


def _tracked_worker_path() -> Path:
    """Path to the tracked TypeScript Worker project.

    Editable checkouts use the repo-level cloudflare/ tree. Wheels include the
    same files under yutome/cloudflare/ so uv/pipx installs can deploy too.
    """
    here = Path(__file__).resolve()
    repo_worker_project = here.parents[3] / "cloudflare" / "yutome-capsule"
    if repo_worker_project.exists():
        return repo_worker_project
    return here.parents[1] / "cloudflare" / "yutome-capsule"


def _ensure_worker_node_modules(worker_project: Path) -> None:
    if (worker_project / "node_modules").exists():
        return
    _require_cloudflare_deploy_runtime()
    typer.echo(f"Installing TypeScript Worker dependencies in {worker_project}")
    returncode, _ = _run_command_streamed(["npm", "install"], cwd=worker_project)
    if returncode != 0:
        typer.echo("npm install failed. Fix the error above and rerun `yutome connect --deploy`.", err=True)
        raise typer.Exit(code=returncode)


_OAUTH_KV_ID_RE = re.compile(r'id\s*=\s*"([0-9a-f]{8,})"')


def _generated_cloudflare_dir(paths: ProjectPaths) -> Path:
    return paths.data_dir / "remote" / "cloudflare"


def _generated_wrangler_config_path(paths: ProjectPaths) -> Path:
    return _generated_cloudflare_dir(paths) / GENERATED_WRANGLER_FILENAME


def _active_oauth_kv_id(content: str) -> str | None:
    active = "\n".join(line for line in content.splitlines() if not line.lstrip().startswith("#"))
    if 'binding = "OAUTH_KV"' not in active:
        return None
    match = _OAUTH_KV_ID_RE.search(active.split('binding = "OAUTH_KV"', 1)[1])
    return match.group(1) if match else None


def _strip_oauth_kv_binding(content: str) -> str:
    lines = content.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip() == "[[kv_namespaces]]":
            block = [line]
            index += 1
            while index < len(lines) and not lines[index].lstrip().startswith("["):
                block.append(lines[index])
                index += 1
            if any('binding = "OAUTH_KV"' in block_line for block_line in block):
                continue
            output.extend(block)
            continue
        output.append(line)
        index += 1
    return "\n".join(output).rstrip() + "\n"


def _write_generated_wrangler_config(worker_project: Path, paths: ProjectPaths, namespace_id: str) -> Path:
    source_config = worker_project / "wrangler.toml"
    content = _strip_oauth_kv_binding(source_config.read_text(encoding="utf-8"))
    absolute_main = str((worker_project / "src" / "index.ts").resolve()).replace("\\", "\\\\")
    content = re.sub(r'^main\s*=\s*"[^"]+"', f'main = "{absolute_main}"', content, count=1, flags=re.MULTILINE)
    content = (
        content.rstrip()
        + "\n\n# Generated by `yutome connect --deploy`; account-specific and ignored by git.\n"
        + "[[kv_namespaces]]\n"
        + 'binding = "OAUTH_KV"\n'
        + f'id = "{namespace_id}"\n'
    )
    generated_path = _generated_wrangler_config_path(paths)
    generated_path.parent.mkdir(parents=True, exist_ok=True)
    generated_path.write_text(content, encoding="utf-8")
    try:
        generated_path.chmod(0o600)
    except OSError:
        pass
    return generated_path


def _existing_oauth_kv_namespace_id(worker_project: Path) -> str | None:
    completed = _run_wrangler_capture(worker_project, ["kv", "namespace", "list"])
    if completed.returncode != 0:
        return None
    try:
        namespaces = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(namespaces, list):
        return None
    for namespace in namespaces:
        if not isinstance(namespace, dict):
            continue
        if namespace.get("title") == "OAUTH_KV" and isinstance(namespace.get("id"), str):
            return namespace["id"]
    return None


def _ensure_oauth_kv_namespace(worker_project: Path, paths: ProjectPaths) -> Path:
    """Create or reuse an account-local OAUTH_KV binding in ignored state.

    The tracked Worker config deliberately does not contain a real KV id because
    Cloudflare namespace ids are account-specific. Assisted deploy writes the
    actual binding to data/remote/cloudflare/wrangler.generated.toml instead.
    """
    generated_config = _generated_wrangler_config_path(paths)
    if generated_config.exists():
        existing_id = _active_oauth_kv_id(generated_config.read_text(encoding="utf-8"))
        if existing_id:
            current_namespace_id = _existing_oauth_kv_namespace_id(worker_project)
            if current_namespace_id is None or current_namespace_id == existing_id:
                return generated_config
            typer.echo(
                f"[WARN] Refreshing stale OAUTH_KV namespace id={existing_id}; "
                f"current account has id={current_namespace_id}"
            )
            generated_config = _write_generated_wrangler_config(worker_project, paths, current_namespace_id)
            typer.echo(f"[OK] Wrote account-local Wrangler config: {generated_config}")
            return generated_config

    existing_namespace_id = _existing_oauth_kv_namespace_id(worker_project)
    if existing_namespace_id:
        typer.echo(f"[OK] Reusing existing OAUTH_KV namespace id={existing_namespace_id}")
        generated_config = _write_generated_wrangler_config(worker_project, paths, existing_namespace_id)
        typer.echo(f"[OK] Wrote account-local Wrangler config: {generated_config}")
        return generated_config

    typer.echo("Creating Cloudflare KV namespace OAUTH_KV (one-time setup)…")
    completed = _run_wrangler_capture(worker_project, ["kv", "namespace", "create", "OAUTH_KV"])
    if completed.returncode != 0:
        typer.echo("Failed to create OAUTH_KV namespace.", err=True)
        if completed.stdout:
            typer.echo(completed.stdout.rstrip(), err=True)
        raise typer.Exit(code=completed.returncode)

    match = _OAUTH_KV_ID_RE.search(completed.stdout or "")
    if not match:
        typer.echo(
            "OAUTH_KV namespace created but Wrangler's id could not be parsed. "
            "Paste the binding block manually into wrangler.toml.",
            err=True,
        )
        if completed.stdout:
            typer.echo(completed.stdout.rstrip(), err=True)
        raise typer.Exit(code=1)
    namespace_id = match.group(1)
    typer.echo(f"[OK] Created OAUTH_KV namespace id={namespace_id}")
    generated_config = _write_generated_wrangler_config(worker_project, paths, namespace_id)
    typer.echo(f"[OK] Wrote account-local Wrangler config: {generated_config}")
    return generated_config


def _wait_for_worker_online(
    url: str,
    *,
    timeout: float = 60.0,
    interval: float = 1.5,
    request_timeout: float = 3.0,
) -> bool:
    """Poll ``{url}/healthz`` until it responds 200, or ``timeout`` elapses.

    ``wrangler deploy`` returns as soon as Cloudflare's control plane has
    accepted the bundle, but the URL can take several more seconds to
    actually serve requests from the edge (and the secret pushes that
    follow take their own propagation time). Without this wait, the
    deploy-success card and the per-assistant pairing prose are printed
    while the user's first paste into Claude / ChatGPT still 502s.

    Returns True if the worker started responding within the window, False
    if we timed out. A False return is non-fatal — callers print a soft
    warning and proceed; the worker almost always comes up in another
    minute or two.
    """
    health_url = url.rstrip("/") + "/healthz"
    deadline = time.monotonic() + timeout
    last_error: str | None = None
    with _spinner(
        f"Waiting for Cloudflare edge to start serving {url} (up to {int(timeout)}s)…"
    ):
        while time.monotonic() < deadline:
            try:
                req = urllib.request.Request(
                    health_url,
                    headers={
                        "accept": "application/json",
                        "user-agent": "yutome-setup/healthz-probe",
                    },
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=request_timeout) as response:
                    if 200 <= response.status < 300:
                        return True
                    last_error = f"HTTP {response.status}"
            except urllib.error.HTTPError as exc:
                # A 4xx from /healthz means the route is registered (worker
                # is up) but the response wasn't successful — still counts
                # as "reachable enough" since the URL is serving traffic.
                if 400 <= exc.code < 500:
                    return True
                last_error = f"HTTP {exc.code}"
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = str(exc)
            time.sleep(interval)
    typer.secho(
        f"[WARN] Worker not yet reachable at {url} after {int(timeout)}s"
        + (f" (last error: {last_error})" if last_error else "")
        + ". It usually comes up within another minute — try pairing in your assistant in ~60s.",
        fg="yellow",
    )
    return False


def _push_wrangler_secret(worker_project: Path, name: str, value: str, *, wrangler_config: Path | None = None) -> None:
    """Push a secret to the deployed Worker via `wrangler secret put`."""
    typer.echo(f"Setting Cloudflare secret {name}")
    command = ["npx", "--yes", "wrangler", "secret", "put", name]
    if wrangler_config is not None:
        command.extend(["--config", str(wrangler_config)])
    completed = subprocess.run(
        command,
        cwd=worker_project,
        input=f"{value}\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        typer.echo(f"Failed to set {name}.", err=True)
        if completed.stderr:
            typer.echo(completed.stderr.rstrip(), err=True)
        raise typer.Exit(code=completed.returncode)


def _deploy_tracked_worker(
    *,
    paths: ProjectPaths,
    refresh_contract: bool = True,
    relay_token: str | None = None,
    pairing_code: str | None = None,
) -> tuple[str | None, str, str, str]:
    """Deploy the tracked TypeScript Worker from cloudflare/yutome-capsule.

    Generates ``YUTOME_RELAY_TOKEN`` and ``YUTOME_PAIRING_CODE`` if not
    supplied, pushes them to Cloudflare as encrypted secrets, and returns
    ``(deployed_url, worker_name, relay_token, pairing_code)`` so the caller
    can persist them to local state.
    """
    worker_project = _tracked_worker_path()
    if not worker_project.exists():
        typer.echo(
            f"Expected bundled TypeScript Worker project at {worker_project}, but it is missing.",
            err=True,
        )
        typer.echo(
            "Reinstall yutome from a build that includes cloudflare/yutome-capsule, "
            "or run the deploy from a repository checkout.",
            err=True,
        )
        raise typer.Exit(code=1)
    _require_cloudflare_deploy_runtime()

    effective_relay_token = relay_token or secrets.token_urlsafe(32)
    effective_pairing_code = pairing_code or secrets.token_hex(5).upper()

    if refresh_contract:
        from yutome.contract_export import emit_contract_json

        contract_path = worker_project / "src" / "contract.json"
        emit_contract_json(contract_path)
        typer.echo(f"[OK] Refreshed contract: {contract_path}")

    _ensure_worker_node_modules(worker_project)
    _ensure_wrangler_authenticated(worker_project)
    _ensure_workers_dev_subdomain(worker_project)
    wrangler_config = _ensure_oauth_kv_namespace(worker_project, paths)

    typer.echo(f"Deploying Cloudflare Worker from {worker_project}")
    command = ["npx", "--yes", "wrangler", "deploy", "--config", str(wrangler_config)]
    while True:
        returncode, output = _run_command_streamed(command, cwd=worker_project)
        if returncode == 0:
            break
        if _is_email_unverified_error(output):
            _print_email_unverified_help(output)
            recoverable = True
        elif _is_workers_dev_subdomain_error(output):
            _print_workers_dev_subdomain_help(output)
            recoverable = True
        else:
            recoverable = False
        if recoverable and setup_prompts.is_interactive():
            typer.echo("", err=True)
            if setup_prompts.confirm(
                "Press Enter to retry the deploy once you've fixed the issue above (or 'n' to abort)",
                default=True,
            ):
                continue
        typer.echo(
            "Cloudflare Worker deploy failed. Fix the Wrangler error above and rerun `yutome connect --deploy`.",
            err=True,
        )
        raise typer.Exit(code=returncode)

    # Worker is up. Push secrets so OAuth pairing + bridge auth work.
    _push_wrangler_secret(worker_project, "YUTOME_RELAY_TOKEN", effective_relay_token, wrangler_config=wrangler_config)
    _push_wrangler_secret(worker_project, "YUTOME_PAIRING_CODE", effective_pairing_code, wrangler_config=wrangler_config)

    deployed_url = _extract_worker_url(output)
    # Bridge the gap between `wrangler deploy` returning and the Cloudflare
    # edge actually serving requests. Without this, the success card + next
    # steps print before /mcp is reachable and the user's first paste into
    # Claude / ChatGPT 502s.
    if deployed_url:
        if _wait_for_worker_online(deployed_url):
            typer.secho(f"[OK] Worker responding at {deployed_url}", fg="green")
    return deployed_url, WORKER_PROJECT_NAME, effective_relay_token, effective_pairing_code


def _delete_tracked_worker(worker_name: str) -> None:
    """Run `wrangler delete` from the tracked Worker project directory."""
    worker_project = _tracked_worker_path()
    problem = _cloudflare_deploy_runtime_problem()
    if problem is not None:
        typer.echo(f"{problem} Delete the Worker manually in the Cloudflare dashboard.", err=True)
        typer.echo(f"Cloudflare Workers dashboard: {CLOUDFLARE_WORKERS_DASHBOARD_URL}", err=True)
        raise typer.Exit(code=1)
    command = ["npx", "--yes", "wrangler", "delete", worker_name, "--force"]
    typer.echo(f"Removing Cloudflare Worker {worker_name!r} via wrangler in {worker_project}")
    completed = subprocess.run(
        command, cwd=worker_project, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    if completed.stdout:
        typer.echo(completed.stdout.rstrip())
    if completed.stderr:
        typer.echo(completed.stderr.rstrip(), err=True)
    if completed.returncode != 0:
        typer.echo("Worker removal failed. Fix the error above and rerun.", err=True)
        raise typer.Exit(code=completed.returncode)
