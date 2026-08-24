from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import (
    auth, products, orders, customers, appointments, inventory, coupons, analytics
)

app = FastAPI(
    title="Véra Hair Co. API",
    description="Backend for the Véra Hair Co. wig & hairstyling e-commerce platform.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(products.router)
app.include_router(orders.router)
app.include_router(customers.router)
app.include_router(appointments.router)
app.include_router(inventory.router)
app.include_router(coupons.router)
app.include_router(analytics.router)


@app.get("/api/health")
def health_check():
    return {"status": "ok"}
