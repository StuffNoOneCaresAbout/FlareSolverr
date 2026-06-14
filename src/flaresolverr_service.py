import asyncio
import contextlib
import logging
import platform
import sys
import time
from datetime import timedelta

import zendriver as zd
from zendriver.cdp import network
from zendriver.core.cloudflare import (
    cf_is_interactive_challenge_present,
    verify_cf,
)

import utils
from dtos import (
    STATUS_ERROR,
    STATUS_OK,
    ChallengeResolutionResultT,
    ChallengeResolutionT,
    HealthResponse,
    IndexResponse,
    V1RequestBase,
    V1ResponseBase,
)
from sessions import SessionsStorage

ACCESS_DENIED_TITLES = [
    # Cloudflare
    "Access denied",
    # Cloudflare http://bitturk.net/ Firefox
    "Attention Required! | Cloudflare",
]
ACCESS_DENIED_SELECTORS = [
    # Cloudflare
    "div.cf-error-title span.cf-code-label span",
    # Cloudflare http://bitturk.net/ Firefox
    "#cf-error-details div.cf-error-overview h1",
]
CHALLENGE_TITLES = [
    # Cloudflare
    "Just a moment...",
    # DDoS-GUARD
    "DDoS-Guard",
]
CHALLENGE_SELECTORS = [
    # Cloudflare
    "#cf-challenge-running",
    ".ray_id",
    ".attack-box",
    "#cf-please-wait",
    "#challenge-spinner",
    "#trk_jschal_js",
    "#turnstile-wrapper",
    ".lds-ring",
    # Custom CloudFlare for EbookParadijs, Film-Paleis, MuziekFabriek and Puur-Hollands
    "td.info #js_info",
    # Fairlane / pararius.com
    "div.vc div.text-box h2",
]

TURNSTILE_SELECTORS = [
    "input[name='cf-turnstile-response']",
]

SHORT_TIMEOUT = 2
SESSIONS_STORAGE = SessionsStorage()


async def test_browser_installation():
    logging.info("Testing web browser installation...")
    logging.info("Platform: " + platform.platform())

    logging.info("Launching web browser...")
    browser = None
    try:
        browser = await utils.get_browser()
    except Exception as e:
        logging.error("Error starting browser: %s", e)
        sys.exit(1)

    try:
        user_agent = await utils.get_user_agent(browser)
    except Exception as e:
        logging.error("Error retrieving User-Agent: %s", e)
        sys.exit(1)
    finally:
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.stop()

    logging.info("FlareSolverr User-Agent: " + user_agent)
    logging.info("Test successful!")


def index_endpoint() -> IndexResponse:
    res = IndexResponse({})
    res.msg = "FlareSolverr is ready!"
    res.version = utils.get_flaresolverr_version()
    res.userAgent = utils.get_user_agent_sync()
    return res


def health_endpoint() -> HealthResponse:
    res = HealthResponse({})
    res.status = STATUS_OK
    return res


async def controller_v1_endpoint(req: V1RequestBase) -> V1ResponseBase:
    start_ts = int(time.time() * 1000)
    logging.info(f"Incoming request => POST /v1 body: {utils.object_to_dict(req)}")
    res: V1ResponseBase
    try:
        res = await _controller_v1_handler(req)
    except Exception as e:
        res = V1ResponseBase({})
        res.__error_500__ = True
        res.status = STATUS_ERROR
        res.message = "Error: " + str(e)
        logging.error(res.message)

    res.startTimestamp = start_ts
    res.endTimestamp = int(time.time() * 1000)
    res.version = utils.get_flaresolverr_version()
    logging.debug(f"Response => POST /v1 body: {utils.object_to_dict(res)}")
    logging.info(f"Response in {(res.endTimestamp - res.startTimestamp) / 1000} s")
    return res


