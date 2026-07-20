from __future__ import annotations

from pathlib import Path

import ui_obs5o_prepare as preview

preview.preview.OUTPUT = Path("reports/phase_ui_obs5q")
preview.preview.SNAPSHOT = preview.preview.OUTPUT / "ui_obs5q_snapshot.json"
preview.preview.HTML = preview.preview.OUTPUT / "ui_obs5q_dashboard.html"


if __name__ == "__main__":
    preview.main()
