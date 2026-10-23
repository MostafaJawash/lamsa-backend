# Lamsa API

Backend for the Lamsa store: FastAPI, async SQLAlchemy 2 on MySQL, Alembic migrations, its own JWT auth (phone + password, or Google), and local-disk image storage.

## Requirements

- Python 3.12+
- MySQL 8.0.16+ (needs CHECK constraints and JSON expression defaults; developed on 9.6) with the `ngram` full-text parser (built in)
- No Docker needed for now. Images are stored on disk.

## Setup

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env    # then set DB_PASSWORD and JWT_SECRET
copy .env.test.example .env.test    # the tests' database connection; set DB_PASSWORD
.\venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"   # JWT_SECRET
```

Start the cache (optional: without it every request simply goes to the database):

```powershell
docker compose up -d valkey
```

Create the databases and user once, as MySQL root:

```sql
CREATE DATABASE IF NOT EXISTS lamsa      CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE IF NOT EXISTS lamsa_test CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS 'lamsa'@'localhost' IDENTIFIED BY '<password>';
GRANT ALL PRIVILEGES ON lamsa.*      TO 'lamsa'@'localhost';
GRANT ALL PRIVILEGES ON lamsa_test.* TO 'lamsa'@'localhost';
FLUSH PRIVILEGES;
```

`.env` must be UTF-8 **without BOM**. Don't write it with PowerShell 5.1 `Set-Content` or `Out-File`. `.env` is git-ignored, so never commit it.

| Key | Example | Notes |
|---|---|---|
| `APP_ENV` | `development` | `/media` is served by the app only in `development` |
| `DEBUG` | `true` | echoes SQL |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | | MySQL connection |
| `TEST_DATABASE_NAME` | `lamsa_test` | the tests' database, set in `.env.test` (see *Tests*); must end in `_test` |
| `STORAGE_BACKEND` | `local` | only `local` for now |
| `MEDIA_ROOT` | `./media` | where image files are written |
| `MEDIA_URL` | `http://127.0.0.1:8000/media` | public base URL for `MEDIA_ROOT` |
| `MAX_UPLOAD_MB` | `5` | upload size limit |
| `JWT_SECRET` | | **required**, at least 32 bytes. The app refuses to start otherwise |
| `JWT_ALGORITHM` | `HS256` | |
| `ACCESS_TOKEN_MINUTES` | `15` | access token lifetime |
| `REFRESH_TOKEN_DAYS` | `60` | refresh token lifetime |
| `REFRESH_REUSE_GRACE_SECONDS` | `20` | safety net for duplicate refreshes (see below) |
| `SIGNUP_TOKEN_MINUTES` | `10` | time to finish Google signup |
| `GOOGLE_CLIENT_IDS` | `web-id,android-id,ios-id` | comma separated; tokens for any of them are accepted |
| `GOOGLE_DEV_MODE` | `false` | accept fake Google tokens; **only with `APP_ENV=development`** (the app refuses to start otherwise) |
| `DEFAULT_PHONE_REGION` | `DE` | region for phone numbers written without country code |
| `CORS_ORIGINS` | `http://localhost:3000,...` | comma separated |
| `TRUST_PROXY_HEADERS` | `false` | set `true` only behind your own reverse proxy that overwrites `X-Forwarded-For` (client IP for rate limits). **Never** enable it when clients can reach the API directly |
| `CACHE_ENABLED`, `CACHE_URL`, `CACHE_KEY_PREFIX`, `CACHE_TIMEOUT_MS` | `true`, `redis://127.0.0.1:6379/0`, `lamsa:`, `1000` | Valkey cache (see *Performance*) |
| `PASSWORD_HASH_CONCURRENCY`, `PASSWORD_HASH_QUEUE_TIMEOUT` | cores, `5` | hashes at once per process; seconds to wait before 503 SERVICE_BUSY |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT`, `DB_POOL_RECYCLE` | `10`, `10`, `5`, `3600` | database pool per process (see *Production tuning*) |
| `SLOW_QUERY_MS`, `SLOW_REQUEST_MS` | `200`, `1000` | slow-query and slow-request warnings |

## Run

```powershell
.\venv\Scripts\python.exe -m scripts.init_storage   # create MEDIA_ROOT
.\venv\Scripts\alembic.exe upgrade head             # create / update tables
.\venv\Scripts\python.exe -m scripts.seed_base      # categories, delivery methods, settings (safe to re-run)
.\venv\Scripts\fastapi.exe dev app/main.py          # http://127.0.0.1:8000/docs
```

In development, the API also runs the background worker (notifications, cart reminders, cleanup). In production, run the worker separately with `python -m app.workers.run` (see *Push notifications: Firebase and the worker*).

Optional, development only (`APP_ENV=development`): a demo catalog built from the prototype (`docs/prototype/lamsa-presentation.html`). It contains 7 brands with logos, 19 products and one gift box, with placeholder images (one per color variant plus a general one), published through the normal service layer. Demo items use slugs `demo-…` and SKUs `DEMO-…`. The command is safe to re-run, and `--reset` removes only demo data.

```powershell
.\venv\Scripts\python.exe -m scripts.seed_demo_catalog          # needs scripts.seed_base first
.\venv\Scripts\python.exe -m scripts.seed_demo_catalog --reset
.\venv\Scripts\python.exe -m scripts.recalculate_products        # rebuild price_from / in_stock / is_on_sale / search_text
```

Create the first admin with one of these. The password is asked interactively and is never passed as an argument.

```powershell
.\venv\Scripts\python.exe -m scripts.create_admin --name "Store Admin" --phone "0151 23456789"
.\venv\Scripts\python.exe -m scripts.make_admin --phone "0151 23456789"     # promote an existing user
```

`GET /health` returns `{"status": "ok", "database": "ok", "storage": "ok"}`, or HTTP 503 if the database is unreachable or storage is unavailable.

## Tests

```powershell
.\venv\Scripts\python.exe -m pytest -q
```

- **The tests never read `.env`.** `tests/conftest.py` turns off Settings' dotenv loading (`LAMSA_ENV_FILE=""`) before importing the app and sets every setting that matters itself. For example, `PUSH_BACKEND=log` is forced, so no test can reach Firebase, whatever your `.env` says. A guard test (`tests/core/test_test_isolation.py`) fails if that ever changes. Only the database connection comes from outside: environment variables (CI) or the git-ignored `.env.test` (copy `.env.test.example`). That file may only contain `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD` and `TEST_DATABASE_NAME`.
- Database tests use `TEST_DATABASE_NAME` (`lamsa_test`), never `lamsa`. `tests/conftest.py` switches `DB_NAME` before importing the app and **refuses to run unless the name ends in `_test`**.
- Migrations run once per session. Each test runs in one transaction that is rolled back, so nothing is left behind.
- Google is always mocked, and the tests never call it.
- Time-dependent rules (rate limits, refresh grace, expiry) use a controllable clock (`app/core/clock.py`), never `sleep`.
- Argon2 uses cheap parameters in tests for speed.
- Images go to a temporary `MEDIA_ROOT` per test.
- MySQL FULLTEXT only sees committed rows, so the search tests (`tests/catalog/test_search.py`) really commit to `lamsa_test` and wipe the catalog and user tables afterwards.
- The list, detail, facets and category tree endpoints have a query-budget test (6 or fewer SQL statements, independent of page size).
- The catalog cache is an in-memory fake per test (same interface as Valkey); `tests/core/test_valkey_integration.py` runs against a real Valkey and is skipped when none is reachable. Tests that change catalog rows with direct SQL call `bump_catalog()`, which is what an admin change does after its commit.
- Pushes never leave the machine: a fake sender captures them, and background dispatch is off, so tests call the dispatcher themselves. The notification pipeline gets its own session inside the test transaction. Query-budget tests count its statements separately from the request's.

## Migrations

```powershell
.\venv\Scripts\alembic.exe revision --autogenerate -m "describe change"
.\venv\Scripts\alembic.exe upgrade head
.\venv\Scripts\alembic.exe check      # must report "No new upgrade operations detected."
```

Always review autogenerated migrations by hand. Autogenerate can miss or mangle generated columns, FULLTEXT indexes, CHECK constraints, JSON expression defaults and table options. It also tends to drop FK-backing indexes before their tables in `downgrade()`, which MySQL refuses (error 1553).

---

## Auth for app developers

All endpoints are under `/api/v1`. There is no SMS anywhere. Firebase is used only for push notifications, not for sign-in.

### Tokens

Every successful sign-in returns a **TokenPair**:

```json
{
  "access_token": "eyJ...",
  "refresh_token": "Yq3...",
  "token_type": "bearer",
  "expires_in": 900,
  "user": {"id": 1, "name": "ليلى", "phone": "+4915123456789", "email": null, "role": "customer",
           "auth_provider": "phone", "must_change_password": false, "created_at": "2026-10-22T17:00:00"}
}
```

- **Access token:** send it as `Authorization: Bearer <access_token>`. It's valid for 15 minutes.
- **Refresh token:** store it securely (Keychain or Keystore). It's valid for 60 days, and **each refresh token works once**.
  - `POST /auth/refresh {refresh_token}` returns a new pair. Store the new refresh token immediately.
  - **Send only one refresh request at a time.** Queue other API calls until the refresh finishes. A duplicate within 20 seconds is tolerated as a safety net, not as the normal mechanism.
  - Reusing an old refresh token after that is treated as theft, and all sessions from that login are ended (`TOKEN_REUSED`).
- When a call returns `401 TOKEN_EXPIRED`, refresh once and retry. If the refresh fails, send the user to the login screen.
- Login and register bodies accept an optional `device_name` (for example `"iPhone 15"`). The user agent and IP are taken from the request.

### Phone + password

| Step | Request | Result |
|---|---|---|
| Register | `POST /auth/register {name, phone, password, device_name?}` | `201` TokenPair, or `409 PHONE_TAKEN`, `422 INVALID_PHONE` / `WEAK_PASSWORD` |
| Login | `POST /auth/login {phone, password, device_name?}` | `200` TokenPair, or `401 INVALID_CREDENTIALS`, `429 TOO_MANY_ATTEMPTS` (wait `Retry-After` seconds), `403 ACCOUNT_DISABLED` |

- **Phone formats:** local (`0151 2345 6789`) and international (`+49…`, `0049…`) are accepted and stored as E.164 (`+4915123456789`). One account per phone.
- **Password rules:** 8–128 characters, not only digits, not the phone number. Error messages are in Arabic.
- **Login errors don't say whether the phone exists.** A wrong phone, a wrong password, or a Google account all give the same `INVALID_CREDENTIALS`.
- **Login rate limit:** 5 failures per phone and 20 per IP within 15 minutes.

### Google

1. Sign in with Google in the app and get an **ID token**.
2. `POST /auth/google {id_token, device_name?}`
   - `{"registered": true, "tokens": TokenPair}`: done.
   - `{"registered": false, "signup_token": "...", "profile": {"email", "name"}}`: show the **name + phone** screen, prefilled with `profile.name`. The phone isn't verified.
3. `POST /auth/google/complete {signup_token, name, phone, device_name?}` returns `201` TokenPair. The `signup_token` is valid for 10 minutes; after that the response is `TOKEN_EXPIRED`, so start again at step 2. Possible conflicts: `409 PHONE_TAKEN` and `409 ALREADY_REGISTERED`.

**Google client IDs:** create OAuth client IDs in the Google Cloud Console (APIs & Services → Credentials) for **Web**, **Android** (package name + SHA-1) and **iOS** (bundle ID). Put all of them in `GOOGLE_CLIENT_IDS`. The app must request the ID token for one of these IDs. On Android this is usually the **web** client ID, passed as `serverClientId`.

**Dev Google tokens:** with `GOOGLE_DEV_MODE=true` (development only), any `id_token` of the form `dev-google|<sub>|<email>|<name>` is accepted without Google, for example `dev-google|123|dev@example.com|Dev User`. These users get the Google subject `dev-<sub>`. The server logs a warning at startup.

### Sessions and account

| Endpoint | Notes |
|---|---|
| `POST /auth/logout {refresh_token}` | Ends this session. The access token is optional; if sent, it must belong to the same user. Always `204`. Call `POST /users/me/devices/unregister` first. |
| `POST /auth/logout-all` | Ends all sessions of the user. |
| `POST /auth/password/change {current_password, new_password}` | Phone accounts only. Ends all other sessions and returns a new TokenPair; store it. |
| `GET /users/me`, `PATCH /users/me {name?, email?}` | |
| `POST /users/me/phone {phone, current_password?}` | Phone accounts must send `current_password`. |
| `DELETE /users/me {current_password?}` | Phone accounts must send `current_password`. Accounts with orders are anonymised instead of deleted. |
| `/users/me/addresses` | `GET`, `POST`, `PATCH /{id}`, `DELETE /{id}`, `POST /{id}/default`. The first address is the default; there's a maximum of 10. |
| `POST /users/me/devices {fcm_token, platform}` | Register the FCM token (`android`, `ios` or `web`) for push notifications. |

### Forgot password

There is no self-service reset. The customer **calls the store**. An admin uses `POST /admin/users/{id}/reset-password`, which returns a 10-character temporary password once, and reads it to the customer. All the customer's sessions end. After logging in with it, `user.must_change_password` is `true`. Every endpoint except `GET /users/me`, `POST /auth/password/change`, `POST /auth/logout` and `POST /auth/refresh` returns `403 PASSWORD_CHANGE_REQUIRED` until the password is changed. The app should open the change-password screen when it sees that flag or that error.

### Admin

`GET /admin/users?search=&role=&is_active=&page=&page_size=` (newest first), `GET/PATCH /admin/users/{id} {is_active?, role?}` and `POST /admin/users/{id}/reset-password`. Deactivating a user ends their sessions and blocks their access token immediately. Admins can't deactivate or demote themselves.

Paginated responses look like `{"items": [...], "page": 1, "page_size": 20, "total": 57}`, with `page_size` from 1 to 100.

### Error format

Every error, including validation errors and unexpected ones, has this shape:

```json
{"error": {"code": "PHONE_TAKEN", "message": "رقم الهاتف مستخدم في حساب آخر.", "details": null}}
```

`code` is stable English, so branch on it. `message` is a short Arabic sentence you can show. For `VALIDATION_ERROR`, `details` is a list of `{field, message}`.

| HTTP | Code | When |
|---|---|---|
| 422 | `VALIDATION_ERROR` | Request body or parameters are invalid (`details` lists the fields) |
| 500 | `INTERNAL_ERROR` | Unexpected server error (details are logged, never returned) |
| 404 | `NOT_FOUND` | Unknown URL |
| 405 | `METHOD_NOT_ALLOWED` | Wrong HTTP method |
| other | `HTTP_ERROR` | Any other HTTP-level error |
| 401 | `NOT_AUTHENTICATED` | No `Authorization: Bearer` header |
| 401 | `TOKEN_INVALID` | Malformed or tampered token, wrong token type, unknown or expired refresh token |
| 401 | `TOKEN_EXPIRED` | Access token or signup token expired: refresh, or restart Google signup |
| 401 | `TOKEN_REUSED` | An old refresh token was reused. All sessions from that login are ended, so log in again |
| 401 | `INVALID_CREDENTIALS` | Wrong phone or password (also a wrong current password) |
| 429 | `TOO_MANY_ATTEMPTS` | Too many failed logins; wait `Retry-After` seconds (`details.retry_after`) |
| 403 | `ACCOUNT_DISABLED` | The account was deactivated by the store |
| 401 / 404 | `USER_NOT_FOUND` | The token's user no longer exists (401), or an admin looked up an unknown user (404) |
| 403 | `ADMIN_ONLY` | Admin endpoint called by a customer |
| 403 | `PASSWORD_CHANGE_REQUIRED` | A temporary password must be changed first |
| 401 | `GOOGLE_TOKEN_INVALID` | The Google ID token couldn't be verified |
| 409 | `ALREADY_REGISTERED` | This Google account already has an account; sign in instead |
| 422 | `INVALID_PHONE` | The phone number isn't valid |
| 409 | `PHONE_TAKEN` | The phone number belongs to another account |
| 422 | `WEAK_PASSWORD` | The password breaks a rule (the message says which) |
| 422 | `SAME_PASSWORD` | The new password equals the current one |
| 422 | `NOT_A_PASSWORD_ACCOUNT` | A Google account has no password to change or reset |
| 422 | `CURRENT_PASSWORD_REQUIRED` | A phone account must confirm with `current_password` |
| 404 | `ADDRESS_NOT_FOUND` | The address doesn't exist or isn't yours |
| 422 | `ADDRESS_LIMIT_REACHED` | Already 10 addresses |
| 422 | `CANNOT_MODIFY_SELF` | An admin tried to deactivate or demote themselves |
| 404 | `CATEGORY_NOT_FOUND` | Unknown or hidden category (also an unknown `category` filter) |
| 404 | `BRAND_NOT_FOUND` | Unknown or hidden brand |
| 404 | `PRODUCT_NOT_FOUND` | Unknown or hidden product (hidden products are always 404, never 403) |
| 404 | `VARIANT_NOT_FOUND` | Variant isn't part of the product or isn't active (also an image upload by a color with no active variant) |
| 404 | `IMAGE_NOT_FOUND` | Image isn't part of the product |
| 404 | `ATTRIBUTE_NOT_FOUND` | Attribute or variant-option definition isn't part of the category |
| 409 | `SLUG_TAKEN` | The explicit slug is used (or reserved: `facets`, `related`) |
| 409 | `SKU_TAKEN` | The SKU is used by any product or variant |
| 409 | `CATEGORY_NOT_EMPTY` | Category has children or products (`details`: counts); deactivate instead |
| 422 | `CATEGORY_DEPTH_EXCEEDED` | Only two levels; the parent must be top-level; a category with children can't get a parent |
| 409 | `BRAND_HAS_PRODUCTS` | Brand still has products; deactivate instead |
| 409 | `ATTRIBUTE_KEY_TAKEN` | Key already defined in this category |
| 409 | `ATTRIBUTE_IN_USE` | Products use this attribute (type change or delete; `details.products_count`; delete accepts `?force=true`) |
| 409 | `OPTION_IN_USE` | A removed option value is used by products or variants (`details.value`, `products_count`) |
| 409 | `VARIANT_OPTION_IN_USE` | Variants use this option (type change or delete) |
| 422 | `UNKNOWN_ATTRIBUTE` | Attribute key isn't defined for the product's category (`details.key`) |
| 422 | `INVALID_ATTRIBUTE_VALUE` | Value doesn't match the attribute type (`details.key`, `expected`) |
| 422 | `MISSING_REQUIRED_ATTRIBUTE` | Required attributes are empty (`details.keys`) |
| 422 | `INVALID_VARIANT_OPTIONS` | Unknown option key, bad value or hex, or variants with different option keys |
| 409 | `DUPLICATE_VARIANT` | A variant with the same options exists |
| 422 | `PRODUCT_TYPE_IMMUTABLE` | `type` can't change |
| 422 | `INVALID_PRODUCT_FOR_TYPE` | Field not allowed for this type, or a variant/bundle endpoint used on the wrong type (`details.field`, `type`) |
| 422 | `PRODUCT_NOT_PUBLISHABLE` | Can't activate; `details.reasons` lists all of `NO_IMAGES`, `MISSING_REQUIRED_ATTRIBUTE`, `NO_ACTIVE_VARIANTS`, `NO_BUNDLE_ITEMS`, `CATEGORY_INACTIVE`, `BRAND_INACTIVE` |
| 409 | `PRODUCT_HAS_ORDERS` | Product appears in orders; deactivate instead |
| 409 | `PRODUCT_IN_BUNDLE` | Product is a component of a gift box |
| 409 | `VARIANT_HAS_ORDERS` | Variant appears in orders; deactivate instead |
| 409 | `VARIANT_IN_BUNDLE` | Variant is a component of a gift box |
| 422 | `INVALID_BUNDLE_ITEM` | Item is the bundle itself or another bundle, has a missing or foreign variant, a duplicate, … (`details.index`, `reason`) |
| 422 | `STOCK_NEGATIVE` | A stock adjustment would go below 0 |
| 422 | `IMAGE_LIMIT_REACHED` | More than 15 images per product |
| 422 | `IMAGE_ORDER_MISMATCH` | The reorder list isn't exactly the product's images |
| 413 | `FILE_TOO_LARGE` | Upload bigger than `MAX_UPLOAD_MB`, or an image over ~40 megapixels |
| 422 | `INVALID_IMAGE` | Not a JPEG, PNG or WEBP image |
| 422 | `INVALID_FILTER` | Unknown or non-filterable filter key, bad value for its type, or `min_price` > `max_price` (`details.param`) |
| 404 | `CART_ITEM_NOT_FOUND` | The cart line doesn't exist or isn't yours |
| 422 | `VARIANT_REQUIRED` | A variable product needs `variant_id` |
| 422 | `VARIANT_NOT_ALLOWED` | A simple product or bundle takes no `variant_id` |
| 422 | `QUANTITY_LIMIT_EXCEEDED` | More than `cart_max_quantity_per_line` of one item (`details.max`). Quantity 0 or less is `VALIDATION_ERROR` |
| 409 | `INSUFFICIENT_STOCK` | Fewer in stock than requested. `details` is always `{product_id, variant_id, available}` (`variant_id` null for simple products and bundles): cart, favorites and checkout |
| 409 | `OUT_OF_STOCK` | Nothing in stock (also `move-to-cart` when no variant is in stock). `details`: `{product_id, variant_id}` |
| 422 | `CART_LIMIT_REACHED` | More than `cart_max_lines` different lines (`details.max`) |
| 422 | `FAVORITES_LIMIT_REACHED` | More than `favorites_max` favorites (`details.max`) |
| 404 | `ORDER_NOT_FOUND` | The order doesn't exist or isn't yours |
| 422 | `CART_EMPTY` | Checkout with an empty cart |
| 409 | `CART_HAS_ISSUES` | A cart line has an issue; `details.cart` is the current cart, so show it |
| 422 | `DELIVERY_METHOD_NOT_FOUND` | Unknown or inactive delivery method |
| 422 | `ADDRESS_REQUIRED` | The delivery method needs `address_id` |
| 409 | `PRICE_CHANGED` | The total differs from `expected_total`; `details`: `{expected_total, total, cart}` |
| 409 | `ORDER_NOT_CANCELLABLE` | The order can't be cancelled in its current status (`details.status`) |
| 422 | `INVALID_STATUS_TRANSITION` | Not the next step of the workflow; `details`: `{from, to, allowed}` |
| 404 | `NOTIFICATION_NOT_FOUND` | The notification isn't in your inbox (for admins: it doesn't exist) |
| 409 | `NOTIFICATION_NOT_EDITABLE` | Edit, cancel or send-now on a campaign that is no longer `scheduled` (`details.status`) |
| 422 | `INVALID_SCHEDULE` | `scheduled_at` is less than 1 minute or more than 90 days ahead (`details`: `{earliest, latest}`) |
| 422 | `INVALID_LINK` | A banner or campaign link breaks the link rules (`details`: `{reason, link_type, value?}`) |
| 404 | `BANNER_NOT_FOUND` | Unknown banner |
| 503 | `SERVICE_BUSY` | Overloaded right now (hashing queue full, database pool exhausted, persistent deadlock). Wait `Retry-After` seconds and retry once (`details.retry_after`) |
| 422 | `BANNER_ORDER_MISMATCH` | `banner_ids` doesn't list every banner exactly once (`details.expected`) |

`OUT_OF_STOCK`, `INSUFFICIENT_STOCK` and `QUANTITY_LIMIT_EXCEEDED` are also used as cart **line issue** codes (same meaning, see below). `PRODUCT_NOT_FOUND` and `VARIANT_NOT_FOUND` (404) are reused by the cart for hidden products and missing or inactive variants.

---

## Catalog for app developers

All catalog endpoints are public (no token needed). Money is always a **string with two decimals**, for example `"8.95"`. Hidden items (inactive, or in a hidden category or brand) return `404`.

| Endpoint | Returns |
|---|---|
| `GET /categories` | Visible tree: `[{id, name, slug, description, image, product_count, children: [...]}]`. Counts include children. |
| `GET /categories/{slug}` | Category plus `breadcrumb`, visible `children`, filterable `attributes` and `variant_options` (with inherited ones). Use these to build the filter screen. |
| `GET /brands?category=<slug>` | Visible brands with `logo` and `product_count`; with `category`, only brands that have products there. |
| `GET /brands/{slug}` | One brand with its `product_count`. |
| `GET /products` | Paginated product **cards** (filters below). |
| `GET /products/facets` | Counts for the filter UI (same filters, no page or sort). |
| `GET /products/{slug}` | Product **detail**. |
| `GET /products/{slug}/related?limit=8` | Up to 12 in-stock products from the same category (then the parent category). |

`image` / `logo` objects are `{thumb, medium, large}` URLs (300 / 800 / 1600 px WebP), or `null`.

### Filters (`/products` and `/products/facets`)

| Param | Meaning |
|---|---|
| `category=<slug>` | Category including its children |
| `brand=<slug>` | Repeatable; OR between brands |
| `q=<text>` | Search (Arabic or English) |
| `min_price`, `max_price` | On the shown price |
| `in_stock=true`, `on_sale=true`, `is_new=true` | Flags |
| `attr.<key>=<value>` | Attribute filter. Repeat a key for OR; different keys are AND. Multiselect matches if the product has any of the values. |
| `attr.<key>.min`, `attr.<key>.max` | Number attributes |
| `attr.<key>=true\|false` | Boolean attributes |
| `opt.<key>=<value>` | Repeatable; products with an **active** variant having that option value (for example a color) |
| `sort` | `newest` (default), `price_asc`, `price_desc`, `relevance` (default when `q` is given) |
| `page`, `page_size` | 1.., 1..100 (default 20) |

Examples:

```
GET /api/v1/products?category=makeup&brand=demo-luma&brand=demo-bella&on_sale=true
GET /api/v1/products?category=care&attr.skin_type=جافة&attr.skin_type=حساسة&in_stock=true
GET /api/v1/products?category=perfumes&attr.concentration=EDP&opt.size=50%20ml&sort=price_asc
GET /api/v1/products?q=ارغان
```

Keys must be filterable definitions of the selected category, or of any category when none is given. Anything else is `422 INVALID_FILTER`.

### Facets

`GET /products/facets` returns `brands: [{slug, name, count}]`, `price: {min, max}` and, **when a category is given**:
- `attributes`: select, multiselect and boolean attributes as `{key, label, type, values: [{value, count}]}`; numbers as `{key, label, unit, min, max}`.
- `options`: `{key, label, type, values: [{value, hex?, count}]}`.

Each facet ignores its own filter, so after choosing one brand the other brands still show with their counts. Selected values are listed even when their count is 0.

### Cards and sale prices

A card is `{id, slug, name, type, brand, category, image, price, compare_at_price, discount_percent, is_new, is_on_sale, in_stock, swatches}`.

- `price` is what the customer pays: the lowest current price, with variants included.
- When `compare_at_price` is not null, show it crossed out next to `price`, plus a badge with `discount_percent` (for example "-25%").
- `swatches` are up to 8 `{name, hex}` colors of a variable product, in variant order.

### Detail

The detail has everything in the card, plus:
- `description`.
- `breadcrumb`.
- `attributes`: `[{key, label, type, unit, value, display}]`, in the category's order. Show `display`, for example `"70 %"`, or multiselect values joined with `"، "`.
- `images`: `[{id, alt_text, variant_id, color, width, height, urls}]`.

By product type:
- **Simple and bundle:** `regular_price`, `sale_price`, `current_price`, `stock_status`, `stock_left`.
- **Variable:**
  - `variant_options: [{key, label, type, values: [{value, hex?}]}]`, limited to values that active variants have.
  - `variants: [{id, options, price, sale_price, current_price, discount_percent, stock_status, stock_left, image_ids}]`.
  - **When the user selects a variant, show `variant.image_ids` in that order.** That's the images of the variant's color (shared by all sizes of that color), followed by the general images.
- **Bundle:** `items: [{product: {id, slug, name, image}, variant_label, quantity}]`.

`price` and `compare_at_price` keep the card meaning everywhere; the extra price fields are for the selected item.

### Stock

Exact stock is never public. `stock_status` is one of:
- `in_stock`
- `low`: 1 to `low_stock_threshold` left (a store setting, default 3). `stock_left` then holds the count, for example "باقي ٣ قطع بس".
- `out`

### Search

Search is Arabic-aware. Letter variants are folded on both the stored text and the query: أ إ آ ٱ → ا, ى → ي, ة → ه, tashkeel and tatweel are removed, and Arabic-Indic digits become 0–9. Search covers the name, description, brand, category, attribute values and variant option values.

Every word must match. Operators and quotes are ignored, and words shorter than 2 letters are dropped. If nothing usable remains, the response is an empty page.

### Favorites hearts on catalog screens

`GET /products`, `GET /products/{slug}` and `GET /products/{slug}/related` accept an **optional** access token:
- With a token, every card and the detail have `is_favorite` set for that user.
- Without a token, `is_favorite` is always `false`.
- A token that is sent but invalid or expired returns `401`, so refresh it and retry.
- Browsing works even while `PASSWORD_CHANGE_REQUIRED` is pending; the gate doesn't apply to these public endpoints.

## Cart and favorites for app developers

All `/cart` and `/favorites` endpoints need a logged-in user, and the password-change gate applies.

### Guests

The API stores nothing for guests. Keep the guest cart and favorites **on the device**, and right after login or register send them once:

- `POST /cart/merge {items: [{product_id, variant_id?, quantity}]}` (at most 100 items). It never fails because of one bad item. The response is the cart plus `merge_report: [{index, product_id, variant_id, status, reason, quantity}]`:
  - `added` or `merged` (summed with an existing line).
  - `capped`, with `reason` `INSUFFICIENT_STOCK` or `QUANTITY_LIMIT_EXCEEDED`.
  - `skipped`, with `reason` `PRODUCT_NOT_FOUND`, `VARIANT_REQUIRED`, `VARIANT_NOT_ALLOWED`, `VARIANT_NOT_FOUND`, `OUT_OF_STOCK` or `CART_LIMIT_REACHED`.

  Clear the local cart after a successful response.
- `POST /favorites/merge {product_ids: [...]}` (at most 200) returns `{added, skipped: [{product_id, reason}]}`.

### Cart endpoints

Every mutating endpoint returns the **whole cart**, so the app needs one round trip.

| Endpoint | Notes |
|---|---|
| `GET /cart` | The cart |
| `POST /cart/items {product_id, variant_id?, quantity=1}` | `201` for a new line, `200` when an existing line grew (quantities add up) |
| `PATCH /cart/items/{id} {quantity?, variant_id?}` | Picking a variant that's already in the cart merges the two lines; the result is capped silently and reported in `notices` |
| `DELETE /cart/items/{id}`, `DELETE /cart` | |
| `POST /cart/resolve-issues` | The **"update my cart"** button: removes unavailable or out-of-stock lines and caps quantities. `notices` lists every change |

**The cart does not reserve stock.** Stock is checked when you add or update, and shown on every read. It's taken only when an order is placed, so the last item can sell out while it sits in a cart.

Store limits (settings): `cart_max_quantity_per_line` (10), `cart_max_lines` (50), `favorites_max` (200).

### Cart response

Each line has `{id, quantity, product {id, slug, name, type, brand}, variant {id, options, label} | null, image, unit_price, regular_price, compare_at_price, discount_percent, line_total, stock_status, stock_left, items (bundles), issue}`.

- **`image`:** the first image of the selected variant's color, otherwise the product's main image.
- **Prices:** live; nothing is stored in the cart. Show `compare_at_price` crossed out when it isn't null.
- **`items`:** for bundles, `[{name, variant_label, quantity}]`.

Summary fields:
- `items_count`, `lines_count`, `subtotal`, `savings`.
- `delivery_quotes: [{code, name, description, fee, is_free, free_from, amount_to_free_shipping, requires_address}]`, one per active delivery method. Show a free-shipping progress bar with `amount_to_free_shipping`, for example "باقي ٣٣ € للشحن المجاني".
- `can_checkout`: true only when the cart has lines and **no** line has an issue.
- `issues_count`.
- `notices`: what this request changed; always empty on `GET`.

**Line issues** (not errors). The line is still returned with `line_total` 0 and isn't counted in `subtotal`. Show the message on the line and offer "update my cart":

| `issue.code` | Arabic text | Fix |
|---|---|---|
| `UNAVAILABLE` | هذا المنتج لم يعد متوفراً. | Removed by resolve-issues |
| `VARIANT_UNAVAILABLE` | هذا الخيار (اللون أو الحجم) لم يعد متوفراً. | Removed (or pick another variant with PATCH) |
| `OUT_OF_STOCK` | نفد من المخزون. | Removed |
| `INSUFFICIENT_STOCK` | الكمية المتوفرة أقل من المطلوب. (`issue.available`) | Capped to `available` |
| `QUANTITY_LIMIT_EXCEEDED` | الكمية أكبر من المسموح لهذا المنتج. (`issue.max`) | Capped to `max` |

**Notices** (not errors), returned only by the request that made the change:

| `notices[].code` | Arabic text | Fields |
|---|---|---|
| `QUANTITY_CAPPED` | تم تعديل الكمية حسب المتوفر. | `item_id, from_quantity, to_quantity, reason` |
| `LINES_MERGED` | تم دمج منتجين متطابقين في سطر واحد. | `item_id, from_quantity, to_quantity` |
| `LINE_REMOVED` | تم حذف منتج لم يعد متوفراً. | `item_id, from_quantity, reason` |

### Favorites

| Endpoint | Notes |
|---|---|
| `GET /favorites?page=&page_size=` | Visible favorites as normal product cards, newest favorite first |
| `GET /favorites/ids` | `{product_ids}`: call once after login to fill the hearts |
| `PUT /favorites/{product_id}` | `204`, idempotent; `404` if the product isn't visible |
| `DELETE /favorites/{product_id}` | `204`, idempotent, never `404` |
| `POST /favorites/{product_id}/move-to-cart {variant_id?, quantity=1}` | Adds to the cart (same rules as `POST /cart/items`) and removes the favorite, in one step. Without `variant_id`, the first active variant in stock is used. If the add fails, the favorite stays |

A favorite whose product is hidden (deactivated, or in a hidden category or brand) isn't listed, but it is kept. It shows up again when the product is published again.

## Orders for app developers

Payment is cash on delivery only, with no minimum order and no coupons (`discount` is 0).

### Checkout

1. Show the cart (`GET /cart`). Checkout is possible only when `can_checkout` is true.
2. The customer picks a delivery method and, for delivery (not pickup), an address. Show `total = subtotal + delivery_quotes[method].fee`.
3. `POST /orders` with:
   - header `Idempotency-Key: <fresh UUID>`, generated **once per checkout attempt** and reused if you retry the same attempt after a network error;
   - body `{delivery_method_code, address_id?, customer_notes?, gift_message?, expected_total}`, where `expected_total` is the total you showed.

   Pickup takes no `address_id`.
4. Responses:
   - `201`: the new order (`OrderDetailOut`). Your cart is now empty.
   - `200`: the same Idempotency-Key was used before; this is that order, and nothing new happened.
   - `409 CART_HAS_ISSUES`, `PRICE_CHANGED` or `INSUFFICIENT_STOCK`: something changed since the cart was shown. **Reload the cart** (`details.cart` already contains it for the first two), show what changed, and let the customer confirm again with a **new** Idempotency-Key.
   - `422 CART_EMPTY`, `ADDRESS_REQUIRED` or `DELIVERY_METHOD_NOT_FOUND`.

Stock is taken when the order is placed and given back when it's cancelled. If two customers try to buy the last item at the same time, exactly one succeeds. The other gets `CART_HAS_ISSUES`, with that line marked `OUT_OF_STOCK`.

### Statuses

| status | label (delivery) | label (pickup) |
|---|---|---|
| `pending` | استلمنا طلبك | استلمنا طلبك |
| `preparing` | قيد التجهيز | قيد التجهيز |
| `shipped` | انشحن | (not used) |
| `ready_for_pickup` | (not used) | جاهز للاستلام |
| `delivered` | وصل | تم الاستلام |
| `cancelled` | ملغي | ملغي |

- Delivery: `pending → preparing → shipped → delivered`. Pickup: `pending → preparing → ready_for_pickup → delivered`.
- `status_label` is returned with every order; show it as is.
- On `delivered`, `payment_status` becomes `paid`.

### Customer endpoints

| Endpoint | Notes |
|---|---|
| `GET /orders?status=&page=&page_size=` | Newest first: `{id (order number), created_at, status, status_label, items_count, total, image, delivery_method_name}` |
| `GET /orders/{id}` | Full detail with item snapshots (name, variant label, options, image, prices, `bundle_contents`) and `product_slug`, which is set only while the product is still visible |
| `POST /orders/{id}/cancel {reason?}` | **Only while `pending`** (`can_cancel`); afterwards only the store can cancel. Stock is restored |
| `POST /orders/{id}/reorder` | Puts the items back into the cart with the cart-merge rules and returns the cart plus `merge_report`; deleted or unavailable items are `skipped` |

`timeline` lists every step of the order's workflow, `[{status, label, reached_at, is_current}]`, with `reached_at` null for steps not reached yet. A cancelled order shows the steps it reached, then a `cancelled` step with `cancel_reason`.

## Admin order workflow

`GET /admin/orders?status=&delivery_method=&q=&date_from=&date_to=&sort=newest|oldest|total_desc`:
- **Numeric `q`** (digits, ignoring spaces, `+`, `-` and brackets) matches the **order number exactly** *or* a **customer phone containing those digits**. Leading zeros are ignored for the phone, so `6773`, `0151 23456773`, `+49 151 23456773` and `0049-151-…` all find `+4915123456773`. An exact order-number match is always listed first, then the rest by the chosen sort.
- **Any other `q`** matches part of the customer's name or phone.
- Dates are **UTC**, and `date_to` is inclusive.

`GET /admin/orders/{id}` returns the full detail plus `customer {user_id, name, phone, orders_count}`, `history` (who changed what, when, and the note), `allowed_next_statuses` and `can_cancel`.

- **Status buttons:** render one button per `allowed_next_statuses` entry. It's always exactly the next step. Moving calls `POST /admin/orders/{id}/status {to_status, note?}`; any other move returns `422 INVALID_STATUS_TRANSITION`.
- **Cancelling:** `POST /admin/orders/{id}/cancel {reason (3–255 characters), restore_stock=true}` works from `pending`, `preparing`, `shipped` or `ready_for_pickup`. Use `restore_stock=false` only for `shipped` orders whose goods are lost or not returned.
- **Stats:** `GET /admin/orders/stats?date_from=&date_to=` returns counts per status, `delivered_revenue`, `average_order_value`, `cancelled_rate` and the top 5 products by quantity (delivered orders only).

Domain events (`OrderPlaced`, `OrderStatusChanged`, `OrderCancelled`, `StockLow`, `StockOut`, `ProductBackInStock`, `ProductPriceDropped`, and the cache events `CatalogChanged` and `ProductsChanged`) are published in-process after each commit (`app/shared/events`, `commit_and_publish`). Nothing is published for a transaction that rolled back. The notification handlers subscribe to these events (see below).

## Notifications for app developers

Every notification is a row in the user's **inbox**. It is also pushed (FCM) to each of the user's registered devices (`POST /users/me/devices`). A user without devices still gets the inbox row.

### What is sent, and when

| `type` | Who | When | Title (Arabic, from `app/modules/notifications/templates.py`) |
|---|---|---|---|
| `order_update` | the customer | order placed | استلمنا طلبك #<id> ✅ |
| | | preparing | طلبك #<id> قيد التجهيز |
| | | shipped | طلبك #<id> انشحن 🚚 |
| | | ready for pickup | طلبك #<id> جاهز للاستلام 🛍️ |
| | | delivered (delivery / pickup) | وصل طلبك #<id>، نتمنى تعجبك المنتجات / تم استلام طلبك #<id>، شكراً إلك |
| | | cancelled by the store | انلغى طلبك #<id> (the reason is the body) |
| `admin_alert` | every **active** admin when the alert is created | new order | طلب جديد #<id> (items, total and delivery method in the body) |
| | | customer cancelled | الزبون لغى الطلب #<id> |
| | | stock low / out | <product> (<variant>): باقي <n> قطع / <product> (<variant>) خلص من المخزون |
| `marketing` | everyone | an admin campaign | (written by the admin) |
| `message` | one user | an admin message | (written by the admin) |
| `favorite_back_in_stock` | fans of the product | it is in stock again | رجع للمخزون 🎉 |
| `favorite_price_drop` | fans of the product | a sale starts, or `price_from` drops by at least `price_drop_min_percent` (5) | نزل السعر 🔥 |
| `cart_reminder` | the cart owner | `cart_reminder_after_hours` (24) after the last cart change, with no order since | سلتك ناطرتك 🛍️ (the body names the first available product and the number of other lines) |

Rules:
- When the **customer** cancels their own order, the customer gets nothing (the admins get an alert).
- Favorite alerts have a cooldown of `favorite_alert_cooldown_days` (7) per kind and product.
- A cart reminder is sent once per cart change. It isn't sent when the cart has been left alone for more than 72 hours past the reminder time, or when nothing in it can be bought right now.

### Preferences and quiet hours

`GET /users/me/notification-preferences` returns the switches. `PATCH` it with any of `notify_marketing`, `notify_favorites` and `notify_cart_reminders` (all true by default). Order updates can't be switched off, so `order_updates` is always true. When `notify_marketing` is off, marketing is also **hidden from the inbox**.

**Quiet hours** run from 21:00 to 09:00 Europe/Berlin, including daylight saving time (store settings `quiet_hours_start`, `quiet_hours_end` and `timezone`). They apply to marketing, favorite alerts and cart reminders, which are held until the next 09:00. Order updates, admin alerts and one-user messages are never held back.

### Inbox endpoints

| Endpoint | |
|---|---|
| `GET /notifications?page=&page_size=` | `{items, page, page_size, total, unread_count}`, newest first. Item: `{id, type, title, body, link {type, value}, order_id, sent_at, is_read, read_at}` |
| `GET /notifications/unread-count` | `{unread_count}`, for the badge |
| `POST /notifications/{id}/read` | Idempotent; returns `{unread_count}`. `404 NOTIFICATION_NOT_FOUND` if it isn't in your inbox |
| `POST /notifications/read-all` | Returns `{unread_count: 0}` |

If a link's target (product, category or brand) has been hidden since, the link is shown as `{type: "none"}` and the text stays.

### Push payload and deep links

Every push has a `notification` (title and body) and this `data`. All values are strings, and empty when not set:

```json
{"notification_id": "123", "type": "order_update", "link_type": "order", "link_value": "45", "order_id": "45"}
```

| `link_type` | Open |
|---|---|
| `none` | the inbox (for `cart_reminder`, open the cart) |
| `product` / `category` / `brand` | that screen; `link_value` is the slug |
| `new` / `sale` | the new-arrivals / sale list |
| `url` | `link_value` (always https) in the browser |
| `order` | the customer's order `link_value` (always one of the recipient's own orders) |
| `admin_order` / `admin_product` | the admin app's order / product screen (only ever sent to admins) |

- **Delivery is at least once.** After a crash, a push may arrive twice. **The app must ignore a push whose `notification_id` it has already shown.** Test pushes from `POST /admin/notifications/test` have an empty `notification_id` and `"test": "1"`.
- **Android channels:** create `orders` (high importance, for order updates and admin alerts) and `marketing` (everything else). Every push sets its `channel_id`.
- **iOS:** the sound is `default`, and `badge` is the unread count including this notification.
- After the user opens a push, call `POST /notifications/{notification_id}/read`.

## Banners for app developers

`GET /banners` (public) returns at most 10 active banners within their time window, in the admin's order: `[{id, title, subtitle, image {thumb, medium, large}, link {type, value}}]`. Banners that link to a hidden product, category or brand are skipped. Links use the catalog types from the table above: `none`, `product`, `category`, `brand`, `new`, `sale` and `url`.

## Admin campaigns and banners

**Campaigns:** `POST /admin/notifications {audience, user_id?, title (≤150), body (≤500), link? {type, value}, scheduled_at?}`.
- `audience: "all"` sends `marketing` to everyone who has `notify_marketing` on. `audience: "user"` with a `user_id` sends a `message` to that one user.
- Without `scheduled_at` it goes out now. A `scheduled_at` must be 1 minute to 90 days ahead (`422 INVALID_SCHEDULE`). Times are UTC unless you give an offset, as in `2026-07-02T10:00:00+02:00`.
- Marketing that would go out during quiet hours is moved to their end. The response then has `status: "scheduled"`, the new `scheduled_at` and `deferred_by_quiet_hours: true`.
- Links use catalog types only:
  - `category`, `product` and `brand` need an existing slug;
  - `url` must be https and at most 255 characters;
  - `none`, `new` and `sale` take no value.

  Anything else returns `422 INVALID_LINK`.
- `GET /admin/notifications?status=&type=&audience=` lists campaigns (marketing and message; pass `type` to see other kinds). Once a campaign is sent, it shows `recipients_count`, `success_count` and `failure_count`. `GET /admin/notifications/{id}` returns one campaign.
- While a campaign is `scheduled`, you can edit it with `PATCH` (title, body, link, scheduled_at), cancel it with `POST /{id}/cancel`, or send it with `POST /{id}/send-now`. Send-now goes out right away, even during quiet hours. In any other status these return `409 NOTIFICATION_NOT_EDITABLE`.
- `POST /admin/notifications/test {title?, body?}` pushes to **your own devices only** and creates no inbox row. It returns the result for each device (`OK`, `UNREGISTERED`, …), which is a quick way to check the Firebase setup.

**Banners:** `GET /admin/banners` lists all banners with their `state`: `inactive`, `scheduled`, `active` or `expired`.
- `POST /admin/banners` is a multipart form:
  - `image` (required; JPEG, PNG or WEBP);
  - `title`;
  - optional: `subtitle`, `link_type`, `link_value`, `is_active` (`true`/`false`, default true), and `starts_at` / `ends_at` (UTC).

  A new banner goes to the end of the order.
- `PATCH /admin/banners/{id}` takes JSON; `null` clears `subtitle`, `starts_at` and `ends_at`. `PUT /admin/banners/{id}/image` replaces the image, and `DELETE /admin/banners/{id}` deletes the banner. Old image files are removed only after the database change is committed.
- `PUT /admin/banners/order {banner_ids}` must list every banner exactly once, or it returns `422 BANNER_ORDER_MISMATCH`.

## Push notifications: Firebase and the worker

**How it works:**
1. Events (order placed, status changed, stock low, back in stock, …) are published only after their transaction commits.
2. Handlers write the notification rows (an outbox) in their own transaction and start a background send. Pushing never happens inside a request's transaction, and a failed push never fails a request.
3. The **dispatcher** claims due rows with `FOR UPDATE SKIP LOCKED`, so two workers never send the same row. It pushes in batches of 500 and stores the counts.
4. Tokens that FCM reports as `UNREGISTERED`, `INVALID_ARGUMENT` or `SENDER_ID_MISMATCH` are deleted. `UNAVAILABLE` and `INTERNAL` errors are retried once.
5. If a worker crashes while sending, the row stays in `sending`. After 10 minutes it is queued again.

**The worker** runs the periodic jobs:
- dispatch every 30 s;
- recover stuck rows every 5 min;
- cart reminders every 15 min (one worker at a time, through a MySQL `GET_LOCK`);
- a daily cleanup of inbox rows older than `notifications_retention_days` (180).

In production, run it as its own process. It stops gracefully on SIGTERM:

```powershell
.\venv\Scripts\python.exe -m app.workers.run
```

`RUN_WORKER_IN_APP=true` makes the API process run the worker itself, so `fastapi dev` is enough locally. It defaults to true only when `APP_ENV=development`. In production, set it to false and run one or more worker processes.

**Firebase setup:**
1. In the Firebase console, open *Project settings → Service accounts → Generate new private key*.
2. Save the JSON file in `secrets/`, for example `secrets/firebase-service-account.json`. The folder is git-ignored; **never commit the file**.
3. In `.env`, set `PUSH_BACKEND=firebase` and `FIREBASE_CREDENTIALS_PATH=./secrets/<file>.json`. With `PUSH_BACKEND=firebase`, the app refuses to start if the file is missing or unreadable.
4. Sign in to the admin app on a phone and call `POST /admin/notifications/test`.

With `PUSH_BACKEND=log` (the default), pushes are only written to the log. Use it for development without Firebase. Tests always use a fake sender.

### FCM registration tokens and FIDs

**Pin:** `firebase-admin` is pinned at **7.7.0** (`requirements.txt`). Before any upgrade, and especially to 8.x, re-check the `token` / `fid` situation below. A major version may remove `Message.token`.

**Why we still send with `token`:** Since firebase-admin 7.5.0, `Message.token` is deprecated in favor of `Message.fid`, the Firebase Installation ID. FCM is moving from registration tokens (the value `getToken()` returns) to FIDs. An FID is a different identifier, so `fid` is **not** a drop-in replacement for the registration tokens apps send today. During FCM's migration period, though, ["the `token` field also accepts FIDs"](https://firebase.google.com/docs/cloud-messaging/send/admin-sdk). So `token` works for every client, whether it registered with a token or an FID. The backend uses `token` and silences only that one deprecation warning, only where the message is built (`app/integrations/firebase/push.py`). The error codes we rely on (`UNREGISTERED`, `INVALID_ARGUMENT`, `SENDER_ID_MISMATCH`) are unchanged and apply to FIDs too.

**Migration plan (no schema change):**
1. The app registers with FIDs and sends the FID to `POST /users/me/devices` in the same `fcm_token` field. The column holds up to 512 characters, and an FID is much shorter.
2. Once every active client sends FIDs (or before `token` is removed, whichever comes first), change the one line in `fcm_message()` from `token=m.token` to `fid=m.token` and remove the warning filter.

**For the app developers:** the app isn't built yet, so you may register with FIDs from day one, if your Firebase SDK supports FID registration as a **stable** (not beta) feature. Don't mix the token and FID registration APIs in one app. The backend accepts both today through the `token` field, so nothing else changes. If you do use FIDs, tell the backend team; we switch to `fid=` once that is confirmed in production.


## Admin catalog workflow

Admin endpoints live under `/api/v1/admin/...` and need an admin token.

Admin responses show everything: inactive items, exact stock, SKUs, timestamps, `publishable: {ok, reasons}`, and `warnings`. For example, `COLOR_WITHOUT_IMAGES` lists active colors that have no images yet; it's a warning only and doesn't block publishing.

1. **Categories:** `POST /admin/categories` (two levels at most). Add definitions with `POST /admin/categories/{id}/attributes` (text, number, select, multiselect, boolean, date) and `/variant-options` (color or select). Children inherit their parent's definitions, and a child definition with the same key wins.
2. **Brands:** `POST /admin/brands`, then `PUT /admin/brands/{id}/logo` (multipart `file`).
3. **Create the product:** `POST /admin/products`. It's always created **inactive**.
   - `simple` needs `price` and `stock`.
   - `variable` takes `variants: [{options, price, sale_price?, stock, sku?}]`. Color options carry `"<key>_hex": "#RRGGBB"`.
   - `bundle` needs `price`, and takes `bundle_items: [{product_id, variant_id?, quantity}]`.
   - SKUs are generated when omitted: `LM-<id>` for products, `LM-<id>-<n>` for variants.
4. **Add images:** `POST /admin/products/{id}/images` (multipart, 1–8 `files`, at most 15 per product). Optionally send `variant_id` or `color=<value>` to link the images to a color.
5. **Publish:** `PATCH /admin/products/{id} {"is_active": true}`. Every failing reason comes back at once in `PRODUCT_NOT_PUBLISHABLE`.
6. **Day to day:**
   - `POST …/stock-adjust {delta}` (atomic) on products and variants.
   - `GET /admin/products/low-stock`.
   - `PUT …/bundle-items` (replaces the whole list).
   - `PUT …/images/order {image_ids}` (the first becomes the main image).
   - `PATCH` on products, variants and images.
   - Deactivate instead of deleting anything that has orders.
   - `PATCH` with `attributes` **replaces** the whole object; keys set to `null` are dropped.

Images are removed from storage only after the database change commits, and new files are cleaned up if the commit fails.

---

## Performance: caching and load protection

### What is cached

Only anonymous catalog responses are cached, as JSON bytes in Valkey. **Cart, checkout, stock, auth, orders, notifications and everything under `/admin` are never cached.**

| Endpoint | TTL | Fresh again after |
|---|---|---|
| `GET /categories`, `GET /categories/{slug}` | 300 s | any admin catalog change |
| `GET /brands` (with or without `?category=`), `GET /brands/{slug}` | 300 s | any admin catalog change |
| `GET /banners` | 60 s | any admin catalog change (a banner whose time window starts or ends shows up within 60 s) |
| `GET /products` (all query parameters), `GET /products/facets` | 45 s | any admin catalog change; **not** after orders (see below) |
| `GET /products/{slug}` | 30 s | any admin catalog change, **and** any change to that product's stock or price (orders, cancellations, stock adjusts) |
| `GET /products/{slug}/related` | 120 s | any admin catalog change |

TTLs vary by ±10 % so that entries don't all expire at once.

- **`is_favorite` is never stored.** The cache holds the anonymous response. For a logged-in user, `is_favorite` is filled in with one query per request, as before.
- **Product pages are fresh; lists may be stale.** A product page shows "out of stock" right after the last unit is sold. Lists, facets and related products may show it as in stock for up to their TTL. That's on purpose: checkout re-checks stock in its own transaction, so a stale list can never sell something that isn't there.
- **Invalidation uses versions, never key scans.** Every key contains a global catalog version, and product pages also contain a per-product version. Both are random tokens, bumped only after a successful commit (through `commit_and_publish`):
  - every POST, PUT, PATCH or DELETE on `/admin/categories`, `/admin/brands`, `/admin/products` and `/admin/banners` bumps the catalog version;
  - any stock or price change (orders, cancellations, stock adjusts, edits) bumps the version of the products concerned.

  Key format: `lamsa:c:<CACHE_SCHEMA_VERSION>:<catalog version>:<kind>:<parts>` (product pages: `…:p:<product version>:product:<slug>`). List keys hash the normalized query (parameters sorted, defaults filled in, unknown parameters ignored).
- **Deploys and code changes.** Every API process bumps the catalog version when it starts, so a new deploy never serves entries written by the old code. **Bump `CACHE_SCHEMA_VERSION` in `app/core/cache.py` whenever a cached response model changes.**
- **Settings and delivery fees** are kept per process for 60 s, because many requests read them. `seed_base` and SQL changes reach the other processes within 60 s. **Checkout always reads delivery fees fresh**: a fee shown stale in the cart ends in a clean `409 PRICE_CHANGED`, never a wrong charge.
- **Stampedes are prevented.** One computation per key runs at a time across all processes (a lock with `SET NX PX`); the others wait up to 1 s for its result.
- **The cache is optional.** If Valkey is down or slow, requests skip it: after 3 consecutive errors, the cache is skipped for 5 s, and a warning is logged at most once a minute. A cache problem never causes an error response. Each process uses a bounded pool of 32 connections.

**Running Valkey (development):** `docker compose up -d valkey` (Valkey 9.1.2; only on 127.0.0.1:6379, 256 MB, LRU, no persistence). Settings:

| Key | Default | Notes |
|---|---|---|
| `CACHE_ENABLED` | `true` | `false` turns the cache off completely |
| `CACHE_URL` | `redis://127.0.0.1:6379/0` | |
| `CACHE_KEY_PREFIX` | `lamsa:` | |
| `CACHE_TIMEOUT_MS` | `1000` | connect and read timeout per call (see *Production tuning*) |

