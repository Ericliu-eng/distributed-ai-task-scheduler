"""Render the README flow animation offline (requires Pillow).

The animation follows one task through routing, a SKIP LOCKED claim, a worker
crash, lease recovery, and a fenced stale write. It is an illustration of the
implemented protocol, not a recording; the only numbers shown come from the
code defaults and the saved benchmark reports in bench/results/.

Colors match the dashboard tokens in app/static/styles.css.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
W, H, S, FPS = 1200, 675, 2, 24  # S: supersampling factor for smooth edges
FADE_SECONDS = 0.3  # crossfade between beats so state changes never snap

BG, PANEL, SOFT, LINE = "#f7f8fa", "#ffffff", "#f3f5f8", "#d9dee6"
TEXT, MUTED, DIM, WIRE = "#202735", "#626d7d", "#9aa4b2", "#b7c0cc"
ACCENT, GREEN, AMBER, RED = "#365ec9", "#26734d", "#94611d", "#b33b3b"
TINT = {ACCENT: "#eef3fd", GREEN: "#e9f4ee", AMBER: "#fbf3e6", RED: "#fbeeee"}

CLIENT, GATEWAY, QUEUE = (40, 190, 160, 150), (240, 190, 200, 150), (490, 180, 260, 165)
WORKER_A, WORKER_B, MONITOR = (835, 115, 320, 150), (835, 295, 320, 150), (490, 385, 260, 85)
SLOT = {"client": (120, 308), "gateway": (340, 308), "queue": (620, 310),
        "a": (995, 236), "b": (995, 416)}

BEATS = [  # (caption, detail line 1, detail line 2, seconds)
    ("A client submits a task", "POST /tasks  idempotency_key=report-7",
     "A retry with the same key returns the same task", 2.8),
    ("The router picks a tier and records why", "difficulty = len(prompt) / 1200 = 0.74",
     "0.74 ≥ 0.55 threshold  →  LARGE tier", 2.8),
    ("Exactly one worker claims it", "SELECT ... FOR UPDATE SKIP LOCKED",
     "Attempt 1 gets a 15 s lease, renewed every 3 s", 3.2),
    ("The worker is killed mid-task", "SIGKILL: no cleanup code runs",
     "Nobody renews the lease, so it runs out", 2.8),
    ("The monitor recovers the task", "lease_until < now()  →  status = queued",
     "Worker B claims attempt 2 and completes it", 3.6),
    ("The late write from attempt 1 is fenced", "UPDATE ... WHERE attempt = 1 AND status = 'running'",
     "0 rows match, so the stale result is rejected", 3.6),
]
HOLD_SECONDS = 2.6
PROOF = ["1,000 tasks  ·  0 duplicate claims", "20 / 20 crashed tasks recovered",
         "Retries: 1–30 s exponential backoff"]
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
    """Blend two hex colors; used to fade elements in without a second frame."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


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
                  "left": [(8, -5), (0, 0), (8, 5)]}
        self.d.polygon([((x + dx) * S, (y + dy) * S) for dx, dy in shapes[direction]], fill=fill)

    def pill(self, x, y, label, color, size=11, filled=False, level=1.0):
        w = self.width(label, size, "bold") + 16
        fill = mix(BG, color, level) if filled else PANEL
        ink = mix(BG, PANEL, level) if filled else mix(PANEL, color, level)
        self.rect((x, y, w, size + 10), fill, mix(BG, color, level), 1, radius=(size + 10) // 2)
        self.text(x + 8, y + 3, label, size, ink, "bold")
        return w


def look(color):
    """Fill, border, and tag color for a highlight color, or the resting look for None."""
    return (TINT[color], color, color) if color else (PANEL, LINE, ACCENT)


def blend_look(previous, current, k):
    a, b = look(previous), look(current)
    return tuple(mix(x, y, k) for x, y in zip(a, b))


def node(f, box, title, detail, tag=None, style=None):
    fill, border, tag_color = style or look(None)
    f.rect(box, fill, border, 2 if border != LINE else 1)
    x, y, _, _ = box
    if tag:
        f.text(x + 14, y + 12, tag, 11, tag_color, "bold")
    f.text(x + 14, y + (30 if tag else 14), title, 20, TEXT, "bold")
    f.text(x + 14, y + (58 if tag else 42), detail, 14, MUTED)


def card(f, center, label, attempt, tier=None, color=ACCENT, dim=False):
    cx, cy = center
    f.rect((cx - 76, cy - 23, 156, 52), "#e3e8ef", radius=8)  # soft drop shadow
    f.rect((cx - 78, cy - 26, 156, 52), SOFT if dim else PANEL, DIM if dim else color, 2, radius=8)
    f.text(cx - 66, cy - 20, label, 14, DIM if dim else TEXT, "bold")
    f.text(cx - 66, cy + 4, attempt, 12, DIM if dim else MUTED, "mono")
    if tier:
        tw = f.width(tier, 10, "bold") + 12
        f.rect((cx + 66 - tw, cy - 17, tw, 16), DIM if dim else color, radius=8)
        f.text(cx + 72 - tw, cy - 16, tier, 10, PANEL, "bold")


def worker(f, box, name, status, status_color, lease=None, lease_color=ACCENT, style=None,
           killed=False):
    node(f, box, name, "" if killed else "large tier", style=style)
    if killed:
        f.text(box[0] + 14, box[1] + 44, "SIGKILL · process gone", 13, RED, "mono")
    x, y, w, _ = box
    f.pill(x + w - f.width(status, 11, "bold") - 30, y + 14, status, status_color)
    if lease is not None:
        f.text(x + 14, y + 70, "lease", 12, MUTED, "mono")
        f.rect((x + 62, y + 73, 150, 8), "#e7ebf0", radius=4)
        if lease > 0.01:
            f.rect((x + 62, y + 73, 150 * lease, 8), lease_color, radius=4)


def highlights(beat, t, final=False):
    lit = dict.fromkeys(("client", "gateway", "queue", "monitor", "a", "b"))
    if final:
        lit.update(b=GREEN)
        return lit
    if beat == 0:
        lit.update(client=ACCENT if t < 0.5 else None, gateway=ACCENT)
    elif beat == 1:
        lit.update(gateway=ACCENT, queue=ACCENT)
    elif beat == 2:
        lit.update(queue=ACCENT, a=ACCENT, b=ACCENT if t < 0.55 else None)
    elif beat == 3:
        lit.update(a=RED)
    elif beat == 4:
        lit.update(monitor=AMBER if t < 0.45 else None, queue=ACCENT,
                   b=(GREEN if t > 0.9 else ACCENT) if t > 0.62 else None)
    else:
        lit.update(a=RED if t < 0.6 else None, b=GREEN)
    return lit


def draw_frame(beat, t, final=False):
    f = Frame()
    # Header
    f.rect((40, 30, 5, 30), ACCENT, radius=2)
    f.text(56, 26, "Orbit", 30, TEXT, "bold")
    f.text(146, 38, "Lease-based recovery: one task, one crash, no duplicate work", 17, MUTED)
    tag = "ILLUSTRATED FLOW"
    f.text(1160 - f.width(tag, 12, "bold"), 40, tag, 12, DIM, "bold")

    # Static wiring
    f.line([(200, 260), (240, 260)]); f.arrow(240, 260, "right")
    f.line([(440, 260), (490, 260)]); f.arrow(490, 260, "right")
    f.line([(750, 236), (790, 236), (790, 190), (835, 190)]); f.arrow(835, 190, "right")
    f.line([(750, 300), (790, 300), (790, 370), (835, 370)]); f.arrow(835, 370, "right")

    # Highlights ease in from the previous beat's final state instead of snapping.
    k = ease(span(t, 0, FADE_SECONDS / BEATS[beat][3])) if beat and not final else 1.0
    now, before = highlights(beat, t, final), highlights(beat - 1, 1.0) if beat else {}
    style = {key: blend_look(before.get(key), now.get(key), k) for key in now}

    scanning = now["monitor"] is not None
    f.line([(620, 385), (620, 347)], AMBER if scanning else WIRE, 2, True, t * 4)
    f.arrow(620, 347, "up", AMBER if scanning else WIRE)

    node(f, CLIENT, "Client", "POST /tasks", style=style["client"])
    node(f, GATEWAY, "FastAPI", "router · idempotency", "API", style=style["gateway"])
    node(f, QUEUE, "Task queue", "status · priority · lease_until", "POSTGRESQL", style=style["queue"])
    node(f, MONITOR, "Recovery monitor", "scans leases every 2 s", style=style["monitor"])

    # Worker status text and lease bars
    a_status, a_color, a_lease, a_lease_color = "IDLE", DIM, None, ACCENT
    b_status, b_color, b_lease = "IDLE", DIM, None
    if beat == 2:
        polling = t < 0.55
        a_status, a_color = ("POLLING", ACCENT) if polling else ("RUNNING", ACCENT)
        b_status, b_color = ("POLLING", ACCENT) if polling else ("NO ROW", DIM)
        if not polling:
            # The renewal pulse: the lease drains slightly, then a renewal refills it.
            a_lease = 1.0 - 0.12 * (1 - abs(((t - 0.55) / 0.45) * 2 - 1))
    elif beat == 3:
        a_status, a_color = "KILLED", RED
        a_lease, a_lease_color = 1.0 - ease(span(t, 0.15, 0.9)), AMBER
    elif beat >= 4:
        a_status, a_color, a_lease = "KILLED", RED, 0.0
        if beat == 4 and t > 0.62 or beat == 5 or final:
            done = beat == 5 or final or t > 0.9
            b_status, b_color = ("SUCCEEDED", GREEN) if done else ("RUNNING", ACCENT)
            b_lease = None if done else 1.0
    worker(f, WORKER_A, "Worker A", a_status, a_color, a_lease, a_lease_color, style["a"],
           killed=beat >= 3)
    worker(f, WORKER_B, "Worker B", b_status, b_color, b_lease, style=style["b"])

    # The task card's journey
    pos, attempt, tier, dim, color = SLOT["client"], "queued", None, False, ACCENT
    if beat == 0:
        pos = lerp(SLOT["client"], SLOT["gateway"], ease(span(t, 0.05, 0.5)))
    elif beat == 1:
        pos = lerp(SLOT["gateway"], SLOT["queue"], ease(span(t, 0.4, 0.95)))
        tier = "LARGE" if t > 0.2 else None
    elif beat == 2:
        pos, tier = lerp(SLOT["queue"], SLOT["a"], ease(span(t, 0.55, 0.9))), "LARGE"
        attempt = "attempt 1" if t > 0.55 else "queued"
    elif beat == 3:
        pos, tier, attempt, dim = SLOT["a"], "LARGE", "attempt 1", True
    elif beat == 4:
        tier = "LARGE"
        if t < 0.62:
            pos, attempt = lerp(SLOT["a"], SLOT["queue"], ease(span(t, 0.28, 0.6))), "queued"
        else:
            pos, attempt = lerp(SLOT["queue"], SLOT["b"], ease(span(t, 0.62, 0.9))), "attempt 2"
            color = GREEN if t > 0.9 else ACCENT
    else:
        pos, tier, attempt, color = SLOT["b"], "LARGE", "attempt 2", GREEN
    if final:
        attempt = "attempt 2 · done"
    card(f, pos, "task 7f3a", attempt, tier, color=color, dim=dim)

    # Beat-specific overlays
    if beat == 0 and t > 0.5:
        # A duplicate submission with the same key comes back as the same task.
        go, back = span(t, 0.52, 0.72), span(t, 0.75, 0.95)
        x = 200 + 40 * ease(go) - 40 * ease(back)
        f.line([(200, 290), (x, 290)], ACCENT, 2, True)
        if back > 0:
            f.arrow(201, 290, "left", ACCENT)
            f.text(CLIENT[0], CLIENT[1] + CLIENT[3] + 8, "retry, same key → same task", 12,
                   mix(BG, ACCENT, ease(span(t, 0.75, 0.9))), "bold")
    if beat == 1:
        f.pill(GATEWAY[0], GATEWAY[1] - 30, "0.74 ≥ 0.55 → LARGE", ACCENT, 11, True,
               ease(span(t, 0.08, 0.3)))
    if beat == 4 and t < 0.45:
        f.text(636, 360, "lease expired → requeue", 12, mix(BG, AMBER, ease(span(t, 0, 0.12))), "bold")
    if beat == 5 or final:
        travel = 1.0 if final else ease(span(t, 0.0, 0.35))
        x = WORKER_A[0] + (QUEUE[0] + QUEUE[2] + 6 - WORKER_A[0]) * travel
        f.line([(WORKER_A[0], 214), (x, 214)], RED, 2, True, t * 3)
        if travel >= 1:
            f.arrow(QUEUE[0] + QUEUE[2] + 2, 214, "left", RED)
            level = 1.0 if final else ease(span(t, 0.35, 0.48))
            f.pill(QUEUE[0] + QUEUE[2] - 186, QUEUE[1] - 30, "STALE WRITE REJECTED", RED, 11, True, level)

    # Proof chips (final beat): real numbers from bench/results/ and code defaults.
    if beat == 5 or final:
        x = 40
        for index, label in enumerate(PROOF):
            level = 1.0 if final else ease(span(t, 0.45 + index * 0.13, 0.6 + index * 0.13))
            w = f.width(label, 15, "bold") + 44
            if level > 0:
                f.rect((x, 500, w, 44), mix(BG, PANEL, level), mix(BG, LINE, level), 1, radius=22)
                f.d.ellipse(((x + 16) * S, 518 * S, (x + 24) * S, 526 * S), fill=mix(BG, GREEN, level))
                f.text(x + 32, 511, label, 15, mix(BG, TEXT, level), "bold")
            x += w + 14

    # Detail panel (technical) and caption (plain language)
    caption, line1, line2, _ = BEATS[beat]
    if final:
        caption = "Leases recover lost work; attempt numbers fence stale writes"
        line1 = line2 = ""
    if line1:
        f.rect((40, 385, 410, 85), PANEL, LINE, 1)
        f.text(56, 399, line1, 14, ACCENT, "mono")
        f.text(56, 430, line2, 15, MUTED)
    f.rect((40, 588, 1120, 1), LINE, radius=0)
    count = f"{beat + 1:02d} / {len(BEATS):02d}" if not final else "Orbit"
    f.text(40, 610, count, 14, ACCENT, "bold")
    f.text(120 if not final else 100, 606, caption, 20, TEXT, "bold")
    legend = "Solid: data   Dashed: control"
    f.text(1160 - f.width(legend, 12), 612, legend, 12, DIM)
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
    picks = frames[:: max(1, len(frames) // 12)][:12]
    sample = Image.new("RGB", (W, H * len(picks)))
    for i, frame in enumerate(picks):
        sample.paste(frame, (0, i * H))
    palette = sample.quantize(colors=160, method=Image.Quantize.MEDIANCUT)
    quantized = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
    quantized[0].save(ROOT / "orbit-flow.gif", save_all=True, append_images=quantized[1:],
                      duration=round(1000 / FPS), loop=0, optimize=True, disposal=1)
    final.save(ROOT / "orbit-flow.png", optimize=True)
    size = (ROOT / "orbit-flow.gif").stat().st_size / 1024 / 1024
    print(f"{len(frames) / FPS:.1f}s, {len(frames)} frames, {size:.2f} MiB")


if __name__ == "__main__":
    main()
