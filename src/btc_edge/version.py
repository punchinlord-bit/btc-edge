"""Single source of truth for the build version stamp.

The dashboard + rocket widgets read ``BUILD_VERSION`` from the telemetry payload
and compare it against the hardcoded string in the HTML to detect stale caches.
Bump this on every deploy so the UI can surface ``STALE CACHE`` when a browser is
serving an old HTML file.
"""

from __future__ import annotations

BUILD_VERSION = "v2.7.0-ops"
BUILD_TIMESTAMP = "2026-11-03T18:00:00Z"
