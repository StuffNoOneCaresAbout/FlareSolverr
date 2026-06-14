import asyncio
import contextlib
import json
import logging
import os
import platform
import re
import urllib.parse
from html import escape
from urllib.parse import quote, unquote

import zendriver as zd
from zendriver.cdp import fetch, network

FLARESOLVERR_VERSION = None
PLATFORM_VERSION = None
USER_AGENT = None
XVFB_DISPLAY = None


def get_config_log_html() -> bool:
    return os.environ.get("LOG_HTML", "false").lower() == "true"


def get_config_headless() -> bool:
    return os.environ.get("HEADLESS", "true").lower() == "true"


def get_config_disable_media() -> bool:
    return os.environ.get("DISABLE_MEDIA", "false").lower() == "true"


def get_flaresolverr_version() -> str:
    global FLARESOLVERR_VERSION
    if FLARESOLVERR_VERSION is not None:
        return FLARESOLVERR_VERSION

    package_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "package.json")
    if not os.path.isfile(package_path):
        package_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "package.json")
    with open(package_path) as f:
        FLARESOLVERR_VERSION = json.loads(f.read())["version"]
        return FLARESOLVERR_VERSION


def get_current_platform() -> str:
    global PLATFORM_VERSION
    if PLATFORM_VERSION is not None:
        return PLATFORM_VERSION
    PLATFORM_VERSION = os.name
    return PLATFORM_VERSION


def start_xvfb_display():
    """
    On Linux we run a virtual X server (Xvfb) so the browser has a display.
    Mirrors the legacy selenium/undetected-chromedriver setup that was used
    to make the browser appear non-headless to anti-bot services.
    """
    global XVFB_DISPLAY
    if XVFB_DISPLAY is None:
        from xvfbwrapper import Xvfb

        XVFB_DISPLAY = Xvfb()
        XVFB_DISPLAY.start()


def _build_proxy_args(proxy: dict) -> list:
    """
    Translate the legacy FlareSolverr proxy dict into the Chrome
    ``--proxy-server`` argument. Authenticated proxies cannot be expressed as
    a single command-line flag; callers must wire up the CDP auth handler
    via :func:`install_proxy_auth_handler`.
    """
    if not proxy or "url" not in proxy:
        return []
    parsed = urllib.parse.urlparse(proxy["url"])
    if parsed.username or parsed.password:
        # Strip credentials from the proxy URL passed to Chrome.
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        clean = parsed._replace(netloc=netloc, username=None, password=None)
        return [f"--proxy-server={clean.geturl()}"]
    return [f"--proxy-server={proxy['url']}"]


async def install_proxy_auth_handler(tab, proxy: dict):
    """
    Install a ``Fetch.authRequired`` handler that replies with the proxy
    credentials in the legacy FlareSolverr ``proxy`` dict.
    """
    if not proxy or "url" not in proxy:
        return
    parsed = urllib.parse.urlparse(proxy["url"])
    username = parsed.username
    password = parsed.password
    if not (username and password):
        return

    async def _handler(event: fetch.AuthRequired):
        await tab.send(
            fetch.continue_with_auth(
                request_id=event.request_id,
                auth_challenge_response=fetch.AuthChallengeResponse(
                    response="ProvideCredentials",
                    username=username,
                    password=password,
                ),
            )
        )

    await tab.send(fetch.enable(handle_auth_requests=True))
    tab.add_handler(fetch.AuthRequired, _handler)


class BrowserNavigationError(Exception):
    """Raised when a top-level navigation fails at the network layer.

    Surfaces Chromium's ``net::ERR_*`` codes (e.g. ``ERR_NAME_NOT_RESOLVED``,
    ``ERR_PROXY_CONNECTION_FAILED``) so callers can map them to HTTP 5xx
    responses, mirroring the legacy selenium ``WebDriverException`` behavior.
    """

    def __init__(self, error_text: str, url: str, proxy_url: str | None = None):
        self.error_text = error_text
        self.url = url
        self.proxy_url = proxy_url
        message = f"{error_text} for {url} (proxy={proxy_url})" if proxy_url else f"{error_text} for {url}"
        super().__init__(message)


