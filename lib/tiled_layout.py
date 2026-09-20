"""Reconstruct Dwindle splits from saved rectangles, then verify the result.

Adapted from the tiled-layout work in PR #1. Geometry is expanded by the
uniform border/inner-gap padding so adjacent tiles share a split boundary.
No compositor changes are made until the whole workspace has been checked.
"""
from collections import defaultdict
from dataclasses import dataclass
import json
import math
import subprocess
import time


TOLERANCE = 3
SUCCESS = "omasession-layout-ok"
IPC_ERRORS = (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError)


@dataclass
class LayoutResult:
    restored: int = 0
    skipped: int = 0
    failed: int = 0


def query(command):
    result = subprocess.run(["hyprctl", command, "-j"], capture_output=True,
                            text=True, timeout=10, check=True)
    return json.loads(result.stdout)


def option(name):
    result = subprocess.run(["hyprctl", "getoption", name, "-j"],
                            capture_output=True, text=True, timeout=10, check=True)
    return json.loads(result.stdout)


def bounds(items):
    return (min(i["rect"][0] for i in items), min(i["rect"][1] for i in items),
            max(i["rect"][2] for i in items), max(i["rect"][3] for i in items))


def split_tree(items):
    """Infer a complete rectangular partition, allowing pixel rounding only."""
    if not 1 <= len(items) <= 64:
        raise ValueError("expected 1–64 tiled windows")
    for item in items:
        x1, y1, x2, y2 = item["rect"]
        if not all(math.isfinite(v) for v in (x1, y1, x2, y2)) or x2 <= x1 or y2 <= y1:
            raise ValueError("invalid saved rectangle")
    if len(items) == 1:
        return {"leaf": items[0]["address"]}
    region = bounds(items)
    candidates = []
    for axis in (0, 1):
        other = 1 - axis
        ordered = sorted(items, key=lambda item: item["rect"][axis])
        for n in range(1, len(ordered)):
            left, right = ordered[:n], ordered[n:]
            a, b = bounds(left), bounds(right)
            # Both children must span the parent's other dimension. Merely
            # finding a gap would accept diagonal tiles and missing corners.
            if any(abs(child[k] - region[k]) > TOLERANCE
                   for child in (a, b) for k in (other, other + 2)):
                continue
            if abs(a[axis + 2] - b[axis]) > TOLERANCE:
                continue
            cut = (a[axis + 2] + b[axis]) / 2
            ratio = 2 * (cut - region[axis]) / (region[axis + 2] - region[axis])
            if 0.1 <= ratio <= 1.9:
                candidates.append((abs(len(left) - len(right)), axis, ratio, left, right))
    for _, axis, ratio, left, right in sorted(candidates, key=lambda c: c[0]):
        try:
            return {"axis": axis, "ratio": ratio,
                    "left": split_tree(left), "right": split_tree(right)}
        except ValueError:
            continue
    raise ValueError("saved rectangles do not form a complete Dwindle split tree")


def first_leaf(node):
    return node["leaf"] if "leaf" in node else first_leaf(node["left"])


def tree_commands(tree, encode):
    """The first leaf is tiled; materialize its remaining splits."""
    if "leaf" in tree:
        return []
    left, right = first_leaf(tree["left"]), first_leaf(tree["right"])
    commands = [
        f'hl.dsp.focus({{window={encode("address:" + left)}}})',
        f'hl.dsp.layout("preselect {"r" if tree["axis"] == 0 else "d"}")',
        f'hl.dsp.window.float({{window={encode("address:" + right)},action="off"}})',
        f'hl.dsp.focus({{window={encode("address:" + right)}}})',
        f'hl.dsp.layout("splitratio {tree["ratio"]:.9f} exact")',
    ]
    return commands + tree_commands(tree["left"], encode) + tree_commands(tree["right"], encode)


def checked_script(commands, cleanup=()):
    """Check every dispatch and attempt every cleanup even after a failure."""
    body = "; ".join(f"run({command})" for command in commands)
    final = "; ".join(
        f"do local good, why = pcall(function() run({command}) end); "
        "if not good then ok = false; err = tostring(err or '') .. '; ' .. tostring(why) end end"
        for command in cleanup)
    return (
        "local function run(d) local r = hl.dispatch(d); "
        "if not r or not r.ok then error(r and r.error or 'dispatcher failed') end end; "
        f"local ok, err = pcall(function() {body} end); {final}; "
        f"if ok then return '{SUCCESS}' else return 'omasession-layout-error: ' .. tostring(err) end"
    )


def projected_rects(tree, region):
    """Expected tile rectangles, scaled to the monitor's current usable area."""
    if "leaf" in tree:
        return {tree["leaf"]: region}
    axis = tree["axis"]
    cut = region[axis] + (region[axis + 2] - region[axis]) * tree["ratio"] / 2
    left, right = list(region), list(region)
    left[axis + 2], right[axis] = cut, cut
    return {**projected_rects(tree["left"], left), **projected_rects(tree["right"], right)}


def rectangle(position, size, pad):
    x, y = position
    width, height = size
    if not all(math.isfinite(v) for v in (x, y, width, height)) or width <= 0 or height <= 0:
        raise ValueError("invalid window geometry")
    return (x - pad, y - pad, x + width + pad, y + height + pad)


def verify(tree, ws, pad):
    current = [w for w in query("clients") if w.get("mapped") and
               str(w["workspace"]["id"]) == str(ws) and not w.get("floating")]
    expected_addresses = set(projected_rects(tree, (0, 0, 1, 1)))
    if {w["address"] for w in current} != expected_addresses:
        return False
    if any(w.get("fullscreen") or w.get("hidden") or w.get("grouped") for w in current):
        return False
    items = [{"address": w["address"], "rect": rectangle(w["at"], w["size"], pad)} for w in current]
    expected = projected_rects(tree, bounds(items))
    return all(abs(a - b) <= TOLERANCE for item in items
               for a, b in zip(item["rect"], expected[item["address"]]))


