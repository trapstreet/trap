from __future__ import annotations

import sys

from trap.models import ReportData
from trap.report.base import BaseRenderer


class JsonRenderer(BaseRenderer):
    def render(self, data: ReportData) -> None:
        sys.stdout.write(data.model_dump_json(indent=2) + "\n")
