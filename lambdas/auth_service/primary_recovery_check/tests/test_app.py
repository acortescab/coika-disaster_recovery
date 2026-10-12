import importlib.util
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, Mock

import boto3
import pytest
from botocore.exceptions import ClientError


@pytest.fixture
def load(monkeypatch):
    def _load(health_url="", threshold="3"):
        dynamodb, sns = Mock(), Mock()
        clients = {"dynamodb": dynamodb, "sns": sns}
        monkeypatch.setattr(boto3, "client", lambda service: clients[service])
        monkeypatch.setenv("STATE_TABLE", "state")
        monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:eu-central-1:1:alerts")
        monkeypatch.setenv("PRIMARY_REGION", "eu-west-1")
        monkeypatch.setenv("PROBE_SERVICES", "ec2,sts")
        monkeypatch.setenv("PRIMARY_HEALTH_URL", health_url)
        monkeypatch.setenv("RECOVERY_THRESHOLD", threshold)

        app_path = Path(__file__).resolve().parents[1] / "app.py"
        spec = importlib.util.spec_from_file_location("primary_recovery_check_app", app_path)
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        return app, dynamodb, sns

    return _load


def state(status):
    return {"Item": {"service": {"S": "auth_service"}, "status": {"S": status}}}


def streak_response(n):
    return {"Attributes": {"recovery_streak": {"N": str(n)}}}


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "UpdateItem")


@pytest.mark.parametrize("status", ["PRIMARY", "FAILING_OVER", "FAILOVER_FAILED", None])
def test_skips_when_dr_is_not_serving(load, status):
    app, dynamodb, sns = load()
    dynamodb.get_item.return_value = state(status) if status else {}

    result = app.lambda_handler({}, None)

    assert result == {"action": "skip", "status": status}
    dynamodb.update_item.assert_not_called()
    sns.publish.assert_not_called()


def test_unhealthy_primary_resets_the_streak(load, monkeypatch):
    app, dynamodb, sns = load()
    dynamodb.get_item.return_value = state("DR_ACTIVE")
    monkeypatch.setattr(app, "primary_healthy", lambda: (False, {"ec2": False, "sts": True}))

    result = app.lambda_handler({}, None)

    assert result["action"] == "reset"
    kwargs = dynamodb.update_item.call_args.kwargs
    assert "recovery_streak = :zero" in kwargs["UpdateExpression"]
    assert "REMOVE recovery_notified_at" in kwargs["UpdateExpression"]
    sns.publish.assert_not_called()


def test_healthy_primary_below_threshold_only_counts(load, monkeypatch):
    app, dynamodb, sns = load()
    dynamodb.get_item.return_value = state("DR_ACTIVE")
    dynamodb.update_item.return_value = streak_response(2)
    monkeypatch.setattr(app, "primary_healthy", lambda: (True, {"ec2": True, "sts": True}))

    result = app.lambda_handler({}, None)

    assert result == {"action": "counting", "streak": 2, "threshold": 3}
    dynamodb.update_item.assert_called_once()
    sns.publish.assert_not_called()


def test_notifies_when_threshold_is_reached(load, monkeypatch):
    app, dynamodb, sns = load()
    dynamodb.get_item.return_value = state("DR_ACTIVE")
    dynamodb.update_item.return_value = streak_response(3)
    monkeypatch.setattr(app, "primary_healthy", lambda: (True, {"ec2": True, "sts": True}))

    result = app.lambda_handler({}, None)

    assert result == {"action": "notified", "streak": 3, "threshold": 3}
    sns.publish.assert_called_once()
    marker = dynamodb.update_item.call_args.kwargs
    assert "attribute_not_exists(recovery_notified_at)" in marker["ConditionExpression"]


def test_does_not_notify_twice(load, monkeypatch):
    app, dynamodb, sns = load()
    dynamodb.get_item.return_value = state("DR_ACTIVE")
    dynamodb.update_item.side_effect = [
        streak_response(4),
        client_error("ConditionalCheckFailedException"),
    ]
    monkeypatch.setattr(app, "primary_healthy", lambda: (True, {"ec2": True, "sts": True}))

    result = app.lambda_handler({}, None)

    assert result["action"] == "skip"
    sns.publish.assert_not_called()


def test_failed_publish_clears_the_marker_and_raises(load, monkeypatch):
    app, dynamodb, sns = load()
    dynamodb.get_item.return_value = state("DR_ACTIVE")
    dynamodb.update_item.side_effect = [streak_response(3), {}, {}]
    sns.publish.side_effect = client_error("InternalError")
    monkeypatch.setattr(app, "primary_healthy", lambda: (True, {"ec2": True, "sts": True}))

    with pytest.raises(ClientError):
        app.lambda_handler({}, None)

    assert dynamodb.update_item.call_args.kwargs["UpdateExpression"] == (
        "REMOVE recovery_notified_at"
    )


def test_state_change_during_run_is_not_an_error(load, monkeypatch):
    app, dynamodb, _ = load()
    dynamodb.get_item.return_value = state("DR_ACTIVE")
    dynamodb.update_item.side_effect = client_error("ConditionalCheckFailedException")
    monkeypatch.setattr(app, "primary_healthy", lambda: (True, {"ec2": True, "sts": True}))

    assert app.lambda_handler({}, None)["action"] == "skip"


def fake_urlopen(outcomes):
    def urlopen(target, timeout):
        url = getattr(target, "full_url", target)
        outcome = outcomes[url.split("//")[1].split(".")[0]]
        if isinstance(outcome, Exception):
            raise outcome
        response = MagicMock()
        response.status = outcome
        response.__enter__.return_value = response
        return response

    return urlopen


def test_primary_needs_every_probe_to_answer(load, monkeypatch):
    app, _, _ = load()
    monkeypatch.setattr(app.urllib.request, "urlopen", fake_urlopen({"ec2": 200, "sts": 200}))
    assert app.primary_healthy()[0] is True

    # One failing probe is enough to say "not recovered" (the outage check needs several).
    outcomes = {"ec2": 200, "sts": urllib.error.URLError("dns")}
    monkeypatch.setattr(app.urllib.request, "urlopen", fake_urlopen(outcomes))
    assert app.primary_healthy()[0] is False


def test_backend_health_url_is_checked_when_set(load, monkeypatch):
    app, _, _ = load(health_url="https://backend.test/health")
    outcomes = {"ec2": 200, "sts": 200, "backend": 503}
    monkeypatch.setattr(app.urllib.request, "urlopen", fake_urlopen(outcomes))

    healthy, details = app.primary_healthy()

    assert healthy is False
    assert details["backend"] is False
