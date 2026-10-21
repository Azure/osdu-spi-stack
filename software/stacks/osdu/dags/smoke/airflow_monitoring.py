# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Workflow's liveness DAG, including the echo task's return-value XCom."""

from datetime import datetime, timedelta, timezone

from airflow.sdk import DAG, task

with DAG(
    "airflow_monitoring",
    start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    schedule=None,
    catchup=False,
    dagrun_timeout=timedelta(minutes=20),
    default_args={"retries": 1, "retry_delay": timedelta(minutes=5)},
    description="Airflow liveness monitoring",
) as dag:

    @task(task_id="echo", priority_weight=2**31 - 1)
    def echo() -> str:
        print("test")
        return "test"

    echo()
