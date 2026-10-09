# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""Database types shared by SQLite installations and PostgreSQL deployments."""

from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB as PostgreSQLJSONB

JSONB = JSON().with_variant(PostgreSQLJSONB(), "postgresql")

__all__ = ["JSONB"]
