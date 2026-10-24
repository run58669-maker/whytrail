"""Start the WhyTrail API (POST /run). For the CLI use: python -m whytrail --mock "<question>"."""

import os

import uvicorn

from whytrail.env import load_dotenv

if __name__ == "__main__":
    load_dotenv()  # .env in CWD or repo root; shell variables take precedence
    uvicorn.run("whytrail.api:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
