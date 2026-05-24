#!/usr/bin/env python3
"""
Horse Racing SQLite Database — Schema + CSV Import.
Run: python3 db.py --init   (creates tables + imports CSVs)
     python3 db.py --update (incremental update from CSVs)
"""

import sqlite3, os, sys, argparse
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / "data" / "racing.db"
DATA_DIR = Path(__file__).parent / "data"

SCHEMA = """
CREATE TABLE IF NOT EXISTS races (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    course TEXT NOT NULL,       -- ST or HV
    race_no INTEGER NOT NULL,
    distance INTEGER,           -- meters
    class TEXT,                 -- e.g. C4, G1
    going TEXT,                 -- Good, Yielding, etc.
    participants INTEGER,
    prize TEXT,
    race_name TEXT,             -- e.g. 渣打冠軍暨遮打盃
    season TEXT,                -- e.g. 2025/26
    UNIQUE(date, course, race_no)
);

CREATE TABLE IF NOT EXISTS horses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT UNIQUE NOT NULL, -- e.g. K152
    name TEXT,                  -- Chinese name
    age INTEGER,
    sex TEXT,                   -- Gelding, Mare, Colt, etc.
    colour TEXT,
    origin TEXT,                -- 澳洲, 紐西蘭, etc.
    rating INTEGER,
    season_start_rating INTEGER,
    race_count INTEGER,
    import_date TEXT,
    trainer TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id INTEGER NOT NULL REFERENCES races(id),
    horse_id INTEGER REFERENCES horses(id),
    brand TEXT NOT NULL,
    horse_name TEXT,
    jockey TEXT,
    trainer TEXT,
    position INTEGER,          -- finishing position
    draw INTEGER,
    act_wt REAL,               -- actual weight
    decl_wt REAL,              -- declared weight
    odds REAL,
    finish_time REAL,          -- seconds
    lbw REAL,                  -- lengths behind winner
    running_style TEXT,        -- e.g. 跟前, 居中, 後上
    won INTEGER DEFAULT 0,    -- 1 if position == 1
    UNIQUE(race_id, brand)
);

CREATE TABLE IF NOT EXISTS sectionals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id INTEGER NOT NULL REFERENCES races(id),
    distance INTEGER,
    total_time REAL,
    splits TEXT,               -- comma-separated sectional times
    cumulatives TEXT,          -- comma-separated cumulative times
    num_sections INTEGER,
    early_pace REAL,
    late_pace REAL,
    pace_score REAL,
    UNIQUE(race_id)
);

CREATE TABLE IF NOT EXISTS race_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    horse_id INTEGER REFERENCES horses(id),
    brand TEXT NOT NULL,
    date TEXT NOT NULL,
    venue TEXT,
    distance INTEGER,
    going TEXT,
    class TEXT,
    draw INTEGER,
    rating INTEGER,
    trainer TEXT,
    jockey TEXT,
    lbw REAL,
    odds REAL,
    act_wt REAL,
    decl_wt REAL,
    running TEXT,
    finish_time REAL,
    position INTEGER,
    UNIQUE(brand, date, venue)
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,        -- e.g. V10.4
    version TEXT,               -- timestamp
    date TEXT NOT NULL,
    course TEXT,
    race_no INTEGER,
    horse_id INTEGER REFERENCES horses(id),
    brand TEXT,
    horse_name TEXT,
    jockey TEXT,
    trainer TEXT,
    draw INTEGER,
    weight REAL,
    rating REAL,
    prob REAL,                  -- model probability
    odds REAL,                  -- market odds at prediction time
    edge REAL,                  -- prob * odds
    recommendation TEXT,        -- 👍, 👀, or empty
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,  -- e.g. V10.4
    description TEXT,
    features INTEGER,
    training_period TEXT,
    top1_accuracy REAL,
    roi REAL,
    final_bankroll REAL,
    trades INTEGER,
    win_rate REAL,
    params_json TEXT,           -- JSON string of hyperparameters
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_results_race ON results(race_id);
CREATE INDEX IF NOT EXISTS idx_results_horse ON results(horse_id);
CREATE INDEX IF NOT EXISTS idx_results_brand ON results(brand);
CREATE INDEX IF NOT EXISTS idx_predictions_date ON predictions(date);
CREATE INDEX IF NOT EXISTS idx_predictions_model ON predictions(model);
CREATE INDEX IF NOT EXISTS idx_races_date ON races(date);
CREATE INDEX IF NOT EXISTS idx_horses_brand ON horses(brand);
CREATE INDEX IF NOT EXISTS idx_history_brand ON race_history(brand, date);
"""


def init_db():
    """Create tables and indexes."""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    print(f"Database initialized: {DB_PATH}")
    return True


def import_csv(conn, table, csv_path, columns, transform=None):
    """Import a CSV file into a table."""
    import pandas as pd
    if not csv_path.exists():
        print(f"  SKIP: {csv_path} not found")
        return 0

    df = pd.read_csv(csv_path, encoding='utf-8-sig')
    if transform:
        df = transform(df)

    # Filter to only columns that exist in the table
    existing_cols = [c for c in columns if c in df.columns]
    df = df[existing_cols]

    # Upsert: delete existing rows for same keys, then insert
    cursor = conn.cursor()
    rows_before = cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    df.to_sql(table, conn, if_exists='append', index=False)
    rows_after = cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    added = rows_after - rows_before
    conn.commit()
    return added


