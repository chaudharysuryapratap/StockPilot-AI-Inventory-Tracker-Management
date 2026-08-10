# StockPilot AI — Inventory Tracker & Management

A portfolio-ready inventory system for retailers and restaurants. It consumes sale events in near real time, updates stock safely, forecasts demand from historical sales, maintains supplier, fulfilment, and returns operations, exports risk/valuation reports, and emails owner-ready reorder actions.

StockPilot supports multiple isolated businesses in one deployment while keeping
each user account attached to one business. A business can operate many warehouses
and assign Admin, Manager, Picker, or read-only Viewer access. Workspace rows remain
the internal ownership and audit boundary for catalogues, warehouses, orders,
settings, integrations, and AI context; signed cursors and every service lookup are
tenant-bound.

The application is deliberately split into two kinds of intelligence:

- A transparent forecasting engine calculates daily demand, lead-time coverage, stockout date, and reorder quantity from the sales record.
- Amazon Bedrock receives only the resulting business metrics and writes a concise, actionable explanation. It does not invent or override the numeric recommendation.

That separation makes the project more credible than presenting an LLM as a time-series forecasting engine.

## Architecture

```mermaid
flowchart TD
    POS[POS / store / mobile app] -->|signed sale event| EC2[EC2: Flask control panel]
    EC2 -->|transactions| RDS[(RDS MySQL)]
    Scheduler[EventBridge Scheduler] --> Invoker[Lambda invoker]
    Invoker --> SSM[Systems Manager Run Command]
    SSM -->|daily analysis| EC2
    EC2 -->|narrative| Bedrock[Amazon Bedrock]
    EC2 -->|critical owner alert| SES[Amazon SES]
```

`EventBridge Scheduler` is used for the scheduled job because it is the modern successor to scheduled CloudWatch Events rules. It is still the same event-driven idea described in the project brief, with timezone and retry controls built in.

## What is included

