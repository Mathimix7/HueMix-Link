"""Flask server with blueprints."""
from flask import Flask
from servers.blueprints import main_bp, buttons_bp, api_bp, gateways_bp, lightstrips_bp, bridge_bp, overview_bp, admin_bp, ota_bp
from servers.blueprints.mesh import mesh_bp

app = Flask(
    __name__,
    template_folder='templates',
    static_folder='static'
)

# Register blueprints
app.register_blueprint(main_bp)
app.register_blueprint(buttons_bp)
app.register_blueprint(api_bp)
app.register_blueprint(gateways_bp)
app.register_blueprint(lightstrips_bp)
app.register_blueprint(bridge_bp)
app.register_blueprint(overview_bp)
app.register_blueprint(mesh_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(ota_bp)