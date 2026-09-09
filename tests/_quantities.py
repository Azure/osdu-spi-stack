# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.


def _millicores(quantity) -> int:
    """A Kubernetes CPU quantity as millicores: "100m", "1", or "0.3"."""
    text = str(quantity)
    return int(text[:-1]) if text.endswith("m") else round(float(text) * 1000)
