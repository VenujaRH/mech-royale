"""Beams & Bytes - Mech Royale bot (mechfight/1).

Survival-first: stay in the zone, loot early (weapons, cubes, crates, drones, supply drops),
win one-on-one fights by facing the enemy, strafing, leading shots and only firing with a clear
line of fire, and back off to regenerate when losing. Pure Python standard library.
"""
import array
import heapq
import json
import math
import os
import queue
import random
import sys
import threading
import time
import traceback
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
TAU = 2 * math.pi


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def d2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


# ---------------------------------------------------------------- world model
def _pip(x, y, ring):
    inside = False
    n = len(ring) // 2
    j = n - 1
    for i in range(n):
        xi, yi = ring[2 * i], ring[2 * i + 1]
        xj, yj = ring[2 * j], ring[2 * j + 1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def build_nav(map_dir):
    """Rasterize the arena (v2/world.json + terrain.i16) into a walk grid (4 m) and a solid grid (2 m)."""
    w = json.load(open(os.path.join(map_dir, "v2", "world.json")))
    T = w["terrain"]
    nx, ny, tminx, tminy, step = T["nx"], T["ny"], T["minX"], T["minY"], T["step"]
    ter = array.array("h")
    with open(os.path.join(map_dir, "v2", "terrain.i16"), "rb") as f:
        ter.frombytes(f.read())
    if sys.byteorder != "little":
        ter.byteswap()

    def ground(x, y):
        fx = (x - tminx) / step; fy = (y - tminy) / step
        i = max(0, min(nx - 2, int(math.floor(fx)))); j = max(0, min(ny - 2, int(math.floor(fy))))
        ax = min(1.0, max(0.0, fx - i)); ay = min(1.0, max(0.0, fy - j))
        a = ter[j * nx + i]; b = ter[j * nx + i + 1]; c = ter[(j + 1) * nx + i]; d = ter[(j + 1) * nx + i + 1]
        return ((a * (1 - ax) + b * ax) * (1 - ay) + (c * (1 - ax) + d * ax) * ay) * 0.01

    B = w["bounds"]
    minx, miny, maxx, maxy = B["minX"], B["minY"], B["maxX"], B["maxY"]

    def raster(cell):
        gx = int(math.ceil((maxx - minx) / cell)); gy = int(math.ceil((maxy - miny) / cell))
        base, top = {}, {}
        for p in w["prisms"]:
            r = p["ring"]; xs = r[0::2]; ys = r[1::2]
            holes = [h for h in (p.get("holes") or []) if h]
            i0 = max(0, int((min(xs) - minx) / cell)); i1 = min(gx - 1, int((max(xs) - minx) / cell))
            j0 = max(0, int((min(ys) - miny) / cell)); j1 = min(gy - 1, int((max(ys) - miny) / cell))
            for j in range(j0, j1 + 1):
                cy = miny + (j + 0.5) * cell
                for i in range(i0, i1 + 1):
                    cx = minx + (i + 0.5) * cell
                    if _pip(cx, cy, r) and not any(_pip(cx, cy, h) for h in holes):
                        k = j * gx + i
                        if k in base:
                            base[k] = min(base[k], p["base"]); top[k] = max(top[k], p["top"])
                        else:
                            base[k] = p["base"]; top[k] = p["top"]
        return gx, gy, base, top

    WC = 4.0
    wgx, wgy, wb, wt = raster(WC)
    wground = []
    wblock = bytearray(wgx * wgy)
    for j in range(wgy):
        cy = miny + (j + 0.5) * WC
        for i in range(wgx):
            cx = minx + (i + 0.5) * WC
            g = ground(cx, cy)
            wground.append(int(round(g * 100)))
            k = j * wgx + i
            if (k in wb and wb[k] < g + 9.5 and wt[k] > g + 1.2) or i < 2 or j < 2 or i >= wgx - 2 or j >= wgy - 2:
                wblock[k] = 1
    LC = 2.0
    lgx, lgy, lb, lt = raster(LC)
    roofs = []
    for p in w["prisms"]:
        if p.get("kind") != "building":
            continue
        r = p["ring"]; n = len(r) // 2
        if n < 3:
            continue
        area = 0.0
        for i in range(n):
            j = (i + 1) % n
            area += r[2 * i] * r[2 * j + 1] - r[2 * j] * r[2 * i + 1]
        area = abs(area) / 2
        if area < 180:
            continue
        cx = sum(r[0::2]) / n; cy = sum(r[1::2]) / n
        if not _pip(cx, cy, r) or any(_pip(cx, cy, h) for h in (p.get("holes") or []) if h):
            continue
        g = ground(cx, cy)
        hgt = p["top"] - g
        if 6 <= hgt <= 42 and p["base"] <= g + 2.5:
            roofs.append((cx, cy, p["top"], math.sqrt(area / math.pi), r, g))
    return {
        "roofs": roofs,
        "minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy,
        "walk": {"cell": WC, "gx": wgx, "gy": wgy, "ground": wground, "block": wblock},
        "los": {"cell": LC, "gx": lgx, "gy": lgy, "cells": {k: (lb[k], lt[k]) for k in lb}, "raw": True},
        "terrain": {"nx": nx, "ny": ny, "minx": tminx, "miny": tminy, "step": step},
    }


class World:
    def __init__(self, map_dir):
        nav = None
        for d in (map_dir, "/arena", os.path.join(HERE, "arena")):
            try:
                nav = build_nav(d)
                map_dir = d
                break
            except Exception:
                log(f"map build from {d} failed:\n{traceback.format_exc()}")
        if nav is None:
            nav = json.loads(zlib.decompress(open(os.path.join(HERE, "navmap.bin"), "rb").read()))
        self.minx, self.miny = nav["minx"], nav["miny"]
        self.maxx, self.maxy = nav["maxx"], nav["maxy"]
        self.roofs = nav.get("roofs", [])
        wk = nav["walk"]
        self.wc, self.wgx, self.wgy = wk["cell"], wk["gx"], wk["gy"]
        self.wground = wk["ground"]
        self.wblock = bytearray(wk["block"])
        lo = nav["los"]
        self.lc, self.lgx, self.lgy = lo["cell"], lo["gx"], lo["gy"]
        if lo.get("raw"):
            self.lcells = lo["cells"]
        else:
            self.lcells = {int(k): (v[0] * 0.1, v[1] * 0.1) for k, v in lo["cells"].items()}
        tr = nav["terrain"]
        self.tnx, self.tny, self.tminx, self.tminy, self.tstep = tr["nx"], tr["ny"], tr["minx"], tr["miny"], tr["step"]
        self.ter = None
        for d in (map_dir, os.path.join(HERE, "arena")):
            try:
                p = os.path.join(d, "v2", "terrain.i16")
                t = array.array("h")
                with open(p, "rb") as f:
                    t.frombytes(f.read())
                if sys.byteorder != "little":
                    t.byteswap()
                if len(t) == self.tnx * self.tny:
                    self.ter = t
                    break
            except Exception:
                pass

    # ground height
    def ground(self, x, y):
        if self.ter is None:
            i, j = self.wcell(x, y)
            return self.wground[j * self.wgx + i] * 0.01
        fx = (x - self.tminx) / self.tstep
        fy = (y - self.tminy) / self.tstep
        i = int(fx); j = int(fy)
        if i < 0: i = 0
        if j < 0: j = 0
        if i > self.tnx - 2: i = self.tnx - 2
        if j > self.tny - 2: j = self.tny - 2
        ax = fx - i; ay = fy - j
        ax = 0.0 if ax < 0 else (1.0 if ax > 1 else ax)
        ay = 0.0 if ay < 0 else (1.0 if ay > 1 else ay)
        t = self.ter; n = self.tnx; k = j * n + i
        return ((t[k] * (1 - ax) + t[k + 1] * ax) * (1 - ay) + (t[k + n] * (1 - ax) + t[k + n + 1] * ax) * ay) * 0.01

    def solid(self, x, y, z):
        i = int((x - self.minx) / self.lc); j = int((y - self.miny) / self.lc)
        if 0 <= i < self.lgx and 0 <= j < self.lgy:
            c = self.lcells.get(j * self.lgx + i)
            if c is not None and c[0] - 0.2 <= z <= c[1]:
                return True
        return z < self.ground(x, y) - 0.4

    def los(self, a, b, skip_start=3.2, skip_end=2.0, step=1.6):
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        L = math.sqrt(dx * dx + dy * dy + dz * dz)
        if L < 1e-6:
            return True
        n = int((L - skip_start - skip_end) / step)
        for s in range(n + 1):
            f = (skip_start + s * step) / L
            if f >= 1:
                break
            if self.solid(a[0] + dx * f, a[1] + dy * f, a[2] + dz * f):
                return False
        return True

    # walk grid
    def wcell(self, x, y):
        i = int((x - self.minx) / self.wc); j = int((y - self.miny) / self.wc)
        return min(max(i, 0), self.wgx - 1), min(max(j, 0), self.wgy - 1)

    def wcenter(self, i, j):
        return (self.minx + (i + 0.5) * self.wc, self.miny + (j + 0.5) * self.wc)

    def blocked(self, x, y):
        i, j = self.wcell(x, y)
        return self.wblock[j * self.wgx + i] == 1

    def walk_clear(self, a, b, step=2.0):
        L = d2(a, b)
        n = int(L / step)
        for s in range(1, n + 1):
            f = s * step / L
            if self.blocked(a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f):
                return False
        return True

    def nearest_free(self, x, y, rad=6):
        i0, j0 = self.wcell(x, y)
        if not self.wblock[j0 * self.wgx + i0]:
            return i0, j0
        best = None
        for r in range(1, rad + 1):
            for dj in range(-r, r + 1):
                for di in range(-r, r + 1):
                    if max(abs(di), abs(dj)) != r:
                        continue
                    i, j = i0 + di, j0 + dj
                    if 0 <= i < self.wgx and 0 <= j < self.wgy and not self.wblock[j * self.wgx + i]:
                        dd = di * di + dj * dj
                        if best is None or dd < best[0]:
                            best = (dd, i, j)
            if best:
                return best[1], best[2]
        return i0, j0

    def astar(self, start, goal, limit=2800):
        gx = self.wgx
        si, sj = self.nearest_free(*start)
        ti, tj = self.nearest_free(*goal)
        s = sj * gx + si; g = tj * gx + ti
        if s == g:
            return [goal]
        blk = self.wblock; gr = self.wground
        openh = [(0.0, s)]
        cost = {s: 0.0}
        came = {s: -1}
        best = s; bestd = 1e18
        nbr = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0), (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414))
        exp = 0
        while openh and exp < limit:
            _, cur = heapq.heappop(openh)
            if cur == g:
                best = g
                break
            exp += 1
            ci, cj = cur % gx, cur // gx
            hd = math.hypot(ci - ti, cj - tj)
            if hd < bestd:
                bestd, best = hd, cur
            cc = cost[cur]
            for di, dj, w in nbr:
                ni, nj = ci + di, cj + dj
                if ni < 0 or nj < 0 or ni >= gx or nj >= self.wgy:
                    continue
                k = nj * gx + ni
                if blk[k]:
                    continue
                if di and dj and (blk[cj * gx + ni] or blk[nj * gx + ci]):
                    continue
                dh = abs(gr[k] - gr[cur]) * 0.01
                if dh > 4.2 * w:
                    continue
                nc = cc + w + dh * 0.15
                if nc < cost.get(k, 1e18):
                    cost[k] = nc
                    came[k] = cur
                    heapq.heappush(openh, (nc + math.hypot(ni - ti, nj - tj), k))
        path = []
        cur = best
        while cur != -1:
            path.append(self.wcenter(cur % gx, cur // gx))
            cur = came[cur]
        path.reverse()
        if best == g:
            path.append((goal[0], goal[1]))
        return path


# ---------------------------------------------------------------- bot
UPGRADE_PRIORITY = [
    "gun_rate", "armor", "gun_damage", "plating", "lightning_efficiency", "laser_charge", "laser_damage",
    "nanites", "gyros", "gun_mag", "gun_velocity", "servos", "lightning_range", "jets", "capacitor",
    "reactor", "lightning_chain", "sensors", "mortar_reload", "mortar_velocity", "mortar_splash", "laser_pierce",
]
WEAPON_RANK = {"lightning": 3, "laser": 3, "mortar": 1, "gun": 2}


class Bot:
    name = "BeamsAndBytes"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.rng = random.Random(int(hello.get("seed", 1)))
        self.me_id = hello.get("slot")
        self.world = World(hello.get("map", {}).get("dir", "/arena"))
        self.path = []
        self.path_goal = None
        self.path_t = -99.0
        self.stuck_t = 0.0
        self.last_t = 0.0
        self.strafe_dir = 1
        self.strafe_until = 0.0
        self.last_hit_t = -99.0
        self.last_hit_bearing = None
        self.last_attacker = None
        self.wander = None
        self.wander_t = -99.0
        self.scan_phase = self.rng.random() * TAU
        self.sound_bearing = None
        self.sound_t = -99.0
        self.dodge_until = 0.0
        self.dodge_vec = (0.0, 0.0)
        self.crate_focus = None
        self.plan_budget_ok = True
        self.mem = {}            # enemy id -> (pos, vel, t_seen, hp_frac)
        self.vsm = {}            # enemy id -> smoothed velocity (strafers jitter)
        self.roof = None
        self.roof_pick_t = -99.0
        self.climb_t = 0.0
        self.recover = False
        self.recover_t = -99.0
        self.cover = None
        self.cover_t = -99.0

    # ---------------------------------------------------------- helpers
    def zone_live(self, z, t):
        if t <= z["shrink_start"]:
            return z["center"], z["radius"]
        if t >= z["shrink_end"]:
            return z["next_center"], z["next_radius"]
        f = (t - z["shrink_start"]) / max(1e-6, z["shrink_end"] - z["shrink_start"])
        c = (z["center"][0] + (z["next_center"][0] - z["center"][0]) * f,
             z["center"][1] + (z["next_center"][1] - z["center"][1]) * f)
        return c, z["radius"] + (z["next_radius"] - z["radius"]) * f

    def in_circle(self, p, c, r, margin=0.0):
        return d2(p, c) <= r - margin

    def safe_point_ok(self, p, z, t, margin=8.0):
        c, r = self.zone_live(z, t + 5)
        return d2(p, c) <= r - margin

    def lead(self, src, tgt, vel, speed, zoff):
        tx, ty, tz = tgt[0], tgt[1], tgt[2] + zoff
        vx, vy, vz = vel[0], vel[1], vel[2] * 0.3
        tt = 0.0
        for _ in range(3):
            px, py, pz = tx + vx * tt, ty + vy * tt, tz + vz * tt
            dist = math.sqrt((px - src[0]) ** 2 + (py - src[1]) ** 2 + (pz - src[2]) ** 2)
            tt = dist / speed
        px, py, pz = tx + vx * tt, ty + vy * tt, tz + vz * tt
        h = math.hypot(px - src[0], py - src[1])
        return [math.atan2(py - src[1], px - src[0]), math.atan2(pz - src[2], max(h, 0.1))], (px, py, pz), h

    def mortar_solution(self, src, tgt, v, g, high):
        dx, dy = tgt[0] - src[0], tgt[1] - src[1]
        d = math.hypot(dx, dy)
        h = tgt[2] - src[2]
        disc = v ** 4 - g * (g * d * d + 2 * h * v * v)
        if disc < 0 or d < 1:
            return None
        r = math.sqrt(disc)
        ang = math.atan((v * v + r) / (g * d)) if high else math.atan((v * v - r) / (g * d))
        tof = d / (v * math.cos(ang))
        return math.atan2(dy, dx), ang, tof

    def aim_err(self, me_aim, want):
        return abs(wrap(me_aim[0] - want[0])), abs(me_aim[1] - want[1])

    def pick_upgrade(self, offer):
        ids = [c["id"] for c in offer["choices"]]
        for u in UPGRADE_PRIORITY:
            if u in ids:
                return ids.index(u)
        return 0

    # ---------------------------------------------------------- cover
    def hidden_from(self, p, threats, step=2.4):
        """True if a mech standing at p (x, y) is out of every threat's line of fire."""
        w = self.world
        tgt = (p[0], p[1], w.ground(p[0], p[1]) + 4.5)
        for e in threats:
            ep = e[0]
            eye = (ep[0], ep[1], ep[2] + 7.5)
            if w.los(eye, tgt, skip_start=2.0, skip_end=3.0, step=step):
                return False
        return True

    def find_cover(self, pos, threats, zone, t, deadline):
        w = self.world
        tx = sum(e[0][0] for e in threats) / len(threats)
        ty = sum(e[0][1] for e in threats) / len(threats)
        away = math.atan2(pos[1] - ty, pos[0] - tx)
        best = None
        for r in (14.0, 26.0, 40.0, 58.0):
            for k in range(12):
                if time.perf_counter() > deadline:
                    return best[1] if best else None
                a = away + (k - 5.5) * (math.pi / 7.5)
                c = (pos[0] + math.cos(a) * r, pos[1] + math.sin(a) * r)
                if w.blocked(*c) or not self.safe_point_ok(c, zone, t + r / 11.0, margin=10):
                    continue
                if not self.hidden_from(c, threats):
                    continue
                dmin = min(d2(c, e[0]) for e in threats)
                if dmin < 35:
                    continue
                # prefer short runs that do not head toward the threats
                toward = math.cos(wrap(a - away))
                sc = r * 1.0 - toward * 12 - min(dmin, 120) * 0.15
                if not w.walk_clear(pos, c):
                    sc += 18
                if best is None or sc < best[0]:
                    best = (sc, c)
            if best is not None and best[0] < r:
                break
        return best[1] if best else None

    # ---------------------------------------------------------- high ground
    def on_roof(self, pos, roof):
        return roof is not None and pos[2] > roof[2] - 1.6 and _pip(pos[0], pos[1], roof[4])

    def pick_roof(self, pos, zone, t, energy_cap):
        nc, nr = zone["next_center"], zone["next_radius"]
        best = None
        for rf in self.world.roofs:
            hgt = rf[2] - rf[5]
            if hgt * 1.7 + 12 > energy_cap:
                continue
            if d2((rf[0], rf[1]), nc) > nr - max(20.0, rf[3] + 10):
                continue
            dd = d2((rf[0], rf[1]), pos)
            if dd > 320:
                continue
            sc = dd + abs(hgt - 16) * 2.0 - min(rf[3], 30) * 1.5
            if best is None or sc < best[0]:
                best = (sc, rf)
        return best[1] if best else None

    def roof_inside(self, p, roof, margin=3.0):
        if not _pip(p[0], p[1], roof[4]):
            return False
        return d2(p, (roof[0], roof[1])) < roof[3] * 1.2 - margin or _pip(p[0] + margin, p[1], roof[4]) and _pip(p[0] - margin, p[1], roof[4]) and _pip(p[0], p[1] + margin, roof[4]) and _pip(p[0], p[1] - margin, roof[4])

    # ---------------------------------------------------------- navigation
    def steer_to(self, pos, goal, t, me_z):
        w = self.world
        dist = d2(pos, goal)
        if dist < 1.5:
            return (0.0, 0.0)
        on_block = w.blocked(pos[0], pos[1])
        direct_ok = on_block or w.walk_clear(pos, goal)
        if direct_ok or dist < 8:
            tgt = goal
            self.path = []
        else:
            replan = (not self.path or self.path_goal is None or d2(self.path_goal, goal) > 12 or t - self.path_t > 3.0)
            if replan and self.plan_budget_ok:
                try:
                    self.path = self.world.astar(pos, goal)
                except Exception:
                    self.path = []
                self.path_goal = goal
                self.path_t = t
            while len(self.path) > 1 and d2(pos, self.path[0]) < 4.5:
                self.path.pop(0)
            tgt = goal
            if self.path:
                tgt = self.path[0]
                for k in range(min(len(self.path), 8) - 1, 0, -1):
                    if w.walk_clear(pos, self.path[k]):
                        tgt = self.path[k]
                        break
        dx, dy = tgt[0] - pos[0], tgt[1] - pos[1]
        L = math.hypot(dx, dy) or 1.0
        return (dx / L, dy / L)

    def avoid(self, pos, mv):
        """Rotate a move vector away from walls straight ahead."""
        w = self.world
        L = math.hypot(*mv)
        if L < 0.1 or w.blocked(pos[0], pos[1]):
            return mv
        base = math.atan2(mv[1], mv[0])
        for off in (0, 0.4, -0.4, 0.8, -0.8, 1.2, -1.2, 1.6, -1.6, 2.2, -2.2):
            a = base + off
            ok = True
            for s in (3.0, 6.0):
                if w.blocked(pos[0] + math.cos(a) * s, pos[1] + math.sin(a) * s):
                    ok = False
                    break
            if ok:
                return (math.cos(a) * L, math.sin(a) * L)
        return mv

    # ---------------------------------------------------------- main
    def act(self, obs):
        t0 = time.perf_counter()
        t = obs["t"]
        me = obs["self"]
        pos = me["pos"]
        x, y, z = pos
        st = me.get("stats", {})
        cfg = self.cfg
        hp, mhp = me["hp"], max(1.0, me["max_hp"])
        hpf = hp / mhp
        energy = me["energy"]
        zone = obs["zone"]
        dt = max(0.05, t - self.last_t) if self.last_t else 0.1
        self.last_t = t
        muzzle = (x, y, z + st.get("muzzle_height", 7))
        act = {}

        # ---- upgrades
        if obs.get("offer"):
            act["pick"] = self.pick_upgrade(obs["offer"])

        # ---- events
        for e in obs.get("events", []):
            if e.get("type") == "damaged" and e.get("weapon") != "zone":
                self.last_hit_t = t
                if e.get("bearing") is not None:
                    self.last_hit_bearing = e["bearing"]
                if e.get("by") is not None and e.get("by") < 100:
                    self.last_attacker = e["by"]
        for s in obs.get("sounds", []):
            if s["kind"] in ("gun", "laser_charge", "lightning", "step", "boost", "mortar", "jet") and s["dist"] in ("near", "mid"):
                self.sound_bearing = s["bearing"]
                self.sound_t = t

        weapons = me["weapons"]
        held = {w["kind"]: i for i, w in enumerate(weapons) if w}

        # ---- weapon pickups (take / swap)
        reach = cfg["loot"]["pickup_radius"] - 0.3
        for p in obs.get("pickups", []):
            if d2(p["pos"], pos) < reach and abs(p["pos"][2] - z) < 3.8 and p["kind"] not in held:
                if weapons[1] is None:
                    act["take"] = p["id"]
                    break
                cur = weapons[1]["kind"]
                if WEAPON_RANK.get(p["kind"], 0) >= WEAPON_RANK.get(cur, 0) + 2 and not weapons[1].get("charge"):
                    act["take"] = p["id"]; act["drop"] = 1
                    break

        # ---- targets
        enemies = [m for m in obs["mechs"] if m["kind"] == "mech" and m["id"] != me["id"]]
        npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]
        best = None
        for m in enemies:
            dd = math.dist(m["pos"], pos)
            mh = m.get("hp", 1400) / max(1, m.get("max_hp", 1400))
            vis = self.world.los(muzzle, (m["pos"][0], m["pos"][1], m["pos"][2] + 4.5))
            sc = dd + mh * 70 + (0 if vis else 90) - (40 if m["id"] == self.last_attacker else 0) - (50 if m.get("charging") and vis else 0)
            if best is None or sc < best[0]:
                best = (sc, m, dd, vis)
        target = best[1] if best else None
        tdist = best[2] if best else 1e9
        tvis = best[3] if best else False

        # ---- zone
        zc, zr = self.zone_live(zone, t)
        nc, nr = zone["next_center"], zone["next_radius"]
        dnext = d2(pos, nc)
        speed = st.get("walk_speed", 11.7)
        t_to_shrink_end = zone["shrink_end"] - t
        need_in = (dnext - (nr - 15)) / speed
        zone_urgent = (not me["in_zone"]) or (d2(pos, zc) > zr - 6)
        zone_soon = dnext > nr - 15 and (need_in + 12 > t_to_shrink_end or t > zone["shrink_start"] - 15)
        zone_goal = None
        if zone_urgent or zone_soon:
            if dnext > 1:
                k = max(0.0, (dnext - max(5.0, nr * 0.55))) / dnext
                zone_goal = (x + (nc[0] - x) * k, y + (nc[1] - y) * k) if k < 1 else (nc[0], nc[1])
            else:
                zone_goal = (nc[0], nc[1])

        mode = "idle"
        goal = None
        move = None
        boost = False
        aim = None
        face_target = None
        climb_jump = False
        climb_boost = False
        no_avoid = False

        # ---- high ground: pick a roof inside the next zone and hold it
        if self.roof is not None and d2((self.roof[0], self.roof[1]), nc) > nr - max(12.0, self.roof[3] * 0.6):
            self.roof = None
        if self.roof is None and t > 18 and t - self.roof_pick_t > 2.0:
            self.roof_pick_t = t
            self.roof = self.pick_roof(pos, zone, t, me["max_energy"])
        roof = self.roof
        on_roof = self.on_roof(pos, roof)
        roof_active = False

        def climb(target_threat=None):
            """Move onto the roof: path to its foot, then jump and jet over the edge."""
            nonlocal climb_jump, climb_boost, no_avoid
            rc = (roof[0], roof[1])
            dd = d2(pos, rc)
            need = (roof[2] - z) * 1.7 + 6
            if dd > roof[3] + 14:
                return self.steer_to(pos, rc, t, z)
            no_avoid = True
            ux, uy = (rc[0] - x) / max(dd, 1e-3), (rc[1] - y) / max(dd, 1e-3)
            if z < roof[2] - 0.4:
                if energy < need and me["grounded"]:
                    return (0.0, 0.0)  # wait at the wall for energy
                if me["grounded"]:
                    climb_jump = True
                climb_boost = energy > 2
            return (ux, uy)

        # ---- enemy memory and threat level
        for m in enemies:
            self.mem[m["id"]] = (m["pos"], m["vel"], t, m.get("hp", 1400) / max(1, m.get("max_hp", 1400)))
            sv = self.vsm.get(m["id"])
            v = m["vel"]
            self.vsm[m["id"]] = list(v) if sv is None else [sv[i] * 0.6 + v[i] * 0.4 for i in range(3)]
        for k in [k for k, v in self.mem.items() if t - v[2] > 9.0]:
            del self.mem[k]
        threats = []
        for k, (p, v, ts, eh) in self.mem.items():
            age = min(t - ts, 2.0)
            threats.append(((p[0] + v[0] * age, p[1] + v[1] * age, p[2]), eh, k, t - ts))
        threat_score = 0.0
        n_close = 0
        for e in threats:
            dd = d2(e[0], pos)
            threat_score += (0.5 + e[1]) / (1 + (dd / 60.0) ** 2)
            if dd < 130:
                n_close += 1
        weakest_enemy = min((e[1] for e in threats if d2(e[0], pos) < 160), default=1.0)

        # ---- recover mode (hide behind a building and regenerate), with hysteresis
        hold_fire = False
        if not self.recover:
            if False:
                self.recover = True
                self.recover_t = t
                self.cover = None
        else:
            if (hpf > 0.6 and n_close <= 1) or (not threats and hpf > 0.45) or (t - self.recover_t > 20 and hpf > 0.4):
                self.recover = False
                self.cover = None

        # ---- combat
        sec = weapons[1]["kind"] if weapons[1] else None
        close_vis = [m for m in enemies if math.dist(m["pos"], pos) < 32]
        if self.recover and not zone_urgent and threats and not (close_vis and hpf > 0.15 and len(close_vis) == 1 and target is not None and tvis and tdist < 32):
            mode = "recover"
            near = [e for e in threats if d2(e[0], pos) < 220] or threats
            exposed = not self.hidden_from(pos, near, step=3.0)
            if self.cover is None or t - self.cover_t > (0.6 if exposed else 2.5) or (exposed and d2(self.cover, pos) < 5):
                if self.plan_budget_ok:
                    c = self.find_cover(pos, near, zone, t, t0 + 0.015)
                    self.plan_budget_ok = False
                    self.cover_t = t
                    if c is not None:
                        self.cover = c
            if self.cover is not None and (exposed or d2(self.cover, pos) > 3):
                move = self.steer_to(pos, self.cover, t, z)
                boost = energy > 18 and exposed
            elif self.cover is None:
                # nowhere to hide: run directly away from the threats, toward the zone
                ax = sum(e[0][0] for e in near) / len(near); ay = sum(e[0][1] for e in near) / len(near)
                vx, vy = x - ax, y - ay
                L = math.hypot(vx, vy) or 1.0
                vx, vy = vx / L, vy / L
                zx, zy = nc[0] - x, nc[1] - y
                zl = math.hypot(zx, zy) or 1.0
                vx += zx / zl * 0.7; vy += zy / zl * 0.7
                L = math.hypot(vx, vy) or 1.0
                move = (vx / L, vy / L)
                boost = energy > 18
            else:
                move = (0.0, 0.0)
            # keep the front armor toward the nearest threat; shoot back only if cornered
            ne = min(near, key=lambda e: d2(e[0], pos))
            face_target = target if target is not None else {"pos": ne[0], "vel": [0, 0, 0], "kind": "mech"}
            hold_fire = not (target is not None and tvis and tdist < 60 and exposed)
            gi = held.get("gun")
            if hold_fire and gi is not None:
                gw = weapons[gi]
                if gw.get("ammo", 12) < gw["stats"].get("mag", 12) and not gw.get("reload"):
                    act["reload"] = True
        elif target is not None:
            mode = "fight"
            mh = target.get("hp", 1400) / max(1, target.get("max_hp", 1400))
            n_threat = sum(1 for m in enemies if math.dist(m["pos"], pos) < 160)
            retreat = (hpf < 0.32 and mh > hpf - 0.05) or (n_threat >= 3 and hpf < 0.6)
            if sec == "lightning" and energy > 25:
                want = 30.0
            elif sec == "laser":
                want = 95.0
            elif sec == "mortar":
                want = 90.0
            else:
                want = 60.0
            if hpf < 0.5:
                want += 30
            tp = target["pos"]
            dx, dy = tp[0] - x, tp[1] - y
            L = math.hypot(dx, dy) or 1.0
            ux, uy = dx / L, dy / L
            if t > self.strafe_until:
                self.strafe_dir = -self.strafe_dir if self.rng.random() < 0.75 else self.strafe_dir
                self.strafe_until = t + 0.6 + self.rng.random() * 1.0
            px, py = -uy * self.strafe_dir, ux * self.strafe_dir
            if retreat:
                radial = -1.0
                mode = "retreat"
            else:
                radial = max(-1.0, min(1.0, (L - want) / 25.0))
                if not tvis:
                    radial = max(radial, 0.6) if hpf > 0.45 else radial
            mvx, mvy = ux * radial + px * 0.9, uy * radial + py * 0.9
            if roof_active and on_roof:
                # hold the roof: strafe only along it, never step off to chase
                no_avoid = True
                ok = None
                for sd in (self.strafe_dir, -self.strafe_dir):
                    qx, qy = -uy * sd, ux * sd
                    if self.roof_inside((x + qx * 5, y + qy * 5), roof):
                        ok = (qx, qy); self.strafe_dir = sd
                        break
                if ok is None:
                    rc = (roof[0], roof[1]); dd = d2(pos, rc) or 1.0
                    ok = ((rc[0] - x) / dd, (rc[1] - y) / dd)
                mvx, mvy = ok
                if retreat:
                    rc = (roof[0], roof[1]); dd = d2(pos, rc) or 1.0
                    mvx, mvy = (rc[0] - x) / dd, (rc[1] - y) / dd
            elif len(enemies) >= 2 and not (roof_active and d2(pos, (roof[0], roof[1])) < roof[3] + 14):
                # several hunters: pick the step that keeps them all in front of us and away from flanks
                fy = math.atan2(dy, dx)
                bestc = None
                for k in range(12):
                    a = k * (TAU / 12)
                    nx2, ny2 = x + math.cos(a) * 8, y + math.sin(a) * 8
                    if self.world.blocked(nx2, ny2):
                        continue
                    c = 0.0
                    for m in enemies:
                        mp = m["pos"]
                        dm = math.hypot(mp[0] - nx2, mp[1] - ny2)
                        c += 1.0 / (1.0 + (dm / 50.0) ** 2)
                        bz = math.atan2(mp[1] - ny2, mp[0] - nx2)
                        if abs(wrap(bz - fy)) > 1.4:
                            c += 0.8  # would hit our rear armour
                        if not self.world.los((mp[0], mp[1], mp[2] + 7), (nx2, ny2, z + 4.5), step=3.0):
                            c -= 0.5  # building between us
                    c -= 0.3 * math.cos(a - math.atan2(mvy, mvx))
                    if bestc is None or c < bestc[0]:
                        bestc = (c, a)
                if bestc is not None:
                    mvx, mvy = math.cos(bestc[1]), math.sin(bestc[1])
            elif roof_active and d2(pos, (roof[0], roof[1])) < roof[3] + 14 and not retreat:
                cm = climb(target)
                mvx, mvy = cm[0], cm[1]
            if zone_goal is not None and not (roof_active and on_roof):
                gx, gy = self.steer_to(pos, zone_goal, t, z)
                wgt = 1.6 if zone_urgent else 0.8
                mvx += gx * wgt; mvy += gy * wgt
            ml = math.hypot(mvx, mvy) or 1.0
            move = (mvx / ml, mvy / ml)
            boost = energy > 35 and (L < 140 or retreat or zone_urgent) and not (roof_active and on_roof)
            face_target = target
        else:
            # ---- non-combat goals
            drop_goal = None
            for dr in obs.get("drops", []):
                dd = d2(dr["pos"], pos)
                risky = False
                if dd < 220 and not risky and (dr["holder"] in (None, me["id"])) and self.safe_point_ok(dr["pos"], zone, t + dd / speed):
                    eta = dd / speed
                    if dr["lands_in"] < eta + 8:
                        if drop_goal is None or dd < drop_goal[0]:
                            drop_goal = (dd, dr)
            cube_goal = None
            for c in obs.get("cubes", []):
                dd = d2(c["pos"], pos)
                if dd < 110 and self.safe_point_ok(c["pos"], zone, t + dd / speed) and (cube_goal is None or dd < cube_goal[0]):
                    cube_goal = (dd, c)
            pick_goal = None
            for p in obs.get("pickups", []):
                if p["kind"] in held:
                    continue
                if weapons[1] is not None and WEAPON_RANK.get(p["kind"], 0) < WEAPON_RANK.get(weapons[1]["kind"], 0) + 2:
                    continue
                dd = d2(p["pos"], pos)
                if dd < 130 and abs(p["pos"][2] - self.world.ground(p["pos"][0], p["pos"][1])) < 3 and self.safe_point_ok(p["pos"], zone, t + dd / speed):
                    if pick_goal is None or dd < pick_goal[0]:
                        pick_goal = (dd, p)
            farming = me["cubes"] < 5 and t < 140
            npc_goal = None
            if hpf > 0.5:
                for m in npcs:
                    dd = math.dist(m["pos"], pos)
                    if dd < (170 if farming else 110) and self.safe_point_ok(m["pos"], zone, t + dd / speed) and (npc_goal is None or dd < npc_goal[0]):
                        npc_goal = (dd, m)
            crate_goal = None
            for c in obs.get("crates", []):
                dd = math.dist(c["pos"], pos)
                if dd < (120 if farming else 75) and abs(c["pos"][2] - self.world.ground(c["pos"][0], c["pos"][1])) < 3 and (crate_goal is None or dd < crate_goal[0]):
                    crate_goal = (dd, c)

            if zone_urgent and zone_goal is not None:
                mode, goal = "zone", zone_goal
                boost = energy > 25
            elif drop_goal is not None:
                dr = drop_goal[1]
                mode, goal = "drop", (dr["pos"][0], dr["pos"][1])
                boost = energy > 40 and drop_goal[0] > 20
            elif pick_goal is not None:
                mode, goal = "pickup", (pick_goal[1]["pos"][0], pick_goal[1]["pos"][1])
            elif cube_goal is not None and cube_goal[0] < 45 and not on_roof:
                mode, goal = "cube", (cube_goal[1]["pos"][0], cube_goal[1]["pos"][1])
            elif roof_active and not (farming and (cube_goal or npc_goal or crate_goal)):
                if on_roof:
                    mode = "perch"
                    no_avoid = True
                    rc = (roof[0], roof[1]); dd = d2(pos, rc)
                    move = (0.0, 0.0) if dd < max(3.0, roof[3] * 0.3) else ((rc[0] - x) / dd * 0.5, (rc[1] - y) / dd * 0.5)
                else:
                    mode = "climb"
                    move = climb()
            elif cube_goal is not None:
                mode, goal = "cube", (cube_goal[1]["pos"][0], cube_goal[1]["pos"][1])
            elif zone_goal is not None:
                mode, goal = "zone", zone_goal
            elif npc_goal is not None:
                mode = "npc"
                m = npc_goal[1]
                face_target = m
                dd = d2(m["pos"], pos)
                ux, uy = (m["pos"][0] - x) / max(dd, 1), (m["pos"][1] - y) / max(dd, 1)
                radial = max(-1.0, min(1.0, (dd - 55) / 20))
                if t > self.strafe_until:
                    self.strafe_dir = -self.strafe_dir
                    self.strafe_until = t + 1.0 + self.rng.random()
                mv = (ux * radial - uy * self.strafe_dir * 0.7, uy * radial + ux * self.strafe_dir * 0.7)
                ml = math.hypot(*mv) or 1
                move = (mv[0] / ml, mv[1] / ml)
            elif crate_goal is not None:
                mode = "crate"
                c = crate_goal[1]
                face_target = {"pos": c["pos"], "vel": [0, 0, 0], "kind": "crate"}
                dd = d2(c["pos"], pos)
                if dd > 25:
                    goal = (c["pos"][0], c["pos"][1])
                else:
                    move = (0.0, 0.0)
            else:
                mode = "roam"
                if self.wander is None or d2(self.wander, pos) < 10 or t - self.wander_t > 25 or d2(self.wander, nc) > nr * 0.75:
                    for _ in range(12):
                        a = self.rng.random() * TAU
                        r = math.sqrt(self.rng.random()) * nr * 0.6
                        cand = (nc[0] + math.cos(a) * r, nc[1] + math.sin(a) * r)
                        if not self.world.blocked(*cand):
                            break
                    self.wander = cand
                    self.wander_t = t
                goal = self.wander
            if goal is not None and move is None:
                move = self.steer_to(pos, goal, t, z)
                if mode == "drop":
                    dd = d2(goal, pos)
                    if dd < 4:
                        move = (0.0, 0.0)
                    elif dd < 8:
                        move = (move[0] * 0.5, move[1] * 0.5)
                if mode == "roam" and t > 40:
                    move = (move[0] * 0.4, move[1] * 0.4)  # walk slowly: silent

        # ---- projectile dodge
        for pr in obs.get("projectiles", []):
            pp, pv = pr["pos"], pr["vel"]
            vv = pv[0] ** 2 + pv[1] ** 2 + pv[2] ** 2
            if vv < 1:
                continue
            rx, ry, rz = x - pp[0], y - pp[1], (z + 4.5) - pp[2]
            tc = (rx * pv[0] + ry * pv[1] + rz * pv[2]) / vv
            if 0 < tc < 1.2:
                cx = pp[0] + pv[0] * tc - x
                cy = pp[1] + pv[1] * tc - y
                cz = pp[2] + pv[2] * tc - (z + 4.5)
                miss = math.sqrt(cx * cx + cy * cy + cz * cz)
                rad = 9.0 if pr["kind"] == "mortar" else 3.5
                if miss < rad:
                    hv = math.hypot(pv[0], pv[1]) or 1
                    sx, sy = -pv[1] / hv, pv[0] / hv
                    if sx * cx + sy * cy > 0:
                        sx, sy = -sx, -sy
                    self.dodge_vec = (sx, sy)
                    self.dodge_until = t + 0.35
                    break
        if t < self.dodge_until and move is not None:
            mvx = move[0] * 0.4 + self.dodge_vec[0]
            mvy = move[1] * 0.4 + self.dodge_vec[1]
            ml = math.hypot(mvx, mvy) or 1
            move = (mvx / ml, mvy / ml)
            boost = boost or energy > 20

        if move is None:
            move = (0.0, 0.0)
        if not no_avoid:
            move = self.avoid(pos, move)

        # ---- stuck handling: jump and jet over obstacles
        hs = math.hypot(me["vel"][0], me["vel"][1])
        want_speed = math.hypot(*move)
        jump = False
        if want_speed > 0.5 and hs < 2.0:
            self.stuck_t += dt
        else:
            self.stuck_t = max(0.0, self.stuck_t - dt * 2)
        if self.stuck_t > 0.5:
            if me["grounded"]:
                jump = True
            if energy > 10:
                boost = True
            if self.stuck_t > 3.0:
                a = self.rng.random() * TAU
                move = (math.cos(a), math.sin(a))
                self.path = []
                self.stuck_t = 0.0
        if not me["grounded"] and self.stuck_t > 0.2 and energy > 8:
            boost = True
        if climb_jump:
            jump = True
        if climb_boost:
            boost = True
        if no_avoid and mode in ("perch",) or (roof_active and on_roof and mode == "fight"):
            jump = False
            self.stuck_t = 0.0

        # ---- aiming and firing
        fire = [False, False]
        if face_target is not None:
            tp = face_target["pos"]
            kind = face_target.get("kind", "mech")
            zoff = 4.5 if kind == "mech" else (1.75 if kind == "npc" else 1.2)
            gstats = weapons[0]["stats"] if weapons[0] else cfg["weapons"]["gun"]
            lv = face_target.get("vel", [0, 0, 0])
            if kind == "mech" and face_target.get("id") in self.vsm:
                lv = self.vsm[face_target["id"]]
            want, aimpt, hdist = self.lead(muzzle, tp, lv, gstats.get("speed", 252), zoff)
            clear = self.world.los(muzzle, aimpt)
            dist3 = math.dist(muzzle, aimpt)
            use_mortar = False
            mi = held.get("mortar")
            if mi is not None and kind == "mech" and (not clear or dist3 > 85) and 22 < hdist < 225:
                ms = weapons[mi]["stats"]
                src = (x, y, z + st.get("height", 9) + 0.5)
                sol = self.mortar_solution(src, (aimpt[0], aimpt[1], tp[2] + 1.0), ms.get("speed", 72), ms.get("gravity", 22), not clear)
                if sol and weapons[mi]["cooldown"] <= 0.05:
                    tof = sol[2]
                    lp = (tp[0] + face_target["vel"][0] * tof, tp[1] + face_target["vel"][1] * tof, tp[2] + 1.0)
                    sol = self.mortar_solution(src, lp, ms.get("speed", 72), ms.get("gravity", 22), not clear)
                    if sol and cfg["mech"]["min_pitch"] <= sol[1] <= cfg["mech"]["max_pitch"]:
                        use_mortar = True
                        aim = [sol[0], sol[1]]
                        ey, ep = self.aim_err(me["aim"], aim)
                        if ey < 0.025 and ep < 0.03 and d2(lp, pos) > 16:
                            fire[mi] = True
            if not use_mortar:
                aim = want
                ey, ep = self.aim_err(me["aim"], want)
                rad = 2.6 if kind == "mech" else (1.6 if kind == "npc" else 1.2)
                tol = math.atan2(rad, max(dist3, 1.0)) + 0.02
                aligned = ey < tol and ep < tol + 0.02
                gi = held.get("gun")
                if gi is not None and clear and dist3 < weapons[gi]["stats"].get("range", 230) * 0.97 and aligned:
                    fire[gi] = True
                li = held.get("laser")
                if li is not None:
                    lw = weapons[li]
                    ch = lw.get("charge", 0) or 0
                    full = lw["stats"].get("charge", 0.75)
                    in_range = clear and dist3 < lw["stats"].get("range", 280) * 0.97 and kind != "crate"
                    if ch > 0:
                        # holding: release when full and aligned (or target gone)
                        if ch >= full - 0.02 and aligned:
                            fire[li] = False
                        elif not in_range and ch >= lw["stats"].get("min_charge", 0.15):
                            fire[li] = False
                        else:
                            fire[li] = True
                    elif in_range and lw["cooldown"] <= 0.05:
                        fire[li] = True
                ni = held.get("lightning")
                if ni is not None:
                    nw = weapons[ni]["stats"]
                    if clear and dist3 < nw.get("range", 45) * 0.95 and ey < math.radians(nw.get("cone_deg", 30)) * 0.45 and energy > 12:
                        fire[ni] = True
            if gi_reload := (held.get("gun") is not None):
                gw = weapons[held["gun"]]
                if gw.get("ammo", 1) == 0 and not gw.get("reload"):
                    act["reload"] = True
        else:
            # no target: look toward threats, otherwise scan
            look = None
            if t - self.last_hit_t < 3 and self.last_hit_bearing is not None:
                look = self.last_hit_bearing
            elif t - self.sound_t < 2.5 and self.sound_bearing is not None:
                look = self.sound_bearing
            if look is None:
                if math.hypot(*move) > 0.2:
                    base = math.atan2(move[1], move[0])
                else:
                    base = me["aim"][0]
                look = base + math.sin(t * 1.3 + self.scan_phase) * 1.0
            aim = [look, 0.0]
            gi = held.get("gun")
            if gi is not None:
                gw = weapons[gi]
                if gw.get("ammo", 12) < gw["stats"].get("mag", 12) * 0.6 and not gw.get("reload"):
                    act["reload"] = True
            li = held.get("laser")
            if li is not None and (weapons[li].get("charge") or 0) > 0:
                fire[li] = False

        if hold_fire:
            li = held.get("laser")
            fire = [False, False]
            if li is not None and (weapons[li].get("charge") or 0) > 0:
                fire[li] = True  # keep a charge held rather than wasting it
        act["move"] = [round(move[0], 3), round(move[1], 3)]
        act["boost"] = bool(boost)
        act["jump"] = bool(jump)
        if aim is not None:
            act["aim"] = [round(wrap(aim[0]), 4), round(max(-0.69, min(1.29, aim[1])), 4)]
        act["fire"] = fire
        el = time.perf_counter() - t0
        self.plan_budget_ok = el < 0.025
        return act


# ---------------------------------------------------------------- protocol loop
def _send(msg):
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _reader(q):
    for line in sys.stdin:
        if line.strip():
            q.put(line)
    q.put(None)


def run():
    q = queue.Queue()
    threading.Thread(target=_reader, args=(q,), daemon=True).start()
    bot = None
    last = {}
    errors = 0
    while True:
        batch = [q.get()]
        while True:
            try:
                batch.append(q.get_nowait())
            except queue.Empty:
                break
        latest = None
        for raw in batch:
            if raw is None:
                return
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            kind = msg.get("type")
            if kind == "hello":
                try:
                    bot = Bot(msg)
                except Exception:
                    log(traceback.format_exc())
                _send({"type": "ready", "name": Bot.name})
            elif kind == "obs":
                latest = msg
            elif kind == "end":
                return
        if latest is None:
            continue
        action = {}
        if bot is not None:
            try:
                action = dict(bot.act(latest) or {})
                last = {k: action[k] for k in ("move", "aim", "boost") if k in action}
            except Exception:
                errors += 1
                if errors <= 5 or errors % 100 == 0:
                    log(f"act failed at tick {latest.get('tick')}:\n{traceback.format_exc()}")
                action = dict(last)
        action["tick"] = latest["tick"]
        try:
            _send(action)
        except (TypeError, ValueError):
            _send({"tick": latest["tick"], **last})


if __name__ == "__main__":
    run()
