from __future__ import annotations

from pathlib import Path

import ui_obs5m_prepare as preview

preview.OUTPUT = Path("reports/phase_ui_obs5n")
preview.SNAPSHOT = preview.OUTPUT / "ui_obs5n_snapshot.json"
preview.HTML = preview.OUTPUT / "ui_obs5n_dashboard.html"


if __name__ == "__main__":
    preview.main()