async def navigate_or_raise(tab, url: str, proxy_url: str | None = None) -> None:
    """Navigate ``tab`` to ``url`` and raise :class:`BrowserNavigationError`
    if the top-level request fails (bad domain, proxy error, TLS, etc.).

    zendriver's ``tab.get`` does not surface network-level failures: Chrome
    simply renders its built-in error page and the call returns successfully.
    We subscribe to ``Network.requestWillBeSent`` / ``Network.loadingFailed``
    for the duration of the navigation to detect the failure and report it
    to the caller.

    ``proxy_url`` is appended to the raised error message so callers can
    distinguish proxy-induced failures from other network errors.
    """
    main_frame_id = getattr(tab, "frame_id", None) or getattr(tab, "_frame_id", None)
    state: dict[str, str | bool | None] = {"request_id": None, "error_text": None, "doc_request_seen": False}

    def _on_request(event, _connection=None):
        # We only care about the top-level document request for our target frame.
        event_type = getattr(event, "type_", None)
        if event_type is None or event_type != network.ResourceType.DOCUMENT:
            return
        if main_frame_id is not None and event.frame_id != main_frame_id:
            return
        if state["request_id"] is None:
            state["request_id"] = event.request_id
            state["doc_request_seen"] = True

    def _on_failed(event, _connection=None):
        if state["request_id"] is not None and event.request_id != state["request_id"]:
            return
        event_type = getattr(event, "type_", None)
        if event_type is not None and event_type != network.ResourceType.DOCUMENT:
            return
        if state["error_text"] is None:
            error_text = getattr(event, "error_text", None) or "unknown navigation error"
            state["error_text"] = error_text

    tab.add_handler(network.RequestWillBeSent, _on_request)
    tab.add_handler(network.LoadingFailed, _on_failed)
    try:
        await tab.get(url)
    finally:
        tab.remove_handlers(network.RequestWillBeSent, _on_request)
        tab.remove_handlers(network.LoadingFailed, _on_failed)

    if state["error_text"]:
        raise BrowserNavigationError(state["error_text"], url, proxy_url=proxy_url)

    # Defense in depth: Chrome renders a ``<neterror>`` element on the built-in
    # error page. If we see it, the navigation failed even if no
    # ``loadingFailed`` event matched (older Chromium, redirects, etc.).
    try:
        has_neterror = await tab.evaluate("!!document.querySelector('neterror')")
    except Exception:
        has_neterror = False
    if has_neterror:
        raise BrowserNavigationError(
            f"net::ERR_UNKNOWN for {url} (browser error page detected)",
            url,
            proxy_url=proxy_url,
        )


async def get_browser(proxy: dict | None = None) -> zd.Browser:
    """
    Launch a fresh zendriver browser. ``proxy`` follows the legacy
    FlareSolverr shape: ``{"url": "...", "username": "...", "password": "..."}``.
    """
    logging.debug("Launching web browser...")

    # Hide headless markers from anti-bot services when possible.
    use_xvfb = False
    if get_config_headless():
        if os.name == "nt":
            # On Windows there is no Xvfb; rely on zendriver's headless mode.
            pass
        else:
            use_xvfb = True

    if use_xvfb:
        start_xvfb_display()

    # Note: we intentionally do not set ``lang`` here. zendriver's Browser
    # startup code unconditionally calls ``Config.add_argument("--lang=...")``
    # when ``config.lang`` is not None, and that method refuses any argument
    # containing the substring "lang" as a forbidden flag. Passing lang to the
    # constructor still ends up stored on the attribute and re-injected later,
    # so we leave it at the default (en-US,en;q=0.9).
    config = zd.Config(
        headless=False,
        sandbox=False,
        # Disable webgl/webrtc to avoid leaking extra fingerprint data.
        disable_webrtc=True,
        disable_webgl=True,
    )

    # Forward the same stealth-oriented Chrome flags used by the legacy build.
    # Note: zendriver's Config refuses ``--no-sandbox`` via add_argument (it is
    # auto-injected when ``sandbox=False``), so we must not pass it here.
    for arg in (
        "--window-size=1920,1080",
        "--disable-search-engine-choice-screen",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--no-zygote",
        "--ignore-certificate-errors",
        "--ignore-ssl-errors",
        "--disable-features=LocalNetworkAccessChecks",
    ):
        config.add_argument(arg)

    if platform.machine().startswith(("arm", "aarch")):
        config.add_argument("--disable-gpu-sandbox")

    if USER_AGENT:
        config.user_agent = USER_AGENT

    for proxy_arg in _build_proxy_args(proxy or {}):
        config.add_argument(proxy_arg)

    browser = await zd.start(config=config)

    if proxy and "username" in proxy and "password" in proxy:
        # Auth is handled on every navigation target. Use main_tab as a starting
        # point; the handler is registered on the connection so it will fire on
        # any tab the browser creates.
        try:
            tab = browser.main_tab
            if tab is not None:
                await install_proxy_auth_handler(tab, proxy)
        except Exception as e:
            logging.debug("Could not install proxy auth handler: %s", e)

    return browser


