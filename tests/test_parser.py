import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.parser import is_solana_address, parse

REAL_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"   # USDC, в денилисте
FAKE_MINT = "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E"


def test_base58_validation():
    assert is_solana_address(FAKE_MINT)
    assert not is_solana_address("thisisdefinitelynotanaddressatall12")
    assert not is_solana_address("short")


def test_pumpfun_link():
    text = f"new gem just launched https://pump.fun/coin/{FAKE_MINT} $GEM"
    r = parse(text)
    assert r.has_contract
    assert r.primary.address == FAKE_MINT
    assert r.primary.chain == "solana"
    assert r.primary.source == "url"
    assert r.tickers == ["GEM"]


def test_bare_solana_address():
    r = parse(f"CA: {FAKE_MINT}")
    assert r.primary.address == FAKE_MINT
    assert r.primary.source == "text"


def test_evm_address():
    addr = "0x6982508145454Ce325dDbE47a25d4ec3d2311933"
    r = parse(f"ape in {addr} now")
    assert r.primary.address == addr
    assert r.primary.chain == "evm"


def test_denylist_filters_usdc():
    r = parse(f"paid in {REAL_MINT} today")
    assert not r.has_contract


def test_no_false_positive_on_plain_text():
    text = "gm everyone, market looks strong today, watching a few setups closely"
    assert not parse(text).has_contract


def test_tco_link_needs_expanded_urls():
    text = "look at this https://t.co/abc123"
    assert not parse(text).has_contract
    expanded = [f"https://dexscreener.com/solana/{FAKE_MINT}"]
    assert parse(text, expanded_urls=expanded).primary.address == FAKE_MINT


def test_dedup_same_address_twice():
    text = f"{FAKE_MINT} — buy here https://pump.fun/coin/{FAKE_MINT}"
    assert len(parse(text).tokens) == 1


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
                passed += 1
            except AssertionError as exc:
                print(f"FAIL {name}: {exc}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
