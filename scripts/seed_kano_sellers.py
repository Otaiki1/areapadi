#!/usr/bin/env python3
"""
Seed 5 test sellers in Kano city for local development and E2E testing.
Run from the project root:
    python scripts/seed_kano_sellers.py

Requires POSTGRES_* env vars (or a running .env).
Sellers are spread across Kano Central, Sabon Gari, Nassarawa, and Fagge.
"""
from __future__ import annotations
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text

DATABASE_URL = (
    f"postgresql+asyncpg://"
    f"{os.getenv('POSTGRES_USER', 'areapadi_user')}:"
    f"{os.getenv('POSTGRES_PASSWORD', 'devpassword')}@"
    f"{os.getenv('POSTGRES_HOST', 'localhost')}:"
    f"{os.getenv('POSTGRES_PORT', '5432')}/"
    f"{os.getenv('POSTGRES_DB', 'areapadi')}"
)

SELLERS = [
    {
        "phone_number": "2348010000001",
        "business_name": "Mama Nkechi's Kitchen",
        "owner_name": "Nkechi Okonkwo",
        "food_categories": ["jollof rice", "fried rice", "chicken", "egusi soup"],
        "lat": 12.0022,
        "lng": 8.5920,
        "address_text": "Behind NEPA Office, Sabon Gari, Kano",
        "rating": 4.6,
        "menu_items": [
            {"name": "Jollof Rice", "description": "Party-style smoky jollof", "price": 1500},
            {"name": "Fried Rice", "description": "Colourful fried rice with veggies", "price": 1500},
            {"name": "Grilled Chicken (half)", "description": "Suya-spiced grilled chicken", "price": 2500},
            {"name": "Egusi Soup + Fufu", "description": "Rich egusi with stockfish", "price": 2000},
            {"name": "Moi Moi (2 wraps)", "description": "Steamed bean pudding", "price": 600},
        ],
    },
    {
        "phone_number": "2348010000002",
        "business_name": "Alhaji Shawama Spot",
        "owner_name": "Ibrahim Musa",
        "food_categories": ["shawarma", "small chops", "spring rolls"],
        "lat": 12.0055,
        "lng": 8.5875,
        "address_text": "No. 12 Zaria Road, Nassarawa GRA, Kano",
        "rating": 4.3,
        "menu_items": [
            {"name": "Chicken Shawarma", "description": "Wrap with grilled chicken, veggies, garlic sauce", "price": 2000},
            {"name": "Beef Shawarma", "description": "Juicy beef with extra sauce", "price": 2200},
            {"name": "Small Chops (10 pcs)", "description": "Samosa, puff-puff, spring rolls mix", "price": 3000},
            {"name": "Spring Rolls (5 pcs)", "description": "Crispy veggie spring rolls", "price": 1500},
        ],
    },
    {
        "phone_number": "2348010000003",
        "business_name": "Hajiya Fati's Suya",
        "owner_name": "Fatima Abdullahi",
        "food_categories": ["suya", "kilishi", "tsire"],
        "lat": 11.9985,
        "lng": 8.5850,
        "address_text": "Kofar Mata Roundabout, Fagge, Kano",
        "rating": 4.8,
        "menu_items": [
            {"name": "Beef Suya (100g)", "description": "Spiced grilled beef on skewer", "price": 1200},
            {"name": "Chicken Suya (100g)", "description": "Tender grilled chicken suya", "price": 1400},
            {"name": "Kilishi (100g)", "description": "Dried spiced beef jerky", "price": 1800},
            {"name": "Tsire (Wrap)", "description": "Suya wrapped in flatbread with onions", "price": 1500},
            {"name": "Suya Platter (300g)", "description": "Mixed beef and chicken for sharing", "price": 3500},
        ],
    },
    {
        "phone_number": "2348010000004",
        "business_name": "Chisom Cakes & Bakes",
        "owner_name": "Chisom Eze",
        "food_categories": ["cakes", "pastries", "puff puff", "doughnuts"],
        "lat": 12.0100,
        "lng": 8.5960,
        "address_text": "Along Kano-Zaria Expressway, Sabon Gari",
        "rating": 4.4,
        "menu_items": [
            {"name": "Puff Puff (10 pcs)", "description": "Soft fried dough balls", "price": 500},
            {"name": "Meat Pie", "description": "Flaky pastry with spiced beef filling", "price": 400},
            {"name": "Chin Chin (200g)", "description": "Crunchy fried dough snack", "price": 600},
            {"name": "Doughnut (glazed)", "description": "Soft glazed ring doughnut", "price": 300},
            {"name": "Small Chops Box", "description": "Puff puff, meat pie, samosa, doughnut", "price": 2000},
        ],
    },
    {
        "phone_number": "2348010000005",
        "business_name": "Bello's Beans & Yam",
        "owner_name": "Bello Abubakar",
        "food_categories": ["beans", "yam", "porridge", "akara"],
        "lat": 11.9960,
        "lng": 8.5940,
        "address_text": "Opposite Sani Abacha Stadium, Kano Central",
        "rating": 4.2,
        "menu_items": [
            {"name": "Beans Porridge", "description": "Rich honey beans with palm oil and crayfish", "price": 1000},
            {"name": "Yam Porridge", "description": "Soft yam in savoury tomato sauce", "price": 1200},
            {"name": "Akara (6 balls)", "description": "Fried bean cake, best with pap", "price": 600},
            {"name": "Boiled Yam + Egg Sauce", "description": "Yam with spiced scrambled egg sauce", "price": 1300},
            {"name": "Beans + Fried Plantain", "description": "Classic street combo", "price": 1200},
        ],
    },
]


