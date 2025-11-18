import os
import json
import time
from typing import Dict, Any, List
from flask import Flask, jsonify, request
from flask_cors import CORS
from web3 import Web3
from eth_account import Account

# ⭐ ADDED: performance endpoint import
from performance_analysis import compute_performance_for_panel

# -------------------------------------------------------------------
# Configuration (from environment variables)
# -------------------------------------------------------------------
INFURA_URL = os.getenv("INFURA_URL")              # e.g. https://sepolia.infura.io/v3/xxxx
CONTRACT_ADDRESS_ENV = os.getenv("CONTRACT_ADDRESS")
ABI_PATH = os.getenv("ABI_PATH", "contract_abi.json")
PANELS_DIR = os.getenv("PANELS_DIR", "panels")
CHAIN_ID = int(os.getenv("CHAIN_ID", "11155111")) # Sepolia default

PRIVATE_KEY = os.getenv("PRIVATE_KEY")            # Only needed if you sign TXs
ORACLE_ADDRESS = os.getenv("ORACLE_ADDRESS")
ADMIN_ADDRESS = os.getenv("ADMIN_ADDRESS")

if not INFURA_URL:
    raise RuntimeError("INFURA_URL is not set")
if not CONTRACT_ADDRESS_ENV:
    raise RuntimeError("CONTRACT_ADDRESS is not set")

CONTRACT_ADDRESS = Web3.to_checksum_address(CONTRACT_ADDRESS_ENV)

# -------------------------------------------------------------------
# Web3 setup
# -------------------------------------------------------------------
w3 = Web3(Web3.HTTPProvider(INFURA_URL))
if not w3.is_connected():
    raise RuntimeError("Web3 not connected to RPC")

with open(ABI_PATH, "r", encoding="utf-8") as f:
    CONTRACT_ABI = json.load(f)

contract = w3.eth.contract(address=CONTRACT_ADDRESS, abi=CONTRACT_ABI)

# Account object (optional)
if PRIVATE_KEY:
    account = Account.from_key(PRIVATE_KEY)
    print(f"Oracle account loaded: {account.address}")

# -------------------------------------------------------------------
# Flask app with CORS
# -------------------------------------------------------------------
app = Flask(__name__)

# Allow your deployed frontend domains
CORS(app, resources={
    r"/api/*": {
        "origins": [
            "https://www.blockchain-powered-dpp-af.com"

        ]
    }
})

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def load_panel_json(panel_id: str) -> Dict[str, Any]:
    path = os.path.join(PANELS_DIR, f"{panel_id}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Panel JSON not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def filter_by_access(dpp: Dict[str, Any], access: str) -> Dict[str, Any]:
    access = access.lower()
    allowed = {
        "public": {"Public"},
        "tier1": {"Public", "Tier 1"},
        "tier2": {"Public", "Tier 1", "Tier 2"}
    }
    tiers = allowed.get(access, {"Public"})
    filtered = {}

    for key, value in dpp.items():
        if isinstance(value, dict) and "Access_Tier" in value:
            if value.get("Access_Tier") in tiers:
                filtered[key] = value
            elif key == "installation_metadata" and access == "public":
                # Special case: always keep tower_name and location for Public
                filtered[key] = {
                    "tower_name": value.get("tower_name"),
                    "location": value.get("location"),
                    "Access_Tier": "Public"
                }
        elif key in ("fault_log_installation", "fault_log_operation"):
            if "Tier 2" in tiers:
                filtered[key] = value

    return filtered

def fetch_events_for_panel(panel_id: str) -> List[Dict[str, Any]]:
    count = contract.functions.getEventCount(panel_id).call()
    events = []
    for idx in range(count):
        ok, color, status, prediction, reason, timestamp = contract.functions.getEventAt(panel_id, idx).call()
        events.append({
            "timestamp": int(timestamp),
            "color": color,
            "status": status,
            "prediction": int(prediction),
            "reason": reason,
            "ok": bool(ok)
        })
    return events

def merge_events_into_dpp(dpp: Dict[str, Any], events: List[Dict[str, Any]]) -> Dict[str, Any]:
    dpp.setdefault("fault_log_installation", [])
    dpp.setdefault("fault_log_operation", [])
    if "digital_twin_status" in dpp and events:
        latest = events[-1]
        dpp["digital_twin_status"]["current_visual_status"] = latest["status"]
        dpp["digital_twin_status"]["last_color_change"] = latest["color"]
    for evt in events:
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(evt["timestamp"])),
            "color": evt["color"],
            "status": evt["status"],
            "prediction": evt["prediction"],
            "reason": evt["reason"]
        }
        if evt["prediction"] in (1, 2, -1):
            dpp["fault_log_operation"].append(entry)
    return dpp

# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------
@app.get("/api/dpp/<panel_id>")
def get_dpp(panel_id: str):
    access = request.args.get("access", "public").lower()
    try:
        dpp = load_panel_json(panel_id)
    except FileNotFoundError:
        return jsonify({"error": "Panel JSON not found"}), 404
    try:
        events = fetch_events_for_panel(panel_id)
        dpp = merge_events_into_dpp(dpp, events)
    except Exception as e:
        dpp.setdefault("_warnings", []).append(f"Blockchain events not merged: {str(e)}")
    filtered = filter_by_access(dpp, access)
    return jsonify({"panel_id": panel_id, "access": access, "data": filtered})


# ⭐⭐⭐ ADDED: PERFORMANCE ANALYSIS ENDPOINT ⭐⭐⭐
@app.get("/api/performance/<panel_id>")
def get_performance(panel_id: str):
    """
    Returns SSI, TBI, Performance Score + system errors for this panel.
    """
    # Load panel JSON
    try:
        dpp = load_panel_json(panel_id)
    except FileNotFoundError:
        return jsonify({"error": "Panel JSON not found"}), 404

    # Fetch blockchain events
    try:
        events = fetch_events_for_panel(panel_id)
    except Exception as e:
        installation = dpp.get("installation_metadata", {})
        # Return minimal response but DO NOT crash backend
        return jsonify({
            "panel_id": panel_id,
            "data": {
                "panel_metadata": {
                    "tower_name": installation.get("tower_name"),
                    "floor_number": installation.get("floor_number"),
                    "location": installation.get("location"),
                    "panel_azimuth_deg": installation.get("panel_azimuth_deg"),
                    "elevation_m": installation.get("elevation_m"),
                    "exposure_zone": installation.get("exposure_zone"),
                    "tilt_angle_deg": installation.get("tilt_angle_deg"),
                },
                "points": [],
                "system_events": [],
                "_warnings": [f"Performance not computed: {str(e)}"],
            },
        }), 200

    perf = compute_performance_for_panel(dpp, events)
    return jsonify({"panel_id": panel_id, "data": perf})


# -------------------------------------------------------------------
# Health check (optional, useful for Render)
# -------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}, 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
