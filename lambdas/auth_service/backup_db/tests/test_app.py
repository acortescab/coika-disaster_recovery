import importlib.util
from pathlib import Path
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError

HOST = {"Reservations": [{"Instances": [{"InstanceId": "i-1"}]}]}
KEY = "postgres/db_20261012T100000Z.sql.gz"


class FakeClock:
    """time.time/time.sleep replacement: sleeping moves the clock, so the loops end quickly."""

    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def client_error(code, operation):
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


@pytest.fixture
def app_module(monkeypatch):
    clients = {"ec2": Mock(), "ssm": Mock()}
    monkeypatch.setattr(boto3, "client", lambda service, **kwargs: clients[service])
    monkeypatch.setenv("DB_INSTANCE_NAME", "db-host")

    app_path = Path(__file__).resolve().parents[1] / "app.py"
    spec = importlib.util.spec_from_file_location("backup_db_app", app_path)
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)

    clock = FakeClock()
    monkeypatch.setattr(app, "time", clock)
    context = Mock()
    context.get_remaining_time_in_millis.return_value = 900_000
    return app, clients["ec2"], clients["ssm"], clock, context


def test_reports_no_host_when_no_instance_is_running(app_module):
    app, ec2, ssm, _, context = app_module
    ec2.describe_instances.return_value = {"Reservations": []}

    result = app.lambda_handler({}, context)

    assert result["status"] == "NoHost"
    ssm.send_command.assert_not_called()


def test_runs_the_backup_and_returns_the_dump_key(app_module):
    app, ec2, ssm, _, context = app_module
    ec2.describe_instances.return_value = HOST
    ssm.send_command.return_value = {"Command": {"CommandId": "c-1"}}
    ssm.get_command_invocation.side_effect = [
        {"Status": "InProgress"},
        {
            "Status": "Success",
            "StandardOutputContent": f"Info: Uploading dump\nSuccess: {KEY}\n",
        },
    ]

    result = app.lambda_handler({}, context)

    assert result["status"] == "Success"
    assert result["key"] == KEY
    assert (result["instance_id"], result["command_id"]) == ("i-1", "c-1")
    kwargs = ssm.send_command.call_args.kwargs
    assert kwargs["InstanceIds"] == ["i-1"]
    assert kwargs["Parameters"]["commands"][-1] == "sudo -u ubuntu /opt/db/backup.sh"


def test_a_successful_command_without_a_key_is_a_failure(app_module):
    app, _, ssm, _, context = app_module
    ssm.get_command_invocation.return_value = {
        "Status": "Success",
        "StandardOutputContent": "nothing useful",
    }

    result = app.lambda_handler({"instance_id": "i-1", "command_id": "c-1"}, context)

    assert result["status"] == "Failed"
    assert "key" not in result


def test_waits_for_the_ssm_agent_and_asks_to_be_called_again(app_module):
    app, ec2, ssm, _, context = app_module
    ec2.describe_instances.return_value = HOST
    ssm.send_command.side_effect = client_error("InvalidInstanceId", "SendCommand")

    result = app.lambda_handler({}, context)

    assert result["status"] == "InProgress"
    assert result["command_id"] is None
    assert ssm.send_command.call_count > 1


def test_other_ssm_errors_are_raised(app_module):
    app, ec2, ssm, _, context = app_module
    ec2.describe_instances.return_value = HOST
    ssm.send_command.side_effect = client_error("AccessDeniedException", "SendCommand")

    with pytest.raises(ClientError):
        app.lambda_handler({}, context)


def test_returns_in_progress_when_the_backup_outlasts_the_invocation(app_module):
    app, _, ssm, _, context = app_module
    ssm.get_command_invocation.return_value = {"Status": "InProgress"}

    result = app.lambda_handler({"instance_id": "i-1", "command_id": "c-1"}, context)

    assert result["status"] == "InProgress"
    assert result["command_id"] == "c-1"


def test_resumes_without_looking_for_the_host_or_sending_again(app_module):
    app, ec2, ssm, clock, context = app_module
    ssm.get_command_invocation.return_value = {
        "Status": "Success",
        "StandardOutputContent": f"Success: {KEY}",
    }
    event = {"instance_id": "i-1", "command_id": "c-1", "started_at": clock.time() - 60}

    result = app.lambda_handler(event, context)

    assert result["key"] == KEY
    assert result["started_at"] == event["started_at"]
    ec2.describe_instances.assert_not_called()
    ssm.send_command.assert_not_called()


@pytest.mark.parametrize("status", ["Failed", "TimedOut", "Cancelled"])
def test_failed_command_reports_its_output(app_module, status):
    app, _, ssm, _, context = app_module
    ssm.get_command_invocation.return_value = {"Status": status, "StandardErrorContent": "boom"}

    result = app.lambda_handler({"instance_id": "i-1", "command_id": "c-1"}, context)

    assert result["status"] == "Failed"
    assert status in result["message"]
    assert "boom" in result["message"]


def test_gives_up_after_the_maximum_wait(app_module):
    app, ec2, ssm, clock, context = app_module

    result = app.lambda_handler({"started_at": clock.time() - 10_000}, context)

    assert result["status"] == "Failed"
    ec2.describe_instances.assert_not_called()
    ssm.send_command.assert_not_called()
