from flask import Flask
from flask_sqlalchemy import SQLAlchemy

from config import Config


db = SQLAlchemy()


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)

    db.init_app(app)

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(self)")
        if response.mimetype == "text/html":
            response.headers.setdefault("Cache-Control", "private, no-store")
        return response

    from app.commands import register_commands
    from app.routes import api_bp, web_bp

    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp, url_prefix="/api")
    register_commands(app)

    with app.app_context():
        db.create_all()
        from app.services.identity import ensure_default_identity

        ensure_default_identity()

    return app
