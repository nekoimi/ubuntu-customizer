#!/usr/bin/env python3
"""Compatibility command-line entry; implementation lives in image_builder."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from image_builder.pipeline import main

if __name__ == '__main__':
    sys.exit(main())