| Capability | Implementation |
| --- | --- |
| Private control panel | Flask dashboard for live stock, product catalogue, and AI recommendations |
| RDS-ready data layer | SQLAlchemy models compatible with SQLite locally and MySQL on Amazon RDS |
| Near-real-time stock updates | Idempotent `POST /api/webhooks/sales` POS webhook with row locking and transactional rollback |
| Multi-location/bin inventory | Product stock is stored by warehouse and optional bin, with validated bin capacity limits |
| Single stock authority | `stock_levels.quantity_on_hand` and `quantity_reserved` drive every stock badge, API response, forecast, sale, and adjustment |
| Collision-safe positions | A generated position key prevents duplicate unassigned stock rows under concurrent writes |
| Atomic stock transfers | Idempotent location/bin transfers protect reserved stock and update both positions in one transaction |
| Outbound order fulfilment | Manual sales orders reserve exact bin positions, generate pick lists, enforce pick/pack gates, and deduct reserved stock once on shipment |
| Warehouse management | One Admin-only warehouse and bin section with validated activation and capacity safeguards |
| Product catalogue | Validated add/edit/archive/restore operations with SKU, barcode, category, UoM, prices, perishability, and supplier fields |
| Supplier directory | Workspace-scoped supplier CRUD with contacts, lead times, payment terms, archive/restore, and protected product relationships |
| CSV bulk import | Downloadable fixed-format template plus atomic product create/update and optional audited opening warehouse/bin balances, with row-level validation errors and a 1,000-row default limit |
| Unified inbound workflow | One **Add inventory** journey connects product creation/CSV import, supplier and warehouse selection, manual or AI-assisted purchase orders, and batch-aware receiving without mixing catalogue, order, and receipt records |
| Auditability | POS sales, order shipments, adjustments, and paired transfers record location, bin, reference, timestamp, and user attribution |
| Named users and roles | Password-hashed Admin, Manager, Picker, and read-only Viewer accounts with server-enforced permissions and CSRF-protected browser actions |
| SaaS business isolation | Unique business usernames, one-business-per-account memberships, tenant settings, and secret-reference-only integration records |
| Production authentication | Hashed single-use email verification, password-reset and invitation tokens; database-backed login throttling; TOTP MFA/recovery codes; and per-workspace OIDC SSO |
| Scalable catalogues | Signed cursor pagination, totals, and server-side search/filtering for products, locations, suppliers, staff, transfers, sales orders, returns, and purchase orders |
| Camera barcode scanner | Locally bundled `html5-qrcode` scanner with rear-camera, manual/USB-reader, SKU fallback, and live bin-level stock lookup |
| Mobile picker | Touch-first `/picker` queue with exact bin positions, large action targets, connection awareness, and an installable read-only offline shell |
| Returns / RMA | Shipped-order return requests, approval/rejection, partial receiving, restock/damaged disposition, over-return protection, and append-only RMA events |
| Purchase orders | Manual and AI-assisted supplier drafts, approval, ordered-versus-received matching, partial/idempotent receipts, and inbound movement audit |
| Unit conversions | Product-specific packaging conversions normalize supplier units such as boxes or cases into authoritative base-unit stock |
| Lot and expiry control | Perishable receipt validation, manufacture/expiry dates, FEFO lot consumption, and lot-preserving transfers |
| Inventory ageing | Configurable near-expiry and dead-stock recommendations in the UI and API |
| Demand forecasting | Weighted recent + historical demand, weekday adjustment, lead-time coverage, safety stock, confidence score, and persisted calculation factors |
| Forecast accuracy | Seven-day predicted-versus-actual outcomes with MAE, MAPE, and prediction-bias summaries |
| Forecast explainability | Dashboard panels and API JSON expose the exact sales window, averages, weights, weekday multiplier, lead time, available stock, and target stock used by each run |
| GenAI explanation | Optional Bedrock `Converse` call using an EC2 IAM role |
| Dashboard assistant | Workspace-grounded questions about demand changes, stock risks, expiry, dead stock, and forecast accuracy, with deterministic fallback answers |
| Exportable reporting | Workspace-scoped inventory risk and current-unit-cost valuation reports in JSON, Excel, and PDF |
| Automated owner alerts | Optional SES v2 text + HTML delivery for daily actions or critical-only risks, with a durable delivery audit |
| Scheduled analysis | CloudFormation template: EventBridge Scheduler → Lambda → SSM → EC2 command |

## Run locally first

You can demonstrate the entire UI and forecasting workflow without an AWS account. SQLite is the local default; Bedrock and SES stay disabled unless configured.

```bash
cd ai-inventory-tracker
cp .env.example .env
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Upgrades an older StockPilot database without losing its stock records.
.venv/bin/flask --app run migrate-schema

# Populate two locations/bins, four products, and 35 days of demo POS history.
.venv/bin/flask --app run seed-demo --reset

# Optional production-style setup: create the first admin interactively.
# For a local demo, you can instead open /signup on first launch.
.venv/bin/flask --app run create-admin

# Generate forecasts and recommendations.
.venv/bin/flask --app run analyze-inventory

# Optional: email only critical risks and record the delivery attempt.
.venv/bin/flask --app run analyze-inventory --send-email --critical-only

# Open http://127.0.0.1:5000
.venv/bin/flask --app run run --debug
```

On a fresh database, StockPilot redirects to `/signup` until the first Admin creates a business identity and primary warehouse. An Admin can then add warehouses and bins from one section, invite people, create staff accounts, or configure OIDC. Every signed-in user can enable TOTP MFA from **Security**. Camera access works on `localhost`; a deployed instance must use HTTPS for browser camera permission.

Role boundaries:

| Role | Allowed browser operations |
| --- | --- |
| Admin | All inventory, warehouse/bin configuration, outbound, returns, and user-management operations |
| Manager | Product, supplier, purchase-order approval/receiving, unit conversions, stock-adjustment, transfer, reports, alerts, order shipment, and RMA request/review/receiving workflows; warehouse creation/configuration is Admin-only |
| Picker | Dashboard/product/location views, barcode scanning, transfers, mobile pick/pack execution, and authorized return receiving; no catalogue, user, arbitrary adjustment, order creation, shipment, or RMA approval controls |
| Viewer | Read-only dashboard, warehouse, catalogue, order, transfer, return, and reporting views; no operational inventory mutations |

