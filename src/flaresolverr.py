import asyncio
import json
import logging
import os
import sys

import certifi
from bottle import Bottle, ServerAdapter, request, response, run

import flaresolverr_service
import utils
from bottle_plugins import prometheus_plugin
from bottle_plugins.error_plugin import error_plugin
from bottle_plugins.logger_plugin import logger_plugin
from dtos import V1RequestBase

env_proxy_url = os.environ.get("PROXY_URL", None)
env_proxy_username = os.environ.get("PROXY_USERNAME", None)
env_proxy_password = os.environ.get("PROXY_PASSWORD", None)


class JSONErrorBottle(Bottle):
    """
    Handle 404 errors
    """

    def default_error_handler(self, res):
        response.content_type = "application/json"
        return json.dumps({"error": res.body, "status_code": res.status_code})


app = JSONErrorBottle()


@app.route("/")
def index():
    """
    Show welcome message
    """
    res = flaresolverr_service.index_endpoint()
    return utils.object_to_dict(res)


@app.route("/health")
def health():
    """
    Healthcheck endpoint.
    This endpoint is special because it doesn't print traces
    """
    res = flaresolverr_service.health_endpoint()
    return utils.object_to_dict(res)


@app.post("/v1")
def controller_v1():
    """
    Controller v1

    The request body is processed asynchronously via ``asyncio.run`` so the
    zendriver-based service layer can stay fully async.
    """
    data = request.json or {}
    if (
        ("proxy" not in data or not data.get("proxy"))
        and env_proxy_url is not None
        and (env_proxy_username is None and env_proxy_password is None)
    ):
        logging.info("Using proxy URL ENV")
        data["proxy"] = {"url": env_proxy_url}
    if (
        ("proxy" not in data or not data.get("proxy"))
        and env_proxy_url is not None
        and (env_proxy_username is not None or env_proxy_password is not None)
    ):
        logging.info("Using proxy URL, username & password ENVs")
        data["proxy"] = {"url": env_proxy_url, "username": env_proxy_username, "password": env_proxy_password}
    req = V1RequestBase(data)
    res = asyncio.run(flaresolverr_service.controller_v1_endpoint(req))
    if res.__error_500__:
        response.status = 500
    return utils.object_to_dict(res)


def main():
    """
    Entry point used by the ``flaresolverr`` console script and by
    ``python -m flaresolverr`` invocations.
    """
    # fix for HEADLESS=false in Windows binary
    # https://stackoverflow.com/a/27694505
    if os.name == "nt":
        import multiprocessing

        multiprocessing.freeze_support()

    # fix ssl certificates for compiled binaries
    # https://github.com/pyinstaller/pyinstaller/issues/7229
    # https://stackoverflow.com/q/55736855
    os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
    os.environ["SSL_CERT_FILE"] = certifi.where()

    # validate configuration
    log_level = os.environ.get("LOG_LEVEL", "info").upper()
    log_file = os.environ.get("LOG_FILE", None)
    server_host = os.environ.get("HOST", "0.0.0.0")
    server_port = int(os.environ.get("PORT", 8191))

    # configure logger
    logger_format = "%(asctime)s %(levelname)-8s %(message)s"
    if log_level == "DEBUG":
        logger_format = "%(asctime)s %(levelname)-8s ReqId %(thread)s %(message)s"
    if log_file:
        log_file = os.path.realpath(log_file)
        log_path = os.path.dirname(log_file)
        os.makedirs(log_path, exist_ok=True)
        logging.basicConfig(
            format=logger_format,
            level=log_level,
            datefmt="%Y-%m-%d %H:%M:%S",
            handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_file)],
        )
    else:
        logging.basicConfig(
            format=logger_format,
            level=log_level,
            datefmt="%Y-%m-%d %H:%M:%S",
            handlers=[logging.StreamHandler(sys.stdout)],
        )

    # disable warning traces from urllib3 / zendriver / websockets
    logging.getLogger("urllib3").setLevel(logging.ERROR)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("zendriver").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    logging.info(f"FlareSolverr {utils.get_flaresolverr_version()}")
    logging.debug("Debug log enabled")

    # Get current OS for global variable
    utils.get_current_platform()

    # test browser installation
    asyncio.run(flaresolverr_service.test_browser_installation())

    # start bottle plugins
    # plugin order is important
    app.install(logger_plugin)
    app.install(error_plugin)
    prometheus_plugin.setup()
    app.install(prometheus_plugin.prometheus_plugin)

    # start webserver
    # default server 'wsgiref' does not support concurrent requests
    # https://github.com/FlareSolverr/FlareSolverr/issues/680
    # https://github.com/Pylons/waitress/issues/31
    class WaitressServerPoll(ServerAdapter):
        def run(self, handler):
            from waitress import serve

            serve(handler, host=self.host, port=self.port, asyncore_use_poll=True)

    try:
        run(app, host=server_host, port=server_port, quiet=True, server=WaitressServerPoll)
    finally:
        # Make sure all active sessions / browsers are closed on shutdown.
        try:
            asyncio.run(flaresolverr_service.SESSIONS_STORAGE.stop_all())
        except Exception as e:
            logging.debug("Error stopping sessions on shutdown: %s", e)


if __name__ == "__main__":
    main()
