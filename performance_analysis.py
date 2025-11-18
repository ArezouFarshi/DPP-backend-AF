import math
import time
from typing import Any, Dict, List

SECONDS_IN_HOUR = 3600
WINDOW_HOURS = 24.0

# Normalization constants (can be tuned later)
STRUCT_FAULTS_MAX_PER_DAY = 5.0
THERMAL_EVENTS_MAX_PER_DAY = 5.0
NOISE_EVENTS_MAX_PER_DAY = 10.0  # reserved for future use


def _unix_to_iso(ts: int) -> str:
    """
    Convert a UNIX timestamp (seconds) to ISO 8601 string in UTC.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _classify_event(evt: Dict[str, Any]) -> Dict[str, str]:
    """
    Classify an event into:
      - domain: 'facade' or 'system'
      - kind: 'structural', 'thermal', or 'other'
    Uses the existing fields: prediction, reason.
    This is a heuristic and can be refined as your Oracle vocabulary stabilizes.
    """
    prediction = int(evt.get("prediction", 0))
    reason = (evt.get("reason") or "").lower()

    if prediction == -1:
        return {"domain": "system", "kind": "system"}

    # facade-side event
    kind = "other"
    if "tilt" in reason or "structural" in reason or "movement" in reason:
        kind = "structural"
    elif "temp" in reason or "thermal" in reason or "surface" in reason or "ambient" in reason:
        kind = "thermal"

    return {"domain": "facade", "kind": kind}


def _compute_structural_components(events: List[Dict[str, Any]], now_ts: int) -> Dict[str, float]:
    """
    Compute F_s, S_s, T_last, N_s (acoustic term currently 0) for the last WINDOW_HOURS.
    events: list of ALL events up to 'now_ts' (already filtered by panel).
    """
    window_start = now_ts - int(WINDOW_HOURS * SECONDS_IN_HOUR)

    structural_faults: List[Dict[str, Any]] = []
    for e in events:
        ts = int(e.get("timestamp", 0))
        if ts < window_start or ts > now_ts:
            continue
        classification = _classify_event(e)
        if classification["domain"] != "facade":
            continue
        if classification["kind"] != "structural":
            continue
        # treat prediction == 1 as structural fault
        if int(e.get("prediction", 0)) == 1:
            structural_faults.append(e)

    # F_s: structural fault frequency (0–100)
    n_struct = len(structural_faults)
    if STRUCT_FAULTS_MAX_PER_DAY > 0:
        F_s = min(100.0, 100.0 * n_struct / STRUCT_FAULTS_MAX_PER_DAY)
    else:
        F_s = 0.0

    # S_s: structural severity (0–100) – approximated from color / reason
    severities: List[float] = []
    for e in structural_faults:
        color = (e.get("color") or "").upper()
        reason = (e.get("reason") or "").lower()
        if "severe" in reason or "critical" in reason:
            sev = 100.0
        elif color.startswith("RED"):
            sev = 90.0
        elif color.startswith("YELLOW"):
            sev = 60.0
        else:
            sev = 40.0
        severities.append(sev)
    S_s = sum(severities) / len(severities) if severities else 0.0

    # T_last: time-since-last structural fault penalty (0–100)
    if structural_faults:
        last_ts = max(int(e.get("timestamp", 0)) for e in structural_faults)
        delta_hours = max(0.0, (now_ts - last_ts) / SECONDS_IN_HOUR)
        tau_hours = WINDOW_HOURS  # decay over ~24h
        T_last = 100.0 * math.exp(-delta_hours / tau_hours)
    else:
        T_last = 0.0

    # N_s: acoustic anomaly penalty – currently 0 until INMP441 is integrated
    N_s = 0.0

    return {
        "F_s": F_s,
        "S_s": S_s,
        "T_last": T_last,
        "N_s": N_s,
    }


def _compute_thermal_components(events: List[Dict[str, Any]], now_ts: int) -> Dict[str, float]:
    """
    Compute G_t, R_t, A_t, M_t (humidity term currently 0) for the last WINDOW_HOURS.
    Since this backend does not store raw Ts/Ta, we approximate these
    components from the thermal-related events (status, prediction, reason).
    """
    window_start = now_ts - int(WINDOW_HOURS * SECONDS_IN_HOUR)

    thermal_events: List[Dict[str, Any]] = []
    for e in events:
        ts = int(e.get("timestamp", 0))
        if ts < window_start or ts > now_ts:
            continue
        classification = _classify_event(e)
        if classification["domain"] != "facade":
            continue
        if classification["kind"] != "thermal":
            continue
        # include both faults and warnings as thermal anomalies
        pred = int(e.get("prediction", 0))
        if pred in (1, 2):
            thermal_events.append(e)

    n_thermal = len(thermal_events)

    # A_t: thermal anomaly frequency (0–100)
    if THERMAL_EVENTS_MAX_PER_DAY > 0:
        A_t = min(100.0, 100.0 * n_thermal / THERMAL_EVENTS_MAX_PER_DAY)
    else:
        A_t = 0.0

    # G_t: gradient deviation proxy (0–100) – approximate from thermal reason keywords
    gradient_scores: List[float] = []
    for e in thermal_events:
        reason = (e.get("reason") or "").lower()
        if "too high" in reason or "too low" in reason:
            score = 90.0
        elif "high" in reason or "low" in reason:
            score = 75.0
        else:
            score = 60.0
        gradient_scores.append(score)
    G_t = sum(gradient_scores) / len(gradient_scores) if gradient_scores else 0.0

    # R_t: rate-of-change proxy (0–100) – based on how close in time the last events are
    if len(thermal_events) >= 2:
        thermal_events_sorted = sorted(thermal_events, key=lambda e: int(e.get("timestamp", 0)))
        last_ts = int(thermal_events_sorted[-1].get("timestamp", 0))
        prev_ts = int(thermal_events_sorted[-2].get("timestamp", 0))
        delta_hours = max(0.0, (last_ts - prev_ts) / SECONDS_IN_HOUR)
        # If two events occur very close in time, penalty is high.
        # If they are far apart (>= WINDOW_HOURS), penalty drops to ~0.
        if delta_hours >= WINDOW_HOURS:
            R_t = 0.0
        else:
            R_t = max(0.0, 100.0 * (1.0 - delta_hours / WINDOW_HOURS))
    else:
        R_t = 0.0

    # M_t: humidity penalty – currently 0 until SHT41 is integrated
    M_t = 0.0

    return {
        "G_t": G_t,
        "R_t": R_t,
        "A_t": A_t,
        "M_t": M_t,
    }


def _compute_ssi(struct_components: Dict[str, float]) -> float:
    """
    SSI = 100 − (0.35·F_s + 0.35·S_s + 0.20·T_last + 0.10·N_s)
    """
    F_s = struct_components["F_s"]
    S_s = struct_components["S_s"]
    T_last = struct_components["T_last"]
    N_s = struct_components["N_s"]

    ssi = 100.0 - (0.35 * F_s + 0.35 * S_s + 0.20 * T_last + 0.10 * N_s)
    return max(0.0, min(100.0, ssi))


def _compute_tbi(thermal_components: Dict[str, float]) -> float:
    """
    TBI = 100 − (0.40·G_t + 0.25·R_t + 0.20·A_t + 0.15·M_t)
    """
    G_t = thermal_components["G_t"]
    R_t = thermal_components["R_t"]
    A_t = thermal_components["A_t"]
    M_t = thermal_components["M_t"]

    tbi = 100.0 - (0.40 * G_t + 0.25 * R_t + 0.20 * A_t + 0.15 * M_t)
    return max(0.0, min(100.0, tbi))


def _compute_performance_score(ssi: float, tbi: float) -> Dict[str, Any]:
    """
    PS = (SSI × 0.5 + TBI × 0.5) / 25
    Then mapped to A–D.
    """
    ps_raw = (ssi * 0.5 + tbi * 0.5) / 25.0  # 0–4
    if ps_raw >= 3.5:
        letter = "A"
        numeric = 4
    elif ps_raw >= 2.5:
        letter = "B"
        numeric = 3
    elif ps_raw >= 1.5:
        letter = "C"
        numeric = 2
    else:
        letter = "D"
        numeric = 1

    return {
        "raw": ps_raw,
        "numeric": numeric,
        "letter": letter,
    }


def compute_performance_for_panel(dpp: Dict[str, Any], events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Main entry point used from app.py.

    - dpp: full DPP JSON (after loading the panel file)
    - events: raw events from fetch_events_for_panel()

    Returns a dict with:
      - panel_metadata: subset of installation metadata (orientation, height, exposure)
      - points: time series [{timestamp_iso, timestamp_unix, ssi, tbi, performance_letter, performance_numeric}, ...]
      - system_events: [{timestamp_iso, reason, color}, ...] for separate plotting
    """
    # Extract basic panel metadata for context
    installation = dpp.get("installation_metadata", {})
    panel_metadata = {
        "tower_name": installation.get("tower_name"),
        "floor_number": installation.get("floor_number"),
        "location": installation.get("location"),
        "panel_azimuth_deg": installation.get("panel_azimuth_deg"),
        "elevation_m": installation.get("elevation_m"),
        "exposure_zone": installation.get("exposure_zone"),
        "tilt_angle_deg": installation.get("tilt_angle_deg"),
    }

    if not events:
        # No events yet – return empty points and no system errors
        return {
            "panel_metadata": panel_metadata,
            "points": [],
            "system_events": [],
        }

    # Sort events by timestamp
    sorted_events = sorted(events, key=lambda e: int(e.get("timestamp", 0)))
    # We'll compute indexes at each event time (sliding window over last 24h).
    points: List[Dict[str, Any]] = []
    system_events: List[Dict[str, Any]] = []

    for evt in sorted_events:
        now_ts = int(evt.get("timestamp", 0))
        # all events up to "now"
        events_up_to = [e for e in sorted_events if int(e.get("timestamp", 0)) <= now_ts]

        # classify this event for system graph
        cls = _classify_event(evt)
        if cls["domain"] == "system":
            system_events.append({
                "timestamp_unix": now_ts,
                "timestamp": _unix_to_iso(now_ts),
                "color": evt.get("color"),
                "status": evt.get("status"),
                "reason": evt.get("reason"),
            })

        struct_comp = _compute_structural_components(events_up_to, now_ts)
        thermal_comp = _compute_thermal_components(events_up_to, now_ts)

        ssi = _compute_ssi(struct_comp)
        tbi = _compute_tbi(thermal_comp)
        perf = _compute_performance_score(ssi, tbi)

        points.append({
            "timestamp_unix": now_ts,
            "timestamp": _unix_to_iso(now_ts),
            "ssi": ssi,
            "tbi": tbi,
            "performance_numeric": perf["numeric"],
            "performance_letter": perf["letter"],
            "performance_raw": perf["raw"],
        })

    return {
        "panel_metadata": panel_metadata,
        "points": points,
        "system_events": system_events,
    }
