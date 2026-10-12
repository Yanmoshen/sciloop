# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""SQLite JSON type; JSONB remains a compatibility name for existing model imports."""

from sqlalchemy import JSON

JSONB = JSON()

__all__ = ["JSONB"]
