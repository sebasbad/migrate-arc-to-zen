"""Export Arc Spaces from StorableSidebar.json to one Netscape bookmarks HTML file per Space."""
import argparse
import html
import json
import re
from pathlib import Path

DEFAULT_SRC = Path.home() / "Library/Application Support/Arc/StorableSidebar.json"


def pairs(seq):
    """Arc stores maps as flat [key, value, key, value, ...] lists."""
    return zip(seq[0::2], seq[1::2])


def render(item_id, items, depth):
    item = items.get(item_id)
    if not item:
        return []
    pad = "    " * depth
    data = item.get("data", {})
    if "tab" in data:
        tab = data["tab"]
        url = tab.get("savedURL")
        if not url:
            return []
        title = item.get("title") or tab.get("savedTitle") or url
        return [f'{pad}<DT><A HREF="{html.escape(url)}">{html.escape(title)}</A>']
    children = [l for c in item.get("childrenIds", []) for l in render(c, items, depth + 1)]
    if "list" in data and item.get("title"):
        return [f"{pad}<DT><H3>{html.escape(item['title'])}</H3>", f"{pad}<DL><p>", *children, f"{pad}</DL><p>"]
    # splitView / unnamed groups: flatten into parent
    return [l[4:] for l in children]


def folder(name, child_ids, items):
    body = [l for c in child_ids for l in render(c, items, 2)]
    if not body:
        return []
    return [f"    <DT><H3>{html.escape(name)}</H3>", "    <DL><p>", *body, "    </DL><p>"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--out", type=Path, default=Path("arc-export"))
    ap.add_argument("--pinned-only", action="store_true", help="skip unpinned (Today) tabs")
    args = ap.parse_args()

    container = json.loads(args.src.read_text())["sidebar"]["containers"][1]
    items = {i["id"]: i for i in container["items"] if isinstance(i, dict)}
    spaces = [s for s in container["spaces"] if isinstance(s, dict)]
    args.out.mkdir(parents=True, exist_ok=True)

    for n, space in enumerate(spaces, 1):
        title = space.get("title") or f"Space {n}"
        sections = []
        for kind, cid in pairs(space.get("containerIDs", [])):
            if kind == "unpinned" and args.pinned_only:
                continue
            kids = items.get(cid, {}).get("childrenIds", [])
            sections += folder("Pinned" if kind == "pinned" else "Unpinned", kids, items)
        doc = [
            "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
            '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
            f"<TITLE>{html.escape(title)}</TITLE>",
            "<DL><p>",
            f"<DT><H3>Arc - {html.escape(title)}</H3>",
            "<DL><p>",
            *sections,
            "</DL><p>",
            "</DL><p>",
        ]
        safe = re.sub(r"[^\w.-]+", "_", title).strip("_") or f"space_{n}"
        path = args.out / f"{n:02d}_{safe}.html"
        path.write_text("\n".join(doc) + "\n")
        links = sum("<A HREF" in l for l in sections)
        print(f"{path}  ({links} tabs)")


if __name__ == "__main__":
    main()
