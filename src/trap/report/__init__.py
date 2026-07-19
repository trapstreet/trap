from __future__ import annotations

import enum

from trap.report.base import BaseRenderer
from trap.report.json import JsonRenderer
from trap.report.rich import RichRenderer

__all__ = ["BaseRenderer", "JsonRenderer", "OutputFormat", "RichRenderer", "renderer_factory"]


class OutputFormat(enum.StrEnum):
    rich = "rich"
    json = "json"


def renderer_factory(fmt: OutputFormat) -> BaseRenderer:
    return {OutputFormat.rich: RichRenderer, OutputFormat.json: JsonRenderer}[fmt]()