The scanner bundles `html5-qrcode` 2.3.8 locally so the private application has no runtime CDN dependency. Its Apache-2.0 license is included at `app/static/vendor/html5-qrcode.LICENSE.txt`.

The mobile picker registers a service worker that caches only static assets and a generic offline page. Authenticated order data and POST actions are never cached; pick, pack, and return-receipt changes remain online-only so stale offline state cannot alter inventory.

Run the test suite with:

```bash
.venv/bin/pytest -q
```

## POS webhook contract

The external sale ID is required. It makes retries safe: sending the same sale again returns `200` but does not decrement stock a second time.

```bash
curl -X POST http://127.0.0.1:5000/api/webhooks/sales \
  -H 'Content-Type: application/json' \
  -H 'X-POS-Token: replace-with-your-pos-token' \
  -d '{
    "external_sale_id": "pos-order-10423",
    "source": "cash-register-1",
    "location_code": "MAIN",
    "occurred_at": "2026-08-01T14:30:00+05:30",
    "items": [
      {"sku": "COFFEE-250", "quantity": 2, "unit_price": "85.00"}
    ]
  }'
```

Useful endpoints:

| Endpoint | Purpose | Protection |
| --- | --- | --- |
| `GET /api/health` | Health check for the load balancer or monitoring | Public by design |
| `GET /api/dashboard` | Dashboard JSON | Named-user session or `X-Internal-Token` |
| `GET /api/products` | List active products and authoritative stock totals | Named-user session or `X-Internal-Token` |
| `GET /api/locations` | List active locations, bins, capacities, and stock totals | Named-user session or `X-Internal-Token` |
| `GET/POST /api/suppliers` | List or create workspace supplier records | `X-Internal-Token` |
| `PATCH/DELETE /api/suppliers/<id>` | Edit or archive a supplier without breaking product history | `X-Internal-Token` |
| `POST /api/suppliers/<id>/restore` | Restore an archived supplier | `X-Internal-Token` |
| `GET /api/barcodes/lookup?code=...` | Resolve a camera/USB-scanned barcode or SKU to live stock | Named-user session |
| `POST/PATCH /api/locations` | Add or edit a warehouse location | `X-Internal-Token` |
| `POST /api/locations/<id>/bins` | Add a bin to a location | `X-Internal-Token` |
| `PATCH /api/bins/<id>` | Edit a bin or its capacity | `X-Internal-Token` |
| `GET/POST /api/transfers` | List or atomically complete stock transfers | `X-Internal-Token` |
| `GET /api/sales-orders` | List outbound orders and their exact reservations | Named-user session or `X-Internal-Token` |
| `POST /api/sales-orders` | Create a manual order and reserve available stock | `X-Internal-Token` |
| `GET /api/sales-orders/<id>/pick-list` | Generate/read the bin-aware pick list | Named-user session or `X-Internal-Token` |
| `POST /api/sales-orders/<id>/start-picking` | Move a reserved order into picking | `X-Internal-Token` |
| `POST /api/sales-orders/<id>/items/<item-id>/pick` | Confirm one complete order line as picked | `X-Internal-Token` |
| `POST /api/sales-orders/<id>/pack` | Confirm packing after every line is picked | `X-Internal-Token` |
| `POST /api/sales-orders/<id>/ship` | Atomically ship and deduct on-hand + reserved stock | `X-Internal-Token` |
| `POST /api/sales-orders/<id>/cancel` | Cancel an unshipped order and release reservations | `X-Internal-Token` |
| `GET /api/returns` | List workspace-scoped RMAs, optionally filtered by status | Named-user session or `X-Internal-Token` |
| `POST /api/sales-orders/<id>/returns` | Request an RMA against a shipped order | `X-Internal-Token` (Admin/Manager actor) |
| `GET /api/returns/<id>` | Read RMA lines, receipts, actors, and append-only events | Named-user session or `X-Internal-Token` |
| `POST /api/returns/<id>/authorize` | Authorize requested quantities for physical receiving | `X-Internal-Token` (Admin/Manager actor) |
| `POST /api/returns/<id>/reject` | Reject a pending RMA without consuming return allowance | `X-Internal-Token` (Admin/Manager actor) |
| `POST /api/returns/<id>/cancel` | Cancel an unreceived requested/authorized RMA | `X-Internal-Token` (Admin/Manager actor) |
| `POST /api/returns/<id>/items/<item-id>/receive` | Record an idempotent partial restock/damaged receipt | `X-Internal-Token` (Admin/Manager/Picker actor) |
| `GET /api/audit/movements` | Query the user-attributed append-only stock audit trail | `X-Internal-Token` |
| `POST /api/webhooks/sales` | Process one POS sale | `X-POS-Token` |
| `POST /api/products` | Add a product | `X-Internal-Token` |
| `PATCH /api/products/<id>` | Edit a product with field validation | `X-Internal-Token` |
| `DELETE /api/products/<id>` | Archive a product without deleting its history | `X-Internal-Token` |
| `POST /api/products/<id>/restore` | Restore an archived product | `X-Internal-Token` |
| `POST /api/products/import` | Import a UTF-8 CSV catalogue | `X-Internal-Token` |
| `POST /api/stock/adjustments` | Receive stock, count correction, or write-off | `X-Internal-Token` |
| `POST /api/analysis/run` | Run an on-demand forecast | `X-Internal-Token` |
| `GET /api/insights` | Read latest forecast values plus stored factors and readable explanations | Named-user session or `X-Internal-Token` |
| `GET /api/reports/risk?format=...` | Export the stock-risk snapshot as JSON, Excel, or PDF | `X-Internal-Token` |
| `GET /api/reports/valuation?format=...` | Export current-unit-cost valuation as JSON, Excel, or PDF | `X-Internal-Token` |
| `POST /api/alerts/critical` | Analyze inventory and deliver critical risks through SES | `X-Internal-Token` |
| `GET /api/alerts/deliveries` | Read the delivery audit for sent, skipped, or failed alerts | `X-Internal-Token` |

