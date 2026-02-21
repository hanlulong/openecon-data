#!/usr/bin/env python3
"""
econ-data-mcp Development Server Restart Script

This script safely cleans up and restarts both backend and frontend development servers.
It ensures a clean state by killing all existing processes and removing temporary files.

Usage:
    python3 scripts/restart_dev.py              # Restart both backend and frontend
    python3 scripts/restart_dev.py --backend    # Restart backend only
    python3 scripts/restart_dev.py --frontend   # Restart frontend only
    python3 scripts/restart_dev.py --help       # Show help

Features:
    - Kills all existing uvicorn and vite processes
    - Cleans up temporary files and logs
    - Verifies virtual environment exists
    - Starts services with correct parameters
    - Health checks for backend
    - Clear status reporting

Author: econ-data-mcp Team
Date: November 2025
"""

import subprocess
import sys
import time
import argparse
import os
import signal
from pathlib import Path
from typing import Optional, List

# Color codes for terminal output
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    BOLD = '\033[1m'
    END = '\033[0m'

def print_header(text: str):
    """Print a formatted header"""
    print(f"\n{Colors.BOLD}{'=' * 80}{Colors.END}")
    print(f"{Colors.BOLD}{text.center(80)}{Colors.END}")
    print(f"{Colors.BOLD}{'=' * 80}{Colors.END}\n")

def print_step(text: str):
    """Print a step indicator"""
    print(f"{Colors.BLUE}▶{Colors.END} {text}")

def print_success(text: str):
    """Print a success message"""
    print(f"{Colors.GREEN}✓{Colors.END} {text}")

def print_error(text: str):
    """Print an error message"""
    print(f"{Colors.RED}✗{Colors.END} {text}")

def print_warning(text: str):
    """Print a warning message"""
    print(f"{Colors.YELLOW}⚠{Colors.END} {text}")

def run_command(cmd: str, shell: bool = True, check: bool = False, capture_output: bool = False) -> Optional[subprocess.CompletedProcess]:
    """Run a shell command using bash (required for 'source' command)"""
    try:
        result = subprocess.run(
            cmd,
            shell=shell,
            check=check,
            capture_output=capture_output,
            text=True,
            executable='/bin/bash'  # Use bash to support 'source' command
        )
        return result
    except subprocess.CalledProcessError as e:
        if check:
            print_error(f"Command failed: {cmd}")
            print_error(f"Error: {e.stderr if capture_output else str(e)}")
        return None

def kill_processes_by_name(process_name: str) -> int:
    """Kill all processes matching the given name"""
    try:
        # Find PIDs
        result = subprocess.run(
            f"pgrep -f '{process_name}'",
            shell=True,
            capture_output=True,
            text=True
        )

        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split('\n')
            for pid in pids:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass
            return len(pids)
        return 0
    except Exception:
        return 0

def kill_processes_on_port(port: int) -> int:
    """Kill all processes using the specified port"""
    try:
        result = subprocess.run(
            f"lsof -ti:{port}",
            shell=True,
            capture_output=True,
            text=True
        )

        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split('\n')
            for pid in pids:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass
            return len(pids)
        return 0
    except Exception:
        return 0

def cleanup_backend():
    """Clean up backend processes and temporary files"""
    print_step("Cleaning up backend...")

    # Kill uvicorn processes
    killed = kill_processes_by_name('uvicorn')
    if killed > 0:
        print_success(f"Killed {killed} uvicorn process(es)")

    # Kill processes on port 3001
    killed = kill_processes_on_port(3001)
    if killed > 0:
        print_success(f"Killed {killed} process(es) on port 3001")

    # Clean up temporary log files
    temp_files = [
        "/tmp/backend-*.log",
        "/tmp/*backend*.log",
        "/tmp/uvicorn*.log"
    ]

    for pattern in temp_files:
        run_command(f"rm -f {pattern} 2>/dev/null")

    print_success("Backend cleanup complete")

def cleanup_frontend():
    """Clean up frontend processes"""
    print_step("Cleaning up frontend...")

    # Kill vite processes
    killed = kill_processes_by_name('vite')
    if killed > 0:
        print_success(f"Killed {killed} vite process(es)")

    # Kill processes on port 5173
    killed = kill_processes_on_port(5173)
    if killed > 0:
        print_success(f"Killed {killed} process(es) on port 5173")

    print_success("Frontend cleanup complete")

def verify_venv() -> bool:
    """Verify that the backend virtual environment exists"""
    venv_path = Path("/home/hanlulong/econ-data-mcp/backend/.venv")

    if not venv_path.exists():
        print_error("Virtual environment not found at backend/.venv")
        print_warning("Run setup first: ./scripts/setup.sh")
        return False

    python_path = venv_path / "bin" / "python3"
    if not python_path.exists():
        print_error("Python3 not found in virtual environment")
        return False

    uvicorn_path = venv_path / "bin" / "uvicorn"
    if not uvicorn_path.exists():
        print_error("Uvicorn not found in virtual environment")
        print_warning("Run: source backend/.venv/bin/activate && pip install -r backend/requirements.txt")
        return False

    print_success("Virtual environment verified")
    return True

