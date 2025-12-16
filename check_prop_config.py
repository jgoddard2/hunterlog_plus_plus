"""
Quick script to check and initialize propagation config values.
"""

import sqlite3

DB_PATH = "config.db"

def check_and_init_config():
    """Check if propagation config exists and initialize if missing."""
    
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Check existing prop config
        cursor.execute("SELECT key, val FROM config WHERE key LIKE 'prop%'")
        rows = cursor.fetchall()
        
        print("Current propagation config values:")
        for key, val in rows:
            print(f"  {key}: {val}")
        
        # Check if prop_enabled exists
        cursor.execute("SELECT COUNT(*) FROM config WHERE key = 'prop_enabled'")
        count = cursor.fetchone()[0]
        
        if count == 0:
            print("\n[INFO] prop_enabled not found, adding defaults...")
            
            # Insert the defaults
            defaults = [
                ('prop_enabled', 'True', 'bool', 'Enable propagation estimation feature', 'propagation', 'True', 'True'),
                ('prop_enabled_seeded', 'False', 'bool', 'Internal propagation default flag', 'propagation', 'False', 'False'),
                ('prop_refresh_minutes', '3', 'int', 'Propagation data refresh interval in minutes', 'propagation', 'True', 'True'),
                ('prop_ssb_threshold', '10', 'int', 'SSB SNR threshold in dB', 'propagation', 'True', 'True'),
                ('prop_digital_threshold', '-15', 'int', 'Digital SNR threshold in dB', 'propagation', 'True', 'True'),
            ]
            
            for key, val, type_, desc, group, enabled, editable in defaults:
                cursor.execute(
                    "INSERT INTO config (key, val, type, description, `group`, enabled, editable) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (key, val, type_, desc, group, enabled, editable)
                )
                print(f"[OK] Added {key}")
            
            conn.commit()
            print("\n[SUCCESS] Propagation config initialized!")
        else:
            print("\n[INFO] Propagation config already exists.")
        
        conn.close()
        return True
        
    except Exception as e:
        print(f"[ERROR] {e}")
        return False

if __name__ == "__main__":
    print("=" * 60)
    print("Checking Propagation Configuration")
    print("=" * 60)
    print()
    
    check_and_init_config()
    
    print()
    print("=" * 60)