### Stock transfer contract

`external_transfer_id` is optional but recommended. Reusing it with the same transfer returns the existing result without moving stock twice; reusing it for different data returns a conflict. A source bin may be omitted when the SKU has only one stock position at that location. Destination bin capacity and source available stock (`on_hand - reserved`) are enforced before either balance changes.

```bash
curl -X POST http://127.0.0.1:5000/api/transfers \
  -H 'Content-Type: application/json' \
  -H 'X-Internal-Token: replace-with-your-internal-token' \
  -d '{
    "external_transfer_id": "transfer-1042",
    "sku": "COFFEE-250",
    "source_location_code": "MAIN",
    "source_bin_code": "A-01",
    "destination_location_code": "KITCHEN",
    "destination_bin_code": "K-01",
    "quantity": 6,
    "note": "Kitchen rush-hour replenishment"
  }'
```

Browser transfers automatically use the signed-in named user for audit attribution. Trusted machine integrations remain token-protected and use the durable service actor by default.

`X-Actor-Email` attribution is disabled by default. A private integration may
enable `ALLOW_ACTOR_HEADER=true`; use `X-Workspace: <business-username>` when a
the integration identifies a business explicitly. The actor must have an active
membership in that exact workspace. Keep attribution disabled when machine actions should
use the durable service actor.

### Sales-order fulfilment contract

Creating an order reserves available stock (`quantity_reserved`) at exact location/bin positions without reducing `quantity_on_hand`. The workflow is `pending → picking → packed → shipped`. Every line must be confirmed as picked before packing. Only shipment reduces both on-hand and reserved quantities; the available quantity therefore remains consistent before and after fulfilment. Shipment also creates the normal `sales`/`sale_items` history used by demand forecasting and append-only movements for its physical deductions.

`external_order_id` is optional but recommended for retry safety. A matching retry returns the existing order without reserving twice, while reusing the ID for different order data returns a conflict.

