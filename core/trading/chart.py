"""
Renders a single PNG "overview" image of the whole watchlist's most recent
price action + signal direction — sent to Telegram once per scan cycle so
you can see what the bot is actually looking at, not just its trade alerts.
Headless rendering (Agg backend) — no display needed.
"""
import io
import math

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BG = "#0d1117"


def render_watchlist_overview(snapshots: dict) -> bytes:
    """
    snapshots: {symbol: {"closes": [float,...], "direction": str,
                          "confidence": float, "rsi": float, "checklist_ok": bool}}
    Returns PNG bytes, or b"" if there's nothing to draw.
    """
    symbols = [s for s, snap in snapshots.items() if snap.get("closes")]
    if not symbols:
        return b""

    cols = 5
    rows = math.ceil(len(symbols) / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(cols * 2.6, rows * 1.9))
    axes = axes.flatten()

    for ax, symbol in zip(axes, symbols):
        snap = snapshots[symbol]
        closes     = snap.get("closes") or []
        direction  = snap.get("direction", "neutral")
        confidence = snap.get("confidence", 0)
        checklist_ok = snap.get("checklist_ok", False)

        color = "#2ecc71" if direction == "long" else "#e74c3c" if direction == "short" else "#888888"
        ax.plot(closes, color=color, linewidth=1.2)
        mark = " ✓" if checklist_ok else ""
        ax.set_title(f"{symbol}\n{direction.upper()} {confidence:.0%}{mark}",
                     fontsize=7, color=color)
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_facecolor(BG)
        for spine in ax.spines.values():
            spine.set_visible(False)

    for ax in axes[len(symbols):]:
        ax.axis("off")

    fig.patch.set_facecolor(BG)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor(), dpi=110)
    plt.close(fig)
    return buf.getvalue()