async def seed() -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as session:
        for seller in SELLERS:
            # Upsert seller
            await session.execute(
                text("""
                    INSERT INTO sellers (
                        phone_number, business_name, owner_name, food_categories,
                        location, address_text, is_available, rating,
                        onboarding_complete, onboarding_step
                    ) VALUES (
                        :phone, :biz_name, :owner_name, :categories,
                        ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography,
                        :address, TRUE, :rating, TRUE, 'complete'
                    )
                    ON CONFLICT (phone_number) DO UPDATE
                    SET business_name = EXCLUDED.business_name,
                        is_available = TRUE,
                        onboarding_complete = TRUE
                    RETURNING id
                """),
                {
                    "phone": seller["phone_number"],
                    "biz_name": seller["business_name"],
                    "owner_name": seller["owner_name"],
                    "categories": seller["food_categories"],
                    "lng": seller["lng"],
                    "lat": seller["lat"],
                    "address": seller["address_text"],
                    "rating": seller["rating"],
                },
            )
            # Get seller id
            id_row = (await session.execute(
                text("SELECT id FROM sellers WHERE phone_number = :p"),
                {"p": seller["phone_number"]},
            )).fetchone()
            seller_id = str(id_row[0])

            # Clear existing menu items for this seller (idempotent seed)
            await session.execute(
                text("DELETE FROM menu_items WHERE seller_id = :sid"),
                {"sid": seller_id},
            )

            # Insert menu items
            for item in seller["menu_items"]:
                await session.execute(
                    text("""
                        INSERT INTO menu_items (seller_id, name, description, price, is_available)
                        VALUES (:seller_id, :name, :desc, :price, TRUE)
                    """),
                    {
                        "seller_id": seller_id,
                        "name": item["name"],
                        "desc": item.get("description"),
                        "price": item["price"],
                    },
                )

            print(f"  ✓ {seller['business_name']} ({len(seller['menu_items'])} items)")

        await session.commit()

    await engine.dispose()
    print("\nSeeded 5 test sellers in Kano. Run the geo search to verify:")
    print("  curl -X POST http://localhost:8002/sellers/search \\")
    print("    -H 'Content-Type: application/json' \\")
    print("    -d '{\"lat\": 12.0022, \"lng\": 8.5920, \"query\": \"jollof rice\"}'")


if __name__ == "__main__":
    print("Seeding Kano test sellers...\n")
    asyncio.run(seed())
