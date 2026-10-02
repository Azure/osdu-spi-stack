# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""OSDU API helper for an SPI Stack environment.

Usage:
    osdu.py connect [--as IDENTITY]      Read the environment, get a token, check it
    osdu.py status                       Show connection state and token lifetimes
    osdu.py services [--probe]           List the service endpoints, or probe /info
    osdu.py call METHOD PATH [options]   Make an authenticated call
    osdu.py token [--as IDENTITY]        Print a bearer token
    osdu.py disconnect                   Clear local state

IDENTITY is deploy (operator), member, no-access, or me (the person signed in
to az). Endpoints and partitions come from `spi info --json` and tokens from
`spi token --json`; this script derives neither. Standard library only.
"""

import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

STATE_DIR = Path.home() / ".cache" / "spi"
IDENTITIES = {
    "deploy": [],
    "member": ["--member"],
    "no-access": ["--no-access"],
    "me": ["--me"],
}
DEFAULT_IDENTITY = "deploy"
EXPIRY_MARGIN = 120
SPI_TIMEOUT = 180


def current_context() -> str:
    try:
        result = subprocess.run(
            ["kubectl", "config", "current-context"], capture_output=True, text=True, timeout=15
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not run kubectl: {exc}") from None
    context = result.stdout.strip()
    if result.returncode != 0 or not context:
        raise RuntimeError("kubectl has no current context. Run `spi connect` first.")
    return context


def state_file(context: str) -> Path:
    # One file per cluster context, so a token is never sent to another environment.
    return STATE_DIR / f"osdu-api-{re.sub(r'[^A-Za-z0-9_.-]', '_', context)}.json"


STATE_KEYS = {"context", "environment", "base_url", "endpoints", "partition", "identity", "tokens"}


def load_state(context: str) -> dict | None:
    try:
        state = json.loads(state_file(context).read_text())
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) and STATE_KEYS <= state.keys() else None


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = state_file(state["context"])
    # Replaced whole, so a command running alongside never reads a half-written file.
    partial = path.with_suffix(f".{os.getpid()}.tmp")
    partial.touch(mode=0o600)
    partial.write_text(json.dumps(state, indent=2))
    os.replace(partial, path)


def spi_json(arguments: list[str]) -> dict:
    """Run one `spi ... --json` command and return its document."""
    # A clone wins over an installed entry point, so the skill runs the source it ships with.
    root = next((p for p in Path(__file__).resolve().parents if (p / "src" / "spi").is_dir()), None)
    if root and shutil.which("uv"):
        argv = ["uv", "run", "--project", str(root), "spi"]
    elif shutil.which("spi"):
        argv = ["spi"]
    else:
        raise RuntimeError("spi not found. Run from a checkout with uv, or install the CLI.")
    command = " ".join(["spi", *arguments])
    try:
        result = subprocess.run(
            [*argv, *arguments, "--json"],
            capture_output=True,
            text=True,
            timeout=SPI_TIMEOUT,
            # Unwrapped, so the last stderr line is the whole error.
            env={**os.environ, "COLUMNS": "1000"},
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not run `{command}`: {exc}") from None
    if result.returncode != 0:
        lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
        raise RuntimeError(f"`{command}` failed: {lines[-1] if lines else result.returncode}")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise RuntimeError(f"`{command}` printed no JSON document.") from None


def read_environment(context: str) -> dict:
    info = spi_json(["info"])
    if not info.get("base_url"):
        raise RuntimeError("The environment has no ingress address yet. Run `spi status`.")
    partitions = info.get("partitions") or []
    if not partitions:
        raise RuntimeError("The environment declares no partitions. Run `spi up` first.")
    primary = next((p for p in partitions if p.get("primary")), partitions[0])
    return {
        "context": context,
        "environment": (info.get("environment") or {}).get("name") or "",
        "base_url": info["base_url"].rstrip("/"),
        "endpoints": info.get("endpoints") or {},
        "partition": primary["name"],
        "partitions": {
            p["name"]: {"legal_tag": p.get("legal_tag") or "", "primary": bool(p.get("primary"))}
            for p in partitions
        },
        "entitlements_domain": info.get("entitlements_domain") or "",
        "identity": DEFAULT_IDENTITY,
        "tokens": {},
        "connected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def ensure_state(context: str) -> dict:
    return load_state(context) or read_environment(context)


def acquire_token(state: dict, identity: str) -> str:
    cached = state["tokens"].get(identity) or {}
    if cached and time.time() < cached.get("expires_at", 0) - EXPIRY_MARGIN:
        return cached["access_token"]
    minted = spi_json(["token", *IDENTITIES[identity]])
    if not minted.get("token"):
        raise RuntimeError("`spi token` printed a document with no token.")
    try:
        expires_at = float(minted.get("expires_on") or 0)
    except ValueError:
        expires_at = 0.0
    state["tokens"][identity] = {
        "access_token": minted["token"],
        "expires_at": expires_at or time.time() + 300,
        "caller": minted.get("user") or minted.get("client_id") or "",
    }
    save_state(state)
    return minted["token"]


def request(
    state: dict,
    identity: str,
    method: str,
    path: str,
    body: object = None,
    query: str | None = None,
    partition: str | None = None,
    timeout: int = 60,
) -> dict:
    if not path.startswith("/"):
        raise RuntimeError(f"PATH must start with '/', for example /api/legal/v1/legaltags: {path}")
    url = f"{state['base_url']}{path}"
    if query:
        url = f"{url}?{query.lstrip('?')}"
    if not url.startswith(("https://", "http://")):
        raise RuntimeError(f"Refusing a non-HTTP address: {url}")
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method.upper(),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "data-partition-id": partition
            or os.environ.get("OSDU_DATA_PARTITION")
            or state["partition"],
        },
    )
    # urllib resends ordinary headers to a redirect target, another host included.
    req.add_unredirected_header("Authorization", f"Bearer {acquire_token(state, identity)}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:  # nosec B310
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"Could not reach {url}: {getattr(exc, 'reason', exc)}") from None
    except (http.client.HTTPException, ValueError) as exc:
        raise RuntimeError(f"Could not send the request to {url}: {exc}") from None
    text = raw.decode(errors="replace")
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = text
    result = {"status": status, "as": identity, "body": parsed}
    hint = refusal_hint(status, parsed, identity, path)
    if hint:
        result["hint"] = hint
    return result


def refusal_hint(status: int, body: object, identity: str, path: str) -> str:
    """Name the layer that refused: the mesh answers 401 in plain text, a service in JSON."""
    if status == 401 and isinstance(body, str):
        return (
            "The mesh refused the token before any service saw it. Compare the token "
            "audience with azure.token_audience in `spi info --json`."
        )
    if status == 401 and identity == "no-access":
        return "Expected: the no-access identity holds no entitlements groups."
    if status == 401 and identity == "me":
        return (
            "No group in this partition allows the call. If /api/entitlements/v2/groups "
            "also answers 401, run `spi users add --me`; otherwise your role lacks the group."
        )
    if status == 401:
        return f"The {identity} identity holds no group that allows this call."
    if status == 403 and identity == "me" and path.startswith("/api/partition/"):
        return "The partition API admits application tokens only; repeat with --as deploy."
    if status == 403:
        return f"The service refused the {identity} caller: it lacks what the call requires."
    return ""


def read_body(data: str | None) -> object:
    if data is None:
        return None
    if data == "-":
        data = sys.stdin.read()
    elif data.startswith("@"):
        try:
            data = Path(data[1:]).expanduser().read_text()
        except OSError as exc:
            raise RuntimeError(f"Could not read the body file: {exc}") from None
    try:
        return json.loads(data)
    except ValueError as exc:
        raise RuntimeError(f"The request body is not valid JSON: {exc}") from None


def token_summary(state: dict) -> dict:
    now = time.time()
    return {
        name: {
            "caller": entry.get("caller", ""),
            "ttl_seconds": max(0, int(entry.get("expires_at", 0) - now)),
        }
        for name, entry in state["tokens"].items()
    }


def emit(document: object) -> None:
    print(json.dumps(document, indent=2))


def cmd_connect(args: argparse.Namespace) -> None:
    context = current_context()
    state = read_environment(context)
    state["identity"] = args.identity or DEFAULT_IDENTITY
    acquire_token(state, state["identity"])
    check = request(state, state["identity"], "GET", "/api/entitlements/v2/groups", timeout=30)
    groups = check["body"].get("groups") if isinstance(check["body"], dict) else None
    result = {
        "status": "connected",
        "context": context,
        "environment": state["environment"],
        "base_url": state["base_url"],
        "partition": state["partition"],
        "partitions": state["partitions"],
        "entitlements_domain": state["entitlements_domain"],
        "identity": state["identity"],
        "caller": state["tokens"][state["identity"]]["caller"],
        "services": sorted(state["endpoints"]),
        "entitlements_check": {"status": check["status"], "groups": len(groups or [])},
    }
    if check.get("hint"):
        result["entitlements_check"]["hint"] = check["hint"]
    emit(result)


def cmd_status(_args: argparse.Namespace) -> None:
    context = current_context()
    state = load_state(context)
    if not state:
        emit({"status": "disconnected", "context": context, "message": "Run: connect"})
        return
    emit(
        {
            "status": "connected",
            "context": context,
            "environment": state["environment"],
            "base_url": state["base_url"],
            "partition": state["partition"],
            "identity": state["identity"],
            "connected_at": state["connected_at"],
            "tokens": token_summary(state),
        }
    )


def cmd_services(args: argparse.Namespace) -> None:
    state = ensure_state(current_context())
    base = state["base_url"]
    paths = {name: url[len(base) :] for name, url in state["endpoints"].items()}
    if not args.probe:
        save_state(state)
        emit({"count": len(paths), "base_url": base, "paths": paths})
        return
    identity = args.identity or state["identity"]
    acquire_token(state, identity)

    def probe(name: str) -> tuple[str, dict]:
        path = f"{paths[name]}info"
        try:
            answer = request(state, identity, "GET", path, timeout=15)
        except RuntimeError as exc:
            return name, {"status": "unreachable", "path": path, "detail": str(exc)}
        body = answer["body"] if isinstance(answer["body"], dict) else {}
        if answer["status"] == 404:
            return name, {"status": "no info endpoint", "path": path}
        if answer["status"] != 200:
            return name, {"status": answer["status"], "path": path}
        return name, {
            "status": "ok",
            "path": path,
            "version": body.get("version", ""),
            "artifactId": body.get("artifactId", ""),
            "buildTime": body.get("buildTime", ""),
            "commitId": body.get("commitId", ""),
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        probed = dict(pool.map(probe, paths))
    emit({"count": len(probed), "as": identity, "services": probed})


def cmd_call(args: argparse.Namespace) -> None:
    state = ensure_state(current_context())
    result = request(
        state,
        args.identity or state["identity"],
        args.method,
        args.path,
        body=read_body(args.data),
        query=args.query,
        partition=args.partition,
    )
    emit(result)
    if args.fail and result["status"] >= 400:
        sys.exit(22)


def cmd_token(args: argparse.Namespace) -> None:
    state = ensure_state(current_context())
    print(acquire_token(state, args.identity or state["identity"]))


def cmd_disconnect(_args: argparse.Namespace) -> None:
    path = state_file(current_context())
    if not path.exists():
        emit({"status": "already disconnected"})
        return
    path.unlink()
    emit({"status": "disconnected"})


def main() -> None:
    parser = argparse.ArgumentParser(description="OSDU API helper for an SPI Stack environment")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_identity(p: argparse.ArgumentParser, purpose: str) -> argparse.ArgumentParser:
        p.add_argument("--as", dest="identity", choices=sorted(IDENTITIES), help=purpose)
        return p

    with_identity(
        sub.add_parser("connect", help="Read the environment and get a token"),
        "Identity later commands default to (default: deploy)",
    )
    sub.add_parser("status", help="Show connection state")
    sub.add_parser("disconnect", help="Clear local state")
    with_identity(sub.add_parser("token", help="Print a bearer token"), "Identity to print")

    services = with_identity(
        sub.add_parser("services", help="List service endpoints"), "Identity to probe as"
    )
    services.add_argument("--probe", action="store_true", help="Call each service's /info")

    call = with_identity(
        sub.add_parser("call", help="Make an authenticated OSDU API call"),
        "Identity for this call only",
    )
    call.add_argument("method", help="HTTP method (GET, POST, PUT, PATCH, DELETE)")
    call.add_argument("path", help="API path, for example /api/storage/v2/records/{id}")
    call.add_argument("-d", "--data", help="JSON body, @file to read one, or - for stdin")
    call.add_argument("-q", "--query", help="Query string, for example 'limit=10&offset=0'")
    call.add_argument("-p", "--partition", help="Partition for this call (default: primary)")
    call.add_argument("--fail", action="store_true", help="Exit 22 on an HTTP status of 400 or up")

    args = parser.parse_args()
    try:
        {
            "connect": cmd_connect,
            "status": cmd_status,
            "services": cmd_services,
            "call": cmd_call,
            "token": cmd_token,
            "disconnect": cmd_disconnect,
        }[args.command](args)
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
