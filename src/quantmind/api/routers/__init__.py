"""Router registry: create_app includes every router here with auth applied.
Parallel page tasks each own one module — this list is the only shared touch
point and it changes only when a NEW domain is added (not during page work)."""
from quantmind.api.routers.portfolio import router as portfolio_router
from quantmind.api.routers.risk import router as risk_router
from quantmind.api.routers.lab import router as lab_router
from quantmind.api.routers.macro import router as macro_router
from quantmind.api.routers.whatif import router as whatif_router
from quantmind.api.routers.hedge import router as hedge_router
from quantmind.api.routers.sync import router as sync_router
from quantmind.api.routers.book import router as book_router
from quantmind.api.routers.instruments import router as instruments_router
from quantmind.api.routers.options import router as options_router
from quantmind.api.routers.news import router as news_router
from quantmind.api.routers.rotation import router as rotation_router
from quantmind.api.routers.setup import router as setup_router

ROUTERS = [portfolio_router, risk_router, lab_router, macro_router, whatif_router, hedge_router, sync_router, book_router, instruments_router, options_router, news_router, rotation_router, setup_router]
