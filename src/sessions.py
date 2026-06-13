import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Tuple
from uuid import uuid1

import zendriver as zd

import utils


@dataclass
class Session:
    session_id: str
    browser: zd.Browser
    tab: Optional[zd.Tab] = field(default=None)
    created_at: datetime = field(default_factory=datetime.now)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def lifetime(self) -> timedelta:
        return datetime.now() - self.created_at


class SessionsStorage:
    """SessionsStorage creates, stores and process all the sessions."""

    def __init__(self):
        self.sessions = {}
        self._registry_lock = asyncio.Lock()

    async def create(self, session_id: Optional[str] = None, proxy: Optional[dict] = None,
                     force_new: Optional[bool] = False) -> Tuple[Session, bool]:
        """
        Create a new browser-backed session.

        The function is idempotent: if ``session_id`` already exists a new
        browser will not be launched and the existing session is returned.
        ``force_new=True`` will close and recreate the session regardless.
        """
        session_id = session_id or str(uuid1())

        async with self._registry_lock:
            if force_new:
                await self._close(session_id)

            existing = self.sessions.get(session_id)
            if existing is not None:
                return existing, False

            browser = await utils.get_browser(proxy)
            tab = browser.main_tab
            if tab is None:
                tab = await browser.get('about:blank')
            session = Session(session_id=session_id, browser=browser, tab=tab)
            self.sessions[session_id] = session
            return session, True

    def exists(self, session_id: str) -> bool:
        return session_id in self.sessions

    async def destroy(self, session_id: str) -> bool:
        """Close the browser for ``session_id`` and remove it from storage."""
        async with self._registry_lock:
            return await self._close(session_id)

    async def _close(self, session_id: str) -> bool:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return False
        try:
            await session.browser.stop()
        except Exception as e:
            logging.debug('Error stopping session browser: %s', e)
        return True

    async def get(self, session_id: str, ttl: Optional[timedelta] = None) -> Tuple[Session, bool]:
        session, fresh = await self.create(session_id)

        if ttl is not None and not fresh and session.lifetime() > ttl:
            logging.debug(f"session's lifetime has expired, so the session is recreated "
                          f"(session_id={session_id})")
            session, fresh = await self.create(session_id, force_new=True)

        return session, fresh

    def session_ids(self) -> list[str]:
        return list(self.sessions.keys())

    async def stop_all(self) -> None:
        """Close every active session. Used during server shutdown."""
        async with self._registry_lock:
            session_ids = list(self.sessions.keys())
        for sid in session_ids:
            await self.destroy(sid)


SESSIONS_STORAGE = SessionsStorage()
