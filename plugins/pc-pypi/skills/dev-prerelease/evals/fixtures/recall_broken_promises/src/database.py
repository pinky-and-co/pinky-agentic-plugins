from __future__ import annotations

from typing import Any, ClassVar

from pinky_provider.resources.base import BaseResource
from snowflake.core.database import Database as _SDKDatabase


class Database(BaseResource):
    """Account-scoped database — fields introspected from ``snowflake.core.database.Database``.

    Deployed via Core API ``create_or_alter`` (ADR-0005). No SDK warts curated yet — the model is
    the raw SDK surface; curation is added one line at a time as a real apply reveals one (ADR-0015).
    """

    _sdk_class: ClassVar[type] = _SDKDatabase

    def collection(self, root: Any) -> Any:
        """Account-level database collection."""
        return root.databases
