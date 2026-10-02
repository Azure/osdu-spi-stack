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

"""Entitlements membership for people, written as the deploy identity.

A person's token passes the mesh but entitlements holds no groups for them
until they are a member. The deploy identity sits in ``users.datalake.ops``,
which entitlements lets manage any group, so membership is written through
the public entitlements API with the bearer ``spi token`` mints.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from .bootstrap import read_cluster_config, read_init_values, read_workload_identity_client_id
from .info import collect_endpoint, read_entitlements_domain
from .templates import parse_init_values

ELEMENTARY_GROUP = "users"

ROLE_PRESETS = {
    "viewer": (ELEMENTARY_GROUP, "users.datalake.viewers"),
    "editor": (ELEMENTARY_GROUP, "users.datalake.editors"),
    "admin": (ELEMENTARY_GROUP, "users.datalake.admins"),
    # The data root is what lets ops create and reassign data groups.
    "ops": (ELEMENTARY_GROUP, "users.data.root", "users.datalake.ops"),
}
DEFAULT_ROLE = "admin"
ROLE_GROUPS = tuple(
    dict.fromkeys(g for groups in ROLE_PRESETS.values() for g in groups if g != ELEMENTARY_GROUP)
)

VERIFY_TIMEOUT = 60.0
VERIFY_INTERVAL = 5.0
_TIMEOUT = 30


class UsersError(RuntimeError):
    """A users operation failed; ``code`` is set for a typed refusal."""

    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Environment:
    base: str
    partitions: tuple[str, ...]
    domain: str
    seeded: dict[str, str]

    def group_email(self, partition: str, group: str) -> str:
        return f"{group}@{partition}.{self.domain}"

    def kind(self, member: str) -> str:
        return self.seeded.get(member.lower(), "user")


def load_environment() -> Environment:
    base = collect_endpoint("entitlements")
    if not base:
        raise UsersError("The environment has no ingress address yet; run 'spi up' first.")
    partitions = parse_init_values(read_init_values()).get("partitions") or []
    if not partitions:
        raise UsersError("The environment declares no partitions; run 'spi up' first.")
    domain = read_entitlements_domain()
    if not domain:
        raise UsersError("The entitlements service is not deployed, so its domain is unknown.")
    cluster_cfg = read_cluster_config()
    seeded = {
        read_workload_identity_client_id(): "workload identity",
        cluster_cfg.get("DEPLOY_IDENTITY_CLIENT_ID", ""): "deploy identity",
        cluster_cfg.get("MEMBER_IDENTITY_CLIENT_ID", ""): "member identity",
    }
    return Environment(
        base=base.rstrip("/"),
        partitions=tuple(partitions),
        domain=domain,
        seeded={client_id.lower(): kind for client_id, kind in seeded.items() if client_id},
    )


def select_partitions(env: Environment, partition: Optional[str]) -> tuple[str, ...]:
    if not partition:
        return env.partitions
    if partition not in env.partitions:
        raise UsersError(
            f"No partition '{partition}' on this environment (has: {', '.join(env.partitions)})."
        )
    return (partition,)


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _parse(raw: bytes) -> Any:
    text = raw.decode(errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def _detail(body: Any) -> str:
    if isinstance(body, dict):
        body = body.get("message") or body.get("reason") or body.get("error") or body
    return str(body).strip()[:300]


def _request(
    method: str, url: str, token: str, partition: str, body: Optional[dict] = None
) -> tuple[int, Any]:
    """One entitlements call. A refusal is returned; only a transport failure raises."""
    if not url.startswith(("https://", "http://")):
        raise UsersError(f"Refusing a non-HTTP entitlements address: {url}")
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "data-partition-id": partition,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # nosec B310
            return response.status, _parse(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _parse(exc.read())
    except urllib.error.URLError as exc:
        raise UsersError(f"Could not reach {url}: {exc.reason}") from None
    except OSError as exc:
        raise UsersError(f"Could not reach {url}: {exc}") from None


def _refused(action: str, partition: str, status: int, body: Any) -> UsersError:
    return UsersError(f"{partition}: {action} was refused ({status}): {_detail(body)}")


def group_members(env: Environment, token: str, partition: str, group: str) -> Optional[list[str]]:
    """The people and identities directly in a group; ``None`` when the group is absent."""
    email = env.group_email(partition, group)
    status, body = _request(
        "GET", f"{env.base}/groups/{_quote(email)}/members?includeType=true", token, partition
    )
    if status == 404:
        return None
    if status != 200 or not isinstance(body, dict):
        raise _refused(f"listing {group}", partition, status, body)
    suffix = f"@{partition}.{env.domain}".lower()
    return [
        str(entry["email"])
        for entry in body.get("members") or []
        if entry.get("email")
        and entry.get("memberType") != "GROUP"
        and not str(entry["email"]).lower().endswith(suffix)
    ]


def _role_members(
    env: Environment, token: str, partition: str, require: tuple[str, ...] = ()
) -> dict[str, set[str]]:
    """Lowercased direct members of each role group; a ``require`` group must exist."""
    listing = {}
    for group in dict.fromkeys((*ROLE_GROUPS, *require)):
        members = group_members(env, token, partition, group)
        if members is None and group in require:
            raise UsersError(
                f"{partition}: the group {group} does not exist.", code="group_missing"
            )
        listing[group] = {member.lower() for member in members or []}
    return listing


def role_of(groups: Any) -> str:
    """The preset whose role groups these are, ``custom`` for another mix, else ``none``."""
    held = {group for group in groups if group in ROLE_GROUPS}
    if not held:
        return "none"
    for role, preset in ROLE_PRESETS.items():
        if held == set(preset) - {ELEMENTARY_GROUP}:
            return role
    return "custom"


def _add_member(env: Environment, token: str, partition: str, group: str, member: str) -> str:
    email = env.group_email(partition, group)
    status, body = _request(
        "POST",
        f"{env.base}/groups/{_quote(email)}/members",
        token,
        partition,
        {"email": member, "role": "MEMBER"},
    )
    if status in (200, 201):
        return "added"
    if status == 409:
        return "already"
    if status == 404:
        raise UsersError(f"{partition}: the group {group} does not exist.", code="group_missing")
    raise _refused(f"adding to {group}", partition, status, body)


def _remove_from_group(
    env: Environment, token: str, partition: str, group: str, member: str
) -> None:
    email = env.group_email(partition, group)
    status, body = _request(
        "DELETE", f"{env.base}/groups/{_quote(email)}/members/{_quote(member)}", token, partition
    )
    if status not in (200, 204, 404):
        raise _refused(f"removing from {group}", partition, status, body)


def _refuse_seeded(env: Environment, member: str, verb: str) -> None:
    kind = env.kind(member)
    if kind != "user":
        raise UsersError(
            f"Refusing to {verb} the {kind}. The environment's test callers are seeded by spi up.",
            code="seeded_identity",
        )


def _undo(env: Environment, token: str, partition: str, added: list[str], member: str) -> str:
    """Take back this run's additions, ``users`` last, and say what the member is left with."""
    kept = []
    for group in reversed(added):
        try:
            _remove_from_group(env, token, partition, group, member)
        except UsersError:
            kept.append(group)
    if kept:
        return f"Could not undo {', '.join(reversed(kept))}; remove the member and run it again."
    return "The groups this run added were removed."


