from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_real_provider_rejects_blank_api_key() -> None:
    with pytest.raises(ValidationError, match="AGENT_API_KEY is required"):
        Settings(provider="qwen", api_key="")
