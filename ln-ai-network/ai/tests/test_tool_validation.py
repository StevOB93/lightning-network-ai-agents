"""Tests for additional tool argument validation in _normalize_tool_args."""
from __future__ import annotations
from ai.tools import _normalize_tool_args

def test_amount_msat_zero_rejected():
    _, err, _ = _normalize_tool_args("ln_invoice", {"node": 1, "amount_msat": 0, "label": "x", "description": "y"})
    assert err is not None
    assert "amount_msat" in err

def test_amount_msat_negative_rejected():
    _, err, _ = _normalize_tool_args("ln_invoice", {"node": 1, "amount_msat": -1, "label": "x", "description": "y"})
    assert err is not None

def test_amount_msat_positive_accepted():
    _, err, _ = _normalize_tool_args("ln_invoice", {"node": 1, "amount_msat": 1000, "label": "x", "description": "y"})
    assert err is None

def test_amount_sat_zero_rejected():
    _, err, _ = _normalize_tool_args("ln_openchannel", {"from_node": 1, "peer_id": "02aa", "amount_sat": 0})
    assert err is not None

def test_amount_sat_positive_accepted():
    _, err, _ = _normalize_tool_args("ln_openchannel", {"from_node": 1, "peer_id": "02aa", "amount_sat": 100000})
    assert err is None

def test_port_zero_rejected():
    _, err, _ = _normalize_tool_args("ln_connect", {"from_node": 1, "peer_id": "02aa", "host": "127.0.0.1", "port": 0})
    assert err is not None
    assert "port" in err

def test_port_too_large_rejected():
    _, err, _ = _normalize_tool_args("ln_connect", {"from_node": 1, "peer_id": "02aa", "host": "127.0.0.1", "port": 99999})
    assert err is not None

def test_port_valid_accepted():
    _, err, _ = _normalize_tool_args("ln_connect", {"from_node": 1, "peer_id": "02aa", "host": "127.0.0.1", "port": 9735})
    assert err is None

def test_bolt11_invalid_prefix_rejected():
    _, err, _ = _normalize_tool_args("ln_pay", {"from_node": 1, "bolt11": "notaninvoice"})
    assert err is not None
    assert "bolt11" in err

def test_bolt11_valid_regtest_accepted():
    # lnbcrt prefix is regtest
    _, err, _ = _normalize_tool_args("ln_pay", {"from_node": 1, "bolt11": "lnbcrt1234567890"})
    assert err is None

def test_bolt11_valid_mainnet_accepted():
    _, err, _ = _normalize_tool_args("ln_pay", {"from_node": 1, "bolt11": "lnbc1234567890"})
    assert err is None

def test_amount_btc_zero_rejected():
    _, err, _ = _normalize_tool_args("btc_sendtoaddress", {"address": "bcrt1qtest", "amount_btc": "0"})
    assert err is not None

def test_amount_btc_positive_accepted():
    _, err, _ = _normalize_tool_args("btc_sendtoaddress", {"address": "bcrt1qtest", "amount_btc": "0.001"})
    assert err is None