### HTTP caching (for app developers)

- **Cached public endpoints, without `Authorization`:** `Cache-Control: public, max-age=<TTL>, stale-while-revalidate=60`, a weak `ETag` and `Vary: Authorization`.
- **The same endpoints with `Authorization`:** `Cache-Control: private, no-cache` (the response contains your `is_favorite` values).
- **Everything else:** `Cache-Control: no-store`.

To revalidate, send the last `ETag` in `If-None-Match`; an unchanged response comes back as `304` with no body. Images under `/media` are immutable: cache them forever.

**`503 SERVICE_BUSY`** means the server is overloaded right now: the password-hashing queue is full, the database pool is exhausted, or a database deadlock persisted. It always carries `Retry-After` (seconds). **Wait that long and retry once**; if it fails again, show the error. The request had no effect.

### Load protection

- **Password hashing.** Argon2id with the OWASP baseline (19 MiB, t=2, p=1; about 40 ms per hash on a laptop). Older hashes still verify and are re-hashed on the next login.
  - At most `PASSWORD_HASH_CONCURRENCY` hashes run at once per process; the default is the number of cores. With several workers on one machine, set it to *cores ÷ workers*.
  - A request that waits longer than `PASSWORD_HASH_QUEUE_TIMEOUT` (5 s) gets `503 SERVICE_BUSY`. This applies to every path: register, login (including the dummy check for unknown phones), password change, admin reset and the `create_admin` script.
  - The database connection is released while a request waits for hashing.
