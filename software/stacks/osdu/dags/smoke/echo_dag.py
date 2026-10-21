# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""OSDU's package-inventory smoke contract, implemented with the Airflow 3 SDK."""

import logging
from datetime import datetime, timedelta, timezone
from importlib.metadata import distributions

from airflow.sdk import DAG, task

with DAG(
    "Echo_DAG",
    start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    schedule=None,
    catchup=False,
    dagrun_timeout=timedelta(minutes=180),
    default_args={
        "retries": 0,
        "retry_delay": timedelta(seconds=30),
        "trigger_rule": "none_failed",
    },
    description="List installed packages for Workflow's execution smoke test",
) as dag:

    @task(task_id="list_packages_task")
    def list_packages() -> list[str]:
        packages = sorted(f"{dist.metadata['Name']}=={dist.version}" for dist in distributions())
        for package in packages:
            logging.info("%s", package)
        return packages

    list_packages()
