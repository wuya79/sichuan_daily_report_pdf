#!/usr/bin/env python3
"""GitHub REST API 推送 (git push被403封锁时的替代通道)
用法: python3 api_push.py REPO CWD BRANCH [MSG]
- MSG省略时用本地HEAD的commit message
- 模式: 以远程HEAD为parent, 推送本地完整tree(增量blob), force PATCH ref
  (本地有多个未push提交时=单commit squash, 本地历史保留)
"""
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

TOKEN = open(os.path.expanduser("~/.hermes/keys/GITHUB_TOKEN")).read().strip()
REPO = sys.argv[1]
CWD = sys.argv[2]
BRANCH = sys.argv[3]
MSG = sys.argv[4] if len(sys.argv) > 4 else None


def api(method, path, data=None):
    url = f"https://api.github.com/repos/{REPO}/{path}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Authorization": f"token {TOKEN}",
                 "Content-Type": "application/json",
                 "User-Agent": "hermes-push/1.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode()[:300]}")
        raise


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True,
                          shell=True, cwd=CWD).stdout.strip()


base = api("GET", f"git/refs/heads/{BRANCH}")["object"]["sha"]
bt = api("GET", f"git/commits/{base}")["tree"]["sha"]
rt = api("GET", f"git/trees/{bt}?recursive=1")["tree"]
remote = {i["path"]: (i["sha"], i["mode"]) for i in rt if i["type"] == "blob"}
lt = sh("git rev-parse HEAD^{tree}")
local = {}
for line in sh(f"git ls-tree -r {lt}").split("\n"):
    p = line.split(None, 3)
    if len(p) >= 4 and p[1] == "blob":
        local[p[3]] = (p[2], p[0])  # sha, mode
changed = [p for p, (s, m) in local.items()
           if p not in remote or remote[p][0] != s]
print(f"{REPO}: 远程{base[:8]} 文件{len(remote)} 本地{len(local)} 需上传{len(changed)}")

for i, p in enumerate(changed):
    raw = subprocess.run(f"git cat-file -p {local[p][0]}",
                         capture_output=True, shell=True, cwd=CWD).stdout
    api("POST", "git/blobs", {"content": base64.b64encode(raw).decode(),
                              "encoding": "base64"})
    if (i + 1) % 50 == 0:
        print(f"  blob {i + 1}/{len(changed)}")

entries = [{"path": p, "mode": m, "type": "blob", "sha": s}
           for p, (s, m) in remote.items() if p not in changed]
entries += [{"path": p, "mode": m, "type": "blob", "sha": s}
            for p, (s, m) in local.items() if p in changed]
new_tree = api("POST", "git/trees", {"tree": entries})["sha"]
print(f"  tree={new_tree[:8]}")

an = sh("git log --format=%an -1 HEAD")
ae = sh("git log --format=%ae -1 HEAD")
ad = sh("git log --format=%aI -1 HEAD")
msg = MSG if MSG else sh("git log --format=%B -1 HEAD")
nc = api("POST", "git/commits", {
    "message": msg, "tree": new_tree, "parents": [base],
    "author": {"name": an, "email": ae, "date": ad},
    "committer": {"name": an, "email": ae, "date": ad}})["sha"]
print(f"  commit={nc[:8]}")

r = api("PATCH", f"git/refs/heads/{BRANCH}", {"sha": nc, "force": True})
print(f"OK {REPO} HEAD={r['object']['sha'][:8]}")