- **Database pool.** Settings: `DB_POOL_SIZE` (10), `DB_MAX_OVERFLOW` (10), `DB_POOL_TIMEOUT` (5 s), `DB_POOL_RECYCLE` (3600 s). A request that waits longer than the timeout gets `503 SERVICE_BUSY`, not a 500. Authenticated requests release their connection right after the user is loaded.
- **Deadlocks** (MySQL 1213) in cart writes and checkout are retried up to 3 times with a short random backoff. After that the request returns `503 SERVICE_BUSY`.
- **Observability.** One JSON line per request (logger `app.access`), for example:

  `{"request_id", "method", "path" (the route template), "status", "duration_ms", "db_statements", "db_ms", "cache": {"hit", "miss", "bypass"}}`

  - `X-Request-ID` is echoed back, or generated when it's missing.
  - Statements slower than `SLOW_QUERY_MS` (200) and requests slower than `SLOW_REQUEST_MS` (1000) are logged as warnings (logger `app.slow`).
  - In development, `Server-Timing` shows app and database time.

## Load testing

Everything runs against the dedicated database **`lamsa_load`**, never `lamsa` or `lamsa_test`. The seed script, the monitor and the run script all refuse any other database. Push notifications use the log backend.

One-time setup:

```powershell
# As MySQL root (once):
#   CREATE DATABASE lamsa_load CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
#   GRANT ALL ON lamsa_load.* TO 'lamsa'@'localhost';
$env:DB_NAME = "lamsa_load"
.\venv\Scripts\alembic.exe upgrade head
.\venv\Scripts\python.exe -m scripts.seed_base
python -m venv .venv-load                                   # Locust stays out of requirements.txt
.\.venv-load\Scripts\python.exe -m pip install -r loadtest\requirements.txt
docker compose up -d valkey
```

