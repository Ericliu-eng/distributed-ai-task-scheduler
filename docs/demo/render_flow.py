"""Render the README flow animation offline (requires Pillow).

Two tasks walk through every scheduler path: routing rules, the small tier,
a provider failure with exponential backoff, the large tier, a SKIP LOCKED
claim, a killed worker, lease recovery, a fenced stale write, and the
dashboard plus saved evidence. It is an illustration of the implemented
protocol, not a recording. Event names match app/store.py, and the numbers
come from code defaults and the saved reports in bench/results/.

Colors match the dashboard tokens in app/static/styles.css.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
W, H, S, FPS = 1200, 620, 2, 24  # S: supersampling factor for smooth edges
FADE_SECONDS = 0.3  # highlight transitions between beats

BG, PANEL, SOFT, LINE = "#f7f8fa", "#ffffff", "#f3f5f8", "#d9dee6"
TEXT, MUTED, DIM, WIRE = "#202735", "#626d7d", "#9aa4b2", "#b7c0cc"
ACCENT, GREEN, AMBER, RED = "#365ec9", "#26734d", "#94611d", "#b33b3b"
TINT = {ACCENT: "#eef3fd", GREEN: "#e9f4ee", AMBER: "#fbf3e6", RED: "#fbeeee"}

CLIENT, DASHBOARD = (30, 110, 160, 100), (30, 250, 160, 160)
API = (225, 110, 240, 280)
QUEUE, MONITOR = (500, 110, 260, 180), (500, 320, 260, 70)
SMALL, WORKER_A, WORKER_B = (800, 100, 370, 100), (800, 215, 370, 100), (800, 330, 370, 100)
DETAIL, EVIDENCE = (30, 440, 440, 95), (500, 440, 670, 95)
SLOT = {"client": (110, 182), "api": (345, 352), "queue": (630, 230),
        "small": (1090, 150), "a": (1090, 265), "b": (1090, 380)}
RULES = ["urgent SLA → shorter queue", "difficulty ≥ 0.55 → large",
         "small queue > 4 → large", "otherwise → small"]

BEATS = [  # (caption, code line, explanation, seconds)
    ("A client submits a task", "POST /tasks  idempotency_key=ticket-42",
     "A retry with the same key returns the same task", 2.8),
    ("The router checks its rules in order", "difficulty = len(prompt) / 1200 = 0.04",
     "No rule escalates it, so it takes the cheaper SMALL tier", 3.4),
    ("The provider fails; the task backs off", "503 → queued, next_attempt_at = now + 0.8 s",
     "delay = min(30 s, 1 s × 2^(attempt−1)) × 50–100% jitter", 3.0),
    ("Attempt 2 succeeds after the delay", "claim ... WHERE next_attempt_at <= now()",
     "After 3 failed attempts the task is marked failed", 2.8),
    ("A long prompt is routed to the large tier", "difficulty = 890 / 1200 = 0.74 ≥ 0.55",
     "The decision and its reason are stored with the task", 3.2),
    ("Exactly one worker claims it", "SELECT ... FOR UPDATE SKIP LOCKED",
     "Attempt 1 holds a 15 s lease, renewed every 3 s", 3.0),
    ("The worker is killed mid-task", "SIGKILL: no cleanup code runs",
     "Nobody renews the lease, so it runs out", 2.8),
    ("The monitor recovers the task", "lease_until < now()  →  status = queued",
     "Worker B claims attempt 2 and completes it", 3.6),
    ("The late write from attempt 1 is fenced", "UPDATE ... WHERE attempt = 1 AND status = 'running'",
     "0 rows match, so the stale result is rejected", 3.2),
    ("Every step is observable and measured", "GET /metrics/summary · /metrics/timeseries · /events",
     "Saved benchmark and evaluation reports are served read-only", 3.2),
]
HOLD_SECONDS = 3.0
# Events as app/store.py records them: (beat, t, kind, text)
EVENTS = [(1, 0.62, "routed", "routed 2c91 → small"), (1, 0.92, "claimed", "claimed 2c91 #1"),
          (2, 0.35, "retry", "retry 2c91 in 0.8 s"), (3, 0.45, "claimed", "claimed 2c91 #2"),
          (3, 0.85, "succeeded", "succeeded 2c91"), (4, 0.62, "routed", "routed 7f3a → large"),
          (5, 0.65, "claimed", "claimed 7f3a #1"), (7, 0.3, "recovered", "recovered 7f3a"),
          (7, 0.66, "claimed", "claimed 7f3a #2"), (7, 0.92, "succeeded", "succeeded 7f3a")]
EVIDENCE_ITEMS = [("Throughput benchmark", "135 tasks/s · 0 duplicates"),
                  ("Crash-recovery benchmark", "20 / 20 recovered"),
                  ("Claude routing evaluation", "100% quality · −35% cost")]
FONTS = {}


def font(size, weight="regular"):
    key = size, weight
    if key not in FONTS:
        names = {"regular": ("segoeui.ttf", "DejaVuSans.ttf"),
                 "bold": ("segoeuib.ttf", "DejaVuSans-Bold.ttf"),
                 "mono": ("consola.ttf", "DejaVuSansMono.ttf")}[weight]
        for path in (Path("C:/Windows/Fonts") / names[0],
                     Path("/usr/share/fonts/truetype/dejavu") / names[1],
                     Path("/System/Library/Fonts/Supplemental/Arial.ttf")):
            if path.exists():
                FONTS[key] = ImageFont.truetype(str(path), size * S)
                break
        else:
            raise RuntimeError("Install Segoe UI, DejaVu, or Arial to render the animation.")
    return FONTS[key]


def ease(t):
    """Cubic ease-in-out: gentle start and stop for every movement."""
    t = min(1.0, max(0.0, t))
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def span(t, start, end):
    """Progress of t through [start, end], clamped to 0..1."""
    return min(1.0, max(0.0, (t - start) / (end - start)))


def lerp(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(2))


def mix(a, b, t):
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def partial_path(points, fraction):
    """The leading part of a polyline, `fraction` of its total length."""
    lengths = [((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 for a, b in zip(points, points[1:])]
    remaining, result = fraction * sum(lengths), [points[0]]
    for (a, b), length in zip(zip(points, points[1:]), lengths):
        if remaining >= length:
            result.append(b)
            remaining -= length
        else:
            result.append(lerp(a, b, remaining / length if length else 0))
            break
    return result


def after(beat, t, at_beat, at_t):
    """True once the story has reached (at_beat, at_t)."""
    return (beat, t) >= (at_beat, at_t)


class Frame:
    def __init__(self):
        self.image = Image.new("RGB", (W * S, H * S), BG)
        self.d = ImageDraw.Draw(self.image)

    def rect(self, box, fill, outline=None, width=1, radius=10):
        x, y, w, h = box
        self.d.rounded_rectangle((x * S, y * S, (x + w) * S, (y + h) * S), radius * S,
                                 fill=fill, outline=outline, width=width * S)

    def text(self, x, y, value, size=16, fill=TEXT, weight="regular"):
        self.d.text((x * S, y * S), value, fill=fill, font=font(size, weight))

    def width(self, value, size, weight="regular"):
        return self.d.textlength(value, font=font(size, weight)) / S

    def line(self, points, fill=WIRE, width=2, dashed=False, phase=0.0):
        pts = [(x * S, y * S) for x, y in points]
        if not dashed:
            self.d.line(pts, fill=fill, width=width * S, joint="curve")
            return
        for a, b in zip(pts, pts[1:]):
            length = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
            step, dash = 12 * S, 6 * S
            d = -step + (phase % 1) * step
            while d < length:
                p, q = max(0, d), min(length, d + dash)
                if q > p:
                    self.d.line([lerp(a, b, p / length), lerp(a, b, q / length)],
                                fill=fill, width=width * S)
                d += step

    def arrow(self, x, y, direction, fill=WIRE):
        shapes = {"right": [(-8, -5), (0, 0), (-8, 5)], "up": [(-5, 8), (0, 0), (5, 8)],
                  "left": [(8, -5), (0, 0), (8, 5)], "down": [(-5, -8), (0, 0), (5, -8)]}
        self.d.polygon([((x + dx) * S, (y + dy) * S) for dx, dy in shapes[direction]], fill=fill)

    def pill(self, x, y, label, color, size=11, filled=False, level=1.0):
        w = self.width(label, size, "bold") + 16
        fill = mix(BG, color, level) if filled else PANEL
        ink = mix(BG, PANEL, level) if filled else mix(PANEL, color, level)
        self.rect((x, y, w, size + 10), fill, mix(BG, color, level), 1, radius=(size + 10) // 2)
        self.text(x + 8, y + 3, label, size, ink, "bold")
        return w


def look(color):
    return (TINT[color], color, color) if color else (PANEL, LINE, ACCENT)


def blend_look(previous, current, k):
    return tuple(mix(x, y, k) for x, y in zip(look(previous), look(current)))


def node(f, box, title, detail="", tag=None, style=None, title_size=18):
    fill, border, tag_color = style or look(None)
    f.rect(box, fill, border, 2 if border != LINE else 1)
    x, y, _, _ = box
    if tag:
        f.text(x + 14, y + 11, tag, 11, tag_color, "bold")
    f.text(x + 14, y + (27 if tag else 11), title, title_size, TEXT, "bold")
    if detail:
        f.text(x + 14, y + (53 if tag else 37), detail, 13, MUTED)


def card(f, center, label, note, tier=None, color=ACCENT, dim=False):
    cx, cy = center
    f.rect((cx - 68, cy - 19, 140, 46), "#e3e8ef", radius=8)  # soft drop shadow
    f.rect((cx - 70, cy - 22, 140, 46), SOFT if dim else PANEL, DIM if dim else color, 2, radius=8)
    f.text(cx - 59, cy - 17, label, 13, DIM if dim else TEXT, "bold")
    f.text(cx - 59, cy + 3, note, 11, DIM if dim else MUTED, "mono")
    if tier:
        tw = f.width(tier, 9, "bold") + 10
        f.rect((cx + 60 - tw, cy - 14, tw, 14), DIM if dim else color, radius=7)
        f.text(cx + 65 - tw, cy - 13, tier, 9, PANEL, "bold")


def worker(f, box, name, tier_label, status, status_color, lease=None, lease_color=ACCENT,
           style=None, killed=False):
    node(f, box, name, style=style)
    x, y, _, _ = box
    f.pill(x + 22 + f.width(name, 18, "bold"), y + 13, status, status_color, 10)
    if killed:
        f.text(x + 14, y + 38, "SIGKILL · process gone", 12, RED, "mono")
    else:
        f.text(x + 14, y + 38, tier_label, 13, MUTED)
    if lease is not None:
        f.text(x + 14, y + 66, "lease", 11, MUTED, "mono")
        f.rect((x + 56, y + 69, 130, 7), "#e7ebf0", radius=4)
        if lease > 0.01:
            f.rect((x + 56, y + 69, 130 * lease, 7), lease_color, radius=4)


def highlights(beat, t, final=False):
    lit = dict.fromkeys(("client", "api", "queue", "monitor", "small", "a", "b",
                         "dashboard", "evidence"))
    if final:
        lit.update(dashboard=ACCENT, evidence=GREEN)
        return lit
    if beat == 0:
        lit.update(client=ACCENT if t < 0.5 else None, api=ACCENT)
    elif beat == 1:
        lit.update(api=ACCENT, queue=ACCENT if t > 0.6 else None,
                   small=ACCENT if t > 0.8 else None)
    elif beat == 2:
        lit.update(small=RED if t < 0.4 else None, queue=AMBER if t > 0.35 else None)
    elif beat == 3:
        lit.update(queue=ACCENT if t < 0.45 else None, small=GREEN if t > 0.85 else ACCENT)
    elif beat == 4:
        lit.update(client=ACCENT if t < 0.25 else None, api=ACCENT, queue=ACCENT if t > 0.6 else None)
    elif beat == 5:
        lit.update(queue=ACCENT, a=ACCENT, b=ACCENT if t < 0.55 else None)
    elif beat == 6:
        lit.update(a=RED)
    elif beat == 7:
        lit.update(monitor=AMBER if t < 0.45 else None, queue=ACCENT,
                   b=(GREEN if t > 0.9 else ACCENT) if t > 0.62 else None)
    elif beat == 8:
        lit.update(a=RED if t < 0.6 else None, b=GREEN)
    else:
        lit.update(dashboard=ACCENT, api=ACCENT if t < 0.5 else None, evidence=GREEN if t > 0.4 else None)
    return lit


def draw_frame(beat, t, final=False):
    f = Frame()
    f.rect((30, 28, 5, 28), ACCENT, radius=2)
    f.text(46, 22, "Orbit", 28, TEXT, "bold")
    f.text(132, 33, "Cost-aware routing, backoff retries, and lease-based crash recovery", 16, MUTED)
    tag = "ILLUSTRATED FLOW"
    f.text(1170 - f.width(tag, 11, "bold"), 36, tag, 11, DIM, "bold")

    k = ease(span(t, 0, FADE_SECONDS / BEATS[beat][3])) if beat and not final else 1.0
    now, before = highlights(beat, t, final), (highlights(beat - 1, 1.0) if beat else {})
    style = {key: blend_look(before.get(key), now.get(key), k) for key in now}

    # Wiring: solid = task data, dashed = reads and control.
    f.line([(190, 160), (225, 160)]); f.arrow(225, 160, "right")
    f.line([(465, 200), (500, 200)]); f.arrow(500, 200, "right")
    f.line([(760, 230), (780, 230)])
    f.line([(780, 150), (780, 380)])
    for y in (150, 265, 380):
        f.line([(780, y), (800, y)]); f.arrow(800, y, "right")
    reading = now["dashboard"] is not None
    f.line([(190, 330), (225, 330)], ACCENT if reading else WIRE, 2, True, -t * 4)
    f.arrow(225, 330, "right", ACCENT if reading else WIRE)
    f.line([(345, 390), (345, 418), (560, 418), (560, 440)], GREEN if now["evidence"] else WIRE,
           2, True, t * 4)
    scanning = now["monitor"] is not None
    f.line([(630, 320), (630, 292)], AMBER if scanning else WIRE, 2, True, t * 4)
    f.arrow(630, 292, "up", AMBER if scanning else WIRE)

    node(f, CLIENT, "Client", "POST /tasks", style=style["client"])
    node(f, QUEUE, "Task queue", "priority · retry time · lease", "POSTGRESQL", style=style["queue"])
    node(f, MONITOR, "Recovery monitor", "scans leases every 2 s", style=style["monitor"])

    # API + router with its rules evaluated top to bottom.
    node(f, API, "FastAPI + router", "idempotency · stored reason", "API", style=style["api"])
    f.text(API[0] + 14, API[1] + 86, "ROUTING RULES, IN ORDER", 10, MUTED, "bold")
    checking, match = None, None
    if beat == 1:
        checking, match = min(3, int(span(t, 0.08, 0.5) * 4)), (3 if t > 0.5 else None)
    elif beat == 4:
        checking, match = min(1, int(span(t, 0.08, 0.3) * 2)), (1 if t > 0.3 else None)
    for i, rule in enumerate(RULES):
        y = API[1] + 106 + i * 26
        if match == i:
            f.rect((API[0] + 10, y - 3, API[2] - 20, 23), TINT[ACCENT], ACCENT, 1, radius=6)
        elif checking is not None and i < (match if match is not None else checking + 1):
            f.text(API[0] + API[2] - 34, y + 1, "no", 11, DIM, "bold")
        f.text(API[0] + 18, y, f"{i + 1}  {rule}", 13, ACCENT if match == i else TEXT)
        if match == i:
            f.text(API[0] + API[2] - 50, y + 1, "match", 11, ACCENT, "bold")

    # Dashboard: live event feed with the latest four events.
    node(f, DASHBOARD, "Dashboard", "metrics · events", style=style["dashboard"])
    events = [e for e in EVENTS if final or after(beat, t, e[0], e[1])][-4:]
    colors = {"retry": AMBER, "recovered": AMBER, "succeeded": GREEN}
    for i, (_, _, kind, label) in enumerate(events):
        f.text(DASHBOARD[0] + 14, DASHBOARD[1] + 64 + i * 22, label, 11, colors.get(kind, MUTED), "mono")

    # Small worker: claims, fails with a provider error, then succeeds on attempt 2.
    s_status, s_color = "IDLE", DIM
    if beat == 1 and t > 0.8 or beat == 3 and 0.45 < t <= 0.85:
        s_status, s_color = "RUNNING", ACCENT
    elif beat == 2 and t < 0.4:
        s_status, s_color = "HTTP 503", RED
    elif beat == 3 and t > 0.85 or beat > 3 or final:
        s_status, s_color = "SUCCEEDED", GREEN
    worker(f, SMALL, "Small worker", "small tier", s_status, s_color, style=style["small"])

    # Large workers: one claim, a crash, recovery, completion.
    a_status, a_color, a_lease, a_lease_color = "IDLE", DIM, None, ACCENT
    b_status, b_color, b_lease = "IDLE", DIM, None
    if beat == 5:
        polling = t < 0.55
        a_status, a_color = ("POLLING", ACCENT) if polling else ("RUNNING", ACCENT)
        b_status, b_color = ("POLLING", ACCENT) if polling else ("NO ROW", DIM)
        if not polling:
            a_lease = 1.0 - 0.12 * (1 - abs(((t - 0.55) / 0.45) * 2 - 1))
    elif beat == 6:
        a_status, a_color = "KILLED", RED
        a_lease, a_lease_color = 1.0 - ease(span(t, 0.15, 0.9)), AMBER
    elif beat >= 7 or final:
        a_status, a_color, a_lease = "KILLED", RED, 0.0
        if beat == 7 and t > 0.62 or beat > 7 or final:
            done = beat > 7 or final or t > 0.9
            b_status, b_color = ("SUCCEEDED", GREEN) if done else ("RUNNING", ACCENT)
            b_lease = None if done else 1.0
    worker(f, WORKER_A, "Worker A", "large tier", a_status, a_color, a_lease, a_lease_color,
           style["a"], killed=beat >= 6 or final)
    worker(f, WORKER_B, "Worker B", "large tier", b_status, b_color, b_lease, style=style["b"])

    # Task 2c91: short prompt, small tier, one retry.
    pos, note, tier, color = SLOT["client"], "queued", None, ACCENT
    if beat == 0:
        pos = lerp(SLOT["client"], SLOT["api"], ease(span(t, 0.05, 0.45)))
    elif beat == 1:
        pos = lerp(SLOT["api"], SLOT["queue"], ease(span(t, 0.55, 0.75)))
        pos = lerp(pos, SLOT["small"], ease(span(t, 0.8, 0.98)))
        tier = "SMALL" if t > 0.5 else None
        note = "attempt 1" if t > 0.8 else "queued"
    elif beat == 2:
        pos, tier = lerp(SLOT["small"], SLOT["queue"], ease(span(t, 0.38, 0.65))), "SMALL"
        remaining = 0.8 * (1 - span(t, 0.65, 1.0))
        note, color = (f"retry in {remaining:.1f} s" if t > 0.38 else "attempt 1"), (
            AMBER if t > 0.38 else RED)
    elif beat == 3:
        pos, tier = lerp(SLOT["queue"], SLOT["small"], ease(span(t, 0.12, 0.45))), "SMALL"
        note = "attempt 2" if t > 0.12 else "retry in 0.0 s"
        color = GREEN if t > 0.85 else ACCENT
    else:
        pos, note, tier, color = SLOT["small"], "attempt 2 · done", "SMALL", GREEN
    card(f, pos, "task 2c91", note, tier, color)

    # Task 7f3a: long prompt, large tier, crash and recovery.
    if beat >= 4 or final:
        pos, note, tier, dim, color = SLOT["client"], "queued", None, False, ACCENT
        if beat == 4:
            pos = lerp(SLOT["client"], SLOT["api"], ease(span(t, 0.0, 0.22)))
            pos = lerp(pos, SLOT["queue"], ease(span(t, 0.62, 0.95)))
            tier = "LARGE" if t > 0.3 else None
        elif beat == 5:
            pos, tier = lerp(SLOT["queue"], SLOT["a"], ease(span(t, 0.55, 0.9))), "LARGE"
            note = "attempt 1" if t > 0.55 else "queued"
        elif beat == 6:
            pos, tier, note, dim = SLOT["a"], "LARGE", "attempt 1", True
        elif beat == 7:
            tier = "LARGE"
            if t < 0.62:
                pos, note = lerp(SLOT["a"], SLOT["queue"], ease(span(t, 0.28, 0.6))), "queued"
            else:
                pos, note = lerp(SLOT["queue"], SLOT["b"], ease(span(t, 0.62, 0.9))), "attempt 2"
                color = GREEN if t > 0.9 else ACCENT
        else:
            pos, tier, note, color = SLOT["b"], "LARGE", "attempt 2 · done", GREEN
        card(f, pos, "task 7f3a", note, tier, color, dim)

    # Beat-specific overlays
    if beat == 0 and t > 0.5:
        go, back = span(t, 0.52, 0.72), span(t, 0.75, 0.95)
        x = 190 + 35 * ease(go) - 35 * ease(back)
        f.line([(190, 196), (x, 196)], ACCENT, 2, True)
        if back > 0:
            f.arrow(191, 196, "left", ACCENT)
            f.text(30, 218, "retry, same key → same task", 11, mix(BG, ACCENT, ease(span(t, 0.75, 0.9))), "bold")
    if beat == 7 and t < 0.45:
        f.text(645, 297, "lease expired → requeue", 11, mix(BG, AMBER, ease(span(t, 0, 0.12))), "bold")
    if beat == 8 or beat == 9 or final:
        travel = 1.0 if beat > 8 or final else ease(span(t, 0.0, 0.35))
        # The dead worker's write travels back to the queue along an L-shaped path.
        path = [(1020, 300), (790, 300), (790, 272), (766, 272)]
        f.line(partial_path(path, travel), RED, 2, True, t * 3)
        if travel >= 1:
            f.arrow(768, 272, "left", RED)
            level = 1.0 if beat > 8 or final else ease(span(t, 0.35, 0.48))
            f.pill(QUEUE[0] + QUEUE[2] - 172, QUEUE[1] - 28, "STALE WRITE REJECTED", RED, 10, True, level)

    # Saved evidence: values light up in the last beat.
    fill, border, tag_color = style["evidence"]
    f.rect(EVIDENCE, fill, border, 2 if border != LINE else 1)
    f.text(EVIDENCE[0] + 14, EVIDENCE[1] + 11, "SAVED EVIDENCE · BENCH/RESULTS · SERVED READ-ONLY", 11,
           tag_color, "bold")
    shown = 1.0 if final else (ease(span(t, 0.4, 0.75)) if beat == 9 else 0.0)
    column = (EVIDENCE[2] - 28) / 3
    for i, (label, value) in enumerate(EVIDENCE_ITEMS):
        x = EVIDENCE[0] + 14 + i * column
        f.text(x, EVIDENCE[1] + 34, label, 12, MUTED)
        f.text(x, EVIDENCE[1] + 56, value if shown > 0 else "saved report", 15,
               mix(DIM, GREEN, shown) if shown > 0 else DIM, "bold")

    caption, code, explanation, _ = BEATS[beat]
    if final:
        caption = "Route by cost, retry with backoff, recover with leases, fence with attempts"
        code = "Two tasks, two tiers, one retry, one crash"
        explanation = "No duplicate work and no lost task"
    f.rect(DETAIL, PANEL, LINE, 1)
    f.text(DETAIL[0] + 16, DETAIL[1] + 18, code, 13, ACCENT, "mono")
    f.text(DETAIL[0] + 16, DETAIL[1] + 52, explanation, 14, MUTED)

    f.rect((30, 558, 1140, 1), LINE, radius=0)
    count = f"{beat + 1:02d} / {len(BEATS):02d}" if not final else "Orbit"
    f.text(30, 579, count, 13, ACCENT, "bold")
    f.text(105 if not final else 88, 575, caption, 19, TEXT, "bold")
    legend = "Solid: tasks   Dashed: reads and control"
    f.text(1170 - f.width(legend, 11), 581, legend, 11, DIM)
    return f.image.resize((W, H), Image.LANCZOS)


def main():
    frames = []
    for beat, (*_, seconds) in enumerate(BEATS):
        count = round(seconds * FPS)
        frames += [draw_frame(beat, i / (count - 1)) for i in range(count)]
        print(f"beat {beat + 1}/{len(BEATS)}", flush=True)
    final = draw_frame(len(BEATS) - 1, 1.0, final=True)
    frames += [final] * round(HOLD_SECONDS * FPS)
    # Loop seam: fade out to the empty canvas, then fade the opening frame in.
    blank, fade = Image.new("RGB", (W, H), BG), round(FADE_SECONDS * FPS)
    frames += [Image.blend(final, blank, ease((i + 1) / fade)) for i in range(fade)]
    frames[:fade] = [Image.blend(blank, frames[i], ease((i + 1) / fade)) for i in range(fade)]

    # One shared palette keeps colors stable between frames (no flicker).
    picks = frames[:: max(1, len(frames) // 16)][:16]
    sample = Image.new("RGB", (W, H * len(picks)))
    for i, frame in enumerate(picks):
        sample.paste(frame, (0, i * H))
    palette = sample.quantize(colors=176, method=Image.Quantize.MEDIANCUT)
    quantized = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
    quantized[0].save(ROOT / "orbit-flow.gif", save_all=True, append_images=quantized[1:],
                      duration=round(1000 / FPS), loop=0, optimize=True, disposal=1)
    final.save(ROOT / "orbit-flow.png", optimize=True)
    size = (ROOT / "orbit-flow.gif").stat().st_size / 1024 / 1024
    print(f"{len(frames) / FPS:.1f}s, {len(frames)} frames, {size:.2f} MiB")


if __name__ == "__main__":
    main()
