import sys
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("huicr requires Python 3.11 or newer on PATH")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from huicr.cli import main

main()