def populate_all():
    """Populate all tables from CSV files."""
    conn = sqlite3.connect(DB_PATH)
    print("Populating database...")

    # Races + Results are combined in hkjc_all_results_CN.csv
    results_csv = DATA_DIR / "hkjc_all_results_CN.csv"
    meta_csv = DATA_DIR / "hkjc_race_meta_CN.csv"
    profiles_csv = DATA_DIR / "hkjc_horse_profiles_CN.csv"
    history_csv = DATA_DIR / "hkjc_horse_race_history_CN.csv"
    sectionals_csv = DATA_DIR / "hkjc_sectionals_CN.csv"

    # Import horse profiles
    if profiles_csv.exists():
        import pandas as pd
        df = pd.read_csv(profiles_csv, encoding='utf-8-sig')
        df.columns = df.columns.str.strip()
        # Map columns: BrandNo -> brand
        col_map = {}
        for c in df.columns:
            if c == 'BrandNo': col_map[c] = 'brand'
            elif c == 'Age': col_map[c] = 'age'
            elif c == 'Sex': col_map[c] = 'sex'
            elif c == 'Colour': col_map[c] = 'colour'
            elif c == 'Origin': col_map[c] = 'origin'
            elif c == 'Rating': col_map[c] = 'rating'
            elif c == 'SeasonStartRating': col_map[c] = 'season_start_rating'
            elif c == 'TrainerCN': col_map[c] = 'trainer'
            elif c == 'ImportDate': col_map[c] = 'import_date'
            elif c == 'RaceCount': col_map[c] = 'race_count'
        df.rename(columns=col_map, inplace=True)
        cols = ['brand','age','sex','colour','origin','rating','season_start_rating',
                'trainer','import_date','race_count']
        df = df[[c for c in cols if c in df.columns]]
        df.to_sql('horses', conn, if_exists='replace', index=False)
        print(f"  Horses: {len(df)} rows")

    # Import races + results from combined CSV
    if results_csv.exists():
        import pandas as pd
        df = pd.read_csv(results_csv, encoding='utf-8-sig', usecols=range(15))
        df['Date'] = pd.to_datetime(df['Date'], format='%Y/%m/%d').dt.strftime('%Y-%m-%d')
        df['Brand'] = df['HorseCN'].str.extract(r'\(([A-Z]\d+)\)')
        df['won'] = (df['Pla'].astype(str).str.strip() == '1').astype(int)

        # Import race metadata
        if meta_csv.exists():
            meta = pd.read_csv(meta_csv, encoding='utf-8-sig')
            meta['Date'] = pd.to_datetime(meta['Date'], format='%Y/%m/%d').dt.strftime('%Y-%m-%d')
            meta['RaceNo'] = pd.to_numeric(meta['RaceNo'], errors='coerce')
            df['RaceNo'] = pd.to_numeric(df['RaceNo'], errors='coerce')
            df = df.merge(meta[['Date','Course','RaceNo','Distance','Class','Going','Participants']],
                          on=['Date','Course','RaceNo'], how='left')

        # Import races
        race_cols = ['Date','Course','RaceNo','Distance','Class','Going','Participants']
        races_df = df[race_cols].drop_duplicates()
        races_df.columns = [c.lower() for c in races_df.columns]
        races_df.to_sql('races', conn, if_exists='replace', index=False)
        print(f"  Races: {len(races_df)} rows")

        # Import results
        res_cols = ['Date','RaceNo','Course','Brand','HorseCN','JockeyCN','TrainerCN',
                     'Pla','Draw','ActWt','Odds','FinishTime','LBW','RunningCN','won']
        res_df = df[res_cols].copy()
        res_df.columns = ['date','race_no','course','brand','horse_name','jockey','trainer',
                          'position','draw','act_wt','odds','finish_time','lbw','running_style','won']
        res_df.to_sql('results', conn, if_exists='replace', index=False)
        print(f"  Results: {len(res_df)} rows")

    # Import sectionals
    if sectionals_csv.exists():
        import pandas as pd
        df = pd.read_csv(sectionals_csv, encoding='utf-8-sig')
        df['Date'] = pd.to_datetime(df['Date'], format='%Y/%m/%d').dt.strftime('%Y-%m-%d')
        df.columns = [c.lower() for c in df.columns]
        df.to_sql('sectionals', conn, if_exists='replace', index=False)
        print(f"  Sectionals: {len(df)} rows")

    # Import race history
    if history_csv.exists():
        import pandas as pd
        df = pd.read_csv(history_csv, encoding='utf-8-sig')
        df.columns = [c.lower() for c in df.columns]
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'], format='mixed', dayfirst=True).dt.strftime('%Y-%m-%d')
        df.to_sql('race_history', conn, if_exists='replace', index=False)
        print(f"  Race History: {len(df)} rows")

    conn.commit()
    conn.close()
    print("Database populated.")


def show_stats():
    """Print database statistics."""
    conn = sqlite3.connect(DB_PATH)
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    for (name,) in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        print(f"  {name}: {count:,} rows")
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true", help="Initialize DB + import CSVs")
    parser.add_argument("--stats", action="store_true", help="Show table stats")
    args = parser.parse_args()

    if args.init:
        init_db()
        populate_all()
        print("\nDatabase stats:")
        show_stats()
    elif args.stats:
        show_stats()
    else:
        print("Usage: python3 db.py --init | --stats")