async def _controller_v1_handler(req: V1RequestBase) -> V1ResponseBase:
    # do some validations
    if req.cmd is None:
        raise Exception("Request parameter 'cmd' is mandatory.")
    if req.headers is not None:
        logging.warning("Request parameter 'headers' was removed in FlareSolverr v2.")
    if req.userAgent is not None:
        logging.warning("Request parameter 'userAgent' was removed in FlareSolverr v2.")

    # set default values
    if req.maxTimeout is None or int(req.maxTimeout) < 1:
        req.maxTimeout = 60000

    # execute the command
    res: V1ResponseBase
    if req.cmd == "sessions.create":
        res = await _cmd_sessions_create(req)
    elif req.cmd == "sessions.list":
        res = _cmd_sessions_list(req)
    elif req.cmd == "sessions.destroy":
        res = await _cmd_sessions_destroy(req)
    elif req.cmd == "request.get":
        res = await _cmd_request_get(req)
    elif req.cmd == "request.post":
        res = await _cmd_request_post(req)
    else:
        raise Exception(f"Request parameter 'cmd' = '{req.cmd}' is invalid.")

    return res


async def _cmd_request_get(req: V1RequestBase) -> V1ResponseBase:
    # do some validations
    if req.url is None:
        raise Exception("Request parameter 'url' is mandatory in 'request.get' command.")
    if req.postData is not None:
        raise Exception("Cannot use 'postBody' when sending a GET request.")
    if req.returnRawHtml is not None:
        logging.warning("Request parameter 'returnRawHtml' was removed in FlareSolverr v2.")
    if req.download is not None:
        logging.warning("Request parameter 'download' was removed in FlareSolverr v2.")

    challenge_res = await _resolve_challenge(req, "GET")
    res = V1ResponseBase({})
    res.status = challenge_res.status
    res.message = challenge_res.message
    res.solution = challenge_res.result
    return res


async def _cmd_request_post(req: V1RequestBase) -> V1ResponseBase:
    # do some validations
    if req.postData is None:
        raise Exception("Request parameter 'postData' is mandatory in 'request.post' command.")
    if req.returnRawHtml is not None:
        logging.warning("Request parameter 'returnRawHtml' was removed in FlareSolverr v2.")
    if req.download is not None:
        logging.warning("Request parameter 'download' was removed in FlareSolverr v2.")

    challenge_res = await _resolve_challenge(req, "POST")
    res = V1ResponseBase({})
    res.status = challenge_res.status
    res.message = challenge_res.message
    res.solution = challenge_res.result
    return res


async def _cmd_sessions_create(req: V1RequestBase) -> V1ResponseBase:
    logging.debug("Creating new session...")

    session, fresh = await SESSIONS_STORAGE.create(session_id=req.session, proxy=req.proxy)
    session_id = session.session_id

    if not fresh:
        return V1ResponseBase({"status": STATUS_OK, "message": "Session already exists.", "session": session_id})

    return V1ResponseBase({"status": STATUS_OK, "message": "Session created successfully.", "session": session_id})


def _cmd_sessions_list(req: V1RequestBase) -> V1ResponseBase:
    session_ids = SESSIONS_STORAGE.session_ids()
    return V1ResponseBase({"status": STATUS_OK, "message": "", "sessions": session_ids})


async def _cmd_sessions_destroy(req: V1RequestBase) -> V1ResponseBase:
    if req.session is None:
        raise Exception("Request parameter 'session' is mandatory in 'sessions.destroy' command.")
    session_id = req.session
    existed = await SESSIONS_STORAGE.destroy(session_id)

    if not existed:
        raise Exception("The session doesn't exist.")

    return V1ResponseBase({"status": STATUS_OK, "message": "The session has been removed."})


async def _resolve_challenge(req: V1RequestBase, method: str) -> ChallengeResolutionT:
    timeout = int(req.maxTimeout or 60000) / 1000
    browser: zd.Browser | None = None
    owns_browser = False
    try:
        if req.session:
            session_id = req.session
            ttl = timedelta(minutes=req.session_ttl_minutes) if req.session_ttl_minutes else None
            session, fresh = await SESSIONS_STORAGE.get(session_id, ttl)

            if fresh:
                logging.debug(f"new session created to perform the request (session_id={session_id})")
            else:
                logging.debug(
                    f"existing session is used to perform the request (session_id={session_id}, "
                    f"lifetime={session.lifetime()!s}, ttl={ttl!s})"
                )

            browser = session.browser
        else:
            browser = await utils.get_browser(req.proxy)
            owns_browser = True
            logging.debug("New instance of browser has been created to perform the request")

        return await asyncio.wait_for(_evil_logic(req, browser, method), timeout=timeout)
    except TimeoutError:
        raise Exception(f"Error solving the challenge. Timeout after {timeout} seconds.") from None
    except Exception as e:
        raise Exception("Error solving the challenge. " + str(e).replace("\n", "\\n")) from e
    finally:
        if owns_browser and browser is not None:
            try:
                await browser.stop()
            except Exception as e:
                logging.debug("Error closing browser: %s", e)
            logging.debug("A used instance of browser has been destroyed")


