"""
FlightWatch -- an open, free, self-hosting fare tracker.

Main purpose: scan the same itineraries (seeded with the Christchurch <-> Colombo
corridor) once a day, build a per-itinerary price history, and surface a simple
buy / wait signal on a public dashboard -- all running for $0 on GitHub's free tier.

This package keeps every path relative to the repo root so the same code works
locally and inside GitHub Actions.
"""

import os

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(PACKAGE_DIR)

DATA_DIR = os.path.join(ROOT_DIR, "data")
DOCS_DIR = os.path.join(ROOT_DIR, "docs")
CONFIG_PATH = os.path.join(ROOT_DIR, "config.yaml")

__all__ = ["PACKAGE_DIR", "ROOT_DIR", "DATA_DIR", "DOCS_DIR", "CONFIG_PATH"]
