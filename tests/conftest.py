"""Pytest configuration and mongomock compatibility shims."""
import pytest
from datetime import datetime
import mongomock

# Patch $dateFromString in mongomock if not implemented
orig_handle_date = mongomock.aggregate._Parser._handle_date_operator

def patched_handle_date(self, operator, values):
    if operator == "$dateFromString":
        val = self.parse(values) if not isinstance(values, str) else values
        if isinstance(val, dict):
            date_str = val.get("dateString")
        else:
            date_str = val
        if isinstance(date_str, datetime):
            return date_str
        return datetime.fromisoformat(str(date_str))
    return orig_handle_date(self, operator, values)

mongomock.aggregate._Parser._handle_date_operator = patched_handle_date
