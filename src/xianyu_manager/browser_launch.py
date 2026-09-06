"""Explicit Linux sandbox policy; never retry without the Chromium sandbox."""
import sys


def sandbox_options():
    return {"chromium_sandbox": True} if sys.platform.startswith("linux") else {}
