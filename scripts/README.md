# econ-data-mcp Scripts

This directory contains utility scripts for development and deployment.

## restart_dev.py

**Purpose**: Clean up and restart development servers (backend and/or frontend)

### Features
- Automatically kills all existing processes on ports 3001 (backend) and 5173 (frontend)
- Cleans up temporary log files
- Verifies virtual environment setup
- Starts services with correct parameters
- Performs health checks
- Color-coded status output

### Usage

```bash
# Restart both backend and frontend
python3 scripts/restart_dev.py

# Restart backend only
python3 scripts/restart_dev.py --backend

# Restart frontend only
python3 scripts/restart_dev.py --frontend

# Show current status (no restart)
python3 scripts/restart_dev.py --status

# Skip health check (faster startup)
python3 scripts/restart_dev.py --no-health-check

# Show help
python3 scripts/restart_dev.py --help
```

### What It Does

**Backend restart:**
1. Kills all uvicorn processes
2. Kills processes on port 3001
3. Cleans up temporary log files in /tmp
4. Starts uvicorn with: `--host 0.0.0.0 --port 3001 --reload --reload-dir backend`
5. Verifies process started
6. Performs health check (unless --no-health-check)

**Frontend restart:**
1. Kills all vite processes
2. Kills processes on port 5173
3. Starts Vite dev server via `npm run dev:frontend`
4. Verifies process started

### Logs

After restart, view logs at:
- Backend: `/tmp/backend-dev.log`
- Frontend: `/tmp/frontend-dev.log`

```bash
# Follow backend logs
tail -f /tmp/backend-dev.log

# Follow frontend logs
tail -f /tmp/frontend-dev.log
```

### Troubleshooting

**Virtual environment not found:**
```bash
./scripts/setup.sh  # Run first-time setup
```

**Backend fails to start:**
```bash
# Check the logs
tail -50 /tmp/backend-dev.log

# Verify dependencies
source backend/.venv/bin/activate
pip install -r backend/requirements.txt
```

**Frontend fails to start:**
```bash
# Check the logs
tail -50 /tmp/frontend-dev.log

# Reinstall dependencies
npm install
```

### When to Use

Use this script:
- ✅ After pulling new code changes
- ✅ When processes become unresponsive
- ✅ After modifying dependencies
- ✅ When you see spurious process spawning
- ✅ To ensure a clean development state
- ✅ Before starting a debugging session

### Exit Codes

- `0`: Success - all requested services started
- `1`: Failure - one or more services failed to start
- `130`: User interrupted (Ctrl+C)

## Other Scripts

- `setup.sh` / `setup.ps1` / `setup.bat`: First-time project setup
- `fetch_statscan_metadata.py`: Update Statistics Canada metadata cache
- (Add other scripts as they are created)

## Contributing

When adding new scripts:
1. Add execute permissions: `chmod +x scripts/yourscript.sh`
2. Include usage documentation in this README
3. Add error handling and clear output messages
4. Test on a clean environment before committing
