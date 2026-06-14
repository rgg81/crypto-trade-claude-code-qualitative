import pytest
from pydantic import ValidationError

from futures_fund.contracts import AgentProposal
from futures_fund.cycle_io import cycle_dir, load_output, save_output, validate_output


def test_save_and_load_roundtrip(tmp_path):
    save_output(tmp_path, 3, "watcher", {"candidates": []})
    assert cycle_dir(tmp_path, 3).name == "3"
    assert load_output(tmp_path, 3, "watcher") == {"candidates": []}


def test_save_accepts_pydantic_model(tmp_path):
    ap = AgentProposal(symbol="BTCUSDT", direction="long", entry=100.0, stop=95.0,
                       take_profits=[110.0], atr=2.0, confidence=0.6)
    save_output(tmp_path, 1, "trader_BTCUSDT", ap)
    assert load_output(tmp_path, 1, "trader_BTCUSDT")["symbol"] == "BTCUSDT"


def test_validate_output_returns_model_on_good_data():
    data = {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 95.0,
            "take_profits": [110.0], "atr": 2.0, "confidence": 0.6}
    model = validate_output(data, AgentProposal)
    assert isinstance(model, AgentProposal) and model.symbol == "BTCUSDT"


def test_validate_output_raises_clear_error_on_bad_data():
    with pytest.raises(ValidationError):
        validate_output({"symbol": "BTCUSDT", "direction": "sideways"}, AgentProposal)


def test_load_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_output(tmp_path, 9, "nope")