Run (about 11 minutes each; results in `loadtest\results\cache-on|off\`):

```powershell
.\scripts\run_loadtest.ps1 -Cache on
.\scripts\run_loadtest.ps1 -Cache off
```

The script:
1. Re-seeds `lamsa_load` with `scripts/seed_load_data.py`: 3,000 products of every type, 10,000 users with devices, addresses, favorites and carts, all with the password `LoadTest-pass1`.
2. Starts 4 API processes (ports 8001–8004) and the worker process with production-like settings.
3. Runs Locust in four phases:
   - **browse:** anonymous browsing, logged-in browsing and shoppers, at 50, 200, 500 and 1,000 users;
   - **flash:** 300 buyers for one product with stock 50, then a database check that stock never went negative and units sold equal the starting stock;
   - **login:** a login burst;
   - **broadcast:** a campaign to all 10,000 users while browsing continues.

`TRUST_PROXY_HEADERS=true` is set **inside the run script only**, so each simulated user can send its own fake client IP. **Never enable it in production unless the API is behind a trusted proxy that overwrites `X-Forwarded-For`** (as `docker/nginx/lamsa.conf` does).

Results and their interpretation: `loadtest/RESULTS.md`.

## Production tuning

- **Processes.** Run one Uvicorn worker per core, either `uvicorn app.main:app --workers 4 --proxy-headers --timeout-keep-alive 15` or Gunicorn with `-k uvicorn.workers.UvicornWorker -w 4`. Behind Nginx, keep-alive on both sides (`keepalive 32` upstream). Set `RUN_WORKER_IN_APP=false` and run `python -m app.workers.run` as its own service.
- **Database connections:** workers × (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`) + worker process (≈ 20) + headroom for admin, backups and migrations (≈ 20) ≤ MySQL `max_connections`.

  Example, 4 cores: 4 × (10 + 10) + 20 + 20 = 120 → `max_connections = 150`.