def start_backend() -> bool:
    """Start the backend development server"""
    print_step("Starting backend server...")

    if not verify_venv():
        return False

    # Change to project directory
    os.chdir("/home/hanlulong/econ-data-mcp")

    # Start uvicorn in background with development environment variables
    # ALLOW_TEST_USER=true enables the test user for development mode
    cmd = """
    source backend/.venv/bin/activate && \
    export ALLOW_TEST_USER=true && \
    nohup uvicorn backend.main:app --host 0.0.0.0 --port 3001 --reload --reload-dir backend \
    > /tmp/backend-dev.log 2>&1 &
    """

    result = run_command(cmd)

    # Wait for server to start
    print_step("Waiting for backend to start...")
    time.sleep(3)

    # Check if process is running
    result = subprocess.run(
        "pgrep -f 'uvicorn.*backend.main:app'",
        shell=True,
        capture_output=True,
        text=True
    )

    if result.returncode == 0:
        pids = result.stdout.strip().split('\n')
        print_success(f"Backend started (PID: {pids[0]})")
        return True
    else:
        print_error("Backend failed to start")
        print_warning("Check logs: tail -f /tmp/backend-dev.log")
        return False

def check_backend_health() -> bool:
    """Check if backend is responding"""
    print_step("Checking backend health...")

    try:
        import urllib.request
        import json

        response = urllib.request.urlopen("http://localhost:3001/api/health", timeout=5)
        data = json.loads(response.read().decode())

        if data.get('status') == 'ok':
            services = sum(1 for v in data.get('services', {}).values() if v)
            print_success(f"Backend healthy (status: {data['status']}, services: {services})")
            return True
        else:
            print_warning(f"Backend responded but status is: {data.get('status')}")
            return False
    except Exception as e:
        print_error(f"Backend health check failed: {e}")
        return False

def start_frontend() -> bool:
    """Start the frontend development server"""
    print_step("Starting frontend server...")

    # Change to project directory
    os.chdir("/home/hanlulong/econ-data-mcp")

    # Check if node_modules exists
    if not Path("node_modules").exists():
        print_warning("node_modules not found. Installing dependencies...")
        result = run_command("npm install", check=False, capture_output=True)
        if result and result.returncode != 0:
            print_error("Failed to install dependencies")
            return False

    # Start vite in background
    cmd = "nohup npm run dev:frontend > /tmp/frontend-dev.log 2>&1 &"
    run_command(cmd)

    # Wait for server to start
    print_step("Waiting for frontend to start...")
    time.sleep(4)

    # Check if process is running
    result = subprocess.run(
        "pgrep -f 'vite'",
        shell=True,
        capture_output=True,
        text=True
    )

    if result.returncode == 0:
        pids = result.stdout.strip().split('\n')
        print_success(f"Frontend started (PID: {pids[0]})")
        return True
    else:
        print_error("Frontend failed to start")
        print_warning("Check logs: tail -f /tmp/frontend-dev.log")
        return False

def show_status():
    """Show current server status"""
    print_header("Development Server Status")

    # Backend status
    backend_running = subprocess.run(
        "pgrep -f 'uvicorn.*backend.main:app'",
        shell=True,
        capture_output=True
    ).returncode == 0

    if backend_running:
        print_success("Backend: RUNNING (http://localhost:3001)")
    else:
        print_error("Backend: NOT RUNNING")

    # Frontend status
    frontend_running = subprocess.run(
        "pgrep -f 'vite'",
        shell=True,
        capture_output=True
    ).returncode == 0

    if frontend_running:
        print_success("Frontend: RUNNING (http://localhost:5173)")
    else:
        print_error("Frontend: NOT RUNNING")

    print()
    print("View logs:")
    print("  Backend:  tail -f /tmp/backend-dev.log")
    print("  Frontend: tail -f /tmp/frontend-dev.log")
    print()

def main():
    parser = argparse.ArgumentParser(
        description="Clean up and restart econ-data-mcp development servers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 scripts/restart_dev.py              # Restart both servers
  python3 scripts/restart_dev.py --backend    # Restart backend only
  python3 scripts/restart_dev.py --frontend   # Restart frontend only
  python3 scripts/restart_dev.py --status     # Show status only

After restart:
  Backend:  http://localhost:3001/api/health
  Frontend: http://localhost:5173
        """
    )

    parser.add_argument(
        '--backend',
        action='store_true',
        help='Restart backend only'
    )

    parser.add_argument(
        '--frontend',
        action='store_true',
        help='Restart frontend only'
    )

    parser.add_argument(
        '--status',
        action='store_true',
        help='Show server status only (no restart)'
    )

    parser.add_argument(
        '--no-health-check',
        action='store_true',
        help='Skip backend health check'
    )

    args = parser.parse_args()

    # Show status only
    if args.status:
        show_status()
        return 0

    # Determine what to restart
    restart_backend = args.backend or not args.frontend
    restart_frontend = args.frontend or not args.backend

    print_header("econ-data-mcp Development Server Restart")

    success = True

    # Backend
    if restart_backend:
        cleanup_backend()
        time.sleep(1)

        if start_backend():
            if not args.no_health_check:
                time.sleep(2)
                if not check_backend_health():
                    success = False
        else:
            success = False

    # Frontend
    if restart_frontend:
        cleanup_frontend()
        time.sleep(1)

        if not start_frontend():
            success = False

    # Final status
    time.sleep(1)
    show_status()

    if success:
        print_success("All services started successfully!")
        return 0
    else:
        print_error("Some services failed to start. Check logs for details.")
        return 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(130)
    except Exception as e:
        print_error(f"Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
