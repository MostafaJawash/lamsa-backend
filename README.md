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
.\venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"   # JWT_SECRET
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
| `TEST_DATABASE_NAME` | `lamsa_test` | used by the tests; must end in `_test` |
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
| `TRUST_PROXY_HEADERS` | `false` | set `true` only behind your own reverse proxy (client IP from `X-Forwarded-For`) |

## Run

```powershell
.\venv\Scripts\python.exe -m scripts.init_storage   # create MEDIA_ROOT
.\venv\Scripts\alembic.exe upgrade head             # create / update tables
.\venv\Scripts\python.exe -m scripts.seed_base      # categories, delivery methods, settings (safe to re-run)
.\venv\Scripts\fastapi.exe dev app/main.py          # http://127.0.0.1:8000/docs
```

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

- Database tests use `TEST_DATABASE_NAME` (`lamsa_test`), never `lamsa`. `tests/conftest.py` switches `DB_NAME` before importing the app and **refuses to run unless the name ends in `_test`**.
- Migrations run once per session. Each test runs in one transaction that is rolled back, so nothing is left behind.
- Google is always mocked, and the tests never call it.
- Time-dependent rules (rate limits, refresh grace, expiry) use a controllable clock (`app/core/clock.py`), never `sleep`.
- Argon2 uses cheap parameters in tests for speed.
- Images go to a temporary `MEDIA_ROOT` per test.
- MySQL FULLTEXT only sees committed rows, so the search tests (`tests/catalog/test_search.py`) really commit to `lamsa_test` and wipe the catalog and user tables afterwards.
- The list, detail, facets and category tree endpoints have a query-budget test (6 or fewer SQL statements, independent of page size).

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
| 404 / 422 | `VARIANT_NOT_FOUND` | Variant isn't part of the product; 422 when an image upload names a color that has no active variant |
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
