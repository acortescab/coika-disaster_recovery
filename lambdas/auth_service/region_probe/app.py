import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

REGION = os.environ["PRIMARY_REGION"]
SERVICES = [s for s in os.environ["PROBE_SERVICES"].split(",") if s]
MIN_FAILED = int(os.environ["MIN_FAILED_PROBES"])
TIMEOUT_SECONDS = 4


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


def lambda_handler(event, context):
    if not SERVICES:
        return {"region_down": False, "failed": 0, "total": 0, "details": {}}

    with ThreadPoolExecutor(len(SERVICES)) as pool:
        results = dict(pool.map(probe, SERVICES))

    failed = [service for service, ok in results.items() if not ok]
    return {
        "region_down": len(failed) >= MIN_FAILED,
        "failed": len(failed),
        "total": len(SERVICES),
        "details": results,
    }