- **MySQL on a small VPS** (see `docker/mysql/my.cnf`, not applied by anything):
  - `innodb_buffer_pool_size` about 50–60 % of the RAM MySQL may use;
  - slow query log on with `long_query_time = 0.2`;
  - `innodb_flush_log_at_trx_commit = 1` (orders must be durable).
- **Password hashing:** `PASSWORD_HASH_CONCURRENCY` = cores ÷ workers (e.g. 1 with 4 workers on 4 cores).
- **Cache:** run Valkey on the same host or a private network, never exposed, with `maxmemory` and `allkeys-lru`.
  - Keep `CACHE_TIMEOUT_MS` at about 1 s. On a busy asyncio worker the timeout also measures event-loop lag, and the load test showed that 150 ms switched a healthy cache off exactly under load.
- **Nginx:** `docker/nginx/lamsa.conf` (validated with `nginx -t`). It provides:
  - a 2 s micro-cache for anonymous catalog GETs;
  - gzip;
  - `/media` with 1-year immutable caching;
  - per-IP rate limits (strict on login, register and Google sign-in) that return the API's JSON error format;
  - `X-Forwarded-For` overwritten with the real client address.

  Try it locally with the commented `nginx` service in `docker-compose.yml` (profile `proxy`).

## Schema conventions

