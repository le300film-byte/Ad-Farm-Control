"""Reply — value object returned by command handlers; the registry renders it with discord.py."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .ports import Embed


@dataclass
class Reply:
    content: str = ""
    embed: Optional[Embed] = None
    ephemeral: bool = True
    view: Any = None                 # discord.ui.View built by the registry (kept opaque here)
    modal: Any = None                # discord.ui.Modal to open instead of replying
    files: list[tuple[str, bytes]] = field(default_factory=list)
    followups: list["Reply"] = field(default_factory=list)

    @staticmethod
    def ok(text: str, **kw: Any) -> "Reply":
        return Reply(content=text, **kw)

    @staticmethod
    def error(text: str) -> "Reply":
        return Reply(content=text, ephemeral=True)

    @staticmethod
    def public(text: str = "", embed: Embed | None = None, **kw: Any) -> "Reply":
        return Reply(content=text, embed=embed, ephemeral=False, **kw)

    def as_dict(self) -> dict[str, Any]:
        return {"content": self.content, "embed": self.embed.to_dict() if self.embed else None, "ephemeral": self.ephemeral}
