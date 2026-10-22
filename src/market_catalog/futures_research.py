"""Prerequisites and a deterministic calendar roll for individual futures research."""

from datetime import date


def require_individual_contract(contract: dict) -> None:
    required = ("instrument_id", "provider_security_id", "first_intent_date", "first_notice_date", "last_trade_date", "broker_closeout_date")
    if any(not contract.get(key) for key in required) or contract.get("exchange_calendar_verified") is not True:
        raise ValueError("Individual futures require verified identity, calendar, intent, notice, expiry and broker cutoff")
    if contract.get("execution_eligible") is False:
        raise ValueError("Research/quarantined evidence is not eligible for execution")
    if contract.get("is_continuous") is not False or contract["instrument_id"] in {"MGC", "MGC=F", "MGC-ACTIVE"}:
        raise ValueError("A continuous/root alias is not an individual tradable contract")
    dates = {key: date.fromisoformat(contract[key]) for key in
             ("first_intent_date", "first_notice_date", "last_trade_date", "broker_closeout_date")}
    if not dates["first_intent_date"] <= dates["first_notice_date"] <= dates["last_trade_date"]:
        raise ValueError("Intent, notice and expiry dates are inconsistent")
    if dates["broker_closeout_date"] > dates["last_trade_date"]:
        raise ValueError("Broker cutoff cannot follow contract expiry")


def rollover_session(contract: dict, reviewed_sessions: list[str], safety_sessions: int = 5) -> str:
    """Roll before the earliest delivery/trading/broker cutoff using supplied CME sessions.

    This accepts an externally reviewed calendar; it cannot certify that a caller
    supplied every exchange session. It never generates dates from NYSE weekdays.
    """
    require_individual_contract(contract)
    if isinstance(safety_sessions, bool) or not isinstance(safety_sessions, int) or safety_sessions < 1:
        raise ValueError("safety_sessions must be a positive integer")
    sessions = [date.fromisoformat(day) for day in reviewed_sessions]
    if not sessions or sessions != sorted(set(sessions)):
        raise ValueError("Reviewed exchange sessions must be ordered and unique")
    cutoff = min(date.fromisoformat(contract[key]) for key in
                 ("first_intent_date", "first_notice_date", "last_trade_date", "broker_closeout_date"))
    if not sessions[0] < cutoff <= sessions[-1]:
        raise ValueError("Reviewed exchange sessions do not cover the cutoff")
    before = [day for day in sessions if day < cutoff]
    if len(before) < safety_sessions:
        raise ValueError("Not enough reviewed sessions before the delivery cutoff")
    return before[-safety_sessions].isoformat()
