#!/bin/bash
cd "$(dirname "$0")"

# Kill any existing instance on port 5001
existing_pid=$(lsof -ti :5001 2>/dev/null)
if [ -n "$existing_pid" ]; then
    echo "Stopping existing instance (PID: $existing_pid)..."
    kill $existing_pid 2>/dev/null
    sleep 1
fi

echo "Setting up Python Environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

source venv/bin/activate
pip install -q -r requirements.txt

echo "Starting YouTube Chronological Player..."
python app.py &
APP_PID=$!

# Cleanup on exit
trap "kill $APP_PID 2>/dev/null" EXIT

sleep 2

# Open default browser on macOS (matching app.py port 5001)
open http://127.0.0.1:5001/

# Wait for the app
wait $APP_PID
