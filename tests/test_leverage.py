"""HLVenue.apply_leverage: gated no-op and correctly-shaped updateLeverage."""
import asyncio
from types import SimpleNamespace

from eth_account import Account
from hyperliquid.utils import signing as hl_signing

from entropy_arb.venue_hl import HLVenue


class _Nonces:
    def __init__(self):
        self.n = 1000

    def next(self):
        self.n += 1
        return self.n


def _make(lev):
    acct = Account.create()

    def fake_post(payload):
        captured.append(payload)

        async def _r():
            return {"status": "ok"}, None, False
        return _r()
    captured = []
    obj = SimpleNamespace(
        conf=SimpleNamespace(hl_leverage=lev),
        account=SimpleNamespace(wallet=acct, nonces=_Nonces(),
                                is_mainnet=True),
        asset_id=200002,
        _signing=hl_signing,
        name="ENTROPY",
        _post_exchange=fake_post,
    )
    return obj, captured


def test_apply_leverage_skips_at_1x():
    obj, cap = _make(1)
    asyncio.run(HLVenue.apply_leverage(obj))
    assert cap == []


def test_apply_leverage_posts_isolated_update():
    obj, cap = _make(5)
    asyncio.run(HLVenue.apply_leverage(obj))
    assert len(cap) == 1
    action = cap[0]["action"]
    assert action == {"type": "updateLeverage", "asset": 200002,
                      "isCross": False, "leverage": 5}
    assert cap[0]["nonce"] > 1000
    assert cap[0]["signature"]["r"]
