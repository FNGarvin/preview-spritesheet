"""
Generate an animated GIF from a pixel-art RPG spritesheet formatted as a
4×4 grid of equal-sized frames:

  Row 0 (frames  0– 3): walk-down  + pick-up action
  Row 1 (frames  4– 7): walk-left  + jump-left
  Row 2 (frames  8–11): walk-right + jump-right
  Row 3 (frames 12–15): walk-up    + dead/lie-down

The character walks a few paces up, left, right, then down (last), pausing
briefly between directions, then performs the pick-up action and finally
collapses into the dead/lie-down frame.
"""

import sys
import argparse
from pathlib import Path
from PIL import Image

# ── Spritesheet frame indices ────────────────────────────────────────────────
WALK_FRAMES = {
    "up":    [12, 13, 14, 13],
    "left":  [ 4,  5,  6,  5],
    "right": [ 8,  9, 10,  9],
    "down":  [ 0,  1,  2,  1],   # down comes last
}
IDLE_FRAME   = {"up": 13, "left": 5, "right": 9, "down": 1}
WALK_ORDER   = ["up", "left", "right", "down"]
PICKUP_FRAME = 3
DEAD_FRAME   = 15

# ── Timing (milliseconds) ────────────────────────────────────────────────────
WALK_MS  = 100    # delay per walk animation frame  (~10 fps)
PAUSE_MS = 600    # idle hold between walk directions
DEAD_MS  = 2500   # hold on the final dead frame

# ── Layout ───────────────────────────────────────────────────────────────────
CANVAS_PX     = 320   # square output canvas (display pixels)
DISPLAY_FRAME = 128   # each sprite frame rendered at this size (square)
STEP_PX       = 6     # display pixels the character moves per animation frame

DEFAULT_CYCLES = 3    # walk animation cycles per direction

# Walk-direction movement vectors (display pixels per animation frame)
DELTA = {
    "up":    ( 0, -STEP_PX),
    "left":  (-STEP_PX,  0),
    "right": ( STEP_PX,  0),
    "down":  ( 0,  STEP_PX),
}


# ── Image helpers ────────────────────────────────────────────────────────────

def remove_bg(frame: Image.Image, threshold: int) -> Image.Image:
    """
    Two-pass background removal:
    Pass 1 – flood-fill from all four corners to hard-remove connected background.
    Pass 2 – fade the alpha of fringe pixels (opaque pixels adjacent to transparent
              that are close to the background colour).  This eliminates the halo
              that appears on dark canvases without downscaling or losing detail.
    """
    img = frame.convert("RGBA")
    px  = img.load()
    w, h = img.size

    # Average background colour across all four corners for robustness.
    corners = [px[0, 0][:3], px[w-1, 0][:3], px[0, h-1][:3], px[w-1, h-1][:3]]
    br = sum(c[0] for c in corners) // 4
    bg = sum(c[1] for c in corners) // 4
    bb = sum(c[2] for c in corners) // 4

    def cdist(r, g, b):
        return ((r - br) ** 2 + (g - bg) ** 2 + (b - bb) ** 2) ** 0.5

    # Pass 1: flood-fill connected background from all four corners.
    seen: set[tuple[int, int]] = set()
    stack = [(0, 0), (w-1, 0), (0, h-1), (w-1, h-1)]
    while stack:
        x, y = stack.pop()
        if not (0 <= x < w and 0 <= y < h) or (x, y) in seen:
            continue
        seen.add((x, y))
        r, g, b, a = px[x, y]
        if cdist(r, g, b) <= threshold:
            px[x, y] = (r, g, b, 0)
            stack += [(x+1, y), (x-1, y), (x, y+1), (x, y-1)]

    # Pass 2: soft-erode the fringe.  Any opaque pixel adjacent to a transparent
    # pixel that falls within [threshold, fringe_limit] colour-distance of the
    # background has its alpha faded linearly from 0 (at the flood-fill boundary)
    # to full (at fringe_limit).  Composited on black this produces a smooth dark
    # fade instead of a bright halo, while preserving full source resolution.
    fringe_limit = threshold * 2.5
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            if not any(
                0 <= nx < w and 0 <= ny < h and px[nx, ny][3] == 0
                for nx, ny in ((x-1, y), (x+1, y), (x, y-1), (x, y+1))
            ):
                continue
            d = cdist(r, g, b)
            if threshold < d < fringe_limit:
                t = (d - threshold) / (fringe_limit - threshold)
                px[x, y] = (r, g, b, round(a * t))

    return img


