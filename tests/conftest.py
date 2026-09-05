import os
import sys
from pathlib import Path

# Permet `import app...` peu importe le repertoire d'ou pytest est lance.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("SESSION_SECRET_KEY", "test-secret-key-not-for-production")

# Le demarrage de l'application lance le planificateur de snapshots
# (app.snapshots.start_scheduler). Sous TestClient, chaque suite le
# declencherait : un thread de fond qui appelle `zfs` pendant que les tests
# tournent. Le comportement du planificateur est teste directement, sans
# passer par le thread.
os.environ.setdefault("NAS_MANAGER_SNAPSHOT_SCHEDULER", "0")
