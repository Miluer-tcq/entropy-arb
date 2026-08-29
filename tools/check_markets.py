#!/usr/bin/env python3
"""列出当前所有可交易的 品种x对冲腿 组合。

用法:  python tools/check_markets.py
标准库实现，无需额外依赖。Entropy(io) 与各对冲场所的上市品种每天都会变，
实盘前先跑一次确认 --symbol 在两边都是 active。
"""
import json
import urllib.request


def hl_info(payload: dict) -> dict:
    req = urllib.request.Request(
        "https://api.hyperliquid.xyz/info",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read())


def hl_universe(dex: str) -> set:
    return {a["name"].split(":")[-1]
            for a in hl_info({"type": "meta", "dex": dex})["universe"]
            if not a.get("isDelisted")}


def main() -> None:
    ent = hl_universe("io")
    print(f"Entropy(io) 可交易: {sorted(ent)}")
    try:
        xyz = hl_universe("xyz")
    except Exception:
        xyz = set()

    for label, url in [("lighter", "https://mainnet.zklighter.elliot.ai/api/v1/orderBooks"),
                       ("lighter-rh", "https://api.rh.lighter.xyz/api/v1/orderBooks")]:
        obs = get(url).get("order_books") or []
        active = {ob["symbol"] for ob in obs if ob.get("status") == "active"}
        inter = sorted(ent & active)
        print(f"--hedge {label:11s} 可交易: {inter or '无'}")

    print(f"--hedge tradexyz  可交易: {sorted(ent & xyz) or '无'}")


if __name__ == "__main__":
    main()