- **Models:** `app/modules/<module>/models.py`. Every model must be imported in `app/db/models.py`. The session module imports it, so all relationships resolve.
- **Module layout:** `router.py` (HTTP only), `schemas.py`, `service.py` (business rules, commits) and `repository.py` (queries).
- **Table options:** use `table_args(...)` from `app/db/base.py` for any `__table_args__`. It adds InnoDB / utf8mb4 / utf8mb4_0900_ai_ci, and a test enforces it.
- **Server-generated values:** `Base` sets `eager_defaults=True`, so `created_at` and `updated_at` are loaded after INSERT and UPDATE, with no implicit async loads.
- **Money:** `Money` is `DECIMAL(10,2)`. A zero default must use `MONEY_ZERO_DEFAULT` (`'0.00'`), because MySQL reflects DECIMAL defaults that way.
- **Nullable JSON:** use `NullableJSON` so Python `None` becomes SQL `NULL`, not JSON `null`.
- **Enums:** `StrEnum` stored as `VARCHAR(30)` through `str_enum()`.
- **Timestamps:** stored in UTC. Every connection runs `SET time_zone = '+00:00'`. Use `clock.utcnow()` for "now".
- **Relationships:** all use `lazy="raise"`. Load what you need explicitly with `selectinload` / `joinedload`.
- **Account kinds:** a phone account has `password_hash` and no `google_sub`; a Google account has the opposite. The services enforce this.
- **Rules MySQL can't enforce:** the service layer must check these, and TODOs in the models point to them:
  - A bundle can't contain itself (`bundle_items`).
  - `notifications`: `target='user'` requires `user_id`, and `target='all'` requires `user_id IS NULL`.

