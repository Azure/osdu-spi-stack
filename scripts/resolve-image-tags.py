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

"""Resolve the latest OSDU image tags from the community GitLab registry.

GitLab's cleanup policy prunes old tags, so a hardcoded SHA goes stale; this
rewrites the image references under software/stacks/osdu/ to tags that exist.

Usage: resolve-image-tags.py [--update]   (without --update, resolve and print)

Env:
  OSDU_IMAGE_BRANCH  branch suffix for image names (default master)
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
    """Rewrite the image reference in a YAML file.

    Handles the HelmRelease split form (`repository:` and `tag:` lines,
    keeping a Flux `${VAR:=default}` default) and the combined Pod-spec form
    (`image: "repo:tag"`).
    """
    content = filepath.read_text()

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

    # Only an `image:` naming this repository is rewritten, so init containers
    # and sidecar images are untouched.
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