async def _block_media(tab: zd.Tab) -> None:
    """
    Block images / CSS / fonts at the network layer to speed up navigation.
    Mirrors the legacy ``Network.setBlockedURLs`` flow but uses zendriver's
    CDP bindings.
    """
    block_urls = [
        # Images
        "*.png",
        "*.jpg",
        "*.jpeg",
        "*.gif",
        "*.webp",
        "*.bmp",
        "*.svg",
        "*.ico",
        "*.PNG",
        "*.JPG",
        "*.JPEG",
        "*.GIF",
        "*.WEBP",
        "*.BMP",
        "*.SVG",
        "*.ICO",
        "*.tiff",
        "*.tif",
        "*.jpe",
        "*.apng",
        "*.avif",
        "*.heic",
        "*.heif",
        "*.TIFF",
        "*.TIF",
        "*.JPE",
        "*.APNG",
        "*.AVIF",
        "*.HEIC",
        "*.HEIF",
        # Stylesheets
        "*.css",
        "*.CSS",
        # Fonts
        "*.woff",
        "*.woff2",
        "*.ttf",
        "*.otf",
        "*.eot",
        "*.WOFF",
        "*.WOFF2",
        "*.TTF",
        "*.OTF",
        "*.EOT",
    ]
    try:
        await tab.send(network.enable())
        # ``set_blocked_urls`` is a newer CDP method that may not be present in
        # older zendriver versions or in ty's stubs.
        set_blocked_urls = getattr(network, "set_blocked_urls", None)
        if set_blocked_urls is not None:
            await tab.send(set_blocked_urls(urls=block_urls))
            logging.debug("Network.setBlockedURLs applied")
    except Exception as e:
        logging.debug("Network.setBlockedURLs failed or unsupported: %s", e)


async def _is_access_denied(tab: zd.Tab) -> bool:
    page_title = tab.title or ""
    for title in ACCESS_DENIED_TITLES:
        if page_title.startswith(title):
            return True
    for selector in ACCESS_DENIED_SELECTORS:
        try:
            elements = await tab.select_all(selector, timeout=0.5)
            if elements:
                return True
        except Exception:
            continue
    return False


async def _has_challenge(tab: zd.Tab) -> bool:
    page_title = tab.title or ""
    for title in CHALLENGE_TITLES:
        if title.lower() == page_title.lower():
            return True
    for selector in CHALLENGE_SELECTORS:
        try:
            elements = await tab.select_all(selector, timeout=0.5)
            if elements:
                return True
        except Exception:
            continue
    return await cf_is_interactive_challenge_present(tab, timeout=0.5)


async def _wait_challenge_gone(tab: zd.Tab, max_attempts: int = 30) -> None:
    """
    Wait until the challenge title / selectors are no longer present.
    Polling is required because zendriver does not ship a "wait for element
    to disappear" helper.
    """
    for attempt in range(1, max_attempts + 1):
        await asyncio.sleep(SHORT_TIMEOUT)
        page_title = (tab.title or "").lower()
        title_gone = bool(page_title and not any(t.lower() == page_title for t in CHALLENGE_TITLES))
        selector_gone = True
        for selector in CHALLENGE_SELECTORS:
            try:
                elements = await tab.select_all(selector, timeout=0.2)
                if elements:
                    selector_gone = False
                    break
            except Exception:
                continue
        if title_gone and selector_gone:
            return
        logging.debug("Challenge still present (attempt %d)", attempt)
    logging.debug("Reached max attempts waiting for challenge to disappear")


