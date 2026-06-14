"""Monthly risk-pacing engine (Pillar 1 DEPLOY): start soft -> press when behind pace (and NOT in
drawdown) -> throttle once the 5%/month target is hit. Anti-martingale: drawdown ALWAYS suppresses
press (the desk presses with UNUSED budget, never into losses)."""
from datetime import UTC, datetime

from futures_fund.pacing import (
    CAUTION_DD,
    PRESS_GAP,
    compute_pacing,
    pacing_state,
)


def _c(mtd, day, dd=0.0, heat=0.0, dim=30, target=0.05):
    # day = days elapsed in the month (1.0 = end of the 1st)
    return compute_pacing(mtd_return=mtd, days_elapsed=day, days_in_month=dim,
                          drawdown=dd, open_heat=heat, monthly_target=target)


def test_throttle_when_target_hit():
    s = _c(mtd=0.05, day=10)
    assert s.mode == "throttle"
    assert s.suggested_risk_mult <= 0.6
    s2 = _c(mtd=0.061, day=2)  # hit early -> still throttle
    assert s2.mode == "throttle"


def test_soft_early_month():
    s = _c(mtd=0.0, day=2)  # day 2 < SOFT_DAYS, behind but early -> soft, NOT press
    assert s.mode == "soft"


def test_press_when_behind_and_underdeployed_and_no_drawdown():
    # day 15 of 30, pace = 2.5%, mtd 0% -> gap -2.5% (> PRESS_GAP behind), no dd, low heat -> press
    s = _c(mtd=0.0, day=15, dd=0.0, heat=0.0)
    assert s.mode == "press"
    assert s.suggested_risk_mult >= 0.95
    assert s.appetite >= 0.8


def test_anti_martingale_drawdown_never_presses():
    # same behind-pace setup BUT in drawdown -> must NOT press (breakers own the loss path)
    s = _c(mtd=-0.04, day=15, dd=CAUTION_DD + 0.001, heat=0.0)
    assert s.mode != "press"
    assert s.mode == "soft"
    assert s.in_drawdown is True
    assert s.suggested_risk_mult <= 0.6


def test_no_press_when_already_deployed():
    # behind pace but heat already high (deployed) -> not under-deployed -> normal, not press
    s = _c(mtd=0.0, day=15, dd=0.0, heat=0.06)
    assert s.mode == "normal"


def test_normal_when_on_pace():
    # day 15, pace 2.5%, mtd 2.5% -> on pace -> normal
    s = _c(mtd=0.025, day=15, dd=0.0, heat=0.0)
    assert s.mode == "normal"


def test_pace_gap_sign_and_fields():
    s = _c(mtd=0.0, day=15, dim=30, target=0.05)
    assert abs(s.pace - 0.025) < 1e-9      # 0.05 * 15/30
    assert abs(s.pace_gap - (-0.025)) < 1e-9
    assert s.mtd_return == 0.0
    assert isinstance(s.directive, str) and len(s.directive) > 0


def test_press_requires_gap_beyond_threshold():
    # only slightly behind (< PRESS_GAP) -> normal, not press
    s = _c(mtd=0.025 - (PRESS_GAP * 0.5), day=15)
    assert s.mode == "normal"


def test_pacing_state_reads_equity_log(tmp_path):
    from futures_fund.equity_log import record_equity
    state = tmp_path / "s"
    # month-start anchor 10000 on Jun 1, latest 10000 on Jun 16 -> mtd 0%, day 15, behind -> press
    record_equity(state, datetime(2026, 6, 1, tzinfo=UTC), 10000.0, cycle=1)
    record_equity(state, datetime(2026, 6, 16, tzinfo=UTC), 10000.0, cycle=2)

    class _H:
        drawdown_from_peak = 0.0
        open_heat = 0.0
    s = pacing_state(state, datetime(2026, 6, 16, tzinfo=UTC), _H(), monthly_target=0.05)
    assert s.mode == "press"
    assert abs(s.mtd_return - 0.0) < 1e-9


def test_pacing_state_empty_log_is_soft(tmp_path):
    class _H:
        drawdown_from_peak = 0.0
        open_heat = 0.0
    s = pacing_state(tmp_path / "s", datetime(2026, 6, 16, tzinfo=UTC), _H())
    assert s.mode == "soft"  # no data -> conservative default


def test_pacing_state_mtd_from_month_start_anchor(tmp_path):
    from futures_fund.equity_log import record_equity
    state = tmp_path / "s"
    record_equity(state, datetime(2026, 5, 20, tzinfo=UTC), 9000.0, cycle=1)   # prior month
    record_equity(state, datetime(2026, 6, 1, tzinfo=UTC), 10000.0, cycle=2)   # month-start anchor
    record_equity(state, datetime(2026, 6, 10, tzinfo=UTC), 10300.0, cycle=3)  # +3% MTD

    class _H:
        drawdown_from_peak = 0.0
        open_heat = 0.0
    s = pacing_state(state, datetime(2026, 6, 10, tzinfo=UTC), _H(), monthly_target=0.05)
    assert abs(s.mtd_return - 0.03) < 1e-6   # vs the Jun-1 anchor, not the May point
