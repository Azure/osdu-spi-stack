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

"""Validate the environment declaration and export it to GITHUB_OUTPUT.

The lifecycle workflows read ops/environments/shared.yaml only through this
script, so every value they consume has passed the schema in spi.environment.
The checkout's own src/ is inserted ahead of any installed wheel so the
declaration is judged by the schema of the commit that changed it.

Exit codes:
  0  declaration absent (clean skip) or exported
  1  declaration present but invalid
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from spi.environment import (  # noqa: E402
    DEFAULT_DECLARATION_PATH,
    EnvironmentDeclarationError,
    load_declaration,
)


def _write_outputs(pairs: dict[str, str]) -> None:
    lines = [f"{key}={value}" for key, value in pairs.items()]
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        for line in lines:
            print(line)
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--declaration-path",
        default=str(DEFAULT_DECLARATION_PATH),
        help="Path to the environment declaration YAML file "
        f"(default: {DEFAULT_DECLARATION_PATH}).",
    )
    args = parser.parse_args(argv)
    path = Path(args.declaration_path)

    try:
        declaration = load_declaration(path)
    except EnvironmentDeclarationError as exc:
        print(f"::error::{path} is present but invalid: {exc}", file=sys.stderr)
        return 1

    if declaration is None:
        print(f"No declaration at {path}; nothing to export (clean skip).")
        _write_outputs({"declaration_found": "false"})
        return 0

    outputs = declaration.to_github_output()
    _write_outputs({"declaration_found": "true", **outputs})
    print(f"Exported declaration from {path}: {outputs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
