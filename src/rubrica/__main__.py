"""``python -m rubrica`` — run the portal with uvicorn."""
import os

import uvicorn

from .app import create_app


def main():
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(create_app(), host=os.environ.get("HOST", "0.0.0.0"), port=port, log_level="info")


if __name__ == "__main__":
    main()