async def _resolve_turnstile_captcha(req: V1RequestBase, tab: zd.Tab):
    """
    Resolve the Turnstile captcha, if any, by tabbing into the iframe and
    pressing space. Mirrors the legacy ``click_verify`` flow.
    """
    if req.tabs_till_verify is None:
        return None

    assert req.url is not None
    logging.debug(f"Navigating to... {req.url} in order to pass the turnstile challenge")
    proxy_url = (req.proxy or {}).get("url") if req.proxy else None
    await utils.navigate_or_raise(tab, req.url, proxy_url=proxy_url)

    turnstile_challenge_found = False
    for selector in TURNSTILE_SELECTORS:
        try:
            elements = await tab.select_all(selector, timeout=0.5)
            if elements:
                turnstile_challenge_found = True
                logging.info("Turnstile challenge detected. Selector found: " + selector)
                break
        except Exception:
            continue
    if not turnstile_challenge_found:
        logging.debug("Turnstile challenge not found")
        return None

    return await _click_turnstile_until_token(tab, num_tabs=req.tabs_till_verify)


async def _click_turnstile_until_token(tab: zd.Tab, num_tabs: int) -> str | None:
    try:
        token_input = await tab.select("input[name='cf-turnstile-response']", timeout=5)
    except Exception:
        logging.debug("Could not find the cf-turnstile-response input")
        return None
    current_value = await token_input.get_attribute("value") or ""
    start = time.monotonic()
    timeout_s = 60.0
    while time.monotonic() - start < timeout_s:
        try:
            await _click_verify(tab, num_tabs=num_tabs)
        except Exception:
            logging.debug("click_verify failed", exc_info=True)
        try:
            turnstile_token = (await token_input.get_attribute("value")) or ""
        except Exception:
            turnstile_token = ""
        if turnstile_token and turnstile_token != current_value:
            logging.info(f"Turnstile token: {turnstile_token}")
            return turnstile_token
        logging.debug("Failed to extract token, possibly click failed")
        # reset focus and try again
        with contextlib.suppress(Exception):
            await tab.evaluate("""
                let el = document.createElement('button');
                el.style.position='fixed';
                el.style.top='0';
                el.style.left='0';
                document.body.prepend(el);
                el.focus();
            """)
        await asyncio.sleep(1)
    logging.debug("Timed out trying to obtain Turnstile token")
    return None


async def _click_verify(tab: zd.Tab, num_tabs: int = 1) -> None:
    """
    Replicates the legacy ``click_verify`` helper: tab into the challenge,
    press space, and (optionally) click the ``Verify you are human`` button.
    """
    from zendriver.cdp import input_

    try:
        logging.debug("Try to find the Cloudflare verify checkbox...")
        for _ in range(num_tabs):
            await tab.send(
                input_.dispatch_key_event(
                    type_="rawKeyDown",
                    key="Tab",
                    code="Tab",
                    windows_virtual_key_code=9,
                    native_virtual_key_code=9,
                )
            )
            await tab.send(
                input_.dispatch_key_event(
                    type_="keyUp",
                    key="Tab",
                    code="Tab",
                    windows_virtual_key_code=9,
                    native_virtual_key_code=9,
                )
            )
            await asyncio.sleep(0.1)
        await asyncio.sleep(1)
        await tab.send(
            input_.dispatch_key_event(
                type_="char",
                text=" ",
                unmodified_text=" ",
                key=" ",
                code="Space",
                windows_virtual_key_code=32,
                native_virtual_key_code=32,
            )
        )
        logging.debug(f"Cloudflare verify checkbox clicked after {num_tabs} tabs!")
    except Exception:
        logging.debug("Cloudflare verify checkbox not found on the page.", exc_info=True)

    try:
        logging.debug("Try to find the Cloudflare 'Verify you are human' button...")
        # zendriver's select() uses CSS selectors; the legacy code used an
        # XPath expression which is equivalent to this attribute selector.
        button = await tab.select(
            "input[type='button'][value='Verify you are human']",
            timeout=0.5,
        )
        if button:
            await button.click()
            logging.debug("The Cloudflare 'Verify you are human' button found and clicked!")
    except Exception:
        logging.debug("The Cloudflare 'Verify you are human' button not found on the page.")

    await asyncio.sleep(2)


