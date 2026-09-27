#!/usr/bin/env python3
"""Deploy iai-command-dashboard to Vercel production by reading files off disk."""
import json
import os
import sys
import time
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
EXCLUDE_DIRS = {".git", "node_modules", ".vercel", "paper-trading"}
EXCLUDE_FILES = {".env", ".env.example", ".gitignore", "deploy.py"}
PROJECT_NAME = "iai-command-dashboard"
TEAM = "team_TW99DrYsJJ7LFf7hV8dCCsgq"


def load_env():
    env = {}
    env_path = os.path.join(ROOT, "..", ".env")
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k] = v
    return env


def load_n8n_env():
    env = {}
    n8n_env_path = "/Users/zayrobinson24/MindPalace/mind-palace-app/.env"
    with open(n8n_env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k in ("N8N_API_KEY", "N8N_BASE_URL"):
                env[k] = v
    return env


def collect_files():
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if fn in EXCLUDE_FILES:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
            with open(full, "r", encoding="utf-8") as fh:
                data = fh.read()
            files.append({"file": rel, "data": data})
    return files


def api_request(method, path, token, body=None):
    req = urllib.request.Request(
        f"https://api.vercel.com{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()}


def ensure_project(token):
    result = api_request("GET", f"/v9/projects/{PROJECT_NAME}?teamId={TEAM}", token)
    if "error" not in result and result.get("id"):
        return result["id"]
    created = api_request("POST", f"/v9/projects?teamId={TEAM}", token, {"name": PROJECT_NAME})
    return created.get("id")


def set_env_vars(project_id, token, n8n_env):
    for key, value in n8n_env.items():
        body = {"key": key, "value": value, "type": "encrypted", "target": ["production", "preview", "development"]}
        result = api_request("POST", f"/v10/projects/{project_id}/env?teamId={TEAM}", token, body)
        print(f"env {key}: {'ok' if 'error' not in result else result['error'][:200]}", file=sys.stderr)


def main():
    env = load_env()
    n8n_env = load_n8n_env()
    token = env["VERCEL_TOKEN"]

    project_id = ensure_project(token)
    print(f"Project id: {project_id}", file=sys.stderr)
    if project_id:
        set_env_vars(project_id, token, n8n_env)

    files = collect_files()
    print(f"Deploying {len(files)} files: {[f['file'] for f in files]}", file=sys.stderr)

    body = json.dumps({
        "name": PROJECT_NAME,
        "target": "production",
        "files": files,
        "projectSettings": {"framework": None},
    }).encode("utf-8")

    req = urllib.request.Request(
        f"https://api.vercel.com/v13/deployments?teamId={TEAM}",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    dep_id = data["id"]
    print(f"Deployment created: {dep_id} — {data.get('url')}", file=sys.stderr)

    for _ in range(30):
        time.sleep(2)
        poll = urllib.request.Request(
            f"https://api.vercel.com/v13/deployments/{dep_id}?teamId={TEAM}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(poll) as resp:
            d = json.loads(resp.read())
        state = d.get("readyState")
        print(f"  state: {state}", file=sys.stderr)
        if state in ("READY", "ERROR", "CANCELED"):
            print(json.dumps({"id": dep_id, "state": state, "url": d.get("url"), "alias": d.get("alias")}))
            return
    print(json.dumps({"id": dep_id, "state": "TIMEOUT"}))


if __name__ == "__main__":
    main()
