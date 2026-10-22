"""Render the isolated single-host DeepInfer serving recipe."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy_deepinfer.render import main

if __name__ == '__main__':
    main()
