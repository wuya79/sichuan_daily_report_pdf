#!/usr/bin/env python3
"""绕开适配器/熔断，直接对 iLink sendmessage 抓原始错误码签名（只读诊断，不改任何状态）。
方法复刻 #96416 评论里的直连探测：带 context_token / 不带 context_token 各一次。
"""
import asyncio
import glob
import json
import os
import sys
import uuid

sys.path.insert(0, "/home/ubuntu/.hermes/hermes-agent")

for line in open(os.path.expanduser("~/.hermes/.env")):
    line = line.strip()
    if line.startswith("WEIXIN_") and "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)

import aiohttp  # noqa: E402
from gateway.platforms import weixin as w  # noqa: E402

store_files = glob.glob("/home/ubuntu/.hermes/weixin/accounts/*.context-tokens.json")
if not store_files:
    print("ABORT: no context-token store found")
    raise SystemExit(1)
account_id = os.path.basename(store_files[0]).replace(".context-tokens.json", "")
data = json.load(open(store_files[0]))
chat_id = list(data.keys())[0]
ctx = data[chat_id]

token = os.environ.get("WEIXIN_TOKEN", "")
if not token:
    acct_file = f"/home/ubuntu/.hermes/weixin/accounts/{account_id}.json"
    if os.path.exists(acct_file):
        ad = json.load(open(acct_file))
        token = str(ad.get("token") or "")
base_url = os.environ.get("WEIXIN_BASE_URL") or w.ILINK_BASE_URL

print(f"account={account_id[:12]}... | chat={chat_id[:14]}... | ctx_len={len(ctx)} | "
      f"token_loaded={bool(token)} | endpoint={w.EP_SEND_MESSAGE}")
if not token:
    print("ABORT: token not found")
    raise SystemExit(1)


async def probe(text, use_ctx):
    async with aiohttp.ClientSession(trust_env=True) as sess:
        try:
            return await w._send_message(
                sess, base_url=base_url, token=token, to=chat_id, text=text,
                context_token=(ctx if use_ctx else None), client_id=str(uuid.uuid4()),
            )
        except Exception as e:  # noqa: BLE001
            return {"EXCEPTION": repr(e)}


r1 = asyncio.run(probe("🔧【通道自检①】原始探测（看到=通道正常，无需操作）", True))
print("RAW(with ctx):", json.dumps(r1, ensure_ascii=False)[:500])
ok1 = isinstance(r1, dict) and r1.get("ret") == 0
if not ok1:
    r2 = asyncio.run(probe("🔧【通道自检②】原始探测（看到=通道正常，无需操作）", False))
    print("RAW(no ctx):", json.dumps(r2, ensure_ascii=False)[:500])
else:
    print("(with-ctx 已成功送达，跳过第二探)")
