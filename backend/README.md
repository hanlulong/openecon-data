# econ-data-mcp Python Backend

## Quick start

```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 3001
```

The API will be available at http://localhost:3001. The React frontend proxies `/api` requests to this port in development.

## Environment variables

Create a `.env` file in the repository root with the following keys:

```
OPENROUTER_API_KEY=your_openrouter_key
FRED_API_KEY=optional_fred_key
COMTRADE_API_KEY=optional_comtrade_key
JWT_SECRET=change-me
```

## Available endpoints

The service mirrors the previous Express API:

- `GET /api/health`
- `POST /api/auth/register`
- `POST /api/auth/login`
- `GET /api/auth/me`
- `GET /api/user/history`
- `POST /api/query`
- `POST /api/export`
- `GET /api/cache/stats`
- `POST /api/cache/clear`
