"""Arrange a flat IMAP folder list into the tree the folder pane shows."""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import SPECIAL_ORDER, Folder


@dataclass
class FolderNode:
    name: str
    folder: Folder | None              # None for a container that can't be opened
    children: list["FolderNode"] = field(default_factory=list)
    path_key: str = ""                 # stable id, for keeping expansion across refreshes


def build(folders: list[Folder]) -> list[FolderNode]:
    """Special folders (Inbox, Sent, ...) come first at the top level; Gmail's "[Gmail]"
    container is dropped so its folders sit at the top too; the rest nest by delimiter."""
    specials: dict[str, Folder] = {}
    rest: list[Folder] = []
    for f in folders:
        if f.special and f.special not in specials and f.selectable:
            specials[f.special] = f
        else:
            rest.append(f)

    roots: list[FolderNode] = []
    for kind in SPECIAL_ORDER:
        if kind in specials:
            f = specials[kind]
            roots.append(FolderNode(f.display_name, f, path_key=f.path))

    by_path: dict[str, FolderNode] = {}
    top: list[FolderNode] = []
    for f in sorted(rest, key=lambda f: f.path.lower()):
        if f.path.upper() == "[GMAIL]" or (not f.selectable and f.path.startswith("[")):
            continue
        parts = f.path.split(f.delimiter) if f.delimiter else [f.path]
        if parts[0].upper() == "[GMAIL]":
            parts = parts[1:]
        if parts and parts[0].upper() == "INBOX" and len(parts) > 1:
            parts = parts[1:]          # INBOX.Work on Dovecot -> Work
        siblings, prefix = top, ""
        for i, part in enumerate(parts):
            prefix = f"{prefix}/{part}"
            node = by_path.get(prefix)
            if node is None:
                is_leaf = i == len(parts) - 1
                node = FolderNode(f.name if is_leaf else part,
                                  f if is_leaf and f.selectable else None, path_key=prefix)
                by_path[prefix] = node
                siblings.append(node)
            elif i == len(parts) - 1 and f.selectable:
                node.folder = f
            siblings = node.children
    _sort(top)
    return roots + top


def _sort(nodes: list[FolderNode]) -> None:
    nodes.sort(key=lambda n: n.name.lower())
    for n in nodes:
        _sort(n.children)


def find(nodes: list[FolderNode], special: str) -> FolderNode | None:
    for n in nodes:
        if n.folder and n.folder.special == special:
            return n
        hit = find(n.children, special)
        if hit:
            return hit
    return None
