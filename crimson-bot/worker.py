#!/usr/bin/env python3
"""
RQ Worker entry point for Crimsonej.
Run as separate process: python worker.py [queue_names...]
"""

import sys
import os

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services.queue import start_worker

if __name__ == "__main__":
    # Default queues if none specified
    queue_names = sys.argv[1:] if len(sys.argv) > 1 else [
        "default", "media", "research", "trading", "maintenance"
    ]
    print(f"Starting RQ worker for queues: {queue_names}")
    start_worker(queue_names)