from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.ui.app import create_app

engine = init_db("sqlite:////tmp/ui_obs5c_browser_harness.db")
app = create_app(session_factory=get_session_factory(engine), settings=Settings())
