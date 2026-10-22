"""Publish CI-built immutable images through Coolify, and wait for its result."""
import json
import os
import re
import time
import urllib.request


def main():
    base = os.environ["COOLIFY_URL"].rstrip("/") + "/api/v1/"
    token = os.environ["COOLIFY_API_TOKEN"]
    app = os.environ["COOLIFY_APPLICATION_UUID"]
    sha = os.environ["BUILD_SHA"]
    if not token or not re.fullmatch(r"[a-z0-9]+", app) or not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise SystemExit("Coolify token, application UUID and full build SHA are required")
    images = {key: os.environ[key] for key in ("BACKEND_IMAGE", "FRONTEND_IMAGE")}
    if any(not value.endswith(":" + sha) for value in images.values()):
        raise SystemExit("Both images must use the CI commit SHA")

    def request(method, path, body=None):
        req = urllib.request.Request(
            base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=45) as response:
            return json.load(response)

    request("PATCH", f"applications/{app}/envs/bulk", {"data": [
        {"key": key, "value": value, "is_preview": False, "is_buildtime": True, "is_runtime": True}
        for key, value in images.items()
    ]})
    request("PATCH", f"applications/{app}", {"git_commit_sha": sha})
    result = request("POST", "deploy", {"uuid": app, "force": True})
    deployment = result["deployments"][0]["deployment_uuid"]
    print(f"Coolify deployment: {deployment}", flush=True)
    previous = None
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        detail = request("GET", f"deployments/{deployment}")
        status = detail["status"]
        if status != previous:
            print(f"Status: {status}", flush=True)
            previous = status
        if status == "finished":
            return
        if status in ("failed", "cancelled", "canceled"):
            raise SystemExit("Coolify deployment failed; inspect its deployment log")
        time.sleep(10)
    raise SystemExit("Timed out waiting for Coolify; inspect the deployment before retrying")


if __name__ == "__main__":
    main()