```bash
curl -X POST http://127.0.0.1:5000/api/sales-orders \
  -H 'Content-Type: application/json' \
  -H 'X-Internal-Token: replace-with-your-internal-token' \
  -d '{
    "external_order_id": "manual-order-1042",
    "location_code": "MAIN",
    "channel": "manual",
    "customer_reference": "CUSTOMER-77",
    "items": [
      {"sku": "COFFEE-250", "quantity": 4},
      {"sku": "WRAP-BOX", "quantity": 2}
    ]
  }'
```

### Returns / RMA contract

An RMA can be created only for a shipped sales order. The workflow is `requested → authorized → receiving → completed`; Admin/Manager users may instead reject a request or cancel it before any receipt. Requested quantities cannot exceed the shipped quantity minus quantities already claimed by active or completed RMAs.

Each physical receipt has an optional but recommended `external_receipt_id`, quantity, receiving location/bin, and one of two dispositions:

- `restock` atomically adds on-hand stock, enforces bin capacity, and appends a user-attributed positive inventory movement.
- `damaged` records the physical receipt and RMA event but deliberately does not add saleable stock.

Partial and mixed-disposition receipts are supported. Retrying an identical external return/receipt ID returns the saved record without applying stock twice; reusing it for different data returns a conflict.

```bash
curl -X POST http://127.0.0.1:5000/api/sales-orders/42/returns \
  -H 'Content-Type: application/json' \
  -H 'X-Internal-Token: replace-with-your-internal-token' \
  -d '{
    "external_return_id": "rma-1042",
    "reason_code": "customer_return",
    "customer_note": "Unopened item",
    "items": [{"sku": "COFFEE-250", "quantity": 2}]
  }'
```

### Product CSV format

Only `sku` and `name` are required. Product columns also include `barcode`, `category`, `unit_of_measure`, `cost_price`, `sell_price`, `reorder_point`, `safety_stock`, `is_perishable`, and `preferred_supplier_id`. Optional `location_code`, `bin_code`, and `quantity_on_hand` columns can establish audited opening balances during onboarding; normal deliveries still go through purchase-order receiving so lots and manufacture/expiry dates stay traceable. Admins and Managers can download the fixed-format template from **Add inventory & purchasing → Catalogue**; the repository example is [`examples/products-import.csv`](examples/products-import.csv).

The import is atomic: if one row fails validation, no rows are committed. Send multipart form data with a field named `file`. Add `update_existing=true` to update matching SKUs; otherwise an existing SKU is reported as an error.

```bash
curl -X POST http://127.0.0.1:5000/api/products/import \
  -H 'X-Internal-Token: replace-with-your-internal-token' \
  -F 'file=@examples/products-import.csv'
```

### Sprint 1–10 database migrations

`flask --app run migrate-schema` is idempotent and supports both local SQLite and RDS MySQL. Sprints 1–6 build the authoritative stock, location/bin, named-user, fulfilment, supplier/reporting, and RMA foundations. Sprint 7 hardened the original single-workspace release. Sprint 8 scopes catalogue identifiers and adds conversions, purchase orders/receipts, expiry-aware lots, forecast outcomes, and assistant history. Sprint 9 safely evolves existing users into tenant memberships, assigns unique business usernames, creates tenant settings and integration records, and adds durable verification/reset/invitation, login-throttle, MFA, and recovery-code tables. Sprint 10 expands the role constraint for Viewer access and introduces the single-business, multi-warehouse product experience without changing inventory ownership. Existing roles and records are preserved.

Recorded revisions:

- `20260806_sprint1_foundation`
- `20260806_sprint2_locations_transfers`
- `20260806_sprint3_barcode_auth_roles`
- `20260806_sprint4_outbound_fulfillment`
- `20260806_sprint5_suppliers_reporting_alerts`
- `20260806_sprint6_picker_explainability_returns`
- `20260808_single_workspace_hardening`
- `20260808_procurement_lots_intelligence`
- `20260809_saas_identity_workspaces`
- `20260810_single_business_warehouses_viewer`

