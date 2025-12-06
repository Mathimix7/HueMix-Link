"""Overview routes blueprint for rooms, lights, and scenes."""
from flask import Blueprint, render_template

overview_bp = Blueprint('overview', __name__)


@overview_bp.route('/rooms')
def rooms():
    """Render the rooms overview page."""
    return render_template('rooms.html')


@overview_bp.route('/lights')
def lights():
    """Render the lights overview page."""
    return render_template('lights.html')


@overview_bp.route('/scenes')
def scenes():
    """Render the scenes overview page."""
    return render_template('scenes.html')
