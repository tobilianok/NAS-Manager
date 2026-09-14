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

# Meme raison pour le planificateur de replication (v1.15.0), en plus grave :
# celui-la ouvre des sessions SSH vers de vraies adresses et peut lancer un
# `zfs send`. Rien de tel ne doit partir d'une suite de tests.
os.environ.setdefault("NAS_MANAGER_REPLICATION_SCHEDULER", "0")

# Et encore plus grave pour le chien de garde du quorum (v1.18.0) : il peut
# arreter des stacks, retirer des partages et PROMOUVOIR un groupe, sans que
# personne ait rien demande. Une suite de tests ne lance pas ca.
os.environ.setdefault("NAS_MANAGER_QUORUM_WATCHDOG", "0")

# Verification horaire des mises a jour (v1.19.0) : elle lance `apt-get -s`,
# un `git fetch` vers GitHub et un `docker manifest inspect` par image. Une
# suite de tests ne fait pas d'acces reseau. Le delai de premier passage
# (120 s) suffisait a la faire passer inapercue, mais compter sur la duree
# de la suite n'est pas une garantie.
os.environ.setdefault("NAS_MANAGER_UPDATE_SCHEDULER", "0")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_smart_cache():
    """La lecture SMART est mise en memoire courte depuis la v1.19.0 (un
    seul passage smartctl par rendu, partage entre la carte de sante et le
    tableau des temperatures). Sans remise a zero, un test heriterait des
    disques simules du precedent."""
    from app import smart, sensors
    smart.reset_reports_cache()
    sensors.reset_cache()
    yield
    smart.reset_reports_cache()
    sensors.reset_cache()