Run `flask --app run schema-version` to inspect the applied revisions.

## Deploy to AWS

### 1. Create RDS MySQL

Create a MySQL RDS instance in private subnets. Its security group should allow port `3306` **only** from the EC2 application security group—not from the internet.

Create a database and set this in `/etc/ai-inventory-tracker.env` on EC2:

```dotenv
APP_ENV=production
AUTO_CREATE_SCHEMA=false
DATABASE_URL=mysql+pymysql://inventory_admin:YOUR_PASSWORD@YOUR_RDS_ENDPOINT:3306/inventory_tracker
SECRET_KEY=replace-with-at-least-32-random-characters
POS_WEBHOOK_TOKEN=replace-with-at-least-32-random-characters
INTERNAL_API_TOKEN=replace-with-at-least-32-random-characters
ALLOW_WEB_SIGNUP=true
ALLOW_ACTOR_HEADER=false
SESSION_COOKIE_SECURE=true
REQUIRE_EMAIL_VERIFICATION=true
AUTH_EMAIL_ENABLED=true
MFA_ENCRYPTION_KEY=replace-with-a-valid-fernet-key
OIDC_ENABLED=true
TRUST_PROXY_HEADERS=true
TRUSTED_HOSTS=inventory.example.com
CRITICAL_STOCKOUT_DAYS=3
NEAR_EXPIRY_DAYS=30
DEAD_STOCK_DAYS=90
FORECAST_ACCURACY_HORIZON_DAYS=7
REPORT_CURRENCY=INR
AWS_REGION=ap-south-1
BEDROCK_ENABLED=true
BEDROCK_MODEL_ID=amazon.nova-micro-v1:0
SES_ENABLED=true
SES_FROM_EMAIL=verified-sender@yourdomain.com
ALERT_RECIPIENTS=owner@yourdomain.com
```

Set `STAFF_AUTH_ENABLED=true`, leave `STAFF_USERNAME` and `STAFF_PASSWORD` blank on a new installation, and keep secure cookies enabled behind HTTPS. For a private installation, set `ALLOW_WEB_SIGNUP=false` and create the first administrator with `flask --app run create-admin`. For SaaS onboarding, set `ALLOW_WEB_SIGNUP=true`, `REQUIRE_EMAIL_VERIFICATION=true`, and configure SES/auth email; unsafe combinations are rejected at startup. Generate a separate Fernet `MFA_ENCRYPTION_KEY`. OIDC client secrets are never stored in the database: workspace settings hold only an environment-variable name such as `OIDC_SECRET_FRESHMART`. Existing shared credentials migrate once into the durable Admin identity. Browser forms use session-backed CSRF tokens, passwords are Werkzeug hashes, auth links are hashed and single-use, and private APIs require a user session or internal machine token. Use Secrets Manager or Parameter Store for production values.

### 2. Create and secure EC2

Use Ubuntu and attach an instance profile containing:

- `AmazonSSMManagedInstanceCore`, so the scheduler can run the local analysis command without opening SSH.
- The policy in [`infra/ec2-app-policy.json`](infra/ec2-app-policy.json), which permits Bedrock inference and SES email sending.

Allow inbound HTTPS only through an ALB or another TLS terminator, with HTTP redirected to HTTPS. HTTPS is also required for the camera scanner outside localhost. Keep RDS in private subnets and restrict the internal/POS tokens to their intended integrations.

Copy this project to the instance, then run:

```bash
sudo ./scripts/bootstrap-ec2.sh /path/to/ai-inventory-tracker
```

On its first run the script creates `/etc/ai-inventory-tracker.env` and stops.
Fill that file with the production values above and rerun the script. It refuses
to start the service with placeholder secrets, SQLite, insecure cookies, or
missing trusted hosts. Application startup separately validates email-verification
and MFA requirements.

The script installs Gunicorn + Nginx, creates a non-login service user, prepares `/etc/ai-inventory-tracker.env`, and registers `/usr/local/bin/run-inventory-analysis` for SSM.

### 3. Enable Bedrock and SES

