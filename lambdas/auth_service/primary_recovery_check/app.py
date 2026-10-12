import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import boto3
from botocore.exceptions import ClientError

dynamodb = boto3.client("dynamodb")
sns = boto3.client("sns")

STATE_TABLE = os.environ["STATE_TABLE"]
SERVICE_KEY = os.environ.get("SERVICE_KEY", "auth_service")
SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
REGION = os.environ["PRIMARY_REGION"]
SERVICES = [s for s in os.environ["PROBE_SERVICES"].split(",") if s]
HEALTH_URL = os.environ.get("PRIMARY_HEALTH_URL", "")
THRESHOLD = int(os.environ.get("RECOVERY_THRESHOLD", "12"))
TIMEOUT_SECONDS = 4

KEY = {"service": {"S": SERVICE_KEY}}


def probe(service: str) -> tuple[str, bool]:
    url = f"https://{service}.{REGION}.amazonaws.com/"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS):
            pass
        return service, True
    except urllib.error.HTTPError as exc:
        # 4xx means the endpoint is answering.
        return service, exc.code < 500
    except (urllib.error.URLError, OSError):  # includes timeouts and SSL errors
        return service, False


def backend_healthy() -> bool:
    request = urllib.request.Request(
        HEALTH_URL, method="GET", headers={"User-Agent": "aws-primary-recovery-check"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def primary_healthy() -> tuple[bool, dict]:
    """The primary counts as recovered only if EVERY probe answers (unlike the outage check, which
    needs several failures): coming back is a decision that is expensive to get wrong."""
    with ThreadPoolExecutor(max(len(SERVICES), 1)) as pool:
        details = dict(pool.map(probe, SERVICES))
    if HEALTH_URL:
        details["backend"] = backend_healthy()
    return bool(details) and all(details.values()), details


def is_condition_failed(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] == "ConditionalCheckFailedException"


def lambda_handler(event, context):
    item = dynamodb.get_item(TableName=STATE_TABLE, Key=KEY, ConsistentRead=True).get("Item", {})
    status = item.get("status", {}).get("S")

    # Only meaningful while the DR region is serving.
    if status != "DR_ACTIVE":
        return {"action": "skip", "status": status}

    healthy, details = primary_healthy()
    in_dr = {
        "TableName": STATE_TABLE,
        "Key": KEY,
        "ExpressionAttributeNames": {"#s": "status"},
    }

    try:
        if not healthy:
            dynamodb.update_item(
                **in_dr,
                UpdateExpression="SET recovery_streak = :zero REMOVE recovery_notified_at",
                ConditionExpression="#s = :dr",
                ExpressionAttributeValues={":zero": {"N": "0"}, ":dr": {"S": "DR_ACTIVE"}},
            )
            return {"action": "reset", "healthy": False, "details": details}

        streak = int(
            dynamodb.update_item(
                **in_dr,
                UpdateExpression="ADD recovery_streak :one",
                ConditionExpression="#s = :dr",
                ExpressionAttributeValues={":one": {"N": "1"}, ":dr": {"S": "DR_ACTIVE"}},
                ReturnValues="UPDATED_NEW",
            )["Attributes"]["recovery_streak"]["N"]
        )
        if streak < THRESHOLD:
            return {"action": "counting", "streak": streak, "threshold": THRESHOLD}

        # Notify once per recovery: the marker is cleared the first time the primary fails again.
        dynamodb.update_item(
            **in_dr,
            UpdateExpression="SET recovery_notified_at = :now",
            ConditionExpression="#s = :dr AND attribute_not_exists(recovery_notified_at)",
            ExpressionAttributeValues={
                ":now": {"S": datetime.now(UTC).isoformat()},
                ":dr": {"S": "DR_ACTIVE"},
            },
        )
    except ClientError as exc:
        if is_condition_failed(exc):
            # Not in DR any more, or already notified for this recovery.
            return {"action": "skip", "status": "changed"}
        raise

    try:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject="Primary region recovered: failback possible",
            Message=(
                f"The primary region has answered {streak} consecutive checks "
                f"(threshold {THRESHOLD}) while the DR region is serving. "
                "Review it and start the failback when you are ready."
            ),
        )
    except ClientError:
        # Allow the next run to retry the notification.
        dynamodb.update_item(
            **in_dr,
            UpdateExpression="REMOVE recovery_notified_at",
        )
        raise

    return {"action": "notified", "streak": streak, "threshold": THRESHOLD}
