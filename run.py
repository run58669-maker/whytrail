"""Start the WhyTrail API (POST /run). For the CLI use: python -m whytrail --mock "<question>"."""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("whytrail.api:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
