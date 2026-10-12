import importlib.util
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def load_app(monkeypatch, services="ec2,sts,s3", min_failed="2"):
    monkeypatch.setenv("PRIMARY_REGION", "eu-central-1")
    monkeypatch.setenv("PROBE_SERVICES", services)
    monkeypatch.setenv("MIN_FAILED_PROBES", min_failed)

    app_path = Path(__file__).resolve().parents[1] / "app.py"
    spec = importlib.util.spec_from_file_location("region_probe_app", app_path)
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    return app


def fake_urlopen(outcomes):
    """Return a urlopen stub that looks the outcome up by service name in the URL."""

    def urlopen(url, timeout):
        outcome = outcomes[url.split("//")[1].split(".")[0]]
        if isinstance(outcome, Exception):
            raise outcome
        response = MagicMock()
        response.__enter__.return_value = response
        return response

    return urlopen


def http_error(code):
    return urllib.error.HTTPError("https://x/", code, "err", None, None)


def test_all_services_up(monkeypatch):
    app = load_app(monkeypatch)
    monkeypatch.setattr(
        app.urllib.request,
        "urlopen",
        fake_urlopen({"ec2": None, "sts": None, "s3": None}),
    )

    result = app.lambda_handler({}, None)

    assert result == {
        "region_down": False,
        "failed": 0,
        "total": 3,
        "details": {"ec2": True, "sts": True, "s3": True},
    }


@pytest.mark.parametrize(("code", "ok"), [(403, True), (404, True), (500, False), (503, False)])
def test_http_status_classification(monkeypatch, code, ok):
    app = load_app(monkeypatch, services="ec2", min_failed="1")
    monkeypatch.setattr(app.urllib.request, "urlopen", fake_urlopen({"ec2": http_error(code)}))

    assert app.probe("ec2") == ("ec2", ok)


def test_region_down_when_failures_reach_threshold(monkeypatch):
    app = load_app(monkeypatch)
    monkeypatch.setattr(
        app.urllib.request,
        "urlopen",
        fake_urlopen({"ec2": TimeoutError(), "sts": http_error(503), "s3": None}),
    )

    result = app.lambda_handler({}, None)

    assert result["region_down"] is True
    assert result["failed"] == 2
    assert result["details"] == {"ec2": False, "sts": False, "s3": True}


def test_region_up_when_failures_below_threshold(monkeypatch):
    app = load_app(monkeypatch)
    monkeypatch.setattr(
        app.urllib.request,
        "urlopen",
        fake_urlopen({"ec2": urllib.error.URLError("dns"), "sts": None, "s3": None}),
    )

    result = app.lambda_handler({}, None)

    assert result["region_down"] is False
    assert result["failed"] == 1


def test_no_services_configured(monkeypatch):
    app = load_app(monkeypatch, services="")

    assert app.lambda_handler({}, None) == {
        "region_down": False,
        "failed": 0,
        "total": 0,
        "details": {},
    }
