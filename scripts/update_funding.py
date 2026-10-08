#!/usr/bin/env python3
"""Update funding only on verified public personal original repositories."""

import argparse
import base64
from collections import Counter
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
OWNER = "jeremykenedy"
FUNDING_PATH = ".github/FUNDING.yml"
MESSAGE = "Add global funding options"
EXPECTED_URLS = {
    "https://github.com/jeremykenedy",
    "https://patreon.com/jeremykenedy",
    "https://ko-fi.com/jeremykenedy",
    "https://www.paypal.com/paypalme/jeremykenedy",
    "https://www.buymeacoffee.com/jeremykenedy",
}


class ApiError(RuntimeError):
    def __init__(self, result):
        self.not_found = "HTTP 404" in result.stderr
        super().__init__(result.stderr.strip() or "GitHub API request failed")


def request(endpoint, method="GET", payload=None, paginate=False, include=False):
    command = ["gh", "api", endpoint, "--method", method]
    if paginate:
        command += ["--paginate", "--slurp"]
    if include:
        command += ["--include"]
    if payload is not None:
        command += ["--input", "-"]
    for attempt in range(3):
        result = subprocess.run(command, input=json.dumps(payload) if payload else None,
                                capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            return result.stdout if include else json.loads(result.stdout)
        if method == "GET" and attempt < 2 and re.search(
            r"HTTP (429|502|503|504)|connection|TLS|timeout", result.stderr, re.I
        ):
            time.sleep(2 ** attempt)
            continue
        raise ApiError(result)


def graph(query):
    result = request("graphql", "POST", {"query": query})
    if result.get("errors"):
        raise RuntimeError("GitHub GraphQL request failed")
    return result["data"]


def exclusion(repo, policy):
    if repo.get("owner", {}).get("login", "").lower() != OWNER:
        return "foreign owner"
    if repo.get("fork") is not False:
        return "fork or missing fork status"
    if repo.get("private") is not False or repo.get("visibility") != "public":
        return "private or non-public"
    if repo.get("name") == ".github":
        return "automation repository"
    if repo.get("archived") is not False or repo.get("disabled"):
        return "archived, disabled, or missing archive status"
    text = " ".join([repo.get("name", ""), repo.get("description") or "",
                     repo.get("homepage") or "", *repo.get("topics", [])])
    patterns = [r"care[\s_-]*link", r"spotr", *policy["excluded_patterns"]]
    if any(re.search(pattern, text, re.I) for pattern in patterns):
        return "work exclusion"
    if repo["name"].lower() in {name.lower() for name in policy["excluded_names"]}:
        return "imported, work, or unclassified exclusion"
    if (repo["name"] not in policy["reviewed_origins"]
            and policy["personal_topic"] not in repo.get("topics", [])):
        return "no reviewed personal classification"
    return None


def oldest_commit(repo):
    endpoint = f"repos/{repo['full_name']}/commits?sha={quote(repo['default_branch'], safe='')}&per_page=1"
    response = request(endpoint, include=True).replace("\r\n", "\n")
    headers, separator, body = response.partition("\n\n")
    if not separator:
        raise RuntimeError("Cannot parse commit response")
    commits = json.loads(body)
    if not commits:
        raise RuntimeError("Repository has no commits")
    last = re.search(r'<([^>]+)>; rel="last"', headers)
    return request(last.group(1))[0] if last else commits[0]


def verify_origin(repo, policy):
    root = oldest_commit(repo)
    if root.get("parents") != []:
        raise RuntimeError("Oldest commit is not a root")
    reviewed = policy["reviewed_origins"].get(repo["name"])
    if reviewed and root["sha"] != reviewed:
        raise RuntimeError("Reviewed repository origin has changed")
    if (root.get("author") or {}).get("login", "").lower() == OWNER:
        return root["sha"]
    legacy = policy["legacy_origins"].get(repo["name"])
    if (not reviewed or not legacy or legacy["root_commit"] != root["sha"]
            or root["commit"]["author"]["name"].lower() != OWNER):
        raise RuntimeError("Origin is not attributed to the owner")
    proof = request(f"repos/{repo['full_name']}/commits/{legacy['personal_commit']}")
    if (proof.get("author") or {}).get("login", "").lower() != OWNER:
        raise RuntimeError("Legacy personal commit is no longer verified")
    return root["sha"]


def file_contents(repo, path):
    try:
        result = request(f"repos/{repo['full_name']}/contents/{quote(path, safe='/')}"
                         f"?ref={quote(repo['default_branch'], safe='')}")
    except ApiError as error:
        if error.not_found:
            return None
        raise
    if result.get("type") != "file" or result.get("encoding") != "base64":
        raise RuntimeError("Expected a regular base64-encoded file")
    return result


def decode(file):
    return base64.b64decode(file["content"], validate=False).decode("utf-8")


def verify_new_project(repo, policy):
    if repo["name"] in policy["reviewed_origins"]:
        return
    entries = request(f"repos/{repo['full_name']}/contents?ref={quote(repo['default_branch'], safe='')}")
    for entry in entries:
        lower = entry["name"].lower()
        if lower not in ("readme.md", "readme", "readme.rst", "composer.json", "package.json"):
            continue
        text = decode(file_contents(repo, entry["path"]))
        if re.search(r"\b(forked?\s+(?:of|from)|cloned?\s+from|original\s+author|"
                     r"full\s+(?:development\s+)?credit|imported\s+from)\b", text, re.I):
            raise RuntimeError("Potential imported project needs a manual provenance review")
        if any(re.search(pattern, text, re.I) for pattern in [r"care[\s_-]*link", r"spotr", *policy["excluded_patterns"]]):
            raise RuntimeError("Project content matches a work exclusion")
        if lower == "composer.json":
            data = json.loads(text)
            namespace = data.get("name", "").split("/")[0]
            if namespace not in ("", OWNER, "laravel"):
                raise RuntimeError("External package namespace needs a provenance review")
            for author in data.get("authors", []):
                identity = " ".join(str(author.get(k, "")) for k in ("name", "email", "homepage"))
                if not re.search(r"jeremy\s*kenedy|jeremykenedy", identity, re.I):
                    raise RuntimeError("External package author needs a provenance review")
        if lower == "package.json":
            author = json.loads(text).get("author")
            if author and not re.search(r"jeremy\s*kenedy|jeremykenedy", json.dumps(author), re.I):
                raise RuntimeError("External package author needs a provenance review")


def sponsor_state(name):
    return graph('''query { repository(owner: "jeremykenedy", name: NAME) {
      id hasSponsorshipsEnabled fundingLinks { url }
    } }'''.replace("NAME", json.dumps(name)))["repository"]


def update_repository(repo, policy, funding, apply=False):
    fresh = request(f"repos/{repo['full_name']}")
    reason = exclusion(fresh, policy)
    if reason:
        return "skipped: " + reason
    root = verify_origin(fresh, policy)
    verify_new_project(fresh, policy)
    old = file_contents(fresh, FUNDING_PATH)
    state = sponsor_state(fresh["name"])
    changed = old is None or decode(old) != funding
    if not apply:
        return "would update" if changed or not state["hasSponsorshipsEnabled"] else "current"
    # Recheck metadata and provenance immediately before any mutation.
    current = request(f"repos/{fresh['full_name']}")
    if exclusion(current, policy) or current["default_branch"] != fresh["default_branch"]:
        raise RuntimeError("Repository eligibility or default branch changed")
    if verify_origin(current, policy) != root:
        raise RuntimeError("Repository origin changed during the run")
    verify_new_project(current, policy)
    if changed:
        payload = {"message": MESSAGE, "branch": fresh["default_branch"],
                   "content": base64.b64encode(funding.encode()).decode(),
                   "author": {"name": "Jeremy Kenedy", "email": "jeremykenedy@gmail.com"},
                   "committer": {"name": "Jeremy Kenedy", "email": "jeremykenedy@gmail.com"}}
        if old:
            payload["sha"] = old["sha"]
        result = request(f"repos/{fresh['full_name']}/contents/{FUNDING_PATH}", "PUT", payload)
        commit = request(f"repos/{fresh['full_name']}/commits/{result['commit']['sha']}")
        if (commit["commit"]["message"] != MESSAGE
                or [f["filename"] for f in commit["files"]] != [FUNDING_PATH]):
            raise RuntimeError("Funding commit did not match the intended change")
    verified = file_contents(current, FUNDING_PATH)
    if verified is None or decode(verified) != funding:
        raise RuntimeError("Funding content verification failed")
    if not state["hasSponsorshipsEnabled"]:
        # Recheck exclusions before changing the Sponsor setting too.
        if exclusion(request(f"repos/{fresh['full_name']}"), policy):
            raise RuntimeError("Repository is no longer eligible")
        graph('''mutation { updateRepository(input: {
          repositoryId: ID, hasSponsorshipsEnabled: true
        }) { repository { hasSponsorshipsEnabled } } }'''.replace("ID", json.dumps(state["id"])))
    for attempt in range(6):
        state = sponsor_state(fresh["name"])
        if state["hasSponsorshipsEnabled"] and EXPECTED_URLS.issubset({x["url"] for x in state["fundingLinks"]}):
            return "updated" if changed else "current"
        if attempt < 5:
            time.sleep(2)
    raise RuntimeError("Sponsor button or funding links were not verified")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    policy = json.loads((ROOT / "funding/policy.json").read_text())
    funding = (ROOT / "funding/FUNDING.yml").read_text()
    if policy["owner"] != OWNER:
        raise RuntimeError("Unexpected policy owner")
    if (ROOT / FUNDING_PATH).exists() or (ROOT / "FUNDING.yml").exists():
        raise RuntimeError("Account-wide funding inheritance must remain disabled")
    if args.apply and request("user")["login"].lower() != OWNER:
        raise RuntimeError("Apply requires the repository owner's credential")
    pages = request(f"users/{OWNER}/repos?type=owner&per_page=100&sort=full_name", paginate=True)
    summary = Counter()
    for repo in [repo for page in pages for repo in page]:
        reason = exclusion(repo, policy)
        if reason:
            summary["skipped"] += 1
            continue
        try:
            status = update_repository(repo, policy, funding, args.apply)
            summary[status] += 1
            print(f"{repo['name']}: {status}", flush=True)
        except (RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            summary["failed"] += 1
            print(f"{repo['name']}: {error}", file=sys.stderr, flush=True)
    print(json.dumps(dict(summary), sort_keys=True), flush=True)
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
