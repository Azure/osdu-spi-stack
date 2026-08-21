#!/usr/bin/env python3
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

"""Resolve latest OSDU container image tags from the GitLab registry.

Queries the OSDU community GitLab container registry API for each service
and updates the HelmRelease YAML files under software/stacks/osdu/ with
the correct image repository and tag.

The GitLab cleanup policy prunes old image tags, so hardcoded SHAs go stale.
This script ensures we always deploy with a tag that exists in the registry.

Usage:
    python scripts/resolve-image-tags.py              # resolve and show
    python scripts/resolve-image-tags.py --update     # resolve and update YAML files

Environment variables:
    OSDU_IMAGE_BRANCH         - Branch suffix for image names (default: master)
"""

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from spi.images import (  # noqa: E402
    DEFAULT_IMAGE_BRANCH,
    IMAGE_REGISTRY,
    resolve_image,
)


def update_yaml_file(filepath: Path, repository: str, tag: str) -> bool:
    """Update the image reference in a YAML file.

    Handles two formats:
      1. HelmRelease values split across two lines:
             repository: foo/bar
             tag: "sha"
         (used by software/stacks/osdu/services/*.yaml)
      2. Kubernetes core Pod spec combined form:
             image: "foo/bar:sha"
         (legacy static manifests only; live-locked manifests keep placeholders)
    """
    content = filepath.read_text()

    # Format 1: split repository: / tag:. Preserve Flux substitution
    # defaults when present, e.g. ${PARTITION_IMAGE_TAG:=sha}.
    new_content = re.sub(
        r"(^\s*repository:\s*)(.+)$",
        lambda m: m.group(1) + _replace_default(m.group(2), repository, quote=False),
        content,
        count=1,
        flags=re.MULTILINE,
    )
    new_content = re.sub(
        r"(^\s*tag:\s*)(.+)$",
        lambda m: m.group(1) + _replace_default(m.group(2), tag, quote=True),
        new_content,
        count=1,
        flags=re.MULTILINE,
    )

    # Format 2: combined image: "repo:tag" (with or without surrounding quotes).
    # Only rewrite lines where the existing value already references the
    # same repository we are updating, so this does not accidentally touch
    # unrelated image fields (istio-proxy, init containers, etc).
    repo_escaped = re.escape(repository)
    new_content = re.sub(
        rf'(^\s*image:\s*)(["\']?){repo_escaped}:[^\s"\']+(["\']?)(\s*)$',
        rf"\g<1>\g<2>{repository}:{tag}\g<3>\g<4>",
        new_content,
        count=1,
        flags=re.MULTILINE,
    )

    if new_content != content:
        filepath.write_text(new_content)
        return True
    return False


def _replace_default(existing: str, value: str, quote: bool) -> str:
    """Replace a static YAML value or a Flux ${VAR:=default} default."""

    if "${" in existing and ":=" in existing:
        return re.sub(r":=[^}]+", f":={value}", existing, count=1)
    if "${" in existing:
        return existing
    return f'"{value}"' if quote else value


def main():
    update_mode = "--update" in sys.argv
    branch = os.environ.get("OSDU_IMAGE_BRANCH", DEFAULT_IMAGE_BRANCH)
    stacks_dir = REPO_ROOT / "software" / "stacks" / "osdu"

    print(f"\nResolving OSDU image tags (branch: {branch})...\n")

    resolved = {}
    errors = []

    for svc_name, entry in IMAGE_REGISTRY.items():
        try:
            result = resolve_image(svc_name, entry, branch)
            resolved[svc_name] = result
            short_tag = result.tag[:12]
            repo_suffix = result.repository.split("/")[-1]
            print(f"  {svc_name:<20} -> {repo_suffix}:{short_tag}")
        except Exception as e:
            print(f"  {svc_name:<20} -> ERROR: {e}")
            errors.append(svc_name)

    print(f"\nResolved {len(resolved)}/{len(IMAGE_REGISTRY)} services")

    if errors:
        print(f"\nWARNING: {len(errors)} service(s) could not be resolved: {', '.join(errors)}")
        if update_mode:
            print("No files updated because resolution did not complete atomically.")
        return 1

    if update_mode and resolved:
        print("\nUpdating HelmRelease files...")
        for svc_name, result in resolved.items():
            entry = IMAGE_REGISTRY[svc_name]
            filepath = stacks_dir / entry.file
            if filepath.exists():
                changed = update_yaml_file(filepath, result.repository, result.tag)
                status = "updated" if changed else "unchanged"
                print(f"  {filepath.name:<25} {status}")
            else:
                print(f"  {filepath.name:<25} NOT FOUND")

    return 0 if resolved else 1


if __name__ == "__main__":
    sys.exit(main())
