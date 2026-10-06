# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""Ingress LoadBalancer address discovery."""

from unittest.mock import patch

from spi.info import _compute_endpoints
from spi.ingress import GATEWAY_NAMESPACE, GATEWAY_SERVICE, get_ingress_ip


def _service(
    name: str,
    *,
    ip: str = "",
    hostname: str = "",
    service_type: str = "LoadBalancer",
) -> dict:
    ingress = [{"ip": ip}] if ip else [{"hostname": hostname}] if hostname else []
    return {
        "metadata": {"name": name},
        "spec": {"type": service_type},
        "status": {"loadBalancer": {"ingress": ingress}},
    }


class TestGetIngressIp:
    def test_discovers_an_alternate_load_balancer_service(self):
        services = {"items": [_service("custom-ingress", hostname="gateway.example.com")]}

        with patch("spi.ingress.kubectl_json", return_value=services) as kubectl_json:
            assert get_ingress_ip() == "gateway.example.com"

        kubectl_json.assert_called_once_with(["get", "svc", "-n", GATEWAY_NAMESPACE])

    def test_prefers_the_gateway_service_then_sorts_alternates_by_name(self):
        services = {
            "items": [
                _service("zeta-ingress", ip="10.0.0.3"),
                _service("alpha-ingress", ip="10.0.0.1"),
                _service(GATEWAY_SERVICE, ip="10.0.0.2"),
            ]
        }

        with patch("spi.ingress.kubectl_json", return_value=services):
            assert get_ingress_ip() == "10.0.0.2"

        services["items"].pop()
        with patch("spi.ingress.kubectl_json", return_value=services):
            assert get_ingress_ip() == "10.0.0.1"

    def test_skips_cluster_ip_services(self):
        services = {
            "items": [
                _service("redis", ip="10.0.0.9", service_type="ClusterIP"),
                _service(GATEWAY_SERVICE, ip="10.0.0.2"),
            ]
        }

        with patch("spi.ingress.kubectl_json", return_value=services):
            assert get_ingress_ip() == "10.0.0.2"

    def test_returns_empty_when_no_load_balancer_address_is_ready(self):
        services = {"items": [_service(GATEWAY_SERVICE)]}

        with patch("spi.ingress.kubectl_json", return_value=services):
            assert get_ingress_ip() == ""

    def test_returns_empty_before_flux_has_applied_the_gateway(self):
        with patch("spi.ingress.kubectl_json", return_value=None):
            assert get_ingress_ip() == ""


def test_info_fallback_uses_shared_ingress_discovery():
    with patch("spi.info.get_ingress_ip", return_value="10.0.0.6") as discover:
        mode, base, endpoints, middleware = _compute_endpoints({"INGRESS_MODE": "ip"})

    assert mode == "ip"
    assert base == "http://10.0.0.6"
    assert endpoints["partition"] == "http://10.0.0.6/api/partition/v1/"
    assert middleware == {}
    discover.assert_called_once_with()