def restore_tiled(specs, hypr, encode):
    """Rebuild complete workspaces; report skips separately from failed writes."""
    result = LayoutResult()
    groups = defaultdict(list)
    for spec in specs:
        if not spec.get("floating"):
            groups[str(spec.get("workspace", "1"))].append(spec)
    groups = {ws: windows for ws, windows in groups.items() if len(windows) > 1}
    # Incomplete/app-only restores must not reconstruct a subset of a desk.
    for ws, windows in list(groups.items()):
        addresses = {w.get("_restored_address") for w in windows}
        if None in addresses or len(addresses) != len(windows) or any(w.get("fullscreen") for w in windows):
            print(f"[layout] ws{ws}: incomplete or fullscreen snapshot, skipped")
            result.skipped += 1
            del groups[ws]
    if not groups:
        return result
    try:
        gaps = [float(x) for x in option("general:gaps_in")["css"].split()]
        if not gaps or len(set(gaps)) != 1 or not math.isfinite(gaps[0]) or gaps[0] < 0:
            raise ValueError("inner gaps must be uniform and nonnegative")
        pad = gaps[0] + option("general:border_size")["int"]
        for name in ("dwindle:preserve_split", "dwindle:use_active_for_splits"):
            if not option(name).get("bool"):
                raise ValueError(f"{name} must be enabled")
        workspaces = {str(w["id"]): w for w in query("workspaces")}
        monitors = {m["name"] for m in query("monitors")}
        focused = query("activewindow").get("address")
        active_ws = query("activeworkspace").get("id")
        cursor = query("cursorpos")
    except IPC_ERRORS as exc:
        print(f"[layout] unavailable: {exc}; tiled reconstruction skipped")
        result.skipped += len(groups)
        return result

    for ws, windows in groups.items():
        try:
            workspace = workspaces.get(ws, {})
            if workspace.get("tiledLayout") != "dwindle":
                raise ValueError("not a Dwindle workspace")
            live = [w for w in query("clients") if w.get("mapped") and str(w["workspace"]["id"]) == ws]
            tiled = [w for w in live if not w.get("floating")]
            addresses = {w["_restored_address"] for w in windows}
            if {w["address"] for w in tiled} != addresses:
                raise ValueError("missing or extra tiled windows")
            if any(w.get("fullscreen") or w.get("hidden") or w.get("grouped") or w.get("pinned") for w in tiled):
                raise ValueError("fullscreen, hidden, grouped or pinned windows")
            items = [{"address": w["_restored_address"],
                      "rect": rectangle(w["position"], w["size"], pad)} for w in windows]
            tree = split_tree(items)
            monitor = windows[0].get("monitor")
            move_monitor = monitor in monitors and workspace.get("monitor") != monitor
            if move_monitor and {w["address"] for w in live} != addresses:
                raise ValueError("moving this workspace would move unrelated floating windows")
        except IPC_ERRORS as exc:
            print(f"[layout] ws{ws}: {exc}; left unchanged")
            result.skipped += 1
            continue

        # Commands execute in one Lua call. pcall guarantees cleanup after a
        # rejected dispatch; a second cleanup call also handles IPC timeouts.
        commands = []
        if move_monitor:
            commands.append(f'hl.dsp.workspace.move({{workspace={encode(int(ws))},monitor={encode(monitor)}}})')
        commands += [f'hl.dsp.window.float({{window={encode("address:" + item["address"])},action="on"}})' for item in items]
        commands.append(f'hl.dsp.window.float({{window={encode("address:" + first_leaf(tree))},action="off"}})')
        commands += tree_commands(tree, encode)
        cleanup = ['hl.dsp.layout("preselect none")']
        cleanup += [f'hl.dsp.window.float({{window={encode("address:" + item["address"])},action="off"}})' for item in items]
        if focused:
            cleanup.append(f'hl.dsp.focus({{window={encode("address:" + focused)}}})')
        elif active_ws is not None:
            cleanup.append(f'hl.dsp.focus({{workspace={encode(active_ws)}}})')
        cleanup.append(f'hl.dsp.cursor.move({{x={encode(cursor["x"])},y={encode(cursor["y"])}}})')
        try:
            response = hypr(checked_script(commands, cleanup))
            if response.strip() != SUCCESS:
                raise ValueError(f"compositor did not confirm reconstruction: {response}")
            deadline = time.monotonic() + 1.5
            while not verify(tree, ws, pad):
                if time.monotonic() >= deadline:
                    raise ValueError("resulting geometry did not match the saved split tree")
                time.sleep(0.1)
            if move_monitor and not any(str(w["id"]) == ws and w.get("monitor") == monitor for w in query("workspaces")):
                raise ValueError("workspace did not return to its saved monitor")
        except IPC_ERRORS as exc:
            print(f"[layout] ws{ws}: failed: {exc}")
            try:
                response = hypr(checked_script([], cleanup))
                if response.strip() != SUCCESS:
                    print(f"[layout] ws{ws}: cleanup could not be confirmed: {response}")
            except IPC_ERRORS as cleanup_exc:
                print(f"[layout] ws{ws}: cleanup failed: {cleanup_exc}")
            result.failed += 1
            continue
        print(f"[layout] ws{ws}: verified {len(windows)} tiled windows")
        result.restored += len(windows)
    return result
