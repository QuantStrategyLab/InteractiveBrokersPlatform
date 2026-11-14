"""Report Secret Manager retention candidates using metadata and audit logs."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def request(url, token, body=None):
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    with urlopen(Request(url, data=body, headers=headers), timeout=30) as response:
        return json.load(response)


def list_pages(url, key, token, params=None):
    query = {"pageSize": 100, **(params or {})}
    while True:
        page = request(f"{url}?{urlencode(query, doseq=True)}", token)
        yield from page.get(key, [])
        if not page.get("nextPageToken"):
            return
        query["pageToken"] = page["nextPageToken"]


def secret_references(template):
    for container in template.get("containers", ()):
        for env in container.get("env", ()):
            ref = env.get("valueSource", {}).get("secretKeyRef")
            if ref:
                yield ref["secret"], str(ref.get("version", "latest"))
    for volume in template.get("volumes", ()):
        ref = volume.get("secret")
        if ref:
            for item in ref.get("items") or [{}]:
                yield ref["secret"], str(item.get("version", "latest"))


def selectors_for_project(project, project_number, token):
    assets = f"https://cloudasset.googleapis.com/v1/projects/{project}:searchAllResources"
    regions = {
        item["location"] for item in list_pages(assets, "results", token, {"assetTypes": ["run.googleapis.com/Job", "run.googleapis.com/Service"]})
        if item.get("assetType") in {"run.googleapis.com/Job", "run.googleapis.com/Service"}
    }
    selectors = defaultdict(set)
    for region in sorted(regions):
        base = f"https://run.googleapis.com/v2/projects/{project}/locations/{region}"
        templates = [j["template"]["template"] for j in list_pages(f"{base}/jobs", "jobs", token)]
        for service in list_pages(f"{base}/services", "services", token):
            templates.append(service["template"])
            revisions = "https://run.googleapis.com/v2/" + service["name"] + "/revisions"
            templates.extend(list_pages(revisions, "revisions", token))
        for template in templates:
            for name, version in secret_references(template):
                path = name.split("/")
                if len(path) == 1 or path[1] in {project, project_number}:
                    selectors[path[-1]].add(version)
    return selectors, regions


def classify(versions, aliases, selectors, reads, cutoff, audit_covered):
    existing = sorted(
        (v for v in versions if v["state"] in {"ENABLED", "DISABLED"}),
        key=lambda v: int(v["name"].rsplit("/", 1)[-1]), reverse=True,
    )
    pinned = set(aliases.values()) | selectors
    unresolved = selectors - {"latest"} - set(aliases) - {v for v in selectors if v.isdecimal()}
    results = Counter()
    for index, version in enumerate(existing):
        number = version["name"].rsplit("/", 1)[-1]
        if index < 2 or number in pinned or reads[number]:
            results["retained"] += 1
        elif datetime.fromisoformat(version["createTime"]) >= cutoff:
            results["retained"] += 1
        elif unresolved or not audit_covered:
            results["needs_evidence"] += 1
        else:
            results["review_candidates"] += 1
    return results


def access_audit(project, token, cutoff, secret):
    query = (
        f'timestamp>="{cutoff.isoformat()}" AND protoPayload.serviceName="secretmanager.googleapis.com" '
        'AND protoPayload.methodName:"AccessSecretVersion" '
        f'AND protoPayload.resourceName:"/secrets/{secret}/versions/"'
    )
    endpoint = "https://logging.googleapis.com/v2/entries:list"
    payload = {"resourceNames": [f"projects/{project}"], "filter": query, "pageSize": 1000}
    reads = Counter()
    for _ in range(20):
        result = request(endpoint, token, json.dumps(payload).encode())
        reads.update(entry["protoPayload"]["resourceName"].rsplit("/", 1)[-1]
                     for entry in result.get("entries", ()))
        if not result.get("nextPageToken"):
            return reads, True
        payload["pageToken"] = result["nextPageToken"]
    return reads, False


def main():
    project = os.environ["INVENTORY_PROJECT"]
    token = os.environ["INVENTORY_ACCESS_TOKEN"]
    resource = f"projects/{project}"
    policy = request(f"https://cloudresourcemanager.googleapis.com/v1/{resource}:getIamPolicy", token, b"{}")
    audit_enabled = any(
        config["logType"] == "DATA_READ" and not config.get("exemptedMembers")
        for entry in policy.get("auditConfigs", ())
        if entry["service"] in {"secretmanager.googleapis.com", "allServices"}
        for config in entry.get("auditLogConfigs", ())
    )
    selectors, regions = selectors_for_project(project, os.environ["INVENTORY_PROJECT_NUMBER"], token)
    base = f"https://secretmanager.googleapis.com/v1/{resource}"
    secrets = list(list_pages(f"{base}/secrets", "secrets", token))
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    summary = Counter()
    for secret in secrets:
        name = secret["name"].rsplit("/", 1)[-1]
        versions_url = "https://secretmanager.googleapis.com/v1/" + secret["name"]
        versions = list(list_pages(versions_url + "/versions", "versions", token))
        reads, complete = (Counter(), False)
        if sum(v["state"] in {"ENABLED", "DISABLED"} for v in versions) > 2:
            reads, complete = access_audit(project, token, cutoff, name)
        summary.update(classify(versions, {k: str(v) for k, v in secret.get("versionAliases", {}).items()},
                                selectors[name], reads, cutoff, audit_enabled and complete))
    if secrets:
        permission_names = ["secrets.delete", "versions.access", "versions.add", "versions.disable",
                            "versions.enable", "versions.destroy"]
        permissions = ["secretmanager." + name for name in permission_names]
        result = request("https://secretmanager.googleapis.com/v1/" + secrets[0]["name"] + ":testIamPermissions",
                         token, json.dumps({"permissions": permissions}).encode())
        if result.get("permissions"):
            raise RuntimeError("Inventory identity has unexpected Secret Manager permissions")
    report = {
        "secrets": len(secrets), "cloud_run_regions": len(regions), "retained": summary["retained"],
        "needs_more_evidence": summary["needs_evidence"], "review_candidates": summary["review_candidates"],
    }
    print(json.dumps(report))
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
        output.write("### Secret metadata inventory\n\n")
        output.writelines(f"- {key.replace('_', ' ')}: {value}\n" for key, value in report.items())
        output.write("\nReview candidates require separate consumer and rollback checks.\n")


if __name__ == "__main__":
    try:
        main()
    except HTTPError as error:
        raise SystemExit(f"Metadata API request failed (HTTP {error.code}).") from None
    except (URLError, OSError, KeyError, ValueError):
        raise SystemExit("Unable to complete the metadata inventory.") from None
