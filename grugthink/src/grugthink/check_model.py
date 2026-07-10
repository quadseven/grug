import os
import sys

from .grug_db import GrugDB

# Add the current directory to PYTHONPATH for grug_db to be found
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    # Initialize GrugDB, which will try to load the SentenceTransformer model
    db = GrugDB("temp_grug_lore.db")
    db.close()
    print("Grug's local thinking spirit model loaded successfully!")
    # Clean up temporary db files
    if os.path.exists("temp_grug_lore.db"):
        os.remove("temp_grug_lore.db")
    if os.path.exists("temp_grug_lore.index"):
        os.remove("temp_grug_lore.index")
except Exception as e:
    print(f"Grug's local thinking spirit model failed to load: {e}")
    sys.exit(1)
