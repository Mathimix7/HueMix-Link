"""Flask server with blueprints."""
from flask import Flask
from servers.blueprints import main_bp, buttons_bp, api_bp, servers_bp, lightstrips_bp, bridge_bp, overview_bp, rooms_overview_bp

app = Flask(
    __name__,
    template_folder='templates',
    static_folder='static'
)

# Register blueprints
app.register_blueprint(main_bp)
app.register_blueprint(buttons_bp)
app.register_blueprint(api_bp)
app.register_blueprint(servers_bp)
app.register_blueprint(lightstrips_bp)
app.register_blueprint(bridge_bp)
app.register_blueprint(overview_bp)
app.register_blueprint(rooms_overview_bp)
