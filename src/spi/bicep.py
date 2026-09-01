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

"""Bicep deployment helper used by azure_infra.py and deploy.py."""

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .console import console
from .shell import run_command


def run_bicep_deployment(
    template_path: str,
    parameters: Dict[str, Any],
    resource_group: str,
    deployment_name: Optional[str] = None,
    what_if: bool = False,
) -> Dict[str, Any]:
    """Deploy a Bicep template to an existing resource group.

    Writes parameters to a temp ARM-parameters JSON file to handle arrays
    and objects cleanly, then calls az deployment group create (or what-if).
    On success, returns a flat dict of output name to unwrapped value.

    In what-if mode, prints the preview and returns an empty dict.
    """
    if deployment_name is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        deployment_name = f"spi-{stamp}"

    params_content = {
        "$schema": (
            "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#"
        ),
        "contentVersion": "1.0.0.0",
        "parameters": {k: {"value": v} for k, v in parameters.items()},
    }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix="spi-params-"
    ) as f:
        json.dump(params_content, f, indent=2)
        params_file = f.name

    try:
        if what_if:
            cmd = [
                "az",
                "deployment",
                "group",
                "what-if",
                "--resource-group",
                resource_group,
                "--template-file",
                template_path,
                "--parameters",
                f"@{params_file}",
            ]
            run_command(cmd, description=f"What-if: {deployment_name}")
            return {}

        cmd = [
            "az",
            "deployment",
            "group",
            "create",
            "--resource-group",
            resource_group,
            "--template-file",
            template_path,
            "--parameters",
            f"@{params_file}",
            "--name",
            deployment_name,
            "--mode",
            "Incremental",
            "--output",
            "json",
        ]
        console.print(
            f"  [info]Monitor progress in a separate terminal with:[/info]\n"
            f"  [dim]az deployment operation group list "
            f"--resource-group {resource_group} --name {deployment_name} "
            f"-o table[/dim]"
        )
        with console.status(
            f"[bold]Bicep deployment in progress: {deployment_name} "
            f"(this takes 10-15 minutes)...[/bold]"
        ):
            result = run_command(cmd, description=f"Bicep deployment: {deployment_name}")

        result_json = json.loads(result.stdout) if result.stdout else {}
        raw_outputs = result_json.get("properties", {}).get("outputs", {})
        return {k: v.get("value") for k, v in raw_outputs.items()}
    finally:
        try:
            os.unlink(params_file)
        except OSError:
            pass
