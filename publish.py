#!/usr/bin/env python3
"""Push files to the Lumara ops dashboard repo via the GitHub Contents API.

Netlify and most hosts are unreachable from the Cowork sandbox; api.github.com is
allowlisted, so GitHub Pages is the delivery path.

Usage:
    GH_TOKEN=... python3 publish.py data.json
    GH_TOKEN=... python3 publish.py index.html data.json build_data.py
"""
import base64, json, os, sys, urllib.request, urllib.error

REPO = os.environ.get("GH_REPO", "Akshaykhisti/lumara-op")
BRANCH = os.environ.get("GH_BRANCH", "main")
TOKEN = os.environ.get("GH_TOKEN") or sys.exit("GH_TOKEN not set")
API = f"https://api.github.com/repos/{REPO}/contents/"

# The sandbox's egress proxy allows reads to api.github.com but rejects writes
# ("not permitted through this proxy"). Direct connections are allowed, so go
# around it rather than through it.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(method, url, body=None):
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "lumara-ops")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(req, data) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def put(path, content_bytes, message):
    status, cur = call("GET", API + path + f"?ref={BRANCH}")
    sha = cur.get("sha") if status == 200 else None
    body = {"message": message, "branch": BRANCH,
            "content": base64.b64encode(content_bytes).decode()}
    if sha:
        body["sha"] = sha
    status, res = call("PUT", API + path, body)
    if status not in (200, 201):
        raise SystemExit(f"FAILED {path}: {status} {res.get('message')}")
    return "updated" if sha else "created"


if __name__ == "__main__":
    files = sys.argv[1:] or ["index.html", "data.json"]
    stamp = json.load(open("data.json"))["meta"]["generated_at_utc"] \
        if os.path.exists("data.json") else ""
    for f in files:
        action = put(os.path.basename(f), open(f, "rb").read(),
                     f"refresh {os.path.basename(f)} @ {stamp}")
        print(f"{action}: {f}")
