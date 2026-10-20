"""Custom resource: wait until Keycloak serves the realm's discovery document at its public URL with the expected issuer.

API Gateway's JWT authorizer fetches the discovery document when it is created and fails with "Invalid issuer" if the realm is
not there yet (seen 2026-10-16). Keycloak imports the realm at start-up, so this only has to wait for the first task.
"""

import json
import time
import urllib.error
import urllib.request

INTERVAL = 10  # seconds between polls


def probe(url):
    """(issuer, description) of one GET; the issuer is None unless the document came back as JSON."""
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:  # noqa: S310 - the URL is this stack's own API
            body = json.loads(resp.read().decode("utf-8"))
        return body.get("issuer"), "200"
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except Exception as exc:  # connection refused, DNS, timeout, bad JSON ...
        return None, f"{type(exc).__name__}: {exc}"


def handler(event, context):
    props = event["ResourceProperties"]
    url, issuer = props["Url"], props["Issuer"]
    physical = f"keycloak-ready:{url}"
    if event["RequestType"] == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId") or physical}
    if event["RequestType"] == "Update" and event.get("OldResourceProperties", {}).get("Url") == url:
        return {"PhysicalResourceId": physical, "Data": {"Issuer": issuer, "Seconds": "0"}}  # nothing to wait for
    started = time.time()
    deadline = started + context.get_remaining_time_in_millis() / 1000 - 30  # fail with a message, not a timeout
    last = "not polled"
    while time.time() < deadline:
        found, last = probe(url)
        if found == issuer:
            return {
                "PhysicalResourceId": physical,
                "Data": {"Issuer": found, "Seconds": str(int(time.time() - started))},
            }
        if found is not None:
            last = f"200 but issuer {found!r} is not {issuer!r}"
        print(f"waiting: {last}")
        time.sleep(INTERVAL)
    raise RuntimeError(
        f"Keycloak did not serve {url} with issuer {issuer} within {int(time.time() - started)} s (last: {last}). "
        "Check the agent-platform-keycloak service's tasks and the /agent-platform/keycloak log group."
    )
