"""연락처, 요청 간격과 제한된 재시도를 적용하는 HTTP 클라이언트."""

import math
import time
import warnings
from collections.abc import Callable

import requests


class MissingContact(Warning):
    """위키 운영자가 연락할 주소가 없는 경우."""


class UpstreamError(RuntimeError):
    """외부 응답 실패. 요청 헤더나 서버의 원문 오류는 노출하지 않는다."""


def build_user_agent(app_name: str, version: str, contact: str | None) -> str:
    """ASCII User-Agent를 만들며 연락처 누락을 경고한다."""
    contact = contact.strip() if contact else ""
    agent = f"{app_name}/{version}" + (f" ({contact})" if contact else "") + " python-requests"
    if not agent.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in agent):
        raise ValueError("User-Agent에는 ASCII 인쇄 문자만 사용하세요.")
    if not contact:
        warnings.warn("WIKI_CONTACT가 없습니다. 느린 요청 간격을 적용하세요.", MissingContact, stacklevel=2)
    return agent


class HttpClient:
    def __init__(self, user_agent: str, min_interval_sec: float, max_retries: int,
                 timeout_sec: float, backoff_base_sec: float, session=None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        if (not isinstance(max_retries, int) or max_retries < 0
                or not all(math.isfinite(n) and n >= 0 for n in
                           (min_interval_sec, timeout_sec, backoff_base_sec)) or timeout_sec == 0):
            raise ValueError("HTTP 간격·재시도·타임아웃 설정을 확인하세요.")
        self.session = session if session is not None else requests.Session()
        self.user_agent = user_agent
        self.min_interval_sec = min_interval_sec
        self.max_retries = max_retries
        self.timeout_sec = timeout_sec
        self.backoff_base_sec = backoff_base_sec
        self.sleep = sleep
        self.clock = clock
        self._last_request: float | None = None
        self.request_count = 0

    def _wait(self) -> None:
        if self._last_request is not None:
            delay = self.min_interval_sec - (self.clock() - self._last_request)
            if delay > 0:
                self.sleep(delay)
        self._last_request = self.clock()

    def _retry_delay(self, header: str | None, attempt: int) -> float:
        try:
            delay = float(header)
            if math.isfinite(delay) and delay >= 0:
                return delay
        except (ValueError, TypeError):
            pass
        return self.backoff_base_sec * 2 ** attempt

    def get_json(self, url: str, params: dict) -> dict:
        """최초 요청 뒤 max_retries회까지만 재시도한다."""
        for attempt in range(self.max_retries + 1):
            self._wait()
            self.request_count += 1
            try:
                response = self.session.get(url, params=params,
                                            headers={"User-Agent": self.user_agent},
                                            timeout=self.timeout_sec)
            except requests.RequestException:
                raise UpstreamError("외부 서버에 연결하지 못했습니다. 잠시 후 다시 시도하세요.") from None
            try:
                status = response.status_code
                if status == 429 or 500 <= status < 600:
                    if attempt == self.max_retries:
                        raise UpstreamError(f"외부 서버 재시도 한도 초과 (HTTP {status}). 잠시 후 다시 시도하세요.")
                    delay = self._retry_delay(response.headers.get("Retry-After"), attempt)
                elif not 200 <= status < 300:
                    raise UpstreamError(f"외부 요청 실패 (HTTP {status}). 요청 조건을 확인하세요.")
                else:
                    try:
                        data = response.json()
                    except ValueError:
                        raise UpstreamError("외부 서버 응답이 JSON이 아닙니다. 잠시 후 다시 시도하세요.") from None
                    if not isinstance(data, dict) or "error" in data:
                        raise UpstreamError("외부 API 응답 오류입니다. 요청 조건을 확인하세요.")
                    return data
            finally:
                response.close()
            self.sleep(delay)
        raise AssertionError("unreachable")
