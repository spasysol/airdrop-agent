"""Testnet faucets with cooldown tracking and real form handling.

Catalog lives in faucets.yaml. Each entry states how it wants the address
(`field`: selector or label) and how to submit, because every faucet differs.
"""
from __future__ import annotations

import asyncio
import random
import time

from .. import config, db, policy
from ..browser import flows
from ..jsonio import Denied
from ..log import get_logger

log = get_logger("faucet")


def catalog() -> dict:
    return config.load_yaml("faucets.yaml").get("faucets", {})


def for_chain(chain: str) -> list[dict]:
    out = []
    for name, f in catalog().items():
        if f.get("chain") != chain or not f.get("enabled", True):
            continue
        last = db.kv_get(f"faucet:{name}", 0) or 0
        cool = float(f.get("cooldown_hours", 24)) * 3600
        out.append({**f, "name": name, "due": (time.time() - last) > cool,
                    "wait_h": round(max(0, cool - (time.time() - last)) / 3600, 1)})
    out.sort(key=lambda x: (not x["due"], x["name"]))
    return out


def pick(chain: str, name: str = "") -> dict:
    if name:
        f = catalog().get(name)
        if not f:
            raise Denied(f"faucet '{name}' not in faucets.yaml")
        return {**f, "name": name}
    cands = [f for f in for_chain(chain) if f["due"]]
    if not cands:
        raise Denied(f"no due faucet for {chain} (all cooling down) — aa testnet faucets --chain {chain}")
    return random.choice(cands[:3])


async def claim(b, identity_id: str, chain: str, name: str = "") -> dict:
    f = pick(chain, name)
    kind = policy.chain(chain).get("kind", "evm")
    # wallets are stored as "sol", policy kind is "solana" — map it
    kind = {"solana": "sol"}.get(kind, kind)
    ws = db.wallets(identity_id, kind)
    if not ws:
        raise Denied(f"identity has no {kind} wallet")
    addr = ws[0]["address"]
    try:
        b.page.set_default_timeout(15000)
    except Exception:  # noqa: BLE001
        pass
    await b.goto(f["url"], check_scam=False)
    await flows.dismiss(b)
    cap = await b.solve_captcha_if_present()
    filled = False
    field = f.get("field")
    if field:
        try:
            if await b.page.locator(field).count():
                await b.fill(addr, selector=field)
                filled = True
        except Exception as e:  # noqa: BLE001
            log.warning("faucet selector miss (%s), trying label fallback", str(e)[:80])
    if not filled:
        try:
            snap = await b.snapshot()
            for fl in snap.get("fields", []):
                if fl.get("type") in ("text", "email") or "address" in (fl.get("label") or "").lower():
                    await b.fill(addr, index=fl["i"])
                    filled = True
                    break
        except Exception as e:  # noqa: BLE001
            log.warning("faucet label fallback failed: %s", e)
    if not filled:
        shot = await b.screenshot("faucet_field_missing")
        return {"faucet": f["name"], "claimed": False, "needs": "manual",
                "screenshot": shot, "captcha": cap if cap.get("present") else None}
    if f.get("select"):
        for sel, val in f["select"].items():
            try:
                await b.select(sel, val)
            except Exception:  # noqa: BLE001
                pass
    for txt in dict.fromkeys((f.get("submit") or "Send", "Request", "Claim", "Kirim")):
        try:
            if not await b.page.get_by_text(txt, exact=False).count():
                continue
            await b.click(text=txt)
            break
        except Exception:  # noqa: BLE001
            continue
    await b.human_wait(2.5, 4.5)
    cap2 = await b.solve_captcha_if_present()
    await b.human_wait(2.0, 4.0)
    txt = (await b.text()).lower()
    ok = any(w in txt for w in ("sent", "success", "successful", "dikirim", "on the way", "received")) and \
        not any(w in txt for w in ("error", "failed", "limit", "cooldown"))
    if ok or f.get("assume_ok"):
        db.kv_set(f"faucet:{f['name']}", time.time())
        db.event("faucet", f"{f['name']} for {addr[:10]}", identity_id)
    shot = await b.screenshot(f"faucet_{f['name']}")
    return {"faucet": f["name"], "chain": chain, "address": addr, "claimed": ok,
            "captcha": {**cap, **cap2} if cap.get("present") or cap2.get("present") else None,
            "screenshot": shot}


async def run_due(identity_id: str, chain: str, rpc_wait: bool = True) -> dict:
    """Claim every due faucet for a chain, then optionally wait for the balance."""
    results = []
    for f in for_chain(chain):
        if not f["due"]:
            continue
        async with __import__("aa.browser.engine", fromlist=["Browser"]).Browser(
                identity_id, profile="faucet", chain=chain) as b:
            try:
                r = await asyncio.wait_for(claim(b, identity_id, chain, f["name"]), timeout=75)
                results.append(r)
            except asyncio.TimeoutError:
                results.append({"faucet": f["name"], "needs": "manual", "error": "75s budget exhausted"})
            except Exception as e:  # noqa: BLE001
                results.append({"faucet": f["name"], "error": str(e)[:200]})
        await asyncio.sleep(random.uniform(5, 15))
    if any(r.get("claimed") for r in results) and rpc_wait:
        await asyncio.sleep(30)
    return {"chain": chain, "results": results}


def status() -> list[dict]:
    out = []
    for chain in (policy.cfg().get("chains") or {}):
        if not policy.chain(chain).get("testnet"):
            continue
        for f in for_chain(chain):
            out.append({"chain": chain, "faucet": f["name"], "due": f["due"], "wait_h": f["wait_h"]})
    return out
