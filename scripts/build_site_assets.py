#!/usr/bin/env python3
"""Generate the site's icons, web manifest and link-preview card.

Everything the browser and a messaging app need comes from one mark: three
sensors around a hub, one of them lit amber — the network's own picture of
itself. The mark is written here as SVG, then rasterized with headless Chrome
so the PNGs can never drift from it.

    uv run python scripts/build_site_assets.py

Writes into `site/assets/`, which `intelnet export` copies to `docs/assets/`.
Re-run it only when the mark or the card text changes; the outputs are
committed so a normal build needs no browser.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "site" / "assets"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

NAME = "Intelligence Network"
SHORT_NAME = "IntelNet IL"
DESCRIPTION = ("Illinois weather, water, soil, agriculture and air — reported by people "
               "on Telegram, corroborated against official feeds.")

INK = "#14201C"          # radar dark, the mark's ground
GROUND_DARK = "#0F1412"
TEAL = "#5FB4C0"         # the dark-theme accent: it has to hold up on the dark ground
AMBER = "#E0A83A"
PAPER = "#E6ECE7"
MUTED = "#98A69E"


def mark(size: int = 64, *, radius: float = 14, inset: float = 0) -> str:
    """The mark itself. `inset` shrinks the nodes toward the centre, leaving the
    safe zone Android needs when it masks an icon into a circle."""
    s = 1 - inset
    def p(x: float, y: float) -> tuple[float, float]:
        return (32 + (x - 32) * s, 32 + (y - 32) * s)

    hub, top, left, right = p(32, 33), p(32, 15), p(16, 45), p(48, 45)
    line = lambda a, b: f'<path d="M{a[0]:.1f} {a[1]:.1f} L{b[0]:.1f} {b[1]:.1f}"/>'  # noqa: E731
    dot = lambda c, r, fill: f'<circle cx="{c[0]:.1f}" cy="{c[1]:.1f}" r="{r * s:.1f}" fill="{fill}"/>'  # noqa: E731
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="{size}" height="{size}" \
role="img" aria-label="{NAME}">
  <rect width="64" height="64" rx="{radius}" fill="{INK}"/>
  <g stroke="{TEAL}" fill="none" stroke-linecap="round">
    <g opacity=".4" stroke-width="{1.9 * s:.1f}">{line(top, left)}{line(left, right)}{line(right, top)}</g>
    <g opacity=".9" stroke-width="{2.6 * s:.1f}">{line(hub, top)}{line(hub, left)}{line(hub, right)}</g>
  </g>
  {dot(hub, 5.2, TEAL)}
  {dot(left, 4.2, TEAL)}
  {dot(right, 4.2, TEAL)}
  <circle cx="{top[0]:.1f}" cy="{top[1]:.1f}" r="{7.4 * s:.1f}" fill="{AMBER}" opacity=".22"/>
  {dot(top, 5.0, AMBER)}
</svg>"""


