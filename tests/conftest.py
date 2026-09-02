import os
import sys
from pathlib import Path

# Permet `import app...` peu importe le repertoire d'ou pytest est lance.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("SESSION_SECRET_KEY", "test-secret-key-not-for-production")
