"""
Public Demo protection layer (second review pass). (Lớp bảo vệ cho Demo công khai, đợt sửa review thứ hai.)

    - Origin checks: when TWIN_CORS_ORIGINS / TWIN_CORS_REGEX is configured, WebSocket and state-changing POST requests must include an allowed Origin. (Kiểm tra Origin: khi cấu hình TWIN_CORS_ORIGINS / TWIN_CORS_REGEX, WebSocket và POST thay đổi trạng thái phải có Origin được phép.)
        Browsers always send it; curl without one is rejected unless TWIN_ALLOW_NO_ORIGIN=1. With no configuration (local development, default "*"), it is not checked. (Trình duyệt luôn gửi Origin; curl không có sẽ bị từ chối trừ khi TWIN_ALLOW_NO_ORIGIN=1. Không cấu hình thì không kiểm tra.)
    - Rate limit: in-memory sliding windows bucketed by client IP (via X-Forwarded-For on Render). (Giới hạn tốc độ: sliding window trong bộ nhớ, chia bucket theo IP client.)
                mutate  inject / clear / create task / play-pause-reset      20 per minute (20 lần/phút)
                ai      Copilot / VLM                                      10 per minute (10 lần/phút)
                whatif  What-if                                             4 per minute (4 lần/phút)
                ws      total messages per WebSocket                       120 per minute (120 lần/phút)
        TWIN_RATE_LIMIT=0 disables it for tests and local development. (TWIN_RATE_LIMIT=0 tắt giới hạn cho test và phát triển local.)
    - Body limits: REST 512 KB measured from actual bytes at the ASGI receive layer; each WS message is 64 KB in UTF-8 bytes. (Giới hạn body: REST 512 KB đo từ byte thực tế; mỗi tin nhắn WS tối đa 64 KB UTF-8.)
    - Task locations are validated by app/sim/rules.py: existing, non-charger, distinct, and compatible with TaskType. (Vị trí tác vụ do app/sim/rules.py kiểm tra: phải tồn tại, không phải trạm sạc, khác nhau và phù hợp TaskType.)
"""
from __future__ import annotations

import os
import re
import time
from collections import defaultdict, deque
from typing import Deque

MAX_BODY_BYTES = 512 * 1024
MAX_WS_MESSAGE_BYTES = 64 * 1024

LIMITS: dict[str, tuple[int, float]] = {   # bucket → (max calls, window seconds)
    "mutate": (20, 60.0),
    "ai": (10, 60.0),
    "whatif": (4, 60.0),
    "ws": (120, 60.0),
}


def rate_limit_enabled() -> bool:
    return os.environ.get("TWIN_RATE_LIMIT", "1") != "0"


class RateLimiter:
    GC_EVERY = 500   # Scan expired keys every N checks to avoid retaining many IPs forever. (Quét key hết hạn mỗi N lần check để tránh giữ nhiều IP trong bộ nhớ mãi mãi.)

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], Deque[float]] = defaultdict(deque)
        self._calls = 0

    def _gc(self, now: float) -> None:
        dead = [k for k, q in self._hits.items() if not q or now - q[-1] > LIMITS[k[0]][1]]
        for k in dead:
            del self._hits[k]

    def check(self, bucket: str, key: str) -> tuple[bool, float]:
        """Return (allowed?, seconds to wait). (Trả về (được phép?, số giây cần chờ).)"""
        if not rate_limit_enabled():
            return True, 0.0
        limit, window = LIMITS[bucket]
        now = time.monotonic()
        self._calls += 1
        if self._calls % self.GC_EVERY == 0:
            self._gc(now)
        q = self._hits[(bucket, key)]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False, round(window - (now - q[0]), 1) if q else window
        q.append(now)
        return True, 0.0

    def reset(self) -> None:
        self._hits.clear()


limiter = RateLimiter()


def client_key(headers: dict[str, str] | None, host: str | None) -> str:
    """A reverse proxy (Render) appends the real client IP to X-Forwarded-For while client-supplied values stay at the front.
    Take the TWIN_TRUSTED_PROXIES-th value from the end (default 1 is the proxy's observed connection).
    TWIN_TRUSTED_PROXIES=0 ignores the header and uses the connection source for local development.
    (Proxy ngược thêm IP thật vào cuối X-Forwarded-For; lấy phần tử từ cuối theo số proxy tin cậy.)"""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    n = int(os.environ.get("TWIN_TRUSTED_PROXIES", "1"))
    xff = h.get("x-forwarded-for")
    if xff and n > 0:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-n] if len(parts) >= n else parts[0]
    return host or "unknown"


def _allowed_origins() -> tuple[list[str], re.Pattern[str] | None, bool]:
    raw = os.environ.get("TWIN_CORS_ORIGINS", "*")
    origins = [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]
    regex = os.environ.get("TWIN_CORS_REGEX") or None
    open_ = "*" in origins and not regex
    return origins, re.compile(regex) if regex else None, open_


def origin_allowed(origin: str | None) -> bool:
    origins, regex, open_ = _allowed_origins()
    if open_:
        return True
    if not origin:
        return os.environ.get("TWIN_ALLOW_NO_ORIGIN", "0") == "1"
    o = origin.rstrip("/")
    if o in origins:
        return True
    return bool(regex and regex.fullmatch(o))
