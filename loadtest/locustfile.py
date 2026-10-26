"""Load-test scenarios for the Lamsa API (Locust). Run through scripts/run_loadtest.ps1 only: it
points the API at the dedicated lamsa_load database and the log push backend.

User classes (pick them on the command line):
  AnonymousBrowser  home, category lists with filters and sort, Arabic search, detail, related
  LoggedInBrowser   the same while logged in (is_favorite overlay)
  Shopper           cart add / patch / remove, then checkout (pickup)
  FlashBuyer        everyone buys the SAME limited product at once (verify with seed_load_data --verify-flash)
  LoginBurst        many logins at once (SERVICE_BUSY instead of crashes when overloaded)
  CampaignAdmin     sends one broadcast to all users while browsing continues

Request names are grouped ("home: categories", "list: filtered", ...) for RESULTS.md. Expected
business answers (sold out, price changed) are counted as "RESULT <code>" entries, not failures.
"""

import os
import random
import threading
import uuid
from typing import Any

import gevent
from locust import FastHttpUser, between, events, task
from locust.exception import StopUser

LOAD_PASSWORD = "LoadTest-pass1"
PHONE_BASE = 4915123400000
USERS = 10_000
ADMIN_PHONE = "+4915123500000"
FLASH_SLUG = "load-flash-sale"
API = "/api/v1"
SEARCH_TERMS = ["أحمر", "كريم", "عطر", "شامبو", "سيروم", "ماسكارا", "زيت شعر", "واقي شمس"]
SORTS = ["newest", "price_asc", "price_desc"]

_catalog_lock = threading.Lock()
_catalog: dict[str, Any] = {}
_phones_lock = threading.Lock()
_next_phone = [random.randrange(USERS)]


def result(name: str) -> None:
    """Count an expected business outcome (shown as a request type "RESULT" in the stats)."""
    events.request.fire(request_type="RESULT", name=name, response_time=0, response_length=0, exception=None, context={})


def unique_phone() -> str:
    with _phones_lock:
        _next_phone[0] = (_next_phone[0] + 1) % USERS
        return f"+{PHONE_BASE + _next_phone[0]}"


def check(response: Any, *ok: int) -> Any:
    """Mark non-expected statuses as failures named by status and error code."""
    if response.status_code in (ok or (200,)):
        response.success()
        return response
    code = ""
    try:
        code = response.json()["error"]["code"]
    except Exception:  # noqa: BLE001
        pass
    response.failure(f"HTTP {response.status_code} {code}".strip())
    return response


_port_counter = [0]


