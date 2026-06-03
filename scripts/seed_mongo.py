"""Seed The Boardroom's MongoDB Atlas database.

Loads:
  - 1 demo user
  - 1 sample portfolio (5 positions, real tickers)
  - 4 director personas from seed_data/director_personas.json
  - 8 cross-call patterns from seed_data/patterns.json
  - 12 historical track records from seed_data/track_records.json
  - All required indexes

Run once after MongoDB Atlas + .env are set up:
  cd the-boardroom
  python scripts/seed_mongo.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

URI = os.getenv("MONGODB_URI")
DB_NAME = os.getenv("MONGODB_DB", "boardroom")

if not URI:
    print("error: MONGODB_URI not set in .env", file=sys.stderr)
    sys.exit(2)

try:
    from pymongo import MongoClient, ASCENDING, DESCENDING
    import certifi
except ImportError:
    print("error: pymongo + certifi not installed. Run pip install -r backend/requirements.txt", file=sys.stderr)
    sys.exit(2)

SEED_DIR = REPO_ROOT / "seed_data"


def _load_json(name: str) -> dict:
    with open(SEED_DIR / name) as f:
        return json.load(f)


def main() -> None:
    print(f"connecting to {URI.split('@')[-1].split('/')[0]}/{DB_NAME}")
    client = MongoClient(URI, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=15000)
    db = client[DB_NAME]

    # --- 1. Demo user ---
    db.users.update_one(
        {"_id": "demo"},
        {"$set": {
            "_id": "demo",
            "display_name": "Demo Investor",
            "timezone": "America/New_York",
            "market_clock": {"open": "09:30", "close": "16:00"},
            "default_risk_tolerance": "moderate",
        }},
        upsert=True,
    )
    print("  ✓ users: 1 demo user")

    # --- 2. Sample portfolio ---
    portfolio_doc = {
        "user_id": "demo",
        "positions": [
            {"ticker": "TSLA",  "qty": 100, "avg_cost": 198.50, "weight_pct": 0.12},
            {"ticker": "NVDA",  "qty": 50,  "avg_cost": 412.30, "weight_pct": 0.18},
            {"ticker": "MSFT",  "qty": 80,  "avg_cost": 350.00, "weight_pct": 0.16},
            {"ticker": "AAPL",  "qty": 100, "avg_cost": 175.00, "weight_pct": 0.10},
            {"ticker": "QQQ",   "qty": 60,  "avg_cost": 380.00, "weight_pct": 0.20},
        ],
        "cash_usd": 50000,
        "total_value_usd": 412000,
    }
    db.portfolios.update_one(
        {"user_id": "demo"},
        {"$set": portfolio_doc},
        upsert=True,
    )
    print(f"  ✓ portfolios: 1 demo portfolio ({len(portfolio_doc['positions'])} positions)")

    # --- 3. Director personas ---
    personas = _load_json("director_personas.json")
    db.director_personas.delete_many({})
    for director in personas["directors"]:
        db.director_personas.insert_one(director)
    # Chairman is stored separately.
    db.director_personas.insert_one(personas["chairman"])
    print(f"  ✓ director_personas: 4 directors + 1 chairman")

    # --- 4. Patterns ---
    pattern_data = _load_json("patterns.json")
    db.patterns.delete_many({})
    for pattern in pattern_data["patterns"]:
        db.patterns.insert_one(pattern)
    print(f"  ✓ patterns: {len(pattern_data['patterns'])} cross-call patterns")

    # --- 5. Track records ---
    track_data = _load_json("track_records.json")
    db.track_records.delete_many({})
    for record in track_data["records"]:
        db.track_records.insert_one(record)
    print(f"  ✓ track_records: {len(track_data['records'])} historical predictions")

    # --- 6. Indexes ---
    db.audio_sources.create_index([("user_id", ASCENDING), ("status", ASCENDING)])
    db.audio_sources.create_index([("pinned", ASCENDING), ("status", ASCENDING)])
    db.transcripts.create_index([("audio_id", ASCENDING)], unique=True)
    db.director_score_blocks.create_index([
        ("audio_id", ASCENDING),
        ("director", ASCENDING),
        ("topic", ASCENDING),
        ("ts_from_start", ASCENDING),
    ])
    db.director_score_blocks.create_index([("audio_id", ASCENDING), ("director", ASCENDING)])
    db.arguments.create_index([("audio_id", ASCENDING), ("topic", ASCENDING)])
    db.portfolio_impacts.create_index([("audio_id", ASCENDING), ("user_id", ASCENDING)])
    db.verdicts.create_index([("audio_id", ASCENDING)], unique=True)
    db.verdicts.create_index([("ticker", ASCENDING), ("created_at", DESCENDING)])
    db.track_records.create_index([("director", ASCENDING), ("ticker", ASCENDING)])
    db.patterns.create_index([("owned_by_director", ASCENDING)])
    db.decision_diary.create_index([("user_id", ASCENDING), ("executed_at", DESCENDING)])
    print("  ✓ indexes: 12 indexes created")

    # --- 7. Summary ---
    print()
    print("collections in db.{}:".format(DB_NAME))
    for name in db.list_collection_names():
        count = db[name].count_documents({})
        print(f"  {name:30s} {count} documents")


if __name__ == "__main__":
    main()
