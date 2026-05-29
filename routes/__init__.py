"""Flask blueprint registry.

Each module under routes/ defines a `bp` Blueprint at module level.
`register_blueprints(app)` is the single entry point server.py calls to
mount all of them onto the app.

Adding a new blueprint:
    1. Create routes/your_module.py with `bp = Blueprint("your_module", __name__)`
    2. Add `from routes.your_module import bp as bp_your` below
    3. Add `app.register_blueprint(bp_your)` inside register_blueprints()
"""

from routes.telegram import bp as bp_telegram
from routes.summarize import bp as bp_summarize
from routes.memory import bp as bp_memory
from routes.coin import bp as bp_coin
from routes.watchtower import bp as bp_watchtower
from routes.settings import bp as bp_settings
from routes.rules import bp as bp_rules
from routes.github_trending import bp as bp_github_trending


def register_blueprints(app):
    """Register every route blueprint onto the given Flask app."""
    app.register_blueprint(bp_telegram)
    app.register_blueprint(bp_summarize)
    app.register_blueprint(bp_memory)
    app.register_blueprint(bp_coin)
    app.register_blueprint(bp_watchtower)
    app.register_blueprint(bp_settings)
    app.register_blueprint(bp_rules)
    app.register_blueprint(bp_github_trending)
