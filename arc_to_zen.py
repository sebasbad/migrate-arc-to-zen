"""Migrate Arc Spaces (pinned tabs + nested folders) into Zen workspaces. Reads Arc, never writes it. Zen must be closed."""
import argparse
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import lz4.block

ARC = Path.home() / "Library/Application Support/Arc/StorableSidebar.json"
ZEN_PROFILES = Path.home() / "Library/Application Support/zen/Profiles"
MAX_DEPTH = 5  # zen.folders.max-subfolders
MAGIC = b"mozLz40\0"
NOW = int(time.time() * 1000)


def read_mozlz4(p):
    b = p.read_bytes()
    assert b[:8] == MAGIC, f"{p} is not mozlz4"
    return json.loads(lz4.block.decompress(b[8:]))


def write_mozlz4(p, data):
    p.write_bytes(MAGIC + lz4.block.compress(json.dumps(data, separators=(",", ":")).encode()))


def sync_id():
    return f"{NOW}-{uuid.uuid4()}"


ARC_ICON_TO_ZEN = {"folder": "folder", "construction": "construct", "planet": "planet"}
ARC_PROFILE_TO_CTX = {"Profile 2": 2, "Profile 4": 1}


def arc_ctx(prof_obj):
    if not isinstance(prof_obj, dict) or "default" in prof_obj:
        return 0
    base = ((prof_obj.get("custom") or {}).get("_0") or {}).get("directoryBasename") or "custom"
    if base not in ARC_PROFILE_TO_CTX:
        used = set(ARC_PROFILE_TO_CTX.values())
        ARC_PROFILE_TO_CTX[base] = next(i for i in range(1, 100) if i not in used)
    return ARC_PROFILE_TO_CTX[base]


def space_icon(space):
    it = (space.get("customInfo") or {}).get("iconType") or {}
    if it.get("emoji_v2"):
        return it["emoji_v2"]
    name = ARC_ICON_TO_ZEN.get(it.get("icon"))
    return f"chrome://browser/skin/zen-icons/selectable/{name}.svg" if name else None


def space_theme(space, fallback):
    wt = (space.get("customInfo") or {}).get("windowTheme") or {}
    try:
        col = wt["background"]["single"]["_0"]["style"]["color"]["_0"]["blendedSingleColor"]["_0"]["color"]
    except (KeyError, TypeError):
        col = (wt.get("primaryColorPalette") or {}).get("midTone")
    if not col:
        return fallback
    rgb = [round(min(max(col[k], 0), 1) * 255) for k in ("red", "green", "blue")]
    dot = {"c": rgb, "isCustom": False, "algorithm": "", "isPrimary": True, "lightness": 50, "type": "undefined"}
    return {"type": "gradient", "gradientColors": [dot], "opacity": 0.5, "texture": 0}


