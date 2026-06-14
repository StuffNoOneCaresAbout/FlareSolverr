STATUS_OK = "ok"
STATUS_ERROR = "error"


class ChallengeResolutionResultT:
    # Note: DTOs are populated from JSON via ``self.__dict__.update(_dict)`` in
    # ``__init__``. Type annotations are intentionally loose because every
    # field is ``T | None`` in practice. Add ``ty: ignore`` markers at consumer
    # sites that access these fields.
    url = None
    status = None
    headers = None
    response = None
    cookies = None
    userAgent = None
    screenshot: str | None = None
    turnstile_token = None

    def __init__(self, _dict):
        self.__dict__.update(_dict)


class ChallengeResolutionT:
    status = None
    message = None
    result = None

    def __init__(self, _dict):
        self.__dict__.update(_dict)
        if self.result is not None:
            self.result = ChallengeResolutionResultT(self.result)


class V1RequestBase:
    # V1RequestBase
    cmd = None
    cookies = None
    maxTimeout = None
    proxy = None
    session = None
    session_ttl_minutes = None
    headers = None  # deprecated v2.0.0, not used
    userAgent = None  # deprecated v2.0.0, not used

    # V1Request
    url = None
    postData = None
    returnOnlyCookies = None
    returnScreenshot = None
    download = None  # deprecated v2.0.0, not used
    returnRawHtml = None  # deprecated v2.0.0, not used
    waitInSeconds = None
    # Optional resource blocking flag (blocks images, CSS, and fonts)
    disableMedia = None
    # Optional when you've got a turnstile captcha that needs to be clicked after X number of Tab presses
    tabs_till_verify = None

    def __init__(self, _dict):
        self.__dict__.update(_dict)


class V1ResponseBase:
    # V1ResponseBase
    status = None
    message = None
    session = None
    sessions = None
    startTimestamp = None
    endTimestamp = None
    version = None

    # V1ResponseSolution
    solution = None

    # hidden vars
    __error_500__: bool = False

    def __init__(self, _dict):
        self.__dict__.update(_dict)
        if self.solution is not None:
            self.solution = ChallengeResolutionResultT(self.solution)


class IndexResponse:
    msg = None
    version = None
    userAgent = None

    def __init__(self, _dict):
        self.__dict__.update(_dict)


class HealthResponse:
    status = None

    def __init__(self, _dict):
        self.__dict__.update(_dict)
