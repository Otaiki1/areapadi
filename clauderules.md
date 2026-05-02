CONTEXT: What you are building
You are building Areapadi — a WhatsApp-native food discovery and delivery platform for Nigeria's informal food economy. It connects everyday food buyers with local SME food sellers (home chefs, caterers, bakers, shawarma vendors, small chops operators) through a conversational AI experience on WhatsApp.
The core insight: There is no app to download. No website to navigate. A buyer adds a WhatsApp number, shares their location, asks in plain English or Pidgin who sells what they want nearby, and an AI agent handles everything — discovery, ordering, payment, and delivery coordination — entirely within a WhatsApp thread. Sellers onboard and receive orders via WhatsApp. Riders get jobs dispatched via WhatsApp. The platform is invisible. The conversation is the product.
This is not Chowdeck or Uber Eats. It targets informal SME sellers that major platforms have deliberately ignored.

MONOREPO STRUCTURE
The project is a Python FastAPI monorepo. All services live in one repo. Build and maintain this exact structure:
areapadi/
├── services/
│ ├── ai-agent/ # Conversational state machine — core of the product
│ ├── seller-service/ # Seller profiles, menus, catalog, availability
│ ├── order-service/ # Order lifecycle: created → confirmed → delivered
│ ├── rider-dispatch/ # Job assignment, fallback chain, rider alerts
│ ├── rating-engine/ # AI scoring pipeline for rider performance
│ ├── geo-service/ # Location parsing, radius search, ETA
│ └── payment-service/ # Paystack integration, disbursements, refunds
├── gateway/ # API gateway — all WhatsApp webhooks land here
├── web/ # Next.js 14 — seller public profile pages
├── infra/
│ ├── docker-compose.yml # Local dev: Postgres/PostGIS, Redis, RabbitMQ
│ └── nginx/
├── scripts/
│ └── init.sql # DB schema, extensions, indexes
├── shared/ # Shared Pydantic models, utils, constants
├── .env.example
├── .gitignore
└── README.md

TECH STACK — Do not deviate from this
Backend services

Language: Python 3.11+
Framework: FastAPI with async/await throughout
Database: PostgreSQL 15 with PostGIS extension (geo queries) and pgvector (embeddings)
Cache / Session store: Redis 7
Message queue: RabbitMQ (aio-pika for async)
HTTP client: httpx (async)
ORM: SQLAlchemy 2.0 (async engine) with Alembic for migrations
Validation: Pydantic v2
Testing: pytest + pytest-asyncio + httpx TestClient

AI / NLP

Primary LLM: Anthropic Claude API (claude-sonnet-4-5 or claude-haiku-4-5 for cheaper tasks)
Embeddings: OpenAI text-embedding-3-small for semantic menu search
Conversation state: Redis-backed custom state machine (NOT LangChain — keep it simple and explicit)

WhatsApp

API: Meta WhatsApp Cloud API
BSP: Termii (Nigerian) as primary — abstract it so it can swap to 360dialog
Webhook signature verification: mandatory on every inbound request

Payments

Gateway: Paystack
Disbursement: Paystack Transfer API
Webhook: HMAC-SHA512 signature verification on every Paystack callback

Frontend (seller pages)

Framework: Next.js 14 App Router with TypeScript
Styling: Tailwind CSS
Hosting target: Vercel