class Builder:
    def __init__(self, items, pinned=True, ctx=0):
        self.items, self.tabs, self.groups, self.folders = items, [], [], []
        self.pinned = pinned  # Zen folders only exist among pinned tabs; unpinned walks flatten
        self.ctx = ctx

    def tab(self, url, title, ws, group=None, empty=False):
        t = {
            "entries": [{"url": url, "title": title, "triggeringPrincipal_base64": '{"3":{}}'}],
            "lastAccessed": NOW, "hidden": False, "pinned": self.pinned, "index": 1,
            "zenWorkspace": ws, "zenSyncId": sync_id(), "zenEssential": False,
            "zenDefaultUserContextId": "true", "zenIsEmpty": empty, "zenHasStaticIcon": False,
            "zenIsGlance": False, "zenGlanceId": None, "zenPinnedIcon": None,
            "zenLiveFolderItemId": None, "userContextId": self.ctx, "attributes": {}, "searchMode": None,
            "userTypedValue": "", "userTypedClear": 0, "image": None,
        }
        if group:
            t["groupId"] = group
        self.tabs.append(t)
        return t["zenSyncId"]

    def walk(self, ids, ws, group, depth):
        """Emit children of a container/folder in order; returns last emitted sibling info."""
        prev = {"type": "start", "id": None}
        for cid in ids:
            it = self.items.get(cid)
            if not it:
                continue
            data = it.get("data", {})
            if "tab" in data:
                url = data["tab"].get("savedURL")
                if url:
                    title = it.get("title") or data["tab"].get("savedTitle") or url
                    prev = {"type": "tab", "id": self.tab(url, title, ws, group)}
            elif "list" in data and depth < MAX_DEPTH and self.pinned:
                prev = {"type": "group", "id": self.folder(it, ws, group, depth + 1, prev)}
            else:  # splitView, unnamed groups, or folders past max depth: flatten
                p = self.walk(it.get("childrenIds", []), ws, group, depth)
                if p["type"] != "start":
                    prev = p
        return prev

    def folder(self, it, ws, parent, depth, prev):
        fid = f"{NOW}-{uuid.uuid4().int % 10**8}"
        name = it.get("title") or "Folder"
        self.groups.append({"pinned": True, "essential": False, "splitView": False, "id": fid,
                            "name": name, "color": "zen-workspace-color", "collapsed": True,
                            "saveOnWindowClose": True})
        empty = self.tab("about:blank", "", ws, fid, empty=True)
        self.folders.append({"pinned": True, "essential": False, "splitViewGroup": False, "id": fid,
                             "name": name, "collapsed": True, "saveOnWindowClose": True,
                             "parentId": parent, "prevSiblingInfo": prev if parent else {"type": "start", "id": None},
                             "emptyTabIds": [empty], "userIcon": None, "isLiveFolder": False,
                             "workspaceId": ws})
        self.walk(it.get("childrenIds", []), ws, fid, depth)
        return fid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arc", type=Path, default=ARC)
    ap.add_argument("--profile", type=Path, help="Zen profile dir (default: the one with zen-sessions.jsonlz4, newest)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--add-unpinned", action="store_true", help="append Arc's open (unpinned) tabs to existing Zen workspaces")
    ap.add_argument("--add-essentials", action="store_true", help="add Arc Favorites as Zen Essentials")
    ap.add_argument("--style-only", action="store_true", help="only copy Space icons/colors onto existing Zen workspaces")
    a = ap.parse_args()

    prof = a.profile or max((p.parent for p in ZEN_PROFILES.glob("*/zen-sessions.jsonlz4")),
                            key=lambda p: (p / "zen-sessions.jsonlz4").stat().st_mtime)
    sess_path = prof / "zen-sessions.jsonlz4"
    if not a.dry_run and subprocess.run(["pgrep", "-f", "Zen.app/Contents/MacOS/zen"], capture_output=True).returncode == 0:
        sys.exit("Zen is running; quit it first (it overwrites the session file on exit).")
    container = json.loads(a.arc.read_text())["sidebar"]["containers"][1]
    items = {i["id"]: i for i in container["items"] if isinstance(i, dict)}
    spaces = [s for s in container["spaces"] if isinstance(s, dict)]

    session = read_mozlz4(sess_path)
    theme = (session.get("spaces") or [{}])[0].get("theme")
    if a.style_only:
        arc_by_name = {(s.get("title") or "Space"): s for s in spaces}
        for zs in session["spaces"]:
            if zs["name"] in arc_by_name:
                s = arc_by_name[zs["name"]]
                zs["theme"] = space_theme(s, zs.get("theme"))
                if space_icon(s):
                    zs["icon"] = space_icon(s)
                print(f"{zs['name']:<20} icon={zs.get('icon') or '-'} color={zs['theme']['gradientColors'][0]['c'] if zs['theme'].get('gradientColors') else '-'}")
        if not a.dry_run:
            shutil.copy2(sess_path, sess_path.with_name(f"zen-sessions.pre-style-{time.strftime('%Y%m%d-%H%M%S')}.jsonlz4"))
            write_mozlz4(sess_path, session)
            print("written")
        return
    if a.add_essentials:
        e = Builder(items)
        ws_by_ctx = {}
        for zs in session["spaces"]:
            ws_by_ctx.setdefault(zs.get("containerTabId", 0), zs["uuid"])
        ws0 = session["spaces"][0]["uuid"]
        tops = container.get("topAppsContainerIDs", [])
        seen = {(t.get("userContextId", 0), t["entries"][-1]["url"])
                for t in session["tabs"] if t.get("zenEssential") and t.get("entries")}
        for prof_obj, cid in zip(tops[0::2], tops[1::2]):
            if not isinstance(cid, str):
                continue
            e.ctx = arc_ctx(prof_obj)
            ws_target = ws_by_ctx.get(e.ctx, ws0)
            before = len(e.tabs)
            e.walk(items.get(cid, {}).get("childrenIds", []), ws_target, None, 0)
            for t in e.tabs[before:]:
                t["zenEssential"] = True
        host = lambda u: urlparse(u).hostname
        have = {(ctx, host(u)) for ctx, u in seen}
        kept = []
        for t in e.tabs:
            u = t["entries"][0]["url"]
            ctx = t.get("userContextId", 0)
            if not t["zenIsEmpty"] and (ctx, host(u)) not in have and (ctx, u) not in seen:
                seen.add((ctx, u))
                have.add((ctx, host(u)))
                kept.append(t)
        e.tabs = kept
        for t in e.tabs:
            print(f"  [ctx={t['userContextId']}] {t['entries'][0]['title'][:36]:<36} {t['entries'][0]['url'][:60]}")
        print(f"total: {len(e.tabs)} essentials")
        if not a.dry_run:
            shutil.copy2(sess_path, sess_path.with_name(f"zen-sessions.pre-essentials-{time.strftime('%Y%m%d-%H%M%S')}.jsonlz4"))
            session["tabs"] = e.tabs + session["tabs"]
            write_mozlz4(sess_path, session)
            print("written")
        return
    if a.add_unpinned:
        u = Builder(items, pinned=False)
        ws_by_name = {zs["name"]: zs for zs in session["spaces"]}
        for s in spaces:
            name = s.get("title") or "Space"
            if name not in ws_by_name:
                continue
            zs = ws_by_name[name]
            u.ctx = zs.get("containerTabId", arc_ctx(s.get("profile")))
            before = len(u.tabs)
            ids = dict(zip(s["containerIDs"][0::2], s["containerIDs"][1::2]))
            u.walk(items.get(ids.get("unpinned"), {}).get("childrenIds", []), zs["uuid"], None, 0)
            print(f"{name:<20} (ctx={u.ctx}) {len(u.tabs) - before:>5} open tabs")
        print(f"total: {len(u.tabs)} open tabs")
        if not a.dry_run:
            shutil.copy2(sess_path, sess_path.with_name(f"zen-sessions.pre-unpinned-{time.strftime('%Y%m%d-%H%M%S')}.jsonlz4"))
            session["tabs"] = session["tabs"] + u.tabs
            write_mozlz4(sess_path, session)
            print("written")
        return
    b = Builder(items)
    new_spaces = []
    for s in spaces:
        ws = "{" + str(uuid.uuid4()) + "}"
        ctx = arc_ctx(s.get("profile"))
        b.ctx = ctx
        new_spaces.append({"uuid": ws, "name": s.get("title") or "Space", "theme": space_theme(s, theme),
                           "containerTabId": ctx, "hasCollapsedPinnedTabs": False})
        if space_icon(s):
            new_spaces[-1]["icon"] = space_icon(s)
        before = len(b.tabs)
        ids = dict(zip(s["containerIDs"][0::2], s["containerIDs"][1::2]))
        b.walk(items.get(ids.get("pinned"), {}).get("childrenIds", []), ws, None, 0)
        real = sum(1 for t in b.tabs[before:] if not t["zenIsEmpty"])
        print(f"{new_spaces[-1]['name']:<20} (ctx={ctx}) {real:>5} tabs")

    print(f"total: {len(new_spaces)} spaces, {sum(not t['zenIsEmpty'] for t in b.tabs)} tabs, {len(b.folders)} folders")
    if a.dry_run:
        return
    backup = sess_path.with_name(f"zen-sessions.pre-arc-{time.strftime('%Y%m%d-%H%M%S')}.jsonlz4")
    shutil.copy2(sess_path, backup)
    session.update(spaces=new_spaces, tabs=b.tabs, groups=b.groups, folders=b.folders,
                   splitViewData=[], lastCollected=NOW)
    write_mozlz4(sess_path, session)
    print(f"written {sess_path}\nbackup  {backup}")


if __name__ == "__main__":
    sys.exit(main())