### Arabic product search

Search products **only** with:

```sql
MATCH(search_text) AGAINST(:q IN BOOLEAN MODE)
```

Build `:q` with `app.shared.utils.search.build_boolean_query()`.

- **Parser:** the FULLTEXT index uses the `ngram` parser (token size 2). **Natural-language mode over-matches**: common bigrams such as `ال` make almost every product match.
- **No Arabic folding in MySQL:** tested on MySQL 9.6, FULLTEXT does not treat أ/ا, ة/ه, ى/ي, tashkeel or tatweel as equal. So `products.search_text` stores Arabic-normalized text (`normalize_arabic`), and the services keep it up to date, including when a brand or category is renamed.
- **After a migration on a database with products:** run `python -m scripts.recalculate_products`.
- **Derived fields:** `price_from`, `in_stock` and `is_on_sale` are recalculated by `recalculate_product_summary()` after every change that can affect them, including bundles reacting to their components.

## Images

`ImageService` (`app/integrations/storage/service.py`) processes each image into WebP at three sizes by longest side, never upscaling: `thumb` 300, `medium` 800, `large` 1600. It stores them as `<folder>/<uuid>_<size>.webp`, for example `products/42/3f9c…e1_medium.webp`. The database stores only the base key (`products/42/3f9c…e1`).

Storage sits behind the `StorageBackend` protocol (`app/integrations/storage/base.py`). `LocalStorage` writes files atomically under `MEDIA_ROOT`. An S3 backend can be added later in `get_storage()` without changing callers.

## Production media

In production FastAPI does **not** serve `/media`. Point Nginx (or Caddy) at `MEDIA_ROOT`. File names are unique per upload, so they can be cached forever:

```nginx
location /media/ {
    alias /srv/lamsa/media/;          # MEDIA_ROOT, with trailing slash
    add_header Cache-Control "public, max-age=31536000, immutable";
    types { image/webp webp; }
    default_type application/octet-stream;
    autoindex off;
    try_files $uri =404;
}
```

Set `MEDIA_URL` to the public URL of that location, for example `https://lamsa.example/media`. Behind Nginx, set `TRUST_PROXY_HEADERS=true` and forward the client IP (`proxy_set_header X-Forwarded-For $remote_addr;`) so login rate limits see real IPs.
