"""
Manual fix for prop_enabled toggle issue.
This will directly update the database to ensure the config exists.
"""

import sqlite3
import os

def find_db_file():
    """Find the correct database file."""
    possible_paths = [
        "config.db",
        "hunterlog.db",
        "spots.db",
        "data/config.db",
        "data/hunterlog.db"
    ]
    
    for path in possible_paths:
        if os.path.exists(path):
            # Check if it has a config table
            try:
                conn = sqlite3.connect(path)
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='config'")
                if cursor.fetchone():
                    conn.close()
                    return path
                conn.close()
            except:
                pass
    
    return None

def fix_prop_config():
    """Initialize or fix propagation config."""
    
    db_path = find_db_file()
    
    if not db_path:
        print("[ERROR] Could not find database with config table")
        print("Please run this from the hunterlog_plus_plus directory")
        return False
    
    print(f"[INFO] Using database: {db_path}")
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check if prop_enabled exists
        cursor.execute("SELECT key, val FROM config WHERE key = 'prop_enabled'")
        row = cursor.fetchone()
        
        if row:
            print(f"[INFO] prop_enabled exists, current value: {row[1]}")
            print("[INFO] Updating to 'True' for testing...")
            cursor.execute("UPDATE config SET val = 'True' WHERE key = 'prop_enabled'")
            conn.commit()
            print("[OK] Updated prop_enabled to 'True'")
        else:
            print("[INFO] prop_enabled doesn't exist, creating it...")
            cursor.execute(
                "INSERT INTO config (key, val, type, description, 'group', enabled, editable) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ('prop_enabled', 'False', 'bool', 'Enable propagation estimation feature', 'propagation', 'True', 'True')
            )
            conn.commit()
            print("[OK] Created prop_enabled config")
        
        # Show all prop configs
        print("\nAll propagation config values:")
        cursor.execute("SELECT key, val FROM config WHERE key LIKE 'prop%' ORDER BY key")
        for key, val in cursor.fetchall():
            print(f"  {key}: {val}")
        
        conn.close()
        return True
        
    except Exception as e:
        print(f"[ERROR] {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    print("=" * 60)
    print("Propagation Config Fixer")
    print("=" * 60)
    print()
    
    if fix_prop_config():
        print("\n[SUCCESS] Config fixed!")
        print("\nNow:")
        print("1. Restart the application")
        print("2. Go to Configuration -> Propagation tab")
        print("3. The switch should be ON")
        print("4. Try toggling it and clicking Save")
    else:
        print("\n[FAILED] Could not fix config")
    
    print("=" * 60)
