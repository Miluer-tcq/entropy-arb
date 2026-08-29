"""HLVenue.spot_usdc_free parsing + unified-account free-margin fallback."""
from entropy_arb.venue_hl import HLVenue


def test_spot_usdc_free_total_minus_hold():
    balances = [{"coin": "USDC", "total": "10.212028", "hold": "0.0"},
                {"coin": "HYPE", "total": "5.0", "hold": "1.0"}]
    assert HLVenue.spot_usdc_free(balances) == 10.212028


def test_spot_usdc_free_subtracts_hold():
    balances = [{"coin": "USDC", "total": "10.0", "hold": "2.5"}]
    assert HLVenue.spot_usdc_free(balances) == 7.5


def test_spot_usdc_free_missing_or_empty():
    assert HLVenue.spot_usdc_free([]) == 0.0
    assert HLVenue.spot_usdc_free(None) == 0.0
    assert HLVenue.spot_usdc_free([{"coin": "HYPE", "total": "1.0"}]) == 0.0


def test_spot_usdc_free_never_negative():
    balances = [{"coin": "USDC", "total": "1.0", "hold": "2.0"}]
    assert HLVenue.spot_usdc_free(balances) == 0.0


def test_spot_usdc_free_bad_values():
    assert HLVenue.spot_usdc_free([{"coin": "USDC", "total": None}]) == 0.0
