"""Router registry: create_app includes every router here with auth applied.
Parallel page tasks each own one module — this list is the only shared touch
point and it changes only when a NEW domain is added (not during page work)."""
from quantmind.api.routers.portfolio import router as portfolio_router
from quantmind.api.routers.risk import router as risk_router
from quantmind.api.routers.lab import router as lab_router

ROUTERS = [portfolio_router, risk_router, lab_router]
