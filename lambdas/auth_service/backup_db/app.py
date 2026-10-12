import os
import re
import time

import boto3
from botocore.exceptions import ClientError

DB_INSTANCE_NAME = os.environ["DB_INSTANCE_NAME"]
# Total time to wait for the whole backup, across all the invocations of the state machine.
MAX_WAIT_SECONDS = int(os.environ.get("MAX_WAIT_SECONDS", "1800"))

POLL_SECONDS = 10
REGISTER_POLL_SECONDS = 15
SAFETY_MARGIN_SECONDS = 30  # stop before the lambda timeout so the state can be returned
COMMAND_TIMEOUT_SECONDS = 1500
OUTPUT_TAIL_CHARS = 1000

RUNNING = ("Pending", "InProgress", "Delayed")
KEY_LINE = re.compile(r"^Success: (\S+)$", re.MULTILINE)

# Runs on the DB host (AWS-RunShellScript). backup.sh dumps, verifies and uploads to the snapshots
# bucket of this region, and prints "Success: <key>" as its last line.
BACKUP_COMMANDS = [
    "for i in $(seq 1 30); do [ -x /opt/db/backup.sh ] && "
    "sudo -u ubuntu docker exec postgres sh -c "
    "'psql -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -tAc \"select 1\"' >/dev/null 2>&1 && break; "
    "sleep 10; done",
    "sudo -u ubuntu /opt/db/backup.sh",
]


def find_db_host(ec2, instance_name: str) -> str | None:
    response = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [instance_name]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )
    ids = [i["InstanceId"] for r in response["Reservations"] for i in r["Instances"]]
    return ids[0] if ids else None


def send_backup(ssm, instance_id: str, deadline: float) -> str | None:
    """Start the backup on the host, or None if its SSM agent is not registered by the deadline."""
    while True:
        try:
            response = ssm.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={
                    "commands": BACKUP_COMMANDS,
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
    """Dumps the database of a DB host to the snapshots bucket of its region.

    Used by the backup+restore state machine: the bucket replicates the dump to the other region,
    where it is restored. A backup can outlast one invocation, so the result carries what is needed
    to resume it: while the status is "InProgress" the state machine calls this function again with
    the same input. Optional input, echoed back: `region` (region of the DB host; default the region
    of the function) and `instance_name` (Name tag of the DB host; default DB_INSTANCE_NAME).
    Statuses: Success (with the `key` of the dump), InProgress, NoHost, Failed.
    """
    state = {
        "instance_id": event.get("instance_id"),
        "command_id": event.get("command_id"),
        "started_at": event.get("started_at") or time.time(),
        "region": event.get("region"),
        "instance_name": event.get("instance_name") or DB_INSTANCE_NAME,
    }
    ec2 = boto3.client("ec2", region_name=state["region"])
    ssm = boto3.client("ssm", region_name=state["region"])

    def result(status: str, message: str = "", **extra) -> dict:
        # `key` is always present (null until the backup succeeds): the state machine selects it.
        return {"status": status, "message": message, "key": None, **state, **extra}

    if time.time() - state["started_at"] > MAX_WAIT_SECONDS:
        return result("Failed", f"The backup did not finish within {MAX_WAIT_SECONDS} seconds")

    deadline = time.time() + context.get_remaining_time_in_millis() / 1000 - SAFETY_MARGIN_SECONDS

    if not state["instance_id"]:
        state["instance_id"] = find_db_host(ec2, state["instance_name"])
        if not state["instance_id"]:
            return result("NoHost", f"No running instance named {state['instance_name']}")

    if not state["command_id"]:
        state["command_id"] = send_backup(ssm, state["instance_id"], deadline)
        if not state["command_id"]:
            return result("InProgress", "Waiting for the SSM agent of the DB host")

    invocation = wait_for_command(ssm, state["command_id"], state["instance_id"], deadline)
    status = invocation["Status"]
    output = invocation.get("StandardOutputContent", "")
    output_tail = output[-OUTPUT_TAIL_CHARS:]

    if status == "Success":
        match = KEY_LINE.search(output)
        if not match:
            return result("Failed", f"backup.sh did not report a dump key: {output_tail}")
        return result("Success", output_tail, key=match.group(1))
    if status == "InProgress":
        return result("InProgress", "The backup is still running")

    error = invocation.get("StandardErrorContent", "")[-OUTPUT_TAIL_CHARS:]
    return result("Failed", f"SSM command {status}. stderr: {error} stdout: {output_tail}")
