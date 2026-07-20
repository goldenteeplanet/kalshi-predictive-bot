from pathlib import Path
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory,init_db
from kalshi_predictor.ui.app import create_app
Path("reports/ui_obs2p").mkdir(parents=True,exist_ok=True)
engine=init_db("sqlite:///reports/ui_obs2p/browser_harness.db")
app=create_app(session_factory=get_session_factory(engine),settings=Settings())
