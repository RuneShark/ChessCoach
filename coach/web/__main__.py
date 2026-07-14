"""Launch the ChessCoach web UI:  python -m coach.web"""
import os

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("COACH_PORT", "6464"))
    print(f"ChessCoach UI  ->  http://127.0.0.1:{port}")
    uvicorn.run("coach.web.server:app", host="127.0.0.1", port=port, reload=False)