class LamsaUser(FastHttpUser):
    abstract = True
    wait_time = between(0.5, 2.0)
    token: str | None = None

    def __init__(self, environment: Any) -> None:
        # LOAD_API_PORTS="8001,8002,...": one API process per port; users are spread round-robin
        # (like Nginx over upstreams; on Windows one multi-worker Uvicorn gets uneven accepts).
        ports = [p for p in os.environ.get("LOAD_API_PORTS", "").split(",") if p]
        if ports:
            _port_counter[0] += 1
            self.host = f"http://127.0.0.1:{ports[_port_counter[0] % len(ports)]}"
        super().__init__(environment)

    def on_start(self) -> None:
        # Load test only (run_loadtest.ps1 trusts 127.0.0.1 in TRUSTED_PROXY_IPS): one fake client IP per user.
        self.ip = f"10.{random.randrange(256)}.{random.randrange(256)}.{random.randrange(1, 255)}"
        self.load_catalog()

    def headers(self) -> dict[str, str]:
        headers = {"X-Forwarded-For": self.ip}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get(self, path: str, name: str, *ok: int) -> Any:
        with self.client.get(API + path, headers=self.headers(), name=name, catch_response=True) as response:
            return check(response, *ok)

    def login(self, phone: str | None = None, name: str = "auth: login", retry: bool = True) -> bool:
        """Like the apps: on 503 SERVICE_BUSY wait Retry-After and retry once."""
        body = {"phone": phone or unique_phone(), "password": LOAD_PASSWORD}
        for attempt in (1, 2) if retry else (1,):
            with self.client.post(f"{API}/auth/login", json=body, headers=self.headers(), name=name, catch_response=True) as r:
                if r.status_code == 200:
                    r.success()
                    self.token = r.json()["access_token"]
                    return True
                if r.status_code == 503:
                    r.success()
                    result(f"login 503 SERVICE_BUSY (attempt {attempt})")
                    if attempt == 1 and retry:
                        gevent.sleep(float(r.headers.get("Retry-After", "2")))
                    continue
                check(r)
                return False
        return False

    def load_catalog(self) -> None:
        with _catalog_lock:
            if _catalog:
                return
            categories = self.client.get(f"{API}/categories", headers=self.headers(), name="setup").json()
            slugs: list[str] = []
            for page in range(1, 11):
                data = self.client.get(f"{API}/products?page={page}&page_size=100", headers=self.headers(), name="setup").json()
                slugs += [p["slug"] for p in data["items"] if p["slug"] != FLASH_SLUG]
            simple = self.client.get(f"{API}/products?in_stock=true&page_size=100&sort=price_asc", headers=self.headers(), name="setup").json()
            flash = self.client.get(f"{API}/products/{FLASH_SLUG}", headers=self.headers(), name="setup").json()
            _catalog.update(
                categories=[c["slug"] for c in categories] + [ch["slug"] for c in categories for ch in c.get("children", [])],
                slugs=slugs, simple=[p["id"] for p in simple["items"] if p["type"] == "simple" and p["slug"] != FLASH_SLUG],
                flash_id=flash["id"],
            )

    def checkout(self, name: str) -> int:
        headers = self.headers()
        cart = self.get("/cart", "cart: get").json()
        if not cart["lines"]:
            return 0
        if cart["issues_count"]:
            with self.client.post(f"{API}/cart/resolve-issues", headers=headers, name="cart: resolve", catch_response=True) as r:
                check(r)
            cart = self.get("/cart", "cart: get").json()
            if not cart["lines"]:
                return 0
        pickup = next(q for q in cart["delivery_quotes"] if q["code"] == "pickup")
        total = f"{float(cart['subtotal']) + float(pickup['fee']):.2f}"
        body = {"delivery_method_code": "pickup", "expected_total": total}
        with self.client.post(f"{API}/orders", json=body, headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
                              name=name, catch_response=True) as r:
            if r.status_code in (200, 201):
                r.success()
                result(f"{name} placed")
            elif r.status_code == 409:
                r.success()  # a clean business answer: sold out / price changed / cart issues
                result(f"{name} 409 {r.json()['error']['code']}")
            else:
                check(r)
            return r.status_code

    # ------------------------------------------------------------------ browsing

    def browse_home(self) -> None:
        self.get("/categories", "home: categories")
        self.get("/banners", "home: banners")
        self.get("/products?is_new=true&page_size=12", "home: new")
        self.get("/products?on_sale=true&page_size=12", "home: sale")

    def browse_category(self) -> None:
        category = random.choice(_catalog["categories"])
        params = [f"category={category}", f"sort={random.choice(SORTS)}", f"page={random.randint(1, 3)}"]
        if random.random() < 0.5:
            params.append("in_stock=true")
        if random.random() < 0.3:
            params.append(f"max_price={random.choice([20, 40, 60])}")
        self.get("/products?" + "&".join(params), "list: category + filters")
        if random.random() < 0.3:
            self.get(f"/products/facets?category={category}", "list: facets")

    def search(self) -> None:
        self.get(f"/products?q={random.choice(SEARCH_TERMS)}", "search: arabic")

    def product(self) -> None:
        slug = random.choice(_catalog["slugs"])
        self.get(f"/products/{slug}", "product: detail")
        if random.random() < 0.5:
            self.get(f"/products/{slug}/related", "product: related")


class AnonymousBrowser(LamsaUser):
    weight = 70

    @task(3)
    def home(self) -> None:
        self.browse_home()

    @task(4)
    def category(self) -> None:
        self.browse_category()

    @task(2)
    def arabic_search(self) -> None:
        self.search()

    @task(5)
    def detail(self) -> None:
        self.product()


