import os
import re
import time

import boto3
from botocore.exceptions import ClientError

DB_INSTANCE_NAME = os.environ["DB_INSTANCE_NAME"]
# Total time to wait for the whole restore, across all the invocations of the state machine.
MAX_WAIT_SECONDS = int(os.environ.get("MAX_WAIT_SECONDS", "2700"))

POLL_SECONDS = 10
REGISTER_POLL_SECONDS = 15
SAFETY_MARGIN_SECONDS = 30  # stop before the lambda timeout so the state can be returned
COMMAND_TIMEOUT_SECONDS = 1800
OUTPUT_TAIL_CHARS = 1000

RUNNING = ("Pending", "InProgress", "Delayed")

KEY_PATTERN = re.compile(r"^[A-Za-z0-9_./=-]+$")  # the key ends up in a shell command

# Runs on the DB host (AWS-RunShellScript). A fresh host needs minutes to install Docker and start
# Postgres, so it waits for them first.
WAIT_FOR_DB = (
    "for i in $(seq 1 90); do [ -x /opt/db/restore.sh ] && "
    "sudo -u ubuntu docker exec postgres sh -c "
    "'psql -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -tAc \"select 1\"' >/dev/null 2>&1 && break; "
    "sleep 10; done"
)


def build_commands(key: str | None) -> list[str]:
    """Restores over the existing data (restore.sh drops the public schema first): the newest dump,
    or a given one (`key`) that may still be replicating into this region's bucket. Whoever calls
    this must not do it while the database holds data that should be kept."""
    commands = [WAIT_FOR_DB]
    if key:
        commands.append(
            "for i in $(seq 1 90); do sudo -u ubuntu bash -c "
            "'. /opt/db/lib.sh; aws s3api head-object --bucket \"$BACKUP_BUCKET\" --key \"$0\"' "
            f"{key} >/dev/null 2>&1 && break; sleep 10; done"
        )
    restore = "sudo -u ubuntu /opt/db/restore.sh --yes"
    if key:
        restore += f" {key}"
    commands.append(restore)
    return commands


def find_db_host(ec2, instance_name: str) -> str | None:
    response = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [instance_name]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )
    ids = [i["InstanceId"] for r in response["Reservations"] for i in r["Instances"]]
    return ids[0] if ids else None


def send_restore(ssm, instance_id: str, deadline: float, key: str | None) -> str | None:
    """Start the restore on the host, or None if its SSM agent is not registered by the deadline."""
    while True:
        try:
            response = ssm.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={
                    "commands": build_commands(key),
                    "executionTimeout": [str(COMMAND_TIMEOUT_SECONDS)],
                },
            )
            return response["Command"]["CommandId"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "InvalidInstanceId":
                raise
        if time.time() + REGISTER_POLL_SECONDS >= deadline:
            return None
        time.sleep(REGISTER_POLL_SECONDS)


def wait_for_command(ssm, command_id: str, instance_id: str, deadline: float) -> dict:
    """Final invocation (status, output tails) or {"Status": "InProgress"} at the deadline."""
    while True:
        try:
            invocation = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except ClientError as exc:
            # The invocation can take a moment to exist right after send_command.
            if exc.response["Error"]["Code"] != "InvocationDoesNotExist":
                raise
            invocation = {"Status": "Pending"}

        if invocation["Status"] not in RUNNING:
            return invocation
        if time.time() + POLL_SECONDS >= deadline:
            return {"Status": "InProgress"}
        time.sleep(POLL_SECONDS)


def lambda_handler(event, context):
    """Restores a dump over the database of a DB host. DESTRUCTIVE: the existing data is replaced.

    A restore can outlast one invocation, so the result carries what is needed to resume it: while
    the status is "InProgress" the state machine calls this function again with the same ids.
    Optional input, echoed back so a resume keeps it: `region` (region of the DB host; default the
    region of the function), `instance_name` (Name tag of the DB host; default DB_INSTANCE_NAME) and
    `key` (restore that dump instead of the newest). They make the function reusable for any
    database host and for the failback.
    Statuses: Success, InProgress, NoHost, Failed.
    """
    state = {
        "instance_id": event.get("instance_id"),
        "command_id": event.get("command_id"),
        "started_at": event.get("started_at") or time.time(),
        "region": event.get("region"),
        "instance_name": event.get("instance_name") or DB_INSTANCE_NAME,
        "key": event.get("key"),
    }
    ec2 = boto3.client("ec2", region_name=state["region"])
    ssm = boto3.client("ssm", region_name=state["region"])

    def result(status: str, message: str = "") -> dict:
        return {"status": status, "message": message, **state}

    if state["key"] and not KEY_PATTERN.fullmatch(state["key"]):
        return result("Failed", "The dump key has unexpected characters")

    if time.time() - state["started_at"] > MAX_WAIT_SECONDS:
        return result("Failed", f"The restore did not finish within {MAX_WAIT_SECONDS} seconds")

    deadline = time.time() + context.get_remaining_time_in_millis() / 1000 - SAFETY_MARGIN_SECONDS

    if not state["instance_id"]:
        state["instance_id"] = find_db_host(ec2, state["instance_name"])
        if not state["instance_id"]:
            return result("NoHost", f"No running instance named {state['instance_name']}")

    if not state["command_id"]:
        state["command_id"] = send_restore(ssm, state["instance_id"], deadline, state["key"])
        if not state["command_id"]:
            return result("InProgress", "Waiting for the SSM agent of the DB host")

    invocation = wait_for_command(ssm, state["command_id"], state["instance_id"], deadline)
    status = invocation["Status"]

    if status == "Success":
        return result("Success", invocation.get("StandardOutputContent", "")[-OUTPUT_TAIL_CHARS:])
    if status == "InProgress":
        return result("InProgress", "The restore is still running")

    error = invocation.get("StandardErrorContent", "")[-OUTPUT_TAIL_CHARS:]
    output = invocation.get("StandardOutputContent", "")[-OUTPUT_TAIL_CHARS:]
    return result("Failed", f"SSM command {status}. stderr: {error} stdout: {output}")
