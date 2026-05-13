-- Areapadi database initialization
-- Run once on first boot. All subsequent changes via Alembic migrations.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
-- pgvector extension for semantic search embeddings
-- pgvector may not be installed; comment out if not available
CREATE EXTENSION IF NOT EXISTS vector;

-- Buyers
CREATE TABLE IF NOT EXISTS buyers (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    phone_number VARCHAR(20) UNIQUE NOT NULL,
    whatsapp_name VARCHAR(200),
    location GEOGRAPHY(POINT, 4326),
    location_updated_at TIMESTAMPTZ,
    preferred_language VARCHAR(10) DEFAULT 'en',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Sellers
CREATE TABLE IF NOT EXISTS sellers (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    phone_number VARCHAR(20) UNIQUE NOT NULL,
    business_name VARCHAR(200) NOT NULL,
    owner_name VARCHAR(200),
    food_categories TEXT[],
    location GEOGRAPHY(POINT, 4326) NOT NULL,
    address_text TEXT,
    is_available BOOLEAN DEFAULT FALSE,
    auto_deactivated BOOLEAN DEFAULT FALSE,
    rating NUMERIC(3,2) DEFAULT 0.0,
    total_orders INT DEFAULT 0,
    total_reviews INT DEFAULT 0,
    is_pro BOOLEAN DEFAULT FALSE,
    pro_expires_at TIMESTAMPTZ,
    onboarding_complete BOOLEAN DEFAULT FALSE,
    onboarding_step VARCHAR(100),
    opening_time VARCHAR(20),
    closing_time VARCHAR(20),
    operating_days TEXT[],
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS sellers_location_idx ON sellers USING GIST (location);
CREATE INDEX IF NOT EXISTS sellers_available_idx ON sellers (is_available) WHERE is_available = TRUE;

-- Menu items
CREATE TABLE IF NOT EXISTS menu_items (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    seller_id UUID REFERENCES sellers(id) ON DELETE CASCADE,
    name VARCHAR(200) NOT NULL,
    description TEXT,
    price NUMERIC(10,2) NOT NULL,
    is_available BOOLEAN DEFAULT TRUE,
    image_url TEXT,
    embedding vector(1536),
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS menu_items_seller_idx ON menu_items (seller_id);

-- Riders (must exist before orders which references it)
CREATE TABLE IF NOT EXISTS riders (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    phone_number VARCHAR(20) UNIQUE NOT NULL,
    full_name VARCHAR(200),
    vehicle_type VARCHAR(50),
    partner_tier VARCHAR(50) DEFAULT 'standard',
    company_name VARCHAR(200),
    is_available BOOLEAN DEFAULT FALSE,
    is_suspended BOOLEAN DEFAULT FALSE,
    current_location GEOGRAPHY(POINT, 4326),
    service_zone VARCHAR(200),
    bank_account_number VARCHAR(20),
    bank_code VARCHAR(10),
    paystack_recipient_code VARCHAR(200),
    rating_score NUMERIC(5,2) DEFAULT 50.0,
    rating_tier VARCHAR(50) DEFAULT 'developing',
    total_deliveries INT DEFAULT 0,
    onboarding_complete BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS riders_location_idx ON riders USING GIST (current_location);
CREATE INDEX IF NOT EXISTS riders_available_idx ON riders (is_available, is_suspended)
    WHERE is_available = TRUE AND is_suspended = FALSE;

-- Orders
CREATE TABLE IF NOT EXISTS orders (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    buyer_id UUID REFERENCES buyers(id),
    seller_id UUID REFERENCES sellers(id),
    rider_id UUID REFERENCES riders(id),
    status VARCHAR(50) DEFAULT 'pending' NOT NULL,
    items JSONB NOT NULL,
    subtotal NUMERIC(10,2) NOT NULL,
    delivery_fee NUMERIC(10,2) NOT NULL,
    platform_commission NUMERIC(10,2),
    platform_delivery_margin NUMERIC(10,2),
    total_amount NUMERIC(10,2) NOT NULL,
    payment_reference VARCHAR(200),
    paystack_reference VARCHAR(200),
    payment_status VARCHAR(50) DEFAULT 'unpaid',
    delivery_address TEXT,
    delivery_location GEOGRAPHY(POINT, 4326),
    buyer_notes TEXT,
    buyer_food_rating INT,
    buyer_delivery_rating INT,
    ignored_by_seller_count INT DEFAULT 0,
    cancelled_reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS orders_buyer_idx ON orders (buyer_id);
CREATE INDEX IF NOT EXISTS orders_seller_idx ON orders (seller_id);
CREATE INDEX IF NOT EXISTS orders_status_idx ON orders (status);

-- Rider performance metrics
CREATE TABLE IF NOT EXISTS rider_metrics (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    rider_id UUID REFERENCES riders(id),
    order_id UUID REFERENCES orders(id),
    job_offered_at TIMESTAMPTZ,
    job_accepted_at TIMESTAMPTZ,
    pickup_confirmed_at TIMESTAMPTZ,
    delivery_confirmed_at TIMESTAMPTZ,
    estimated_pickup_secs INT,
    actual_pickup_secs INT,
    estimated_delivery_secs INT,
    actual_delivery_secs INT,
    was_accepted BOOLEAN,
    buyer_rating INT,
    had_integrity_issue BOOLEAN DEFAULT FALSE,
    response_time_secs INT,
    computed_score_snapshot NUMERIC(5,2),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- WhatsApp conversation logs (Redis is source of truth for live sessions; this is audit log)
CREATE TABLE IF NOT EXISTS conversation_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    phone_number VARCHAR(20) NOT NULL,
    user_role VARCHAR(20),
    stage VARCHAR(100),
    inbound_message TEXT,
    outbound_message TEXT,
    active_order_id UUID,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS conv_logs_phone_idx ON conversation_logs (phone_number);
CREATE INDEX IF NOT EXISTS conv_logs_created_idx ON conversation_logs (created_at DESC);