def add_user(
    env: Environment, token: str, member: str, role: str, partitions: tuple[str, ...]
) -> dict:
    """Set a member's role in each partition: drop the other roles' groups, add its own."""
    _refuse_seeded(env, member, "change")
    preset = ROLE_PRESETS[role]
    changes: dict[str, dict[str, str]] = {}
    previous: Optional[str] = None
    # Every partition is read before the first write, so a missing group grants nothing.
    current = {
        partition: _role_members(env, token, partition, require=preset) for partition in partitions
    }
    for partition in partitions:
        role_members = current[partition]
        held = [group for group in ROLE_GROUPS if member.lower() in role_members[group]]
        if previous is None and held:
            previous = role_of(held)
        # Removals come first so a write that fails midway never leaves the old role standing.
        dropped = [group for group in held if group not in preset]
        for group in dropped:
            _remove_from_group(env, token, partition, group, member)
        changes[partition] = {}
        try:
            for group in preset:
                changes[partition][group] = _add_member(env, token, partition, group, member)
        except UsersError as exc:
            added = [group for group, change in changes[partition].items() if change == "added"]
            raise UsersError(
                f"{exc} {_undo(env, token, partition, added, member)}", code=exc.code
            ) from None
        changes[partition].update(dict.fromkeys(dropped, "removed"))
    return {"member": member, "role": role, "previousRole": previous, "partitions": changes}


def verify(
    env: Environment,
    token: str,
    partition: str,
    *,
    timeout: float = VERIFY_TIMEOUT,
    interval: float = VERIFY_INTERVAL,
) -> dict:
    """Call entitlements with the person's own token until it answers 200.

    A JSON refusal is entitlements not knowing the caller; a plain-text one
    is the mesh refusing the token, which membership cannot fix.
    """
    deadline = time.monotonic() + timeout
    while True:
        status, body = _request("GET", f"{env.base}/groups", token, partition)
        if status == 200 and isinstance(body, dict):
            return {"ok": True, "status": 200, "groups": len(body.get("groups") or [])}
        layer = "entitlements" if isinstance(body, dict) else "mesh"
        if layer == "mesh" or time.monotonic() + interval > deadline:
            return {"ok": False, "status": status, "layer": layer, "detail": _detail(body)}
        time.sleep(interval)


def list_users(env: Environment, token: str) -> dict[str, list[dict]]:
    """Every member of each partition's ``users`` group with its kind and role."""
    listing: dict[str, list[dict]] = {}
    for partition in env.partitions:
        members = group_members(env, token, partition, ELEMENTARY_GROUP)
        if members is None:
            raise UsersError(f"{partition}: the group {ELEMENTARY_GROUP} does not exist.")
        role_members = _role_members(env, token, partition)
        rows = []
        for member in members:
            kind = env.kind(member)
            held = [group for group in ROLE_GROUPS if member.lower() in role_members[group]]
            rows.append(
                {
                    "member": member,
                    "kind": kind,
                    "role": "seeded" if kind != "user" else role_of(held),
                    "groups": [ELEMENTARY_GROUP, *held],
                }
            )
        listing[partition] = sorted(rows, key=lambda row: (row["kind"] == "user", row["member"]))
    return listing


def remove_user(
    env: Environment, token: str, member: str, partitions: tuple[str, ...]
) -> dict[str, str]:
    """Delete a member from every group in each partition; seeded identities are refused."""
    _refuse_seeded(env, member, "remove")
    outcome: dict[str, str] = {}
    for partition in partitions:
        # Entitlements answers 204 for a stranger too, so membership is read first.
        current = group_members(env, token, partition, ELEMENTARY_GROUP) or []
        if member.lower() not in {entry.lower() for entry in current}:
            outcome[partition] = "absent"
            continue
        status, body = _request("DELETE", f"{env.base}/members/{_quote(member)}", token, partition)
        if status not in (200, 204):
            raise _refused(f"removing {member}", partition, status, body)
        outcome[partition] = "removed"
    return outcome