def load_sprites(sheet: Image.Image, threshold: int) -> list[Image.Image]:
    """Extract all 16 frames, remove background, scale to DISPLAY_FRAME px."""
    w, h = sheet.size
    if w != h:
        raise ValueError(f"Spritesheet must be square; got {w}×{h}")
    if w % 4 != 0:
        raise ValueError(f"Spritesheet width ({w}) must be divisible by 4")

    fp       = w // 4
    resample = Image.NEAREST if DISPLAY_FRAME >= fp else Image.LANCZOS

    sprites = []
    for idx in range(16):
        col, row = idx % 4, idx // 4
        x0, y0   = col * fp, row * fp
        raw = sheet.crop((x0, y0, x0 + fp, y0 + fp)).convert("RGBA")
        raw = remove_bg(raw, threshold)
        sprites.append(raw.resize((DISPLAY_FRAME, DISPLAY_FRAME), resample))
    return sprites


def composite(sprite: Image.Image, cx: int, cy: int) -> Image.Image:
    """Paste *sprite* centred at (cx, cy) on a black canvas; return RGB."""
    canvas = Image.new("RGBA", (CANVAS_PX, CANVAS_PX), (0, 0, 0, 255))
    sw, sh  = sprite.size
    canvas.paste(sprite, (cx - sw // 2, cy - sh // 2), sprite)
    return canvas.convert("RGB")


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


# ── Animation builder ────────────────────────────────────────────────────────

def build_frames(sprites: list[Image.Image], cycles: int) -> list[tuple[Image.Image, int]]:
    """Return list of (RGB Image, delay_ms) pairs for the full animation."""
    half = DISPLAY_FRAME // 2
    edge = half + 4          # minimum centre-to-canvas-edge distance
    lo   = edge
    hi   = CANVAS_PX - edge

    cx = cy = CANVAS_PX // 2
    frames: list[tuple[Image.Image, int]] = []

    for direction in WALK_ORDER:
        dx, dy = DELTA[direction]

        for _ in range(cycles):
            for fidx in WALK_FRAMES[direction]:
                cx = clamp(cx + dx, lo, hi)
                cy = clamp(cy + dy, lo, hi)
                frames.append((composite(sprites[fidx], cx, cy), WALK_MS))

        # Brief idle pause between directions
        frames.append((composite(sprites[IDLE_FRAME[direction]], cx, cy), PAUSE_MS))

    # Pick-up action
    frames.append((composite(sprites[PICKUP_FRAME], cx, cy), PAUSE_MS))

    # Short idle beat so the transition to dead reads as a separate moment
    frames.append((composite(sprites[IDLE_FRAME["down"]], cx, cy), PAUSE_MS // 2))

    # Dead / lie-down — held for a long pause before the GIF loops
    frames.append((composite(sprites[DEAD_FRAME], cx, cy), DEAD_MS))

    return frames


# ── CLI entry point ──────────────────────────────────────────────────────────

def main() -> None:
    prog = Path(sys.argv[0]).name

    parser = argparse.ArgumentParser(
        prog=prog,
        description="Generate an animated GIF from a pixel-art RPG spritesheet (4×4 frame grid).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Spritesheet frame layout (4 rows × 4 columns of equal-sized frames):\n"
            "  Row 0 (frames  0-3 ): walk-down  + pick-up action\n"
            "  Row 1 (frames  4-7 ): walk-left  + jump-left\n"
            "  Row 2 (frames  8-11): walk-right + jump-right\n"
            "  Row 3 (frames 12-15): walk-up    + dead / lie-down\n"
            "\n"
            "Output filename defaults to the input filename with a .gif extension."
        ),
    )
    parser.add_argument("spritesheet", help="Input spritesheet image (any size, must be square)")
    parser.add_argument("-o", "--output", metavar="FILE", help="Output GIF path (default: <input>.gif)")
    parser.add_argument(
        "-t", "--threshold", type=int, default=30, metavar="N",
        help="Background-removal colour tolerance 0-255 (default: 30)",
    )
    parser.add_argument(
        "-c", "--cycles", type=int, default=DEFAULT_CYCLES, metavar="N",
        help=f"Walk animation cycles per direction (default: {DEFAULT_CYCLES})",
    )
    args = parser.parse_args()

    in_path  = Path(args.spritesheet)
    out_path = Path(args.output) if args.output else in_path.with_suffix(".gif")

    if not in_path.is_file():
        parser.error(f"file not found: {args.spritesheet}")

    try:
        sheet = Image.open(in_path).convert("RGBA")
    except Exception as exc:
        parser.error(f"cannot open image: {exc}")

    w, h = sheet.size
    print(f"{prog}: {in_path.name}  ({w}×{h} sheet, {w // 4}×{h // 4} px per frame)")

    try:
        sprites = load_sprites(sheet, args.threshold)
    except ValueError as exc:
        parser.error(str(exc))

    frames = build_frames(sprites, args.cycles)

    images = [f[0] for f in frames]
    delays = [f[1] for f in frames]

    images[0].save(
        out_path,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=delays,
        loop=0,
        optimize=False,
    )
    print(f"{prog}: wrote {len(images)}-frame GIF → {out_path}")


if __name__ == "__main__":
    main()
