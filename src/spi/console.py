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

"""Shared Rich Console and display helpers for all SPI modules."""

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.theme import Theme

# Single merged theme covering keys previously scattered across helpers.py,
# secrets.py, status.py, and info.py. "ready"/"notready"/"failed" are the
# status-board idioms; "success"/"error"/"warning"/"info" are the semantic
# idioms; "azure"/"kubectl"/"flux"/"helm" are command-banner idioms.
_theme = Theme(
    {
        "azure": "bold cyan",
        "kubectl": "bold green",
        "flux": "bold magenta",
        "helm": "bold yellow",
        "info": "dim white",
        "success": "bold green",
        "error": "bold red",
        "warning": "bold yellow",
        "ready": "bold green",
        "notready": "bold yellow",
        "failed": "bold red",
        "header": "bold cyan",
        "dim": "dim white",
    }
)

console = Console(theme=_theme)
error_console = Console(theme=_theme, stderr=True)


def display_result(success_message: str) -> None:
    console.print(f"[success]  {success_message}[/success]")


def display_yaml(content: str, title: str = "Kubernetes YAML") -> None:
    yaml_syntax = Syntax(content.strip(), "yaml", theme="monokai", line_numbers=True)
    console.print(
        Panel(
            yaml_syntax,
            title=f"[bold cyan]{title}[/bold cyan]",
            border_style="cyan",
            expand=False,
        )
    )