async def _evil_logic(req: V1RequestBase, browser: zd.Browser, method: str) -> ChallengeResolutionT:
    res = ChallengeResolutionT({})
    res.status = STATUS_OK
    res.message = ""

    # Use the main tab of the supplied browser. If it was already used for a
    # previous request, we still re-use it (matches legacy behavior for
    # session-less requests where a brand-new browser is created anyway).
    tab = browser.main_tab
    if tab is None:
        tab = await browser.get("about:blank")

    # optionally block resources like images/css/fonts using CDP
    disable_media = utils.get_config_disable_media()
    if req.disableMedia is not None:
        disable_media = req.disableMedia
    if disable_media:
        await _block_media(tab)

    # navigate to the page
    logging.debug(f"Navigating to... {req.url}")
    turnstile_token = None

    if method == "POST":
        await _post_request(req, tab)
    else:
        if req.tabs_till_verify is None:
            proxy_url = (req.proxy or {}).get("url") if req.proxy else None
            assert req.url is not None
            await utils.navigate_or_raise(tab, req.url, proxy_url=proxy_url)
        else:
            turnstile_token = await _resolve_turnstile_captcha(req, tab)

    # set cookies if required
    if req.cookies is not None and len(req.cookies) > 0:
        logging.debug("Setting cookies...")
        await utils.apply_request_cookies(tab, req.cookies)
        # reload the page
        if method == "POST":
            await _post_request(req, tab)
        else:
            proxy_url = (req.proxy or {}).get("url") if req.proxy else None
            assert req.url is not None
            await utils.navigate_or_raise(tab, req.url, proxy_url=proxy_url)

    # wait for the page
    if utils.get_config_log_html():
        try:
            html = await tab.get_content()
            logging.debug(f"Response HTML:\n{html}")
        except Exception:
            logging.debug("Could not read page source for LOG_HTML")

    if await _is_access_denied(tab):
        raise Exception(
            "Cloudflare has blocked this request. Probably your IP is banned for this site, check in your web browser."
        )

    challenge_found = await _has_challenge(tab)
    if challenge_found:
        logging.info("Challenge detected. Attempting to solve with zendriver.verify_cf")
        try:
            await verify_cf(tab, timeout=int(req.maxTimeout or 60000) / 1000, click_delay=5)
        except TimeoutError:
            logging.debug("verify_cf timed out, retrying with click_verify as a fallback")
        except Exception as e:
            logging.debug(f"verify_cf failed: {e}")

        # Fall back to the legacy poll + click_verify loop, in case verify_cf
        # missed a challenge variant (DDoS-Guard, custom CF, Fairlane…).
        await _wait_challenge_gone(tab)
        if await _has_challenge(tab):
            try:
                await _click_verify(tab)
            except Exception:
                logging.debug("click_verify fallback failed", exc_info=True)
            await _wait_challenge_gone(tab)

        logging.info("Challenge solved!")
        res.message = "Challenge solved!"
    else:
        logging.info("Challenge not detected!")
        res.message = "Challenge not detected!"

    # collect the challenge solution
    challenge_res = ChallengeResolutionResultT({})
    try:
        if tab.url is not None:
            challenge_res.url = tab.url
    except Exception:
        challenge_res.url = req.url
    challenge_res.status = 200  # todo: real status from the network stack
    try:
        cookies = await browser.cookies.get_all()
        challenge_res.cookies = utils.cookies_to_dict_list(cookies)
    except Exception as e:
        logging.debug("Could not read cookies: %s", e)
        challenge_res.cookies = []
    challenge_res.userAgent = await utils.get_user_agent(browser)
    challenge_res.turnstile_token = turnstile_token or ""

    if not req.returnOnlyCookies:
        challenge_res.headers = {}  # todo: extract headers from a fetch interceptor
        if req.waitInSeconds and req.waitInSeconds > 0:
            logging.info("Waiting " + str(req.waitInSeconds) + " seconds before returning the response...")
            await asyncio.sleep(req.waitInSeconds)
        try:
            challenge_res.response = await tab.get_content()
        except Exception as e:
            logging.debug("Could not read page source: %s", e)
            challenge_res.response = ""

    if req.returnScreenshot:
        try:
            challenge_res.screenshot = await tab.screenshot_b64(format="png")
        except Exception as e:
            logging.debug("Could not take screenshot: %s", e)

    res.result = challenge_res
    return res


async def _post_request(req: V1RequestBase, tab: zd.Tab):
    assert req.url is not None
    assert req.postData is not None
    html_content = utils.render_post_form_html(req.url, req.postData)
    data_url = "data:text/html;charset=utf-8," + html_content
    await tab.get(data_url)
