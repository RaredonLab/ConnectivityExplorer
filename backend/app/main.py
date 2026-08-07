from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import tiles, spatial, edges, layers

APP_VERSION = "0.9.0"

app = FastAPI(title="TissuePlex API", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tiles.router, prefix="/tiles", tags=["tiles"])
app.include_router(spatial.router, prefix="/spatial", tags=["spatial"])
app.include_router(edges.router, prefix="/edges", tags=["edges"])
app.include_router(layers.router, prefix="/layers", tags=["layers"])


@app.get("/health")
def health():
    return {"status": "ok", "version": APP_VERSION}
