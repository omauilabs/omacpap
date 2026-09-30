"""Follow the active Omarchy theme.

Omarchy themes define their palette in ~/.config/omarchy/current/theme/colors.toml
(background, foreground, accent, color0-15, ...). OmaCPAP turns that into CSS
custom properties so the dashboard recolors whenever you switch themes.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None  # type: ignore[assignment]

from . import paths

# Tokyo Night — Omarchy's default theme, used when no theme is found.
FALLBACK = {
    "background": "#1a1b26", "foreground": "#a9b1d6", "accent": "#7aa2f7",
    "color0": "#32344a", "color1": "#f7768e", "color2": "#9ece6a", "color3": "#e0af68",
    "color4": "#7aa2f7", "color5": "#ad8ee6", "color6": "#449dab", "color7": "#787c99",
    "color8": "#444b6a", "color9": "#ff7a93", "color10": "#b9f27c", "color11": "#ff9e64",
    "color12": "#7da6ff", "color13": "#bb9af7", "color14": "#0db9d7", "color15": "#acb0d0",
}
HEX = re.compile(r"^#?[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")


def _norm(v: object) -> str | None:
    if isinstance(v, str) and HEX.match(v.strip()):
        v = v.strip()
        return v if v.startswith("#") else "#" + v
    return None


def load_palette(theme_dir: Path | None = None) -> tuple[dict[str, str], bool, str]:
    theme_dir = theme_dir or paths.omarchy_theme_dir()
    palette = dict(FALLBACK)
    name = "Tokyo Night (fallback)"
    f = theme_dir / "colors.toml"
    if f.exists() and tomllib:
        try:
            data = tomllib.loads(f.read_text())
            flat = {**data, **{k: v for sub in data.values() if isinstance(sub, dict) for k, v in sub.items()}}
            for k, v in flat.items():
                c = _norm(v)
                if c:
                    palette[k.lower()] = c
            name = theme_dir.resolve().name
        except (OSError, ValueError):
            pass
    light = (theme_dir / "light.mode").exists()
    return palette, light, name


def css(theme_dir: Path | None = None) -> str:
    p, light, name = load_palette(theme_dir)
    bg, fg = p["background"], p["foreground"]
    accent = p.get("accent") or p["color4"]
    lines = [
        f"/* Omarchy theme: {name} */",
        ":root {",
        f"  color-scheme: {'light' if light else 'dark'};",
        f"  --bg: {bg};",
        f"  --fg: {fg};",
        f"  --accent: {accent};",
        f"  --muted: color-mix(in srgb, {fg} 55%, {bg});",
        f"  --faint: color-mix(in srgb, {fg} 12%, {bg});",
        f"  --panel: color-mix(in srgb, {fg} 5%, {bg});",
        f"  --border: color-mix(in srgb, {fg} 16%, {bg});",
        f"  --good: {p['color2']};",
        f"  --warn: {p['color3']};",
        f"  --bad: {p['color1']};",
        f"  --info: {p['color6']};",
        f"  --violet: {p['color5']};",
    ]
    for i in range(16):
        lines.append(f"  --c{i}: {p.get(f'color{i}', FALLBACK[f'color{i}'])};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def signature(theme_dir: Path | None = None) -> str:
    """Cheap change detector so the UI can re-theme live."""
    theme_dir = theme_dir or paths.omarchy_theme_dir()
    f = theme_dir / "colors.toml"
    try:
        st = f.stat()
        return f"{theme_dir.resolve()}:{st.st_mtime_ns}"
    except OSError:
        return "fallback"
