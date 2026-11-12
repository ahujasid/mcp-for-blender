"""Workflow guides the model loads on demand.

Craft knowledge (rigging, retopology, level design...) costs context on every
turn if it lives in server instructions or tool descriptions. Guides cost
nothing until the model asks for one with get_guide, and are also published as
guide:// resources for clients that let the user attach them.
"""

from dataclasses import dataclass
from functools import lru_cache
from importlib import resources


@dataclass(frozen=True)
class Guide:
    topic: str
    title: str
    summary: str
    body: str


def _parse(topic: str, text: str) -> Guide:
    meta = {}
    body = text
    if text.startswith("---\n"):
        header, _, body = text[4:].partition("\n---\n")
        for line in header.splitlines():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return Guide(topic, meta.get("title", topic), meta.get("summary", ""), body.lstrip())


@lru_cache(maxsize=1)
def all_guides() -> dict[str, Guide]:
    folder = resources.files("blender_mcp").joinpath("guides")
    guides = {}
    for entry in sorted(folder.iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".md"):
            topic = entry.name[:-3]
            guides[topic] = _parse(topic, entry.read_text(encoding="utf-8"))
    return guides


def index() -> str:
    return "\n".join(f"- {g.topic}: {g.summary}" for g in all_guides().values())


def get(topic: str) -> str:
    guides = all_guides()
    guide = guides.get((topic or "").strip().lower())
    if guide is None:
        return f"No guide called {topic!r}. Available guides:\n{index()}"
    return guide.body
