"""
Quick migration script to add propagation fields to spots table.
Run this once to add the new columns.
"""

import sqlite3
import os

# Path to your spots database
DB_PATH = "spots.db"

def add_propagation_columns():
    """Add propagation estimation columns to the spots table."""
    
    if not os.path.exists(DB_PATH):
        print(f"Error: Database file '{DB_PATH}' not found!")
        print("Make sure you're running this from the hunterlog_plus_plus root directory.")
        return False
    
    try:
        # Connect to the database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        print("Adding propagation columns to spots table...")
        
        columns_added = 0
        columns_skipped = 0
        
        # Try to add each column individually
        columns_to_add = [
            ("propagation_snr", "REAL"),
            ("propagation_status", "TEXT"),
            ("propagation_updated", "TIMESTAMP"),
            ("propagation_probability", "REAL"),
            ("propagation_support", "REAL"),
        ]
        
        for col_name, col_type in columns_to_add:
            try:
                cursor.execute(f"""
                    ALTER TABLE spots 
                    ADD COLUMN {col_name} {col_type};
                """)
                print(f"[OK] Added {col_name} column")
                columns_added += 1
            except sqlite3.OperationalError as e:
                if "duplicate column name" in str(e).lower():
                    print(f"[SKIP] Column {col_name} already exists")
                    columns_skipped += 1
                else:
                    raise
        
        # Commit the changes
        conn.commit()
        
        print(f"\n[SUCCESS] Migration completed!")
        print(f"  Columns added: {columns_added}")
        print(f"  Columns already existed: {columns_skipped}")
        
        if columns_added > 0:
            print("\nYou can now restart the application.")
        
        return True
        
    except Exception as e:
        print(f"[ERROR] Unexpected error: {e}")
        return False
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    print("=" * 60)
    print("Propagation Feature - Database Migration")
    print("=" * 60)
    print()
    
    success = add_propagation_columns()
    
    print()
    if success:
        print("Next steps:")
        print("1. Restart the application: npm run start:windows")
        print("2. Enable propagation: Configuration -> Propagation tab")
    else:
        print("Migration failed. Please check the error messages above.")
    print("=" * 60)
