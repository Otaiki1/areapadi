# Areapadi

WhatsApp-native food discovery and delivery platform for Nigeria's informal food economy.

Buyers message a WhatsApp number in plain English or Pidgin to find local sellers, order food, pay via Paystack, and track delivery — entirely inside a WhatsApp conversation. No app. No website. The conversation is the product.

---

## Architecture

```
WhatsApp Cloud API → Gateway (8000) → AI Agent (8001)
                                           ├── Seller Service (8002)
                                           ├── Order Service  (8003)
                                           ├── Geo Service    (8006)
                                           └── Payment Svc    (8007)

Order Service → Rider Dispatch (8004) → Rating Engine (8005)
Payment Webhook → Order Service (payment_status update)
```

**Stack:** Python 3.11 · FastAPI · PostgreSQL 15 + PostGIS + pgvector · Redis 7 · RabbitMQ · Anthropic Claude API · Paystack · Next.js 14

---

## Local dev setup

**Prerequisites:** Docker Desktop, Python 3.11+, Node.js 18+

```bash
# 1. Start infrastructure
cp .env.example .env        # fill in API keys
cd infra && docker compose up -d

# 2. Install shared deps (example for gateway)
cd gateway && pip install -r requirements.txt

# 3. Run a service
uvicorn main:app --reload --port 8000

# 4. Run tests
cd gateway && pytest tests/ -v
```

---

## Service ports

| Service          | Port |
|------------------|------|
| Gateway          | 8000 |
| AI Agent         | 8001 |
| Seller Service   | 8002 |
| Order Service    | 8003 |
| Rider Dispatch   | 8004 |
| Rating Engine    | 8005 |
| Geo Service      | 8006 |
| Payment Service  | 8007 |
| PostgreSQL       | 5432 |
| Redis            | 6379 |
| RabbitMQ         | 5672 |
| RabbitMQ UI      | 15672|

---

## Build stages

| Stage | Scope |
|-------|-------|
| 1 | Foundation: infra, schema, shared libs, gateway |
| 2 | Core buyer flow: seller/geo/ai-agent/payment/order services |
| 3 | Delivery loop: rider dispatch, status updates, rating prompt |
| 4 | Seller side: onboarding, order confirm/decline, food-ready trigger |
| 5 | Rating engine: score computation, weekly Claude summaries |
| 6 | Web: Next.js seller profile pages |

---

## Key rules

- Phone numbers: `2348012345678` format (no `+`)
- Amounts: always in **kobo** internally (₦1 = 100 kobo)
- PostGIS: distances in **metres** (`ST_DWithin`, 3km = 3000m)
- WhatsApp 24h session: after inactivity, only template messages allowed
- Logs: never log raw phone numbers — use `hash_phone()` from `shared/logger.py`