- In the chosen AWS Region, enable access to the model named by `BEDROCK_MODEL_ID` and set `BEDROCK_ENABLED=true`.
- Verify the sender identity in Amazon SES. While the SES account is in its sandbox, recipient identities must be verified too.
- Test manually on EC2: `sudo /usr/local/bin/run-inventory-analysis`.

The Bedrock adapter uses the current `Converse` API and EC2 role credentials; no access keys are stored in the application.

### 4. Schedule the daily job

After the EC2 instance shows as **Managed** in Systems Manager, deploy the included template:

```bash
aws cloudformation deploy \
  --template-file infra/daily-analysis-schedule.yaml \
  --stack-name stockpilot-daily-analysis \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides TargetInstanceId=i-0123456789abcdef0
```

By default, it runs at **07:00 Asia/Kolkata** every day. The flow is EventBridge Scheduler → Lambda → SSM Run Command → `flask analyze-inventory --send-email --critical-only` on the EC2 application instance. When no position is critical, StockPilot records a skipped delivery rather than emailing an empty alert.

## Reporting and valuation method

The risk report uses authoritative available quantity (`on_hand - reserved`) plus the newest stored forecast for every active SKU/location. A position is critical when it is already unavailable or its predicted stockout falls inside `CRITICAL_STOCKOUT_DAYS`.

The valuation report uses **current unit cost**: `products.cost_price × stock_levels.quantity_on_hand`. It intentionally does not claim FIFO, LIFO, or weighted-average costing. Those methods require receipt-level quantity/cost layers, which are not present until a future inbound purchasing ledger is implemented. Both reports are available as JSON, styled Excel workbooks, and paginated PDF files from the Reports screen or API.

## Forecast logic

For each product-location pair, the system calculates:

1. Overall daily demand from the last 28 days.
2. Recent 7-day demand, weighted more heavily to react to current trends.
3. A bounded weekday factor for predictable busy days.
4. Target stock = forecast demand during supplier lead time + safety stock.
5. Available stock = `quantity_on_hand − quantity_reserved`.
6. Reorder quantity = `max(0, target stock − available stock)`.
7. Expected stockout = `available stock / forecast daily demand`.

The confidence score is intentionally tied to the number of days with observed sales. It is an operational signal, not a claim of statistical certainty.

Every new forecast persists the exact factor JSON used by the calculation: model version, lookback window, total and recent units, long-term and recent averages, blend weights, bounded weekday multiplier, observed-sales days, supplier lead time, safety stock, reorder point, available stock, and target stock. The dashboard renders those values as readable labels, while `/api/insights` retains both the raw JSON and the readable representation. Older forecasts remain valid but show a prompt to rerun analysis because their historical factor inputs were not previously stored.

## Suggested resume bullets

- Engineered a Flask-based, multi-location inventory platform on EC2/RDS MySQL that processes idempotent POS sale events and updates stock in transactional, auditable workflows.
- Built transparent demand forecasting with lead-time and safety-stock logic, then integrated Amazon Bedrock to generate owner-friendly reorder actions from calculated inventory metrics.
- Automated daily inventory analysis through EventBridge Scheduler, Lambda, and Systems Manager, with Amazon SES delivering low-stock and reorder reports using IAM role-based access.

## Project structure

```text
app/                 Flask routes, models, dashboard, and business services
app/services/        Product, procurement, lot/conversion, intelligence, reporting, transfer, fulfilment, returns, forecast, Bedrock, and SES services
app/schema.py        Versioned SQLite/MySQL schema migrations
examples/            Sample product CSV import file
infra/               EC2 IAM policy and Scheduler → Lambda → SSM template
scripts/             EC2 bootstrap and scheduled-job wrapper
deploy/              Gunicorn systemd unit and Nginx reverse-proxy config
tests/               Regression tests across Sprints 1–10, including migrations, business isolation, warehouse scope, Viewer authorization, profiles, CSV templates, auth tokens/MFA, pagination, CRUD, procurement, lots/expiry, forecast accuracy, chat, fulfilment, reports, picker flows, and RMAs
```