class LoggedInBrowser(LamsaUser):
    weight = 20

    def on_start(self) -> None:
        super().on_start()
        self.login()

    @task(2)
    def home(self) -> None:
        self.browse_home()

    @task(3)
    def category(self) -> None:
        self.browse_category()

    @task(4)
    def detail(self) -> None:
        self.product()

    @task(1)
    def favorites(self) -> None:
        if self.token:
            self.get("/favorites/ids", "user: favorite ids")


class Shopper(LamsaUser):
    weight = 10
    wait_time = between(1, 3)

    def on_start(self) -> None:
        super().on_start()
        self.login()

    @task
    def shop_and_checkout(self) -> None:
        if not self.token:
            return
        headers = self.headers()
        for product_id in random.sample(_catalog["simple"], 2):
            with self.client.post(f"{API}/cart/items", json={"product_id": product_id, "quantity": 1}, headers=headers,
                                  name="cart: add", catch_response=True) as r:
                check(r, 200, 201, 409, 422)
        cart = self.get("/cart", "cart: get").json()
        if cart["lines"]:
            line = cart["lines"][0]
            with self.client.patch(f"{API}/cart/items/{line['id']}", json={"quantity": 2}, headers=headers,
                                   name="cart: patch", catch_response=True) as r:
                check(r, 200, 409, 422)
        if len(cart["lines"]) > 2:
            with self.client.delete(f"{API}/cart/items/{cart['lines'][-1]['id']}", headers=headers,
                                    name="cart: remove", catch_response=True) as r:
                check(r)
        self.checkout("checkout")

class FlashBuyer(LamsaUser):
    """Everyone wants the same product with limited stock, at the same moment, exactly once."""

    weight = 1  # phases pick classes explicitly; Locust drops weight 0
    wait_time = between(0, 0.1)

    def on_start(self) -> None:
        LamsaUser.on_start(self)
        self.login()

    @task
    def buy_the_flash_product(self) -> None:
        if not self.token:
            raise StopUser()
        headers = self.headers()
        with self.client.delete(f"{API}/cart", headers=headers, name="flash: clear cart", catch_response=True) as r:
            check(r)
        with self.client.post(f"{API}/cart/items", json={"product_id": _catalog["flash_id"], "quantity": 1}, headers=headers,
                              name="flash: add", catch_response=True) as r:
            if r.status_code in (200, 201):
                r.success()
            elif r.status_code == 409:
                r.success()
                result(f"flash: add 409 {r.json()['error']['code']}")
                raise StopUser()
            else:
                check(r)
                raise StopUser()
        self.checkout("flash: checkout")
        raise StopUser()


class LoginBurst(LamsaUser):
    """Logs in, then again, as fast as possible (a burst right after a push campaign)."""

    weight = 1
    wait_time = between(0, 0.2)

    def on_start(self) -> None:
        self.ip = f"10.{random.randrange(256)}.{random.randrange(256)}.{random.randrange(1, 255)}"

    @task
    def login_again(self) -> None:
        self.login(name="burst: login", retry=False)  # raw burst: count every SERVICE_BUSY


class CampaignAdmin(LamsaUser):
    """Sends ONE broadcast to everyone 10 s into the run, then just browses."""

    weight = 0
    fixed_count = 1
    sent = False
    wait_time = between(1, 2)

    def on_start(self) -> None:
        super().on_start()
        self.login(ADMIN_PHONE, name="admin: login")

    @task
    def campaign(self) -> None:
        if not CampaignAdmin.sent and self.environment.runner and self.environment.runner.state == "running":
            import time

            time.sleep(10)
            CampaignAdmin.sent = True
            body = {"audience": "all", "title": "تخفيضات اختبار الحمل", "body": "خصم ٢٠٪ على كل شي (اختبار)"}
            with self.client.post(f"{API}/admin/notifications", json=body, headers=self.headers(),
                                  name="admin: broadcast", catch_response=True) as r:
                check(r, 201)
                created = r.json() if r.status_code == 201 else None
            if created and created["status"] == "scheduled":  # quiet hours: the admin sends it now anyway
                with self.client.post(f"{API}/admin/notifications/{created['id']}/send-now", headers=self.headers(),
                                      name="admin: send-now", catch_response=True) as r:
                    check(r)
        self.browse_home()
