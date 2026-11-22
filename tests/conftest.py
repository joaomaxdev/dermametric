import sys
from pathlib import Path

# Garante que a raiz do projeto esteja no sys.path quando o pytest roda.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
