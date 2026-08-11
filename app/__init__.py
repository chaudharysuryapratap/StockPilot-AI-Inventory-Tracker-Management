from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config, validate_runtime_config


db = SQLAlchemy()


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)
    validate_runtime_config(app.config)
    if app.config.get("TRUST_PROXY_HEADERS"):
        # Gunicorn is bound to loopback in the supplied deployment, so only the
        # local reverse proxy can supply these forwarded values.
        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=1, x_proto=1, x_host=1
        )

    db.init_app(app)

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(self)")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; media-src 'self' blob:; worker-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'",
        )
        if app.config.get("APP_ENV") == "production":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        if response.mimetype == "text/html":
            response.headers.setdefault("Cache-Control", "private, no-store")
        return response

    from app.commands import register_commands
    from app.routes import api_bp, web_bp

    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp, url_prefix="/api")
    register_commands(app)

    if app.config["AUTO_CREATE_SCHEMA"]:
        with app.app_context():
            db.create_all()
            from app.services.identity import ensure_default_identity

            ensure_default_identity()

    return app
