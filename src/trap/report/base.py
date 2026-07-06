from __future__ import annotations

from trap.models import ReportData


class BaseRenderer:
    def render(self, data: ReportData) -> None:
        raise NotImplementedError