async def get_user_agent(browser: zd.Browser | None = None) -> str:
    """
    Read the User-Agent the browser is currently advertising.
    A short-lived browser is started if one wasn't supplied.
    """
    global USER_AGENT
    if USER_AGENT is not None:
        return USER_AGENT

    owns_browser = browser is None
    try:
        if browser is None:
            browser = await get_browser()
        tab = browser.main_tab
        if tab is None:
            tab = await browser.get("about:blank")
        raw_ua = await tab.evaluate("navigator.userAgent")
        USER_AGENT = raw_ua if isinstance(raw_ua, str) else str(raw_ua)
        # Strip any residual ``HEADLESS`` markers from the UA string.
        USER_AGENT = re.sub("HEADLESS", "", USER_AGENT, flags=re.IGNORECASE)
        return USER_AGENT
    except Exception as e:
        raise Exception("Error getting browser User-Agent. " + str(e)) from e
    finally:
        if owns_browser and browser is not None:
            with contextlib.suppress(Exception):
                await browser.stop()


async def apply_request_cookies(tab, cookies) -> None:
    """
    Set cookies in the browser via the CDP Storage domain. ``cookies`` is the
    legacy list-of-dicts shape (name/value/optional domain/...).
    """
    if not cookies:
        return
    for cookie in cookies:
        params = {
            "name": cookie.get("name"),
            "value": cookie.get("value"),
        }
        if cookie.get("domain"):
            params["domain"] = cookie["domain"]
        if cookie.get("path"):
            params["path"] = cookie["path"]
        if cookie.get("url"):
            params["url"] = cookie["url"]
        if "secure" in cookie:
            params["secure"] = bool(cookie["secure"])
        if "httpOnly" in cookie:
            params["http_only"] = bool(cookie["httpOnly"])
        if cookie.get("sameSite"):
            with contextlib.suppress(ValueError):
                params["same_site"] = network.CookieSameSite(cookie["sameSite"])
        if "expires" in cookie and cookie["expires"] is not None:
            with contextlib.suppress(TypeError, ValueError):
                params["expires"] = float(cookie["expires"])
        await tab.send(network.set_cookie(**params))


def cookies_to_dict_list(cookies) -> list:
    """
    Convert the cookies returned by ``browser.cookies.get_all()`` to the
    legacy list-of-dicts shape FlareSolverr has always returned to clients.
    """
    result = []
    for c in cookies or []:
        entry = {
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
            "size": c.size,
            "httpOnly": c.http_only,
            "secure": c.secure,
            "session": c.session,
        }
        if c.expires is not None:
            entry["expires"] = c.expires
        if c.same_site is not None:
            with contextlib.suppress(Exception):
                entry["sameSite"] = c.same_site.value if hasattr(c.same_site, "value") else str(c.same_site)
        result.append(entry)
    return result


def object_to_dict(_object):
    json_dict = json.loads(json.dumps(_object, default=lambda o: o.__dict__))
    # remove hidden fields
    return {k: v for k, v in json_dict.items() if not k.startswith("__")}


def render_post_form_html(url: str, post_data: str) -> str:
    """
    Build the auto-submitting POST form used by ``request.post``. Mirrors the
    legacy selenium implementation.
    """
    post_form = f'<form id="hackForm" action="{url}" method="POST">'
    query_string = post_data if post_data and post_data[0] != "?" else post_data[1:] if post_data else ""
    pairs = query_string.split("&")
    for pair in pairs:
        parts = pair.split("=", 1)
        # noinspection PyBroadException
        try:
            name = unquote(parts[0])
        except Exception:
            name = parts[0]
        if name == "submit":
            continue
        # noinspection PyBroadException
        try:
            value = unquote(parts[1]) if len(parts) > 1 else ""
        except Exception:
            value = parts[1] if len(parts) > 1 else ""
        # Protection of " character, for syntax
        value = value.replace('"', "&quot;")
        post_form += f'<input type="text" name="{escape(quote(name))}" value="{escape(quote(value))}"><br>'
    post_form += "</form>"
    return f"""
        <!DOCTYPE html>
        <html>
        <body>
            {post_form}
            <script>document.getElementById('hackForm').submit();</script>
        </body>
        </html>"""


def get_user_agent_sync() -> str:
    """
    Synchronous wrapper around :func:`get_user_agent`. Used by endpoints
    that are themselves sync (such as the index endpoint).
    """
    if USER_AGENT is not None:
        return USER_AGENT
    return asyncio.run(get_user_agent(None))
