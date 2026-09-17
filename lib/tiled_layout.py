"""Rebuild a Dwindle split tree from saved tiled window rectangles.

The compositor does not expose the Dwindle tree over IPC. Its non-overlapping
rectangles do expose the recursive cuts. Recreate those cuts with preselect
and exact split ratios; windows remain tiled when restoration is finished.
"""
from collections import defaultdict
import json
import subprocess


def query(command):
    out = subprocess.run(["hyprctl", command, "-j"], capture_output=True,
                         text=True, timeout=10, check=True)
    return json.loads(out.stdout)


def option(name):
    out = subprocess.run(["hyprctl", "getoption", name, "-j"],
                         capture_output=True, text=True, timeout=10, check=True)
    return json.loads(out.stdout)


def bounds(items):
    return (min(i["rect"][0] for i in items), min(i["rect"][1] for i in items),
            max(i["rect"][2] for i in items), max(i["rect"][3] for i in items))


def split_tree(items):
    """Infer a guillotine partition; refuse overlapping/non-BSP geometry."""
    if len(items) == 1:
        return {"leaf": items[0]["address"]}
    region = bounds(items)
    candidates = []
    for axis in (0, 1):
        ordered = sorted(items, key=lambda item: item["rect"][axis])
        for n in range(1, len(ordered)):
            left, right = ordered[:n], ordered[n:]
            edge1 = max(i["rect"][axis + 2] for i in left)
            edge2 = min(i["rect"][axis] for i in right)
            if edge1 > edge2 + 3:
                continue
            cut = (edge1 + edge2) / 2
            ratio = 2 * (cut - region[axis]) / (region[axis + 2] - region[axis])
            if 0.1 <= ratio <= 1.9:
                candidates.append((abs(len(left) - len(right)), axis, ratio, left, right))
    for _, axis, ratio, left, right in sorted(candidates, key=lambda c: c[0]):
        try:
            return {"axis": axis, "ratio": ratio,
                    "left": split_tree(left), "right": split_tree(right)}
        except ValueError:
            continue
    raise ValueError("saved rectangles do not form a Dwindle split tree")


def first_leaf(node):
    return node["leaf"] if "leaf" in node else first_leaf(node["left"])


def tree_commands(tree, encode):
    """The first leaf is already tiled; materialize its remaining splits."""
    if "leaf" in tree:
        return []
    left, right = first_leaf(tree["left"]), first_leaf(tree["right"])
    lines = [
        f'hl.dispatch(hl.dsp.focus({{window={encode("address:" + left)}}}))',
        f'hl.dispatch(hl.dsp.layout("preselect {"r" if tree["axis"] == 0 else "d"}"))',
        f'hl.dispatch(hl.dsp.window.float({{window={encode("address:" + right)},action="off"}}))',
        f'hl.dispatch(hl.dsp.focus({{window={encode("address:" + right)}}}))',
        f'hl.dispatch(hl.dsp.layout("splitratio {tree["ratio"]:.9f} exact"))',
    ]
    return lines + tree_commands(tree["left"], encode) + tree_commands(tree["right"], encode)


def restore_tiled(specs, hypr, encode):
    """Restore only complete workspaces with no unrelated tiled windows."""
    groups = defaultdict(list)
    for spec in specs:
        if not spec.get("floating") and not spec.get("fullscreen"):
            groups[str(spec.get("workspace", "1"))].append(spec)
    live = query("clients")
    workspaces = {str(w["id"]): w for w in query("workspaces")}
    monitors = {m["name"] for m in query("monitors")}
    focused = query("activewindow").get("address")
    cursor = query("cursorpos")
    gaps = [float(x) for x in option("general:gaps_in")["css"].split()]
    border = option("general:border_size")["int"]
    if len(set(gaps)) != 1:
        print("[layout] asymmetric gaps: tiled reconstruction skipped")
        return 0
    pad = gaps[0] + border
    if not option("dwindle:preserve_split").get("bool"):
        print("[layout] preserve_split is disabled: tiled reconstruction skipped")
        return 0
    if not option("dwindle:use_active_for_splits").get("bool"):
        print("[layout] use_active_for_splits is disabled: tiled reconstruction skipped")
        return 0
    restored = 0
    for ws, windows in groups.items():
        if workspaces.get(ws, {}).get("tiledLayout") != "dwindle":
            print(f"[layout] ws{ws}: not a Dwindle workspace, skipped")
            continue
        addresses = {s.get("_restored_address") for s in windows}
        actual = {w["address"] for w in live if str(w["workspace"]["id"]) == ws
                  and w.get("mapped") and not w.get("floating")}
        if None in addresses or len(addresses) != len(windows) or actual != addresses:
            print(f"[layout] ws{ws}: missing or extra tiled windows, left unchanged")
            continue
        try:
            items = []
            for s in windows:
                x, y = s["position"]
                width, height = s["size"]
                if width <= 0 or height <= 0:
                    raise ValueError("invalid saved size")
                items.append({"address": s["_restored_address"],
                              "rect": (x-pad, y-pad, x+width+pad, y+height+pad)})
            tree = split_tree(items)
        except (KeyError, TypeError, ValueError) as exc:
            print(f"[layout] ws{ws}: {exc}; left unchanged")
            continue
        lines = []
        monitor = windows[0].get("monitor")
        if monitor in monitors and workspaces[ws].get("monitor") != monitor:
            lines.append(f'hl.dispatch(hl.dsp.workspace.move({{workspace={encode(int(ws))},monitor={encode(monitor)}}}))')
        for item in items:
            lines.append(f'hl.dispatch(hl.dsp.window.float({{window={encode("address:" + item["address"])},action="on"}}))')
        first = first_leaf(tree)
        lines.append(f'hl.dispatch(hl.dsp.window.float({{window={encode("address:" + first)},action="off"}}))')
        lines += tree_commands(tree, encode)
        lines.append('hl.dispatch(hl.dsp.layout("preselect none"))')
        if focused:
            lines.append(f'hl.dispatch(hl.dsp.focus({{window={encode("address:" + focused)}}}))')
        lines.append(f'hl.dispatch(hl.dsp.cursor.move({{x={cursor["x"]},y={cursor["y"]}}}))')
        result = hypr("; ".join(lines))
        if "error" in result.lower():
            print(f"[layout] ws{ws}: compositor reported {result}")
            continue
        print(f"[layout] ws{ws}: rebuilt {len(windows)} tiled windows")
        restored += len(windows)
    return restored