CARD = f"""<!doctype html>
<meta charset="utf-8">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400..650\
&family=IBM+Plex+Sans:wght@400;500&family=IBM+Plex+Mono:wght@500&display=swap">
<style>
  * {{ box-sizing: border-box; margin: 0; }}
  body {{ width: 1200px; height: 630px; background: {GROUND_DARK}; color: {PAPER}; overflow: hidden;
          font-family: "IBM Plex Sans", system-ui, sans-serif; position: relative; }}
  .glow {{ position: absolute; width: 900px; height: 900px; right: -320px; top: -380px; border-radius: 50%;
           background: radial-gradient(circle, rgba(95,180,192,.20), rgba(95,180,192,0) 62%); }}
  .grid {{ position: absolute; inset: 0; opacity: .5;
           background-image: linear-gradient(rgba(95,180,192,.06) 1px, transparent 1px),
                             linear-gradient(90deg, rgba(95,180,192,.06) 1px, transparent 1px);
           background-size: 60px 60px; }}
  .wrap {{ position: relative; height: 100%; padding: 66px 84px 70px; display: flex; flex-direction: column;
           justify-content: space-between; }}
  .top {{ display: flex; align-items: center; gap: 30px; }}
  .top svg {{ border-radius: 26px; }}
  h1 {{ font-family: "Fraunces", Georgia, serif; font-variation-settings: "opsz" 120; font-weight: 600;
        font-size: 74px; line-height: 1; letter-spacing: -.02em; }}
  .kicker {{ font-family: "IBM Plex Mono", monospace; font-weight: 500; font-size: 20px; letter-spacing: .16em;
             text-transform: uppercase; color: {TEAL}; margin-bottom: 14px; }}
  .lede {{ font-size: 34px; line-height: 1.32; max-width: 940px; color: {PAPER}; }}
  .lede b {{ color: {AMBER}; font-weight: 500; }}
  .packs {{ display: flex; gap: 14px; }}
  .packs span {{ font-size: 22px; line-height: 1; padding: 13px 20px 15px; border-radius: 999px;
                 border: 1px solid currentColor; }}
  .foot {{ display: flex; align-items: center; gap: 18px; font-size: 24px; color: {MUTED}; }}
  .foot .bot {{ font-family: "IBM Plex Mono", monospace; color: {PAPER}; }}
  .dot {{ width: 7px; height: 7px; border-radius: 50%; background: {MUTED}; }}
</style>
<div class="glow"></div><div class="grid"></div>
<div class="wrap">
  <div class="top">{mark(112, radius=0)}<div><div class="kicker">Illinois</div><h1>{NAME}</h1></div></div>
  <div class="packs"><span style="color:#5FB4C0">Weather</span><span style="color:#7FA6E8">Water</span>
     <span style="color:#C08A5A">Soil</span><span style="color:#8BBF63">Agriculture</span>
     <span style="color:#B1A2E3">Air</span></div>
  <p class="lede">Neighbors report what they see on Telegram. The network <b>corroborates</b> it against
     official feeds and publishes a digest every morning.</p>
  <div class="foot"><span class="bot">@intelligence_network_bot</span><span class="dot"></span>
     <span>county and ZIP-level alerts · free · open source</span></div>
</div>"""


def shoot(html: str, out: Path, width: int, height: int) -> None:
    """Rasterize a page with headless Chrome at exactly width × height."""
    if not CHROME.exists():
        raise SystemExit(f"Chrome not found at {CHROME}")
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "page.html"
        page.write_text(html, encoding="utf-8")
        subprocess.run(
            [str(CHROME), "--headless=new", "--disable-gpu", "--hide-scrollbars",
             "--force-device-scale-factor=1", "--virtual-time-budget=4000",
             f"--window-size={width},{height}", f"--screenshot={out}", page.as_uri()],
            check=True, capture_output=True, timeout=120,
        )


def icon_page(svg: str, size: int) -> str:
    return (f'<!doctype html><meta charset="utf-8"><style>*{{margin:0}}'
            f'body{{width:{size}px;height:{size}px;overflow:hidden}}'
            f'svg{{display:block;width:{size}px;height:{size}px}}</style>{svg}')


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    (ASSETS / "icon.svg").write_text(mark(), encoding="utf-8")

    # Maskable PNGs: square ground, nodes pulled in so a circular mask can't clip them.
    masked = mark(radius=0, inset=0.16)
    for name, size in (("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512)):
        shoot(icon_page(masked, size), ASSETS / name, size, size)
    shoot(CARD, ASSETS / "og.png", 1200, 630)

    (ASSETS / "site.webmanifest").write_text(json.dumps({
        "name": NAME,
        "short_name": SHORT_NAME,
        "description": DESCRIPTION,
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "background_color": GROUND_DARK,
        "theme_color": GROUND_DARK,
        "icons": [
            {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            {"src": "icon.svg", "sizes": "any", "type": "image/svg+xml"},
        ],
    }, indent=2) + "\n", encoding="utf-8")

    for f in sorted(ASSETS.iterdir()):
        print(f"  {f.relative_to(ROOT)}  {f.stat().st_size / 1024:.0f} KB")
    if shutil.which("git"):
        print("\nRun `uv run intelnet export` to copy these into docs/.")


if __name__ == "__main__":
    main()
