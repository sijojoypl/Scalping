import random

import pytest
from helpers import ref_atr, ref_ema, ref_rma, ref_rsi, ref_sma, series

from scalper.indicators import ATR, EMA, RMA, RSI, SMA, crossover, crossunder

# Wilder's RSI(14) worked example from StockCharts ChartSchool.
STOCKCHARTS_CLOSES = [
    44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433,
    46.0826, 45.8931, 46.0328, 45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116,
    46.2222, 45.6439, 46.2122, 46.2521, 45.7137, 46.4515, 45.7835, 45.3548, 44.0288,
    44.1783, 44.2181, 44.5672, 43.4205, 42.6628, 43.1314,
]
STOCKCHARTS_RSI = [
    70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38, 54.71,
    50.42, 39.99, 41.46, 41.87, 45.46, 37.30, 33.08, 37.77,
]


def stream(ind, values):
    return [ind.update(v) for v in values]


def assert_series_close(got, want, tol=1e-9):
    assert len(got) == len(want)
    for i, (g, w) in enumerate(zip(got, want)):
        if w is None:
            assert g is None, f"index {i}: expected na, got {g}"
        else:
            assert g == pytest.approx(w, rel=tol, abs=tol), f"index {i}"


def test_rsi_matches_published_wilder_example():
    got = stream(RSI(14), STOCKCHARTS_CLOSES)
    assert got[:14] == [None] * 14
    assert [round(v, 2) for v in got[14:]] == STOCKCHARTS_RSI


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_streaming_indicators_match_reference(seed):
    rng = random.Random(seed)
    closes = [1.0]
    for _ in range(400):
        closes.append(closes[-1] + rng.gauss(0, 0.001))
    bars = series(closes)

    assert_series_close(stream(SMA(5), closes), ref_sma(closes, 5))
    assert_series_close(stream(RMA(20), closes), ref_rma(closes, 20))
    for n in (4, 10, 15, 19, 25):
        assert_series_close(stream(EMA(n), closes), ref_ema(closes, n))
    rsi = stream(RSI(20), closes)
    assert_series_close(rsi, ref_rsi(closes, 20))
    atr = ATR(14)
    assert_series_close([atr.update(b.high, b.low, b.close) for b in bars], ref_atr(bars, 14))


def test_rsi_extremes():
    assert stream(RSI(5), [float(i) for i in range(10)])[-1] == 100.0
    assert stream(RSI(5), [float(-i) for i in range(10)])[-1] == 0.0


def test_atr_first_true_range_is_high_minus_low():
    atr = ATR(1)
    assert atr.update(1.5, 1.0, 1.2) == pytest.approx(0.5)
    # gap up: true range reaches back to the previous close
    assert atr.update(2.0, 1.9, 1.95) == pytest.approx(0.8)


def test_cross_semantics():
    assert crossunder(49.5, 48.9, 49, 49)
    assert crossunder(49.0, 48.9, 49, 49)  # touching the level on the previous bar counts
    assert not crossunder(48.9, 48.5, 49, 49)
    assert not crossunder(None, 48.5, 49, 49)
    assert crossover(55.0, 55.1, 55, 55)
    assert not crossover(55.1, 55.2, 55, 55)
    assert not crossover(54.0, 55.0, 55, 55)  # equal is not above
